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
from claude_web.models import ChatTask
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
