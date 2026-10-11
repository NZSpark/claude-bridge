"""工具调用注入与解析（doc/design.md 6、11.1）。"""

from __future__ import annotations

import json

from claude_web.models import ToolSpec
from claude_web.toolcalls import (
    format_tools_instruction,
    parse_tool_calls,
    strip_tool_call_blocks,
)


def test_instruction_lists_tools_and_required_params():
    text = format_tools_instruction(
        [ToolSpec(name="read_file", description="read a file", parameters={"required": ["path"]})]
    )
    assert "```tool_call" in text
    assert "read_file(path)" in text


def test_instruction_gives_concrete_example_with_typed_placeholders():
    """示例必须带真实工具名 / 参数键，且占位值与声明类型一致（整数不能变成字符串）。"""
    text = format_tools_instruction(
        [
            ToolSpec(
                name="read",
                description="read a file",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                    "required": ["path"],
                },
            )
        ]
    )
    assert '{"name": "read", "arguments": {"path": "..."}}' in text
    # 不得再出现会被原样照抄的尖括号占位符（旧写法会把 < 发进 shell）
    assert "<工具名>" not in text
    assert "<command>" not in text


def test_instruction_states_client_executes_and_blocks_refusal():
    """切断退路：说明「由客户端执行」，并禁止以「我没有工具」为借口拒答。"""
    text = format_tools_instruction([ToolSpec(name="f", description="d")])
    assert "the task FAILS" in text
    assert "have no tools" in text
    assert "ONE tool_call block per reply" in text
    assert "Never emit XML / DSL tags" in text


def test_emphasis_block_uses_same_example_shape():
    from claude_web.toolcalls import EMPHASIS_HEADER, format_tool_call_emphasis

    spec = ToolSpec(name="grep_search", description="search", parameters={"required": ["pattern"]})
    text = format_tool_call_emphasis([spec])
    assert text.startswith(EMPHASIS_HEADER)
    assert '{"name": "grep_search", "arguments": {"pattern": "..."}}' in text
    assert "```tool_call" in text


def test_instruction_verbose_embeds_schema():
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    text = format_tools_instruction(
        [ToolSpec(name="f", description="d", parameters=schema)], verbose=True
    )
    assert json.dumps(schema, ensure_ascii=False) in text


def test_parse_valid_single_call():
    reply = 'Sure.\n```tool_call\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'
    parsed = parse_tool_calls(reply, allowed_names=["read_file"])
    assert parsed.saw_marker
    assert parsed.ok
    call = parsed.calls[0]
    assert call.function.name == "read_file"
    assert json.loads(call.function.arguments) == {"path": "a.txt"}
    assert call.id.startswith("call_")


def test_parse_multiple_calls_in_one_fence():
    reply = (
        "```tool_call\n"
        '[{"name": "a", "arguments": {"x": 1}}, {"name": "b", "arguments": {}}]\n'
        "```"
    )
    parsed = parse_tool_calls(reply, allowed_names=["a", "b"])
    assert [c.function.name for c in parsed.calls] == ["a", "b"]


def test_parse_undeclared_tool_is_diagnostic_not_call():
    reply = '```tool_call\n{"name": "danger", "arguments": {}}\n```'
    parsed = parse_tool_calls(reply, allowed_names=["safe"])
    assert parsed.calls == []
    assert any("未声明" in err for err in parsed.errors)


def test_malformed_json_is_reported():
    reply = "```tool_call\n{not json at all\n```"
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.saw_marker
    assert parsed.calls == []
    assert parsed.errors


def test_arguments_must_be_object():
    reply = '```tool_call\n{"name": "f", "arguments": [1, 2]}\n```'
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.calls == []
    assert parsed.errors


def test_plain_markdown_is_not_a_tool_call():
    reply = "Here is code:\n```python\nprint('hi')\n```\nDone."
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert not parsed.saw_marker
    assert parsed.calls == []


def test_quoted_tool_fence_inside_other_fence_still_requires_json():
    """普通文本里提到 ```tool_call 但没有合法 JSON 时不得变成调用。"""
    reply = "只需输出 `tool_call` 代码块。"
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.calls == []


def test_arguments_accept_json_string():
    reply = '```tool_call\n{"name": "f", "arguments": "{\\"a\\": 1}"}\n```'
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.ok
    assert json.loads(parsed.calls[0].function.arguments) == {"a": 1}


# ---- 宽容路径（移植自 deepseek-bridge 的解析器）----
#
# 实机回归：模型照约定输出了 tool_call + 合法 JSON，但网页 DOM 把 ``` 渲染成
# <pre>，inner_text 只剩「标记 + 裸 JSON」，旧解析器只认围栏，于是 tool_calls=null。

BREA_FREE_REPLY = (
    "\ue056\ntool_call\n"
    '{"name": "read_file", "arguments": {"path": "a.txt"}}\n'
    "Claude works directly with your codebase\njust now"
)


def test_bare_marker_with_json_is_parsed():
    parsed = parse_tool_calls(BREA_FREE_REPLY, allowed_names=["read_file"])
    assert parsed.saw_marker
    assert parsed.ok
    assert parsed.calls[0].function.name == "read_file"
    assert json.loads(parsed.calls[0].function.arguments) == {"path": "a.txt"}


def test_bare_marker_tolerates_noise_lines():
    """标记与 JSON 之间可能夹着引用符号 / 竖线 / 反引号残留。"""
    reply = '> tool_call\n|\n```\n\n{"name": "f", "arguments": {"a": 1}}'
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.ok
    assert json.loads(parsed.calls[0].function.arguments) == {"a": 1}


def test_bare_marker_still_requires_allowed_name():
    reply = 'tool_call\n{"name": "rm_rf", "arguments": {}}'
    parsed = parse_tool_calls(reply, allowed_names=["safe"])
    assert parsed.calls == []
    assert any("未声明" in err for err in parsed.errors)


def test_plain_json_in_prose_is_not_a_call():
    """宽容路径不得把回复里的普通 JSON 当成调用（没有标记就不认）。"""
    reply = '配置如下：\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n完成。'
    parsed = parse_tool_calls(reply, allowed_names=["read_file"])
    assert parsed.calls == []
    assert not parsed.saw_marker


def test_strip_tool_call_blocks_removes_bare_form():
    """无围栏形态的工具块也不得泄漏进正文。"""
    stripped = strip_tool_call_blocks(BREA_FREE_REPLY)
    assert "tool_call" not in stripped
    assert "read_file" not in stripped
    assert "Claude works directly with your codebase" in stripped


def test_dsml_wrapped_tool_uses_is_parsed():
    reply = '<\uff5c\uff5cDSML\uff5c\uff5c calls>{"tool_uses": [{"name": "f", "arguments": {"a": 1}}]}<\uff5c\uff5cDSML\uff5c\uff5c calls>'
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.ok
    assert json.loads(parsed.calls[0].function.arguments) == {"a": 1}


def test_dsml_invoke_parameters_are_parsed():
    reply = (
        '<\uff5c\uff5cDSML\uff5c\uff5c calls>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5c invoke name="bash">\n'
        '<\uff5c\uff5cDSML\uff5c\uff5c parameter name="command" string="true">ls -la<\uff5c\uff5cDSML\uff5c\uff5c parameter>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5c invoke>\n'
        '<\uff5c\uff5cDSML\uff5c\uff5c calls>'
    )
    # 模型自造的通用名 bash 在只有一个 shell 类工具时会被对齐到声明名
    parsed = parse_tool_calls(reply, allowed_names=["run_command"])
    assert parsed.ok
    assert parsed.calls[0].function.name == "run_command"
    assert json.loads(parsed.calls[0].function.arguments) == {"command": "ls -la"}


def test_json_fence_is_accepted_as_fallback():
    reply = '```json\n{"name": "f", "arguments": {}}\n```'
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.ok


def test_openai_native_function_shape_is_accepted():
    reply = '```tool_call\n{"function": {"name": "f", "arguments": {"a": 1}}}\n```'
    parsed = parse_tool_calls(reply, allowed_names=["f"])
    assert parsed.ok
    assert json.loads(parsed.calls[0].function.arguments) == {"a": 1}
