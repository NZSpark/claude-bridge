"""Responses API 子集（doc/design.md 3.4、11.1）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from claude_web import config, server
from claude_web.api.responses_adapter import (
    build_response_object,
    response_events,
    store_thread,
    to_task_from_responses,
)
from claude_web.errors import InvalidRequestError, UnsupportedInputError
from claude_web.models import ChatTask, NormalizedMessage
from tests.fakes import FakePage, make_service


def test_string_input_becomes_user_message():
    task = to_task_from_responses({"input": "hello", "model": "claude-web"}, "b")
    assert task.messages[-1].role == "user"
    assert task.messages[-1].content == "hello"


def test_instructions_become_system_prefix():
    task = to_task_from_responses({"input": "hi", "instructions": "be terse"}, "b")
    assert task.messages[0].role == "system"
    assert "be terse" in task.messages[0].content


def test_function_tools_are_normalized():
    task = to_task_from_responses(
        {
            "input": "hi",
            "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}}],
        },
        "b",
    )
    assert [t.name for t in task.tools] == ["f"]


def test_unsupported_field_rejected():
    with pytest.raises(UnsupportedInputError):
        to_task_from_responses({"input": "hi", "background": True}, "b")


def test_unsupported_content_rejected():
    with pytest.raises(UnsupportedInputError):
        to_task_from_responses(
            {"input": [{"role": "user", "content": [{"type": "input_image"}]}]}, "b"
        )


def test_missing_input_rejected():
    with pytest.raises(InvalidRequestError):
        to_task_from_responses({}, "b")


def test_build_response_object_message_and_function_call():
    task = ChatTask(messages=[], model="claude-web", bucket="b")
    plain = build_response_object(task, text="hello")
    assert plain["output"][0]["type"] == "message"
    assert plain["output"][0]["content"][0]["text"] == "hello"

    from claude_web.models import FunctionCall, ToolCall

    call = ToolCall(id="call_1", function=FunctionCall(name="f", arguments="{}"))
    tooled = build_response_object(task, text="", tool_calls=[call])
    assert tooled["output"][0]["type"] == "function_call"
    assert tooled["output"][0]["call_id"] == "call_1"


def test_previous_response_id_threads_history():
    from claude_web.models import NormalizedMessage

    store_thread("resp_prev", [NormalizedMessage(role="user", content="first")], "answer-1")
    task = to_task_from_responses({"input": "second", "previous_response_id": "resp_prev"}, "b")
    contents = [m.content for m in task.messages]
    assert "first" in contents
    assert "answer-1" in contents
    assert contents[-1] == "second"


def test_response_events_include_completed():
    events = response_events("resp_1", "claude-web", text="hi")
    types = [line.split("\n", 1)[0].split(": ", 1)[1] for line in events if line.startswith("event: ")]
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"


def test_responses_route_gated_by_config(monkeypatch, tmp_path):
    svc = make_service(FakePage(snapshots=["ok"], generating_plan=[False]), tmp_path)
    monkeypatch.setattr(server, "service", svc)
    client = TestClient(server.app)

    monkeypatch.setattr(config, "ENABLE_RESPONSES_API", False)
    assert client.post("/v1/responses", json={"input": "hi"}).status_code == 404

    monkeypatch.setattr(config, "ENABLE_RESPONSES_API", True)
    resp = client.post("/v1/responses", json={"input": "hi"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["object"] == "response"
    assert payload["output_text"] == "ok"


# ==================== 真流式（T4.1 / D4.1）====================


def _event_types(sse_text: str) -> list:
    return [
        line.split(": ", 1)[1]
        for line in sse_text.splitlines()
        if line.startswith("event: ")
    ]


def test_responses_stream_emits_incremental_deltas(monkeypatch, tmp_path):
    """真流式：生成期间就要吐出 ``output_text.delta``，而不是等完成后一次性发。"""
    page = FakePage(snapshots=["答复一", "答复一加上后续"], generating_plan=[True, False])
    svc = make_service(page, tmp_path)
    monkeypatch.setattr(server, "service", svc)
    monkeypatch.setattr(config, "ENABLE_RESPONSES_API", True)
    client = TestClient(server.app)

    resp = client.post("/v1/responses", json={"input": "hi", "stream": True})
    assert resp.status_code == 200
    types = _event_types(resp.text)
    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    # 关键：至少一条增量（此前伪流式只会有 created + completed）。
    assert "response.output_text.delta" in types
    assert resp.text.count("response.output_text.delta") >= 1


def test_responses_stream_appends_tail_when_not_rewritten(monkeypatch, tmp_path):
    """无重写时：增量拼接 == 最终正文（尾部补齐，不重复、不漏字）。"""
    import json as _json

    # 页面文本只增不改：答复一 -> 答复一加上后续。
    page = FakePage(snapshots=["答复一", "答复一加上后续"], generating_plan=[True, False])
    svc = make_service(page, tmp_path)
    monkeypatch.setattr(server, "service", svc)
    monkeypatch.setattr(config, "ENABLE_RESPONSES_API", True)
    client = TestClient(server.app)

    resp = client.post("/v1/responses", json={"input": "hi", "stream": True})
    deltas = []
    completed_text = None
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            payload = _json.loads(line[len("data: "):])
            if payload.get("type") == "response.output_text.delta":
                deltas.append(payload["delta"])
            elif payload.get("type") == "response.completed":
                completed_text = payload["response"]["output_text"]
    assert "".join(deltas) == completed_text == "答复一加上后续"


def test_responses_stream_rewrite_keeps_completed_text_authoritative(monkeypatch, tmp_path):
    """网页重写回复：已发出的增量无法撤回，但 completed 必须给最终完整文本。"""
    import json as _json

    page = FakePage(snapshots=["答复一", "最终完整答复"], generating_plan=[True, False])
    svc = make_service(page, tmp_path)
    monkeypatch.setattr(server, "service", svc)
    monkeypatch.setattr(config, "ENABLE_RESPONSES_API", True)
    client = TestClient(server.app)

    resp = client.post("/v1/responses", json={"input": "hi", "stream": True})
    completed_text = None
    delta_count = 0
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            payload = _json.loads(line[len("data: "):])
            if payload.get("type") == "response.output_text.delta":
                delta_count += 1
            elif payload.get("type") == "response.completed":
                completed_text = payload["response"]["output_text"]
    # 重写后不再追加误导性增量，最终文本以 completed 为准。
    assert completed_text == "最终完整答复"
    assert delta_count == 1


def test_responses_stream_route_passes_request_for_disconnect(monkeypatch, tmp_path):
    """路由必须把 ``request`` 传进生成器，否则断连检测失效（真流式的前置条件）。"""
    import inspect

    sig = inspect.signature(server._stream_responses)
    assert "request" in sig.parameters


def test_stream_keepalive_config_is_wired(monkeypatch):
    """``STREAM_KEEPALIVE_S`` 必须真正被读（此前写死 ``_KEEPALIVE = 10.0``）。"""
    monkeypatch.setattr(config, "STREAM_KEEPALIVE_S", 0.25)
    assert server._keepalive_s() == 0.25
    # 下限保护：0 / 负值不应导致忙轮询。
    monkeypatch.setattr(config, "STREAM_KEEPALIVE_S", 0.0)
    assert server._keepalive_s() > 0


def test_response_event_helpers_compose():
    """新增的三个增量事件构造函数与 ``response_events`` 的事件名一致。"""
    from claude_web.api.responses_adapter import (
        response_completed_event,
        response_created_event,
        response_text_delta_event,
    )

    assert _event_types(response_created_event("resp_1", "m")) == ["response.created"]
    assert _event_types(response_text_delta_event("resp_1", "x")) == [
        "response.output_text.delta"
    ]
    assert _event_types(response_completed_event("resp_1", "m", text="x")) == [
        "response.completed"
    ]


# ==================== 工具循环续接（T4.2 / D4.2）====================


def test_store_thread_preserves_tool_calls_and_ids():
    """续接必须保留 assistant 的工具调用意图与 tool 结果的归属。"""
    from claude_web.models import FunctionCall, ToolCall

    messages = [
        NormalizedMessage(role="user", content="查一下"),
        NormalizedMessage(
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "git_status", "arguments": "{}"},
                }
            ],
        ),
        NormalizedMessage(role="tool", content="(no output)", tool_call_id="call_abc"),
    ]
    store_thread(
        "resp_tools",
        messages,
        "",
        tool_calls=[
            ToolCall(id="call_next", function=FunctionCall(name="ls", arguments="{}"))
        ],
    )
    task = to_task_from_responses(
        {"input": "继续", "previous_response_id": "resp_tools"}, "b"
    )
    roles = [m.role for m in task.messages]
    assert "assistant" in roles and "tool" in roles

    # 两条 assistant 工具调用都要在：历史里的 call_abc 与末尾新加的 call_next。
    call_ids = [c["id"] for m in task.messages if m.role == "assistant" for c in m.tool_calls]
    assert "call_abc" in call_ids
    assert "call_next" in call_ids
    # 名字 / 参数形态完整（不是只剩 id 的空壳）。内部表示为扁平 {id,name,arguments}。
    next_call = next(
        c for m in task.messages if m.role == "assistant" for c in m.tool_calls if c["id"] == "call_next"
    )
    assert next_call["name"] == "ls"
    assert next_call["arguments"] == "{}"

    # 上一轮那条 tool 结果的归属必须还在（否则模型无法把结果对应回调用）。
    tool_msg = next(m for m in task.messages if m.role == "tool")
    assert tool_msg.tool_call_id == "call_abc"


def test_store_thread_without_tool_calls_keeps_reply_text():
    """对照：无工具调用时仍按纯文本 assistant 消息存，不引入空 tool_calls。"""
    store_thread(
        "resp_plain",
        [NormalizedMessage(role="user", content="hi")],
        "答复",
    )
    task = to_task_from_responses(
        {"input": "next", "previous_response_id": "resp_plain"}, "b"
    )
    assistant = [m for m in task.messages if m.role == "assistant"]
    assert any("答复" in (m.content or "") for m in assistant)
    assert all(not m.tool_calls for m in assistant)
