"""消息归一化与提示词构造（doc/design.md 3.2、5.2、5.3）。"""

from __future__ import annotations

import pytest

from claude_web import config
from claude_web.errors import InvalidRequestError, UnsupportedInputError
from claude_web.models import ChatMessage, ToolSpec
from claude_web.prompting import (
    build_prompt,
    content_to_text,
    delta_piece,
    estimate_tokens,
    normalize_messages,
    normalize_tools,
    seed_goal,
)


def _msgs(*pairs):
    return [ChatMessage(role=role, content=content) for role, content in pairs]


def test_content_to_text_string_and_parts():
    assert content_to_text("hi") == "hi"
    assert content_to_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
    assert content_to_text(None) == ""


def test_content_to_text_rejects_unsupported_types():
    with pytest.raises(UnsupportedInputError):
        content_to_text([{"type": "image_url", "image_url": {"url": "http://x"}}])


def test_normalize_maps_developer_and_function_roles():
    normalized = normalize_messages(
        _msgs(("developer", "sys"), ("user", "hi"), ("function", "{}"), ("assistant", "ok"))
    )
    assert [m.role for m in normalized] == ["system", "user", "tool", "assistant"]


def test_normalize_rejects_unknown_role_and_empty():
    with pytest.raises(InvalidRequestError):
        normalize_messages(_msgs(("wizard", "hi")))
    with pytest.raises(InvalidRequestError):
        normalize_messages([])


def test_system_prefix_is_marked_and_budgeted(monkeypatch):
    monkeypatch.setattr(config, "SYSTEM_MAX_CHARS", 40)
    normalized = normalize_messages(_msgs(("system", "x" * 200), ("user", "hi")))
    build = build_prompt(normalized)
    assert "[system]" in build.text
    assert "截断" in build.text


def test_build_prompt_keeps_message_order_and_marks_roles():
    normalized = normalize_messages(_msgs(("system", "S"), ("user", "u1"), ("assistant", "a1"), ("user", "u2")))
    build = build_prompt(normalized)
    assert build.text.index("[user]") < build.text.index("[assistant]")
    # 最后一条 user 必须保留
    assert "u2" in build.text
    assert build.included_messages == 3
    assert build.truncated is False


def test_build_prompt_truncates_history_but_keeps_last_user(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_MAX_CONTEXT_CHARS", 120)
    normalized = normalize_messages(
        _msgs(
            ("user", "A" * 200),
            ("assistant", "B" * 200),
            ("user", "CURRENT-QUESTION"),
        )
    )
    build = build_prompt(normalized)
    assert "CURRENT-QUESTION" in build.text
    assert build.truncated is True
    assert build.included_messages < build.total_messages


def test_tool_result_is_truncated(monkeypatch):
    monkeypatch.setattr(config, "TOOL_RESULT_MAX_CHARS", 10)
    normalized = normalize_messages(_msgs(("tool", "y" * 100)))
    build = build_prompt(normalized)
    assert "工具结果已截断" in build.text


def test_tools_instruction_injected():
    tools = [ToolSpec(name="read_file", description="read", parameters={"required": ["path"]})]
    normalized = normalize_messages(_msgs(("user", "hi")))
    build = build_prompt(normalized, tools=tools)
    assert "[工具调用协议]" in build.text
    assert "read_file" in build.text


def test_normalize_tools_handles_nested_and_flat():
    tools = normalize_tools(
        [
            {"type": "function", "function": {"name": "a", "description": "d"}},
            {"name": "b"},
            {"type": "function", "function": {}},
        ]
    )
    assert [t.name for t in tools] == ["a", "b"]


def test_seed_goal_uses_last_user_message(monkeypatch):
    monkeypatch.setattr(config, "TASK_GOAL_MAX_CHARS", 5)
    normalized = normalize_messages(_msgs(("assistant", "x"), ("user", "abcdefghij")))
    assert seed_goal(normalized) == "abcde"


def test_delta_piece_prefix_growth_and_rewrite():
    assert delta_piece("Hel", "Hello") == ("lo", None)
    assert delta_piece("Hello", "Hello") == ("", None)
    piece, reset = delta_piece("Hello", "Hi there")
    assert piece == "" and reset == "Hi there"


def test_estimate_tokens_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 30) == 10
