"""单轮生成：输入提交、回复提取、结束判定与有限恢复（doc/design.md 4.4、7）。

* :mod:`claude_web.completion.lifecycle`——会话级生命周期（新对话、播种决策、恢复）；
* :mod:`claude_web.completion.generator`——提交并轮询协调回复生成；
* :mod:`claude_web.completion.extractor`——只提取本轮**新增**的 assistant 回复。
"""

from .extractor import ReplyExtractor, ReplyRead
from .generator import GenerationResult, ReplyGenerator
from .lifecycle import Conversation, SessionLifecycle

__all__ = [
    "ReplyExtractor",
    "ReplyRead",
    "GenerationResult",
    "ReplyGenerator",
    "Conversation",
    "SessionLifecycle",
]
