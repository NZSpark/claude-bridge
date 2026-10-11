"""Responses API 兼容子集（doc/design.md 3.4）。

默认**不注册**该路由，由 ``ENABLE_RESPONSES_API`` 控制；只有实现了所需事件类型、
结束语义、错误映射与工具调用映射后才应宣告可用。本模块刻意只覆盖一个**明确列出**
的子集：

支持
----
* 请求：``model``、``input``（字符串或消息数组）、``instructions``、``stream``、
  ``tools``（扁平 function 定义）、``previous_response_id``、``max_output_tokens``；
* 输出：``output`` 里的 ``message``（``output_text``）与 ``function_call`` 项；
* 流式事件：``response.created`` / ``response.output_text.delta`` /
  ``response.function_call_arguments.delta`` / ``response.completed`` / ``error``。

不支持（会明确报错，不伪装成已实现）
------------------------------------
* 图片 / 文件 / 音频输入、``text.format`` 结构化输出、内置工具（web_search 等）、
  ``store`` / ``background`` / ``include`` 等。

``previous_response_id`` 关联的是**桥接层自己的持久化响应状态**，绝不解释为
Claude 网页会话 ID。
"""

from __future__ import annotations

import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from ..errors import InvalidRequestError, UnsupportedInputError
from ..models import ChatMessage, ChatTask, NormalizedMessage, ToolCall, ToolSpec
from ..prompting import normalize_messages
from .streaming_adapter import sse_event

_UNSUPPORTED_INPUT_TYPES = (
    "input_image",
    "input_file",
    "input_audio",
    "image_url",
)
_UNSUPPORTED_REQUEST_FIELDS = ("background", "include")


class ResponseStateStore:
    """桥接层自己的响应状态（``previous_response_id`` 关联用）。"""

    def __init__(self, limit: int = 64) -> None:
        self._items: "OrderedDict[str, List[Any]]" = OrderedDict()
        self._limit = limit

    def save(self, response_id: str, messages: List[Any]) -> None:
        self._items[response_id] = list(messages)
        self._items.move_to_end(response_id)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def load(self, response_id: str) -> List[Any]:
        return list(self._items.get(response_id, []))


response_state = ResponseStateStore()


def _item_to_message(item: Any) -> Optional[ChatMessage]:
    if isinstance(item, str):
        return ChatMessage(role="user", content=item)
    if not isinstance(item, dict):
        raise UnsupportedInputError(f"不支持的 input 项：{type(item).__name__}")
    item_type = str(item.get("type") or "message")
    if item_type == "function_call_output":
        return ChatMessage(
            role="tool",
            content=str(item.get("output") or ""),
            tool_call_id=item.get("call_id") or item.get("id"),
        )
    if item_type == "function_call":
        return ChatMessage(
            role="assistant",
            content=None,
            tool_calls=[
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {
                        "name": item.get("name"),
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            ],
        )
    if item_type != "message":
        raise UnsupportedInputError(f"当前版本不支持的 input 项类型：{item_type}")

    content = item.get("content")
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and str(part.get("type")) in _UNSUPPORTED_INPUT_TYPES:
                raise UnsupportedInputError(
                    f"当前版本不支持的内容类型：{part.get('type')}"
                )
    return ChatMessage(role=str(item.get("role") or "user"), content=content)


def _tools_from_responses(tools: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for entry in tools or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "function")
        if kind != "function":
            raise UnsupportedInputError(f"当前版本不支持的内置工具类型：{kind}")
        out.append(
            {
                "type": "function",
                "function": {
                    "name": entry.get("name"),
                    "description": entry.get("description") or "",
                    "parameters": entry.get("parameters")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def to_task_from_responses(body: Dict[str, Any], bucket: str) -> ChatTask:
    """把 Responses 请求转成内部 :class:`ChatTask`。"""
    if not isinstance(body, dict):
        raise InvalidRequestError("请求体必须是 JSON 对象")
    for field in _UNSUPPORTED_REQUEST_FIELDS:
        if body.get(field) not in (None, False):
            raise UnsupportedInputError(f"当前版本不支持字段：{field}")

    raw_input = body.get("input")
    if raw_input is None:
        raise InvalidRequestError("缺少 input 字段")
    items = raw_input if isinstance(raw_input, list) else [raw_input]

    messages: List[ChatMessage] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append(ChatMessage(role="system", content=str(instructions)))

    previous = body.get("previous_response_id")
    if previous:
        for item in response_state.load(str(previous)):
            if isinstance(item, dict):
                messages.append(ChatMessage(**item))
            elif isinstance(item, ChatMessage):
                messages.append(item)

    for item in items:
        message = _item_to_message(item)
        if message is not None:
            messages.append(message)

    normalized: List[NormalizedMessage] = normalize_messages(messages)
    tool_payload = _tools_from_responses(body.get("tools"))
    from ..prompting import normalize_tools

    tools: List[ToolSpec] = normalize_tools(tool_payload)
    return ChatTask(
        messages=normalized,
        model=str(body.get("model") or "claude-web"),
        bucket=bucket,
        stream=bool(body.get("stream")),
        tools=tools,
    )


def build_response_object(
    task: ChatTask,
    *,
    text: str,
    tool_calls: Optional[List[ToolCall]] = None,
    response_id: Optional[str] = None,
    created: Optional[int] = None,
    status: str = "completed",
) -> Dict[str, Any]:
    """构造非流式 Responses 输出对象。"""
    calls = list(tool_calls or [])
    output: List[Dict[str, Any]] = []
    if calls:
        for call in calls:
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex[:12]}",
                    "call_id": call.id,
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                    "status": "completed",
                }
            )
    else:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:12]}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    return {
        "id": response_id or f"resp_{uuid.uuid4().hex[:16]}",
        "object": "response",
        "created_at": created or int(time.time()),
        "model": task.model,
        "status": status,
        "output": output,
        "output_text": text if not calls else "",
    }


def response_events(
    response_id: str,
    model: str,
    *,
    text: str = "",
    tool_calls: Optional[List[ToolCall]] = None,
    created: Optional[int] = None,
) -> List[str]:
    """构造一次完整（非增量）Responses 流式事件序列。"""
    created = created or int(time.time())
    calls = list(tool_calls or [])
    events: List[str] = [
        sse_event("response.created", {"response": {"id": response_id, "model": model, "created_at": created}})
    ]
    if calls:
        for call in calls:
            events.append(
                sse_event(
                    "response.function_call_arguments.delta",
                    {"item_id": call.id, "delta": call.function.arguments, "name": call.function.name},
                )
            )
    elif text:
        events.append(sse_event("response.output_text.delta", {"item_id": response_id, "delta": text}))
    events.append(
        sse_event(
            "response.completed",
            {
                "response": build_response_object(
                    ChatTask(messages=[], model=model, bucket=""),
                    text=text,
                    tool_calls=calls,
                    response_id=response_id,
                    created=created,
                )
            },
        )
    )
    return events


def store_thread(response_id: str, messages: List[NormalizedMessage], reply_text: str) -> None:
    """把本轮对话写进响应状态，供下次 ``previous_response_id`` 续接。"""
    payload: List[Any] = []
    for message in messages:
        payload.append({"role": message.role, "content": message.content})
    if reply_text:
        payload.append({"role": "assistant", "content": reply_text})
    response_state.save(response_id, payload)


__all__ = [
    "ResponseStateStore",
    "response_state",
    "to_task_from_responses",
    "build_response_object",
    "response_events",
    "store_thread",
]
