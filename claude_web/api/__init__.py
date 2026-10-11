"""协议适配层（doc/design.md 3、7）。

把**同一内部结果**转换为不同客户端所需的协议结构：

* :mod:`claude_web.api.chat_adapter`——OpenAI Chat Completions（含 SSE）；
* :mod:`claude_web.api.streaming_adapter`——通用 SSE 编码辅助；
* :mod:`claude_web.api.responses_adapter`——可选的 Responses API 子集。

适配器只做「内部模型 <-> 协议结构」的转换，不直接查询 DOM。
"""

from .chat_adapter import (
    StreamEncoder,
    build_completion_response,
    build_sse_headers,
    encode_sse,
    estimate_usage,
    to_task,
)

__all__ = [
    "StreamEncoder",
    "build_completion_response",
    "build_sse_headers",
    "encode_sse",
    "estimate_usage",
    "to_task",
]
