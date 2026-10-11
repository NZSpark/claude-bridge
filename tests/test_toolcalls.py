"""工具调用注入与解析（doc/design.md 6、11.1）。"""

from __future__ import annotations

import json

from claude_web.models import ToolSpec
from claude_web.toolcalls import format_tools_instruction, parse_tool_calls


def test_instruction_lists_tools_and_required_params():
    text = format_tools_instruction(
        [ToolSpec(name="read_file", description="read a file", parameters={"required": ["path"]})]
    )
    assert "```tool_call" in text
    assert "read_file(必填: path)" in text


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
