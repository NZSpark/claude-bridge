"""通用 SSE 编码辅助（doc/design.md 3.5）。

Chat Completions 用裸 ``data:`` chunk；Responses API 用命名事件（``event:`` + ``data:``）。
两者共用这里的编码函数，保证换行 / UTF-8 / ``[DONE]`` 语义只有一处实现。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

SSE_DONE = "data: [DONE]\n\n"


def sse_data(payload: Any) -> str:
    """裸 ``data:`` 事件（Chat Completions 风格）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_event(event_type: str, payload: Dict[str, Any]) -> str:
    """命名 SSE 事件：``event:`` + 自带 ``type`` 的 data 载荷。"""
    data = dict(payload)
    data["type"] = event_type
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_comment(text: str = "keep-alive") -> str:
    """SSE 注释行，用于生成期间保活。"""
    return f": {text}\n\n"


def sse_error(message: str, code: Optional[str] = None) -> str:
    """流内错误事件（SSE 已开始后不能再改成普通 HTTP 错误状态）。"""
    payload: Dict[str, Any] = {"error": {"message": message, "type": code or "upstream_error"}}
    return sse_data(payload)


__all__ = ["SSE_DONE", "sse_data", "sse_event", "sse_comment", "sse_error"]
