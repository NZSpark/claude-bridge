"""Chat Completions 协议适配（doc/design.md 3.2、3.3、3.5）。

* :func:`to_task` 把 OpenAI 请求转成内部 :class:`~claude_web.models.ChatTask`；
* :func:`build_completion_response` 生成非流式响应；
* :class:`StreamEncoder` 生成 SSE chunk（合成流：由回复快照增量编码，不宣称
  上游 token 级流）。

``finish_reason`` 只表达桥接层能确认的结束状态（``stop`` / ``tool_calls``），
不伪造无法区分的官方语义。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from .. import config
from ..models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatTask,
    Choice,
    ChoiceMessage,
    FunctionCall,
    ToolCall,
    Usage,
)
from ..prompting import estimate_tokens, normalize_messages, normalize_tools
from .streaming_adapter import SSE_DONE, sse_data

#: 单个 SSE content chunk 的最大字符数（更接近“流”的观感，且不破坏字符边界）。
CHUNK_CHARS = 200


def to_task(request: ChatCompletionRequest, bucket: str) -> ChatTask:
    """把 OpenAI 请求转成内部任务模型（含消息归一化）。"""
    messages = normalize_messages(request.messages)
    tools = normalize_tools(request.tools)
    return ChatTask(
        messages=messages,
        model=request.model or config.DEFAULT_MODEL_ALIAS,
        bucket=bucket,
        stream=bool(request.stream),
        tools=tools,
        tool_choice=request.tool_choice,
    )


def estimate_usage(prompt_text: str, completion_text: str) -> Usage:
    """保守 usage：基于字符估算，明确标注为非精确值（doc/design.md 3.3）。"""
    prompt_tokens = estimate_tokens(prompt_text)
    completion_tokens = estimate_tokens(completion_text)
    return Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def build_completion_response(
    task: ChatTask,
    *,
    text: str,
    tool_calls: Optional[List[ToolCall]] = None,
    finish_reason: str = "stop",
    prompt_text: str = "",
    created: Optional[int] = None,
) -> ChatCompletionResponse:
    """构造非流式 Chat Completion 响应。"""
    calls = list(tool_calls or [])
    message = ChoiceMessage(
        role="assistant",
        content=text if text else (None if calls else ""),
        tool_calls=calls or None,
    )
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex[:12]}",
        created=created or int(time.time()),
        model=task.model,
        choices=[Choice(index=0, message=message, finish_reason=finish_reason)],
        usage=estimate_usage(prompt_text, text),
    )


def build_sse_headers() -> Dict[str, str]:
    return {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def encode_sse(payload: Any) -> str:
    return sse_data(payload)


def _chunk_text(text: str, size: int = CHUNK_CHARS) -> List[str]:
    """按字符（而非字节）切分文本，保证非 ASCII / 代码块不被拆坏。"""
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


class StreamEncoder:
    """把同一轮生成编码成 Chat Completions SSE。"""

    def __init__(self, model: str, *, completion_id: Optional[str] = None, created: Optional[int] = None):
        self.model = model
        self.id = completion_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
        self.created = created or int(time.time())
        self._role_sent = False

    def _payload(self, delta: Dict[str, Any], finish_reason: Optional[str]) -> Dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }

    def role(self) -> str:
        """首个 chunk：assistant role（doc/design.md 3.5 第 1 条）。"""
        self._role_sent = True
        return sse_data(self._payload({"role": "assistant", "content": ""}, None))

    def content(self, piece: str) -> List[str]:
        """正文增量 chunk（按字符切分，保持 UTF-8 完整）。"""
        return [sse_data(self._payload({"content": part}, None)) for part in _chunk_text(piece)]

    def tool_calls(self, calls: List[ToolCall]) -> List[str]:
        """工具调用增量：一次性输出已校验的完整调用（缓冲模式）。"""
        out: List[str] = []
        for index, call in enumerate(calls):
            delta = {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                ]
            }
            out.append(sse_data(self._payload(delta, None)))
        return out

    def final(self, finish_reason: str) -> str:
        return sse_data(self._payload({}, finish_reason))

    @staticmethod
    def done() -> str:
        return SSE_DONE


__all__ = [
    "CHUNK_CHARS",
    "StreamEncoder",
    "build_completion_response",
    "build_sse_headers",
    "encode_sse",
    "estimate_usage",
    "to_task",
    "FunctionCall",
]
