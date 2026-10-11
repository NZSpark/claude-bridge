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
