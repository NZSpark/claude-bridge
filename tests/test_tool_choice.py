"""``tool_choice`` 生效（T4.6 / D4.6）。

此前它只是被透传、从未被消费（doc/updates_deepseek.md §4 死键）。这里锁定语义：
``none`` 不注入工具说明；``required`` / 指定函数在提示里点名。
"""

from __future__ import annotations

from claude_web.models import NormalizedMessage, ToolSpec
from claude_web.prompting import build_prompt, tool_choice_effect

TOOLS = [ToolSpec(name="git_status", description="查看状态")]
MESSAGES = [NormalizedMessage(role="user", content="看看仓库状态")]
HEADER = "[Tool Calling Instructions]"


def test_none_suppresses_tool_instructions():
    """tool_choice=none：本轮不该调工具，连工具说明都不该注入。"""
    inject, directive = tool_choice_effect("none", TOOLS)
    assert inject is False and directive == ""
    build = build_prompt(MESSAGES, tools=TOOLS, tool_choice="none")
    assert HEADER not in build.text


def test_auto_still_injects_without_directive():
    """auto / None：维持现状——注入说明，但不加额外指令。"""
    assert tool_choice_effect("auto", TOOLS) == (True, "")
    assert tool_choice_effect(None, TOOLS) == (True, "")
    assert HEADER in build_prompt(MESSAGES, tools=TOOLS, tool_choice="auto").text


def test_required_names_the_obligation():
    """required：注入并点名「必须发起一次工具调用」。"""
    inject, directive = tool_choice_effect("required", TOOLS)
    assert inject is True
    assert "必须" in directive and "工具调用" in directive
    assert "必须" in build_prompt(MESSAGES, tools=TOOLS, tool_choice="required").text


def test_named_function_is_pointed_out():
    """指定函数：注入并点名只能调它。"""
    inject, directive = tool_choice_effect(
        {"type": "function", "function": {"name": "git_status"}}, TOOLS
    )
    assert inject is True
    assert "git_status" in directive
    assert "git_status" in build_prompt(
        MESSAGES,
        tools=TOOLS,
        tool_choice={"type": "function", "function": {"name": "git_status"}},
    ).text


def test_named_unknown_function_does_not_mislead():
    """点名了未声明的工具：仍注入说明（不静默丢弃），但不给误导性指令。"""
    inject, directive = tool_choice_effect(
        {"type": "function", "function": {"name": "nope"}}, TOOLS
    )
    assert inject is True and directive == ""


def test_no_tools_means_no_injection():
    """没声明工具时，任何 tool_choice 都不注入（也不崩）。"""
    assert tool_choice_effect("required", []) == (False, "")
    assert tool_choice_effect("none", None) == (False, "")
