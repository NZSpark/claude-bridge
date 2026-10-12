"""新回复提取（doc/design.md 4.4）。

核心约束：**只提取当前任务新增的 assistant 回复**。提交前记录页面的回复状态
（``baseline``：assistant 条数 + 最后一条提问的条数 / 文本指纹），提交后只用
``baseline`` 之外的新节点，避免把上一轮残留文本当成本轮输出。

⚠️ 判定不能只看 assistant 条数：真实页面上新增本轮回复时可能同时回收 / 复用旧行
（条数停在基线上），只看条数会漏判，把一次**页面上明明已经回答**的请求报成
``reply_not_found``（事故 2026-10-12）。长会话里页面还会**虚拟化 / 回收**旧行，提交后
条数甚至**比基线更少**（同一次事故的第二轮：基线 8 条 → 6 条回复 / 5 条提问）。因此
:meth:`ReplyExtractor.read_new` 用条数与「本轮提问是否已落到页面上（按文本指纹，与条数
无关）且其后已有 assistant 节点」两把独立的钥匙（见 4.4）。

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

from ..browser.dom_adapter import ClaudeDOMAdapter, ReplyState

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
class ReplyBaseline:
    """提交前的页面回复状态指纹（doc/design.md 4.4）。

    * ``count``：assistant 节点条数；
    * ``user_count`` / ``user_tail``：页面上最后一条提问的条数与文本尾巴。
    """

    count: int = 0
    user_count: int = 0
    user_tail: Optional[str] = None


@dataclass
class ReplyRead:
    """一次回复读取结果。"""

    text: str = ""
    count: int = 0
    is_new: bool = False
    selector: Optional[str] = None
    #: 同时读到的 user 消息条数（诊断）。
    user_count: int = 0
    #: 读取时「最后一条 assistant 节点是否排在最后一条 user 消息之后」（诊断）：
    #: 失败时用它区分「页面根本没渲染回复节点」与「渲染了但条数口径没认出来」。
    trailing: bool = False
    #: ``True`` = 这条新回复是靠**结构信号**认出来的（assistant 条数当时停在基线上）。
    #: 正常轮次为 ``False``；出现 ``True`` 就说明条数口径那一刻失灵了（诊断用）。
    structural: bool = False

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

    async def baseline(self, page: Any) -> ReplyBaseline:
        """记录提交前的回复 / 提问状态指纹。

        除 assistant 条数外还记下「最后一条提问」：只比条数会漏判（doc/design.md 4.4，
        事故 2026-10-12：页面上已有回复，却因为条数没涨而报 ``reply_not_found``）。
        """
        state = await self.dom.reply_state(page, want_tail=True)
        return ReplyBaseline(
            count=state.reply_count,
            user_count=state.user_count,
            user_tail=state.last_user_tail,
        )

    async def read_new(self, page: Any, baseline: ReplyBaseline) -> ReplyRead:
        """读取 ``baseline`` 之后的最新回复；还没有新回复时返回 ``is_new=False``。

        判定用**两把独立的钥匙**（任一成立即算新回复）：

        1. **条数**：assistant 节点比基线多（改造前的唯一口径）；
        2. **结构**：页面上最后一条提问已经不是基线那一条（按**文本指纹**比较，与条数无关），
           且其后已经出现 assistant 节点——所以页面回收 / 复用旧行时依然能认出回复，
           **包括条数不升反降**（页面虚拟化掉旧行，2026-10-12 第二轮事故）的场面。

        两把都不成立才算「还没有新回复」。指纹读不到时（旧页面 / 替身页面）退回只看条数，
        与改造前的行为一致。
        """
        state = await self.dom.reply_state(page)
        if state.reply_count > baseline.count:
            snapshot = await self.dom.read_reply_text(page, index=-1)
            return ReplyRead(
                text=snapshot.text,
                count=state.reply_count,
                is_new=True,
                selector=snapshot.selector,
                user_count=state.user_count,
                trailing=state.trailing_reply,
            )
        # 条数没涨：只有「页面已经在回答本轮的提问」才算有新回复。不能仅凭页面上存在
        # 一条 assistant 节点就下结论——那可能还是上一条回复（会把旧正文当成新回复返回）。
        if not state.trailing_reply or not await self._newer_turn(page, state, baseline):
            return ReplyRead(
                count=state.reply_count,
                user_count=state.user_count,
                trailing=state.trailing_reply,
                is_new=False,
            )
        snapshot = await self.dom.read_reply_text(page, index=-1)
        # 每一轮轮询都会走到这里，所以用 DEBUG；「本轮回复是靠结构信号认出来的」那条
        # 一次性 INFO 由生成循环记（见 generator.generate）。
        logger.debug(
            "[回复] assistant 条数停在基线上（%s），靠结构信号认出本轮回复（提问 %s→%s）",
            baseline.count,
            baseline.user_count,
            state.user_count,
        )
        return ReplyRead(
            text=snapshot.text,
            count=state.reply_count,
            is_new=True,
            selector=snapshot.selector,
            user_count=state.user_count,
            trailing=state.trailing_reply,
            structural=True,
        )

    async def _newer_turn(
        self, page: Any, state: ReplyState, baseline: ReplyBaseline
    ) -> bool:
        """页面上最后一条提问是否已经不是基线那一条（= 本轮提问已落到页面上）。

        首选**身份**口径：比最后一条提问的文本指纹。它与条数无关，因此页面对旧行做
        **回收 / 虚拟化**时同样成立——这类页面提交后条数会**比基线更少**（事故
        2026-10-12 第二轮：基线 8 条，提交后页面上只剩 6 条回复 / 5 条提问，而页面确实
        已经给出回复）。旧口径「条数没变多就当作还是基线那一条」会把整轮判死，报出
        ``reply_not_found``。

        指纹读不到时（旧页面 / 替身页面）退回条数口径：``user`` 条数变多 → 是本轮；
        否则当作「还是基线那一条」，宁可不认，也不能把上一条回复当成新回复返回。
        """
        if state.user_count > baseline.user_count:
            return True
        probed = await self.dom.reply_state(page, want_tail=True)
        tail = probed.last_user_tail
        if tail and baseline.user_tail:
            return tail != baseline.user_tail
        return probed.user_count > baseline.user_count

    async def read_latest(self, page: Any) -> ReplyRead:
        """读取最后一条回复（不区分新旧，用于恢复场景核对页面现状）。"""
        count = await self.dom.reply_count(page)
        if count == 0:
            return ReplyRead(text="", count=0, is_new=False)
        snapshot = await self.dom.read_reply_text(page, index=-1)
        return ReplyRead(text=snapshot.text, count=count, is_new=True, selector=snapshot.selector)


__all__ = [
    "ReplyExtractor",
    "ReplyBaseline",
    "ReplyRead",
    "normalize_reply",
    "strip_page_chrome",
]
