"""新回复提取（doc/design.md 4.4）。

核心约束：**只提取当前任务新增的 assistant 回复**。提交前记录已存在的回复条数
（``baseline``），提交后只读取 ``baseline`` 之外的新节点，避免把上一轮残留文本
当成本轮输出。

除了「新旧回复」这层，还有「节点里哪些文本属于正文」这层：assistant 节点同时包含
无障碍标题、动作栏、时间戳与推广行。DOM 侧（``COMPLETE_TEXT_JS`` + 可配置的
``REPLY_CHROME_SELECTORS``）负责剔除它们；这里再做一层**窄口径的文本兜底**，
只针对可确认的外壳形态，避免站点改版后把「Claude responded: <整段正文>」这类
无障碍标题当成正文（客户端会看到同一段正文出现两遍）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from ..browser.dom_adapter import ClaudeDOMAdapter

logger = logging.getLogger(__name__)

#: 无障碍标题的前缀（站点在回复节点里放一个 ``sr-only`` 标题，内容就是整段正文）。
_A11Y_LABEL_PREFIXES = (
    "claude responded:",
    "claude replied:",
    "claude said:",
    "claude 回复：",
    "claude 回答：",
)

#: 「单独成行的短时间戳」——页面 ``<time>`` 落在节点里时就是这个形态。
_TIMESTAMP_PATTERNS = (
    re.compile(r"^(?:just now|now)$", re.IGNORECASE),
    re.compile(r"^(?:刚刚|刚才|昨天|今天)$"),
    re.compile(
        r"^\d+\s*(?:s|m|h|sec|secs|second|seconds|min|mins|minute|minutes|hour|hours|"
        r"day|days|week|weeks|month|months|year|years)\s+ago$",
        re.IGNORECASE,
    ),
    re.compile(r"^\d+\s*(?:秒|分钟|分|小时|天|周|个月|月|年)前$"),
    re.compile(r"^(?:today|yesterday)\s+\d{1,2}:\d{2}$", re.IGNORECASE),
    re.compile(r"^\d{1,2}:\d{2}(?:\s*[APap]\.?[Mm]\.?)?$"),
)

#: 时间戳行的长度上限：更长的一行不像时间戳，宁可不剔。
_TIMESTAMP_MAX_CHARS = 40


def _is_timestamp_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _TIMESTAMP_MAX_CHARS:
        return False
    return any(pattern.match(stripped) for pattern in _TIMESTAMP_PATTERNS)


def _collapse_doubled_body(text: str) -> str:
    """折叠「整段正文出现两遍」的形态（只在确认是无障碍标题时才调用）。

    无障碍标题的内容就是整段正文，于是剔除前缀后剩下的恰好是 ``X + 分隔 + X``。
    按长度反推 X 并逐字比对一个字符都不差，因此不会误伤「模型有意重复某一段」的正文。
    """
    trimmed = text.strip()
    for separator in ("\n\n", "\n"):
        size = len(trimmed) - len(separator)
        if size <= 0 or size % 2:
            continue
        half = trimmed[: size // 2]
        if half and trimmed == half + separator + half:
            return half.strip()
    return trimmed


def strip_page_chrome(text: str) -> str:
    """去掉回复正文里混进来的页面外壳（时间戳行、无障碍标题及其重复正文）。"""
    cleaned = text or ""
    # 1) 末尾的时间戳：只剔「最后一行且整行就是时间戳」的情况。
    lines = cleaned.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and _is_timestamp_line(lines[-1]):
        lines.pop()
        cleaned = "\n".join(lines)
    # 2) 无障碍标题前缀 + 它带来的整段重复。
    stripped = cleaned.lstrip()
    lowered = stripped.lower()
    for prefix in _A11Y_LABEL_PREFIXES:
        if lowered.startswith(prefix):
            cleaned = _collapse_doubled_body(stripped[len(prefix):])
            break
    return cleaned.strip()


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
    """归一化回复文本（去掉零宽字符与页面外壳，保留内部换行/代码块）。"""
    if not text:
        return ""
    cleaned = text.replace("\u200b", "").replace("\ufeff", "")
    return strip_page_chrome(cleaned)


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


__all__ = ["ReplyExtractor", "ReplyRead", "normalize_reply", "strip_page_chrome"]
