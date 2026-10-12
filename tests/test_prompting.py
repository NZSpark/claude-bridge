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
    assert "[系统指令]" in build.text
    assert "截断" in build.text


def test_build_prompt_keeps_message_order_and_marks_roles():
    normalized = normalize_messages(_msgs(("system", "S"), ("user", "u1"), ("assistant", "a1"), ("user", "u2")))
    build = build_prompt(normalized)
    # 对话口吻的角色标签：用户消息不加标记，AI 历史回复标为 [你之前的回复]
    assert build.text.index("u1") < build.text.index("[你之前的回复]")
    assert "[user]" not in build.text
    # 播种头部必须存在且说明「据此继续、不要复述」
    assert "[上下文重建]" in build.text
    assert "不要复述历史" in build.text
    # 最后一条 user 必须保留
    assert "u2" in build.text
    assert build.included_messages == 3
    assert build.truncated is False
    assert "已省略" not in build.text


def test_truncated_history_is_announced_in_visible_text(monkeypatch):
    """裁剪必须写进**可见正文**：模型看不到 notes，会把被裁掉的历史当成本来就没有。"""
    monkeypatch.setattr(config, "CLAUDE_MAX_CONTEXT_CHARS", 120)
    normalized = normalize_messages(
        _msgs(("user", "A" * 200), ("assistant", "B" * 200), ("user", "CURRENT-QUESTION"))
    )
    build = build_prompt(normalized)
    assert build.truncated is True
    assert "已省略" in build.text
    # 说明必须出现在历史正文之前（紧跟播种头部）
    assert build.text.index("已省略") < build.text.index("CURRENT-QUESTION")


def test_tool_result_label_carries_call_id():
    normalized = normalize_messages(
        [ChatMessage(role="tool", content="result-body", tool_call_id="call_abc123")]
    )
    build = build_prompt(normalized)
    assert "[工具执行结果 call_abc123]" in build.text
    assert "result-body" in build.text


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


def test_empty_tool_result_is_explained_not_passed_through():
    """空输出必须显式说明（否则模型以为命令没生效，会把同一条命令反复重发）。"""
    normalized = normalize_messages(_msgs(("tool", "$ git status --short\n(no output)\nTook 0.0s")))
    build = build_prompt(normalized)
    assert "没有任何输出" in build.text
    assert "(no output)" not in build.text


def test_non_empty_tool_result_is_preserved_verbatim():
    """任何含真实内容的输出必须逐字节保留（edit 工具的 oldText 依赖它）。"""
    body = "line1\n\n  indented\n\tTabbed\n"
    normalized = normalize_messages([ChatMessage(role="tool", content=body)])
    build = build_prompt(normalized)
    assert body.rstrip("\n") in build.text


def test_repeated_empty_call_gets_named_hint():
    """同一命令反复发出且每次空输出时，末尾点名提醒（打断死循环）。"""
    messages = [
        ChatMessage(role="user", content="看看工作区状态"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_a",
                    "function": {"name": "bash", "arguments": '{"command": "git status"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="(no output)", tool_call_id="call_a"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_b",
                    "function": {"name": "bash", "arguments": '{"command": "git status"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="Took 0.0s", tool_call_id="call_b"),
    ]
    build = build_prompt(normalize_messages(messages))
    assert "[Repeated Empty Tool Call]" in build.text
    assert "infinite loop" in build.text


def test_first_empty_call_is_not_flagged_as_repeat():
    messages = [
        ChatMessage(role="user", content="看看工作区状态"),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_a",
                    "function": {"name": "bash", "arguments": '{"command": "git status"}'},
                }
            ],
        ),
        ChatMessage(role="tool", content="(no output)", tool_call_id="call_a"),
    ]
    build = build_prompt(normalize_messages(messages))
    assert "[Repeated Empty Tool Call]" not in build.text


def test_tools_instruction_injected_and_placed_next_to_the_task():
    """工具说明按 chatgpt-bridge 的实测结论**紧贴本轮任务**，而不是堆在末尾。"""
    tools = [ToolSpec(name="read_file", description="read", parameters={"required": ["path"]})]
    normalized = normalize_messages(
        _msgs(("user", "旧请求"), ("assistant", "旧回复"), ("user", "本轮任务"))
    )
    build = build_prompt(normalized, tools=tools)
    assert "[Tool Calling Instructions]" in build.text
    assert "read_file(path)" in build.text
    # 说明必须出现在**本轮任务之前**、旧历史之后
    assert build.text.index("[Tool Calling Instructions]") < build.text.index("本轮任务")
    assert build.text.index("旧请求") < build.text.index("[Tool Calling Instructions]")
    # 新会话还要带一份格式强调块（新桶最容易无视工具）
    assert "[Output Format Emphasis]" in build.text


def test_tools_instruction_not_duplicated_when_already_present():
    """客户端已内联同一份说明时不重复注入，避免两份说明互相干扰。"""
    from claude_web.toolcalls import TOOLCALL_HEADER

    tools = [ToolSpec(name="read_file", description="read", parameters={})]
    normalized = normalize_messages(
        _msgs(("user", f"{TOOLCALL_HEADER} 客户端已附说明"))
    )
    build = build_prompt(normalized, tools=tools)
    assert build.text.count(TOOLCALL_HEADER) == 1


def test_tool_result_mentioning_header_does_not_suppress_injection():
    """回归（P0 3.1）：tool 结果里含标题字面量时**仍要**注入工具说明。

    agent 读取本仓库源码 / 测试 / 文档时会自然产生这段文本；一旦它参与判重，
    之后每一轮以及任何重播种都不再注入工具说明（用本项目开发本项目必然触发）。
    """
    from claude_web.models import NormalizedMessage
    from claude_web.toolcalls import TOOLCALL_HEADER

    tools = [ToolSpec(name="read_file", description="read", parameters={})]
    normalized = [
        NormalizedMessage(role="user", content="读取源码"),
        NormalizedMessage(
            role="tool",
            content=f"{TOOLCALL_HEADER}\nYou are a language model...",
            tool_call_id="call_1",
        ),
    ]
    build = build_prompt(normalized, tools=tools)
    assert TOOLCALL_HEADER in build.text


def test_assistant_history_mentioning_header_does_not_suppress_injection():
    """回归（P0 3.1）：assistant 历史里含标题时仍要注入工具说明。"""
    from claude_web.models import NormalizedMessage
    from claude_web.toolcalls import TOOLCALL_HEADER

    tools = [ToolSpec(name="read_file", description="read", parameters={})]
    normalized = [
        NormalizedMessage(role="user", content="看看 toolcalls.py"),
        NormalizedMessage(
            role="assistant",
            content=f"这里是文件开头：\n{TOOLCALL_HEADER}\n规则如下……",
        ),
        NormalizedMessage(role="user", content="继续"),
    ]
    build = build_prompt(normalized, tools=tools)
    assert TOOLCALL_HEADER in build.text


def test_header_mid_line_in_user_message_does_not_suppress_injection():
    """回归：标题**不在行首**（正文引用 / 代码块）时不判重。"""
    from claude_web.toolcalls import TOOLCALL_HEADER

    tools = [ToolSpec(name="read_file", description="read", parameters={})]
    normalized = normalize_messages(
        _msgs(("user", f"请看这段引用：{TOOLCALL_HEADER} 后面还有字"))
    )
    build = build_prompt(normalized, tools=tools)
    assert TOOLCALL_HEADER in build.text


def test_system_header_at_line_start_suppresses_injection():
    """system 里标题位于行首时视为已内联，不重复注入。"""
    from claude_web.toolcalls import TOOLCALL_HEADER

    tools = [ToolSpec(name="read_file", description="read", parameters={})]
    normalized = normalize_messages(
        _msgs(("system", f"{TOOLCALL_HEADER}\n客户端已附完整说明"))
    )
    build = build_prompt(normalized, tools=tools)
    assert build.text.count(TOOLCALL_HEADER) == 1


def test_build_prompt_renders_each_message_at_most_once(monkeypatch):
    """回归：为「去重」而重渲染整段历史会把 20k 字符的工具结果也渲染一遍。

    旧实现先 ``_render_message`` 全部非 system 消息、拼接成一个 ``inbound`` 字符串，
    只为做一次子串判定，然后又渲染一遍历史做预算。这里断言每条消息至多渲染一次。
    """
    from claude_web import prompting

    rendered: list = []
    original = prompting._render_message

    def spy(message):
        rendered.append(message.role)
        return original(message)

    monkeypatch.setattr(prompting, "_render_message", spy)
    tools = [ToolSpec(name="read_file", description="read", parameters={})]
    messages = (
        ("user", "旧请求"),
        ("assistant", "旧回复"),
        ("tool", "x" * 5000),
        ("user", "本轮任务"),
    )
    build = build_prompt(normalize_messages(_msgs(*messages)), tools=tools)
    assert build.text.count("[Tool Calling Instructions]") == 1
    assert len(rendered) == len(messages)  # 每条消息恰好渲染一次
    assert rendered.count("tool") == 1


def test_seed_env_note_is_injected(monkeypatch):
    monkeypatch.setattr(config, "SEED_ENV_NOTE", "[环境说明] 项目就在本机。")
    build = build_prompt(normalize_messages(_msgs(("user", "hi"))))
    assert "[环境说明] 项目就在本机。" in build.text


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
