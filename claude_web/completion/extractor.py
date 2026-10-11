"""新回复提取（doc/design.md 4.4）。

核心约束：**只提取当前任务新增的 assistant 回复**。提交前记录已存在的回复条数
（``baseline``），提交后只读取 ``baseline`` 之外的新节点，避免把上一轮残留文本
当成本轮输出。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from ..browser.dom_adapter import ClaudeDOMAdapter

logger = logging.getLogger(__name__)


@dataclass
class ReplyRead:
    """一次回复读取结果。"""

    text: str = ""
    count: int = 0
    is_new: bool = False
    selector: Optional[str] = None

    @property
    def normalized(self) -> str:
        return normalize_reply(self.text)


def normalize_reply(text: str) -> str:
    """归一化回复文本（去掉首尾空白与零宽字符，保留内部换行/代码块）。"""
    if not text:
        return ""
    cleaned = text.replace("\u200b", "").replace("\ufeff", "")
    return cleaned.strip()


class ReplyExtractor:
    """基于 :class:`ClaudeDOMAdapter` 的回复提取器。"""

    def __init__(self, dom: Optional[ClaudeDOMAdapter] = None) -> None:
        self.dom = dom or ClaudeDOMAdapter()

    async def baseline(self, page: Any) -> int:
        """记录提交前的回复条数。"""
        return await self.dom.reply_count(page)

    async def read_new(self, page: Any, baseline: int) -> ReplyRead:
        """读取 ``baseline`` 之后的最新回复；还没有新回复时返回 ``is_new=False``。"""
        count = await self.dom.reply_count(page)
        if count <= baseline:
            return ReplyRead(text="", count=count, is_new=False)
        snapshot = await self.dom.read_reply_text(page, index=-1)
        return ReplyRead(
            text=snapshot.text,
            count=count,
            is_new=True,
            selector=snapshot.selector,
        )

    async def read_latest(self, page: Any) -> ReplyRead:
        """读取最后一条回复（不区分新旧，用于恢复场景核对页面现状）。"""
        count = await self.dom.reply_count(page)
        if count == 0:
            return ReplyRead(text="", count=0, is_new=False)
        snapshot = await self.dom.read_reply_text(page, index=-1)
        return ReplyRead(text=snapshot.text, count=count, is_new=True, selector=snapshot.selector)


__all__ = ["ReplyExtractor", "ReplyRead", "normalize_reply"]
