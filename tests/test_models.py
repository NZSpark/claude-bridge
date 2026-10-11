"""模型契约（doc/design.md 11.1）。"""

from __future__ import annotations

from claude_web import config
from claude_web.models import (
    ChatCompletionRequest,
    ChatMessage,
    TaskSnapshot,
    advertised_models_ids,
    model_is_supported,
)


def test_request_allows_unknown_fields():
    """兼容客户端多传参数不能导致 422（doc/design.md 3.2）。"""
    req = ChatCompletionRequest(
        model="claude-web",
        messages=[{"role": "user", "content": "hi"}],
        reasoning_effort="high",
        some_new_field=123,
    )
    assert req.model == "claude-web"
    assert len(req.messages) == 1


def test_message_accepts_content_parts_and_tool_fields():
    msg = ChatMessage(
        role="assistant",
        content=[{"type": "text", "text": "hi"}],
        tool_calls=[{"id": "call_1", "function": {"name": "f", "arguments": "{}"}}],
    )
    assert msg.tool_calls[0].function.name == "f"


def test_advertised_models_only_configured_alias():
    ids = advertised_models_ids()
    assert ids == [config.DEFAULT_MODEL_ALIAS]
    assert model_is_supported("claude-web")
    assert not model_is_supported("some-unknown-model")


def test_advertised_models_includes_configured_aliases(monkeypatch):
    monkeypatch.setitem(config.MODEL_ALIASES, "claude-web-opus", "Opus")
    ids = advertised_models_ids()
    assert "claude-web-opus" in ids


def test_task_snapshot_roundtrip():
    snap = TaskSnapshot(bucket="b", model="m", goal="do the thing", state="generating")
    snap.submitted = True
    snap.recoveries = 2
    restored = TaskSnapshot.from_payload(snap.to_payload())
    assert restored.bucket == "b"
    assert restored.goal == "do the thing"
    assert restored.submitted is True
    assert restored.recoveries == 2


def test_task_snapshot_rejects_unknown_state():
    restored = TaskSnapshot.from_payload({"state": "bogus"})
    assert restored.state == "queued"
