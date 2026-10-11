"""安全与隐私（doc/design.md 10、11.1）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claude_web import config, server
from claude_web.completion.lifecycle import validate_link_url
from claude_web.errors import InvalidRequestError
from tests.fakes import FakePage, make_service


def test_sanitize_session_key_strips_and_limits(monkeypatch):
    assert server._sanitize_key("codex-cli-1") == "codex-cli-1"
    monkeypatch.setattr(config, "SESSION_KEY_MAX_LEN", 8)
    key = server._sanitize_key("  abc/../def  ")
    assert "/" not in key and " " not in key
    assert len(key) <= 8
    assert server._sanitize_key("!!!") == config.DEFAULT_SESSION_KEY
    assert server._sanitize_key(None) == config.DEFAULT_SESSION_KEY


def test_session_key_prefers_header_then_user(monkeypatch):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(b"x-claude-session", b"hdr"), (b"user-agent", b"UA")],
        "method": "POST",
        "path": "/",
    }
    request = Request(scope)
    assert server._session_key(request, {"user": "usr"}) == "hdr"
    assert server._session_key(request, {}) == "hdr"


def test_bridge_token_protects_generation(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "BRIDGE_TOKEN", "secret-token")
    svc = make_service(FakePage(snapshots=["ok"], generating_plan=[False]), tmp_path)
    monkeypatch.setattr(server, "service", svc)
    client = TestClient(server.app)

    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat/completions", json=body).status_code == 401
    assert (
        client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    ok = client.post(
        "/v1/chat/completions", json=body, headers={"Authorization": "Bearer secret-token"}
    )
    assert ok.status_code == 200
    # 管理端点同样受保护
    assert client.post("/session/reset", json={"session": "x"}).status_code == 401


def test_reset_token_protects_reset(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "RESET_TOKEN", "reset-secret")
    svc = make_service(FakePage(), tmp_path)
    monkeypatch.setattr(server, "service", svc)
    client = TestClient(server.app)
    assert client.post("/session/reset", json={"session": "x"}).status_code == 401
    ok = client.post(
        "/session/reset", json={"session": "x"}, headers={"X-Reset-Token": "reset-secret"}
    )
    assert ok.status_code == 200


def test_link_url_validation():
    assert validate_link_url("https://claude.ai/chat/abc-123") == "https://claude.ai/chat/abc-123"
    with pytest.raises(InvalidRequestError):
        validate_link_url("http://claude.ai/chat/abc")  # 必须 https
    with pytest.raises(InvalidRequestError):
        validate_link_url("https://evil.example.com/chat/abc")  # 非允许主机
    with pytest.raises(InvalidRequestError):
        validate_link_url("https://claude.ai/")  # 非会话页面
    with pytest.raises(InvalidRequestError):
        validate_link_url("")


def test_redact_hides_value_but_is_stable():
    from claude_web.logging_setup import redact

    assert redact("") == "-"
    first = redact("https://claude.ai/chat/secret")
    assert "secret" not in first
    assert first == redact("https://claude.ai/chat/secret")


def test_diagnostics_does_not_leak_prompt_or_reply_text(monkeypatch, tmp_path):
    page = FakePage(snapshots=["SECRET-REPLY-TEXT"], generating_plan=[False])
    svc = make_service(page, tmp_path)
    monkeypatch.setattr(server, "service", svc)
    client = TestClient(server.app)
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    body = client.get("/diagnostics").text
    assert "SECRET-REPLY-TEXT" not in body


def test_loopback_warning_condition():
    assert config.is_loopback_host(config.HOST)
