"""``/bridge`` 聊天命令：解析、执行与路由拦截（不联网、不启动浏览器）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from claude_web import server
from claude_web.bridge_commands import (
    handle_command,
    is_command,
    latest_user_text,
    normalize_session_url,
    parse_command,
)
from claude_web.errors import NotReadyError
from claude_web.models import advertised_models_ids
from tests.fakes import FakeDriver, FakePage, make_service

VALID_ID = "b646548c-d27c-4dc7-9205-3134ac5d9c8d"
VALID_URL = f"https://claude.ai/chat/{VALID_ID}"
BUCKET = "cmd-bucket"


# ==================== 解析 ====================


def test_bare_bridge_is_help():
    command = parse_command("/bridge")
    assert command is not None and command.action == "help"


def test_unknown_subcommand_is_invalid_with_usage():
    command = parse_command("/bridge frobnicate")
    assert command is not None and command.action == "invalid"
    assert "用法" in command.reason


def test_only_exact_single_line_is_a_command():
    assert parse_command("请看 /bridge status 的用法") is None
    assert parse_command("/bridge status\n第二行") is None
    assert parse_command("/other status") is None
    assert parse_command("") is None


def test_session_alias_parses_target_and_seed():
    command = parse_command(f"/bridge link {VALID_URL} --seed")
    assert command is not None and command.subaction == "link"
    assert command.target == VALID_URL and command.seed is True


def test_link_requires_target_and_rejects_unknown_option():
    assert parse_command("/bridge link").action == "invalid"
    assert parse_command(f"/bridge link {VALID_URL} --force").action == "invalid"


def test_only_last_message_is_considered():
    history = [
        {"role": "user", "content": "/bridge unlink"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "普通提问"},
    ]
    assert latest_user_text(history) == "普通提问"
    assert is_command(history) is False
    # 工具循环的后续请求以 tool 结尾时不得再执行一遍历史里的命令
    assert latest_user_text([*history, {"role": "tool", "content": "{}"}]) is None


def test_normalize_session_url():
    assert normalize_session_url(VALID_URL) == VALID_URL
    assert normalize_session_url(VALID_ID) == VALID_URL
    assert normalize_session_url("没这回事") is None
    assert normalize_session_url("") is None


# ==================== 执行 ====================


class _UnavailableDriver(FakeDriver):
    """浏览器不可用：任何取页面的尝试都会失败（命令仍必须能回答）。"""

    async def page_for(self, bucket):  # type: ignore[override]
        raise NotReadyError("浏览器尚未启动")


def _service(page: FakePage, tmp_path, driver=None):
    return make_service(page, tmp_path, **({"driver": driver} if driver else {}))


def test_models_reply_lists_advertised_models(tmp_path):
    import asyncio

    service = _service(FakePage(), tmp_path)
    reply = asyncio.run(handle_command([{"role": "user", "content": "/bridge models"}], service, BUCKET))
    assert reply is not None
    assert advertised_models_ids()[0] in reply


def test_status_reply_works_when_browser_unavailable(tmp_path):
    import asyncio

    driver = _UnavailableDriver(lambda: FakePage())
    service = _service(FakePage(), tmp_path, driver=driver)
    reply = asyncio.run(handle_command([{"role": "user", "content": "/bridge status"}], service, BUCKET))
    assert reply is not None
    assert "[Bridge] 桥接状态" in reply
    assert "尚未启动" in reply


def test_modal_command_closes_limit_popup(tmp_path):
    """`/bridge modal`：额度用尽弹窗要能被关掉，并如实报告命中的文案。"""
    import asyncio

    page = FakePage(
        limit_modal="Upgrade to keep chatting. You hit your 5-hour message limit."
    )
    service = _service(page, tmp_path)
    reply = asyncio.run(handle_command([{"role": "user", "content": "/bridge modal"}], service, BUCKET))
    assert reply is not None and "已关闭页面上的额度/升级弹窗" in reply
    assert "message limit" in reply
    assert page.modal_clicks == 1
    assert page.modal_dismissed is True


def test_modal_command_reports_absence(tmp_path):
    import asyncio

    page = FakePage()
    service = _service(page, tmp_path)
    reply = asyncio.run(handle_command([{"role": "user", "content": "/bridge popup"}], service, BUCKET))
    assert reply is not None and "没有额度/升级弹窗" in reply
    assert page.modal_clicks == 0


def test_modal_command_works_when_browser_unavailable(tmp_path):
    """浏览器不可用时也要回一行可解释的应答，而不是抛给客户端。"""
    import asyncio

    driver = _UnavailableDriver(lambda: FakePage())
    service = _service(FakePage(), tmp_path, driver=driver)
    reply = asyncio.run(handle_command([{"role": "user", "content": "/bridge modal"}], service, BUCKET))
    assert reply is not None and "命令未执行" in reply


def test_modal_command_rejects_arguments():
    command = parse_command("/bridge modal now")
    assert command is not None and command.action == "invalid"


def test_session_link_binds_bucket_and_navigates(tmp_path):
    import asyncio

    page = FakePage()
    service = _service(page, tmp_path)
    reply = asyncio.run(handle_command([{"role": "user", "content": f"/bridge link {VALID_ID}"}], service, BUCKET))
    assert reply is not None and "已把会话桶" in reply
    assert service.lifecycle.status(BUCKET)["linked_url"] == VALID_URL
    assert page.goto_calls == [VALID_URL]


def test_session_link_rejects_foreign_host_without_navigating(tmp_path):
    import asyncio

    page = FakePage()
    service = _service(page, tmp_path)
    reply = asyncio.run(
        handle_command([{"role": "user", "content": "/bridge link https://evil.example.com/chat/abcdefgh"}], service, BUCKET)
    )
    assert reply is not None and "命令未执行" in reply
    assert page.goto_calls == []
    assert service.lifecycle.status(BUCKET)["linked_url"] is None


def test_unlink_reports_and_clears_binding(tmp_path):
    import asyncio

    service = _service(FakePage(), tmp_path)
    unbound = asyncio.run(handle_command([{"role": "user", "content": "/bridge unlink"}], service, BUCKET))
    assert unbound is not None and "本来就没有绑定" in unbound
    asyncio.run(handle_command([{"role": "user", "content": f"/bridge session link {VALID_ID}"}], service, BUCKET))
    bound = asyncio.run(handle_command([{"role": "user", "content": "/bridge session unlink"}], service, BUCKET))
    assert bound is not None and "已解除" in bound
    assert service.lifecycle.status(BUCKET)["linked_url"] is None


def test_reset_drops_binding_and_reports_scope(tmp_path):
    import asyncio

    service = _service(FakePage(), tmp_path)
    asyncio.run(handle_command([{"role": "user", "content": f"/bridge link {VALID_ID}"}], service, BUCKET))
    reply = asyncio.run(handle_command([{"role": "user", "content": "/bridge reset"}], service, BUCKET))
    assert reply is not None and "只影响这个桶" in reply
    assert service.lifecycle.status(BUCKET)["linked_url"] is None


def test_reseed_explains_reuse_and_points_at_reset(tmp_path):
    """`reseed` 是解释型命令：首轮完整播种、之后只发增量，要重播就用 session reset。"""
    import asyncio

    service = _service(FakePage(), tmp_path)
    reply = asyncio.run(handle_command([{"role": "user", "content": "/bridge session reseed"}], service, BUCKET))
    assert reply is not None
    assert "不需要单独的 reseed" in reply
    assert "只发增量" in reply
    assert "session reset" in reply


# ==================== 路由拦截 ====================


def test_route_answers_command_without_browser(monkeypatch, tmp_path):
    service = _service(FakePage(), tmp_path, driver=_UnavailableDriver(lambda: FakePage()))
    monkeypatch.setattr(server, "service", service)
    client = TestClient(server.app)
    resp = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": "/bridge models"}]}
    )
    assert resp.status_code == 200
    assert advertised_models_ids()[0] in resp.json()["choices"][0]["message"]["content"]
    # 命令之外的普通提问仍然需要浏览器
    plain = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert plain.status_code == 503


def test_route_command_stream_emits_sse(monkeypatch, tmp_path):
    service = _service(FakePage(), tmp_path, driver=_UnavailableDriver(lambda: FakePage()))
    monkeypatch.setattr(server, "service", service)
    client = TestClient(server.app)
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "/bridge status"}], "stream": True},
    )
    assert resp.status_code == 200
    assert "[DONE]" in resp.text
    assert "桥接状态" in resp.text
