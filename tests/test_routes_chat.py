"""Chat Completions 路由契约（doc/design.md 3、9.2、11.1）。

全部使用 FakeDriver，不联网、不启动浏览器。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from claude_web import config, server
from claude_web.errors import SessionBusyError
from tests.fakes import FakePage, make_service

DEFAULT_LINK = "https://claude.ai/chat/b646548c-d27c-4dc7-9205-3134ac5d9c8d"


@pytest.fixture
def build_client(monkeypatch, tmp_path):
    def _build(page: FakePage, **kwargs):
        svc = make_service(page, tmp_path, **kwargs)
        monkeypatch.setattr(server, "service", svc)
        return TestClient(server.app), svc

    return _build


def _chat(client, **body):
    return client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}], **body})


def test_non_stream_completion(build_client):
    client, _ = build_client(FakePage(snapshots=["Hello there!"], generating_plan=[False]))
    resp = _chat(client)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "claude-web"
    assert payload["choices"][0]["message"]["content"] == "Hello there!"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["total_tokens"] >= 0


def _parse_sse(text: str):
    content = ""
    finish = None
    done = False
    for block in text.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        assert block.startswith("data: ")
        data = block[len("data: "):]
        if data == "[DONE]":
            done = True
            continue
        payload = json.loads(data)
        choice = payload["choices"][0]
        content += choice["delta"].get("content", "") or ""
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
    return content, finish, done


def test_stream_completion_concatenates_and_terminates(build_client):
    page = FakePage(
        snapshots=["Hello", "Hello world", "Hello world"],
        generating_plan=[True, True, False],
    )
    client, _ = build_client(page)
    resp = _chat(client, stream=True)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    content, finish, done = _parse_sse(resp.text)
    assert content == "Hello world"
    assert finish == "stop"
    assert done is True


def test_stream_content_starts_with_role_chunk(build_client):
    client, _ = build_client(FakePage(snapshots=["ok"], generating_plan=[False]))
    resp = _chat(client, stream=True)
    first = resp.text.split("\n\n")[0]
    assert json.loads(first[len("data: "):])["choices"][0]["delta"]["role"] == "assistant"


def test_session_buckets_are_isolated(build_client):
    from claude_web.browser.dom_adapter import ClaudeDOMAdapter
    from tests.fakes import FakeDriver

    pages = []

    def factory():
        page = FakePage(snapshots=[f"answer-{len(pages)}"], generating_plan=[False])
        pages.append(page)
        return page

    driver = FakeDriver(factory, dom=ClaudeDOMAdapter())
    client, svc = build_client(FakePage(), driver=driver)
    for bucket in ("alpha", "beta"):
        resp = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Claude-Session": bucket},
        )
        assert resp.status_code == 200
    assert sorted(svc.driver.pool.buckets()) == ["alpha", "beta"]
    assert len(pages) == 2


def test_user_field_used_for_bucketing(build_client):
    client, svc = build_client(FakePage(snapshots=["ok"], generating_plan=[False]))
    _chat(client, user="my-client")
    assert svc.driver.pool.buckets() == ["my-client"]


def test_authentication_failure_maps_to_503(build_client):
    client, _ = build_client(FakePage(authenticated=False))
    resp = _chat(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "authentication_required"


def test_challenge_title_maps_to_503(build_client):
    """Cloudflare 拦截页正文为空、选择器全不中，必须靠标题区分而不是误报改版。"""
    client, _ = build_client(FakePage(title_text="Just a moment..."))
    resp = _chat(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "challenge_required"


def test_security_verification_page_maps_to_503(build_client):
    """站点安全校验页的标题常是站点名，必须靠正文文案识别。"""
    page = FakePage(
        body_text="Performing security verification\nThis website uses a security service",
        title_text="claude.ai",
    )
    client, _ = build_client(page)
    resp = _chat(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "challenge_required"


def test_missing_composer_maps_to_502(build_client):
    client, _ = build_client(FakePage(composer_available=False))
    resp = _chat(client)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "composer_not_found"


def test_no_new_reply_maps_to_502(build_client, monkeypatch):
    from claude_web import config

    monkeypatch.setattr(config, "STALL_POLLS", 2)
    client, _ = build_client(FakePage(snapshots=[], generating_plan=[False]))
    resp = _chat(client)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] in ("reply_not_found", "generation_stalled")


def test_unsupported_content_type_maps_to_400(build_client):
    client, _ = build_client(FakePage())
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "http://x"}}],
                }
            ]
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_input"


def test_invalid_json_body_maps_to_400(build_client):
    client, _ = build_client(FakePage())
    resp = client.post(
        "/v1/chat/completions",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400


def test_tool_call_returns_structured_calls(build_client):
    reply = '```tool_call\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'
    client, _ = build_client(FakePage(snapshots=[reply], generating_plan=[False]))
    resp = _chat(
        client,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "read",
                    "parameters": {"type": "object", "required": ["path"]},
                },
            }
        ],
    )
    assert resp.status_code == 200
    payload = resp.json()
    choice = payload["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.txt"}
    # 内部工具标记不得泄漏进正文
    assert "tool_call" not in (choice["message"]["content"] or "")


def test_undeclared_tool_not_returned(build_client):
    reply = '```tool_call\n{"name": "rm_rf", "arguments": {}}\n```'
    client, _ = build_client(FakePage(snapshots=[reply], generating_plan=[False]))
    resp = _chat(client, tools=[{"type": "function", "function": {"name": "safe"}}])
    assert resp.status_code == 200
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert not choice["message"].get("tool_calls")


def test_session_busy_maps_to_503(monkeypatch, tmp_path):
    class BusyService:
        class _Driver:
            pass

        driver = _Driver()

        async def run(self, *args, **kwargs):
            raise SessionBusyError("busy")

    monkeypatch.setattr(server, "service", BusyService())
    client = TestClient(server.app)
    resp = _chat(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "session_busy"


def test_responses_disabled_returns_404(build_client):
    client, _ = build_client(FakePage())
    resp = client.post("/v1/responses", json={"input": "hi"})
    assert resp.status_code == 404


def test_default_link_used_for_default_bucket(build_client, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_LINK_URL", DEFAULT_LINK)
    page = FakePage(snapshots=["ok"], generating_plan=[False])
    client, _ = build_client(page)
    assert _chat(client).status_code == 200
    assert DEFAULT_LINK in page.goto_calls
    assert page.new_chat_opened is False


def test_default_link_not_used_for_other_buckets(build_client, monkeypatch):
    """缺省链路只作用于默认桶，其他桶仍旧新开对话（避免多桶串线）。"""
    monkeypatch.setattr(config, "DEFAULT_LINK_URL", DEFAULT_LINK)
    page = FakePage(snapshots=["ok"], generating_plan=[False])
    client, _ = build_client(page)
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Claude-Session": "other"},
    )
    assert resp.status_code == 200
    assert page.goto_calls == []
    assert page.new_chat_opened is True


def test_invalid_default_link_falls_back_to_new_chat(build_client, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_LINK_URL", "https://evil.example.com/chat/x")
    page = FakePage(snapshots=["ok"], generating_plan=[False])
    client, _ = build_client(page)
    assert _chat(client).status_code == 200
    assert page.new_chat_opened is True
    assert page.goto_calls == []


def test_default_link_surfaced_in_session_status(build_client, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_LINK_URL", DEFAULT_LINK)
    client, _ = build_client(FakePage())
    assert client.get(f"/session/{config.DEFAULT_SESSION_KEY}").json()["default_link_url"] == DEFAULT_LINK


def test_health_metrics_diagnostics_and_models(build_client):
    client, _ = build_client(FakePage(snapshots=["ok"], generating_plan=[False]))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/metrics").status_code == 200
    diag = client.get("/diagnostics").json()
    assert "selector_version" in diag
    assert "metrics" in diag
    models = client.get("/v1/models").json()
    assert models["data"][0]["id"] == "claude-web"
