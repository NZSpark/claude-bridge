"""SSE 编码（doc/design.md 3.5、11.1）。"""

from __future__ import annotations

import json

from claude_web.api.chat_adapter import StreamEncoder
from claude_web.api.streaming_adapter import SSE_DONE, sse_comment, sse_data, sse_event


def _data(payload: str) -> str:
    assert payload.startswith("data: ")
    return payload[len("data: "):].strip()


def test_sse_helpers_format():
    assert sse_data({"a": 1}) == 'data: {"a": 1}\n\n'
    assert sse_comment("ping") == ": ping\n\n"
    named = sse_event("response.created", {"x": 1})
    assert named.startswith("event: response.created\ndata: ")
    assert json.loads(named.split("data: ", 1)[1]) == {"x": 1, "type": "response.created"}


def test_stream_encoder_roundtrip_concatenation():
    enc = StreamEncoder("claude-web")
    chunks = [enc.role()]
    for piece in ["Hello", ", ", "world"]:
        chunks.extend(enc.content(piece))
    chunks.append(enc.final("stop"))
    chunks.append(enc.done())

    assert chunks[0] == enc.role()  # role chunk identical shape
    role_payload = json.loads(_data(chunks[0]))
    assert role_payload["choices"][0]["delta"]["role"] == "assistant"

    content = "".join(
        json.loads(_data(c))["choices"][0]["delta"].get("content", "")
        for c in chunks[1:-2]
    )
    assert content == "Hello, world"

    final = json.loads(_data(chunks[-2]))
    assert final["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1] == SSE_DONE


def test_stream_encoder_splits_unicode_by_char_not_byte():
    enc = StreamEncoder("claude-web")
    text = "中文🙂" * 300
    chunks = enc.content(text)
    joined = "".join(
        json.loads(_data(c))["choices"][0]["delta"]["content"] for c in chunks
    )
    assert joined == text


def test_stream_encoder_tool_calls():
    from claude_web.models import FunctionCall, ToolCall

    enc = StreamEncoder("claude-web")
    call = ToolCall(id="call_1", function=FunctionCall(name="f", arguments='{"a":1}'))
    chunks = enc.tool_calls([call])
    payload = json.loads(_data(chunks[0]))
    delta = payload["choices"][0]["delta"]["tool_calls"][0]
    assert delta["function"]["name"] == "f"
    assert delta["id"] == "call_1"
