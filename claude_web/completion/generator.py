"""提交并协调回复生成（doc/design.md 3.5、4.4、6）。

流程：

1. 确认页面可用（未登录 / 挑战 / 无输入区都以语义化异常结束）；
2. 记录提交前的回复条数（baseline）；
3. 填充输入区（失败可安全重试——此时**尚无副作用**）并提交；
4. 轮询回复快照，用 :func:`claude_web.end_detection.evaluate_poll` 判定结束；
5. 需要工具时**缓冲整段回复**再解析，避免把内部工具标记泄漏到普通文本里。

重要：提交一旦**可能成功**，就不再无条件重发（doc/design.md 9.3）——因此本模块
只在提交前重试，提交后只做「读页面现有回复」式的恢复。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence

from .. import config
from ..browser.dom_adapter import ClaudeDOMAdapter
from ..end_detection import EndLimits, EndState, evaluate_poll
from ..errors import (
    ContextLimitError,
    GenerationStalledError,
    GenerationTimeoutError,
    PromptSubmissionError,
    ReplyNotFoundError,
)
from ..models import ToolCall, ToolSpec
from ..toolcalls import ParsedToolCalls, parse_tool_calls, strip_tool_call_blocks
from .extractor import ReplyExtractor, normalize_reply

logger = logging.getLogger(__name__)

#: 内容增量回调：接收**当前完整**的归一化回复文本（由调用方决定如何编码）。
DeltaCallback = Callable[[str], Awaitable[None]]
#: 取消探测：返回 True 表示调用方要求中止本轮。
CancelCheck = Callable[[], bool]


@dataclass
class GenerationResult:
    """一次生成的结果。"""

    text: str = ""
    raw_text: str = ""
    finish_reason: str = "stop"
    tool_calls: List[ToolCall] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    polls: int = 0
    saw_generating: bool = False
    rewritten: bool = False
    parse: Optional[ParsedToolCalls] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def _strip_tool_fences(text: str, fence: str) -> str:
    """去掉承载工具调用的块，保留其余正文。

    包含围栏与**无围栏**（DOM 吃掉 ```）两种形态，见
    :func:`claude_web.toolcalls.strip_tool_call_blocks`。
    """
    return strip_tool_call_blocks(text, fence=fence)


class ReplyGenerator:
    def __init__(
        self,
        dom: Optional[ClaudeDOMAdapter] = None,
        extractor: Optional[ReplyExtractor] = None,
        *,
        poll_interval: Optional[float] = None,
    ) -> None:
        self.dom = dom or ClaudeDOMAdapter()
        self.extractor = extractor or ReplyExtractor(self.dom)
        self._poll_interval = poll_interval

    @property
    def poll_interval(self) -> float:
        return self._poll_interval if self._poll_interval is not None else config.POLL_INTERVAL_S

    def limits(self) -> EndLimits:
        return EndLimits(
            quiet_polls=config.QUIET_POLLS,
            stable_polls=config.STABLE_POLLS,
            stall_limit=config.STALL_POLLS,
            extend_step_s=max(1.0, config.RESPONSE_TIMEOUT_S),
        )

    async def _fill(self, page, prompt: str) -> None:
        """填充输入框；仅在此阶段允许重试（无副作用）。"""
        attempts = max(1, config.MAX_UPSTREAM_RETRIES + 1)
        last: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                await self.dom.fill_composer(page, prompt)
                return
            except BaseException as exc:  # noqa: BLE001
                last = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(config.RETRY_BACKOFF_S * (attempt + 1))
        assert last is not None
        raise last

    async def generate(
        self,
        page,
        prompt: str,
        *,
        tools: Optional[Sequence[ToolSpec]] = None,
        on_delta: Optional[DeltaCallback] = None,
        cancel: Optional[CancelCheck] = None,
        timeout_s: Optional[float] = None,
    ) -> GenerationResult:
        tool_list = list(tools or [])
        buffered = bool(tool_list) and config.TOOL_CALL_BUFFER

        await self.dom.ensure_usable(page)
        baseline = await self.extractor.baseline(page)

        await self._fill(page, prompt)
        await self.dom.submit(page)

        timeout = timeout_s if timeout_s is not None else config.RESPONSE_TIMEOUT_S
        deadline = time.monotonic() + max(1.0, timeout)
        extend_budget = config.RESPONSE_TIMEOUT_EXTEND_S

        state = EndState()
        limits = self.limits()
        reply_seen = False
        normalized = ""
        last_normalized = ""
        last_len = -1
        polls = 0
        rewritten = False
        emitted = ""
        #: 输入区里是否仍留着本轮提示词（``None`` = 未知）。只有确认「还留着」才能在
        #: 页面空闲时断定提交没生效，用于快路径报错，避免空等到绝对超时。
        prompt_in_composer: Optional[bool] = None

        def _log_end(reason: str, *, chars: int = 0) -> None:
            """记一条不含正文的收尾日志：以后「客户端一直等到超时」能直接从日志看出来。"""
            logger.info(
                "[生成] %s：轮询 %s 次，正文 %s 字符，曾经生成中=%s",
                reason,
                polls,
                chars,
                state.saw_generating,
            )

        while True:
            await asyncio.sleep(self.poll_interval)
            polls += 1
            if cancel is not None and cancel():
                raise GenerationStalledError("生成已取消", detail={"polls": polls})

            read = await self.extractor.read_new(page, baseline)
            if read.is_new:
                reply_seen = True
                normalized = normalize_reply(read.text)

            probe = await self.dom.generating_state(page)
            generating = probe.generating

            # 还没有任何新回复时才检查「提交到底有没有生效」：真实页面上提交成功会
            # 立刻清空输入区，所以「输入区里还留着本轮提示词」就是提交没生效的强证据。
            # 一旦有新回复（或输入区读不出来）就不再参与判定。
            if not reply_seen:
                contains = await self.dom.composer_contains(page, prompt)
                if contains is not None:
                    prompt_in_composer = contains
            submit_landed = None if prompt_in_composer is None else not prompt_in_composer

            # 只在正文开始出现后检查「到顶」提示，避免整页扫描开销。
            if reply_seen and polls % 4 == 0 and await self.dom.detect_context_limit(page):
                raise ContextLimitError(
                    "网页会话已达上下文长度上限，请重置该会话桶",
                    detail={"polls": polls},
                )

            past = time.monotonic() > deadline
            verdict = evaluate_poll(
                state,
                limits=limits,
                reply_seen=reply_seen,
                generating=generating,
                normalized=normalized,
                last_normalized=last_normalized,
                last_len=last_len,
                past_deadline=past,
                extend_budget=extend_budget,
                has_partial_text=bool(normalized),
                submit_landed=submit_landed,
            )
            state = verdict.state
            logger.debug(
                "[生成] 轮询 %s：reply_seen=%s 正文=%s 字符 生成中=%s(%s) 静默=%s 空转=%s",
                polls,
                reply_seen,
                len(normalized),
                generating,
                probe.reason,
                state.quiet_count,
                state.stalled,
            )

            # 增量：非工具模式实时推送；工具模式缓冲到结束再解析。
            if on_delta is not None and normalized and not buffered:
                if normalized.startswith(emitted):
                    emitted = normalized
                elif normalized != emitted:
                    rewritten = True
                await on_delta(normalized)

            last_normalized = normalized
            last_len = len(normalized)

            if verdict.action == "finish":
                break
            if verdict.action == "extend":
                deadline = time.monotonic() + verdict.extend_seconds
                extend_budget = max(0.0, extend_budget - verdict.extend_seconds)
                continue
            if verdict.action == "fail_stalled":
                if verdict.submit_lost:
                    _log_end("失败：提交未生效（提示词仍在输入区）")
                    raise PromptSubmissionError(
                        "提示词仍留在网页输入区：这次提交没有生效，页面也没有新增回复",
                        detail={"polls": polls},
                    )
                if not reply_seen:
                    _log_end("失败：未识别到本轮回复")
                    raise ReplyNotFoundError(
                        "提交后未识别到本轮回复（页面无新增 assistant 节点）",
                        detail={"polls": polls, "note": verdict.note},
                    )
                _log_end("失败：生成停滞", chars=len(normalized))
                raise GenerationStalledError(
                    "生成停滞：正文长时间无有效增长", detail={"polls": polls, "note": verdict.note}
                )
            if verdict.action == "fail_timeout":
                _log_end("失败：等待网页版回复超时", chars=len(normalized))
                raise GenerationTimeoutError(
                    "等待网页版回复超时", detail={"polls": polls, "reply_seen": reply_seen}
                )

        if not reply_seen:
            raise ReplyNotFoundError("未识别到本轮回复", detail={"polls": polls})
        _log_end(f"完成（{verdict.note or '正文稳定'}）", chars=len(normalized))

        # 最终快照可能比轮询中读到的更完整，这里再读一次。
        final = await self.extractor.read_new(page, baseline)
        raw = normalize_reply(final.text) or normalized

        result = GenerationResult(
            text=raw,
            raw_text=raw,
            finish_reason="stop",
            polls=polls,
            saw_generating=state.saw_generating,
            rewritten=rewritten,
        )

        if tool_list:
            parsed = parse_tool_calls(raw, allowed_names=[spec.name for spec in tool_list])
            result.parse = parsed
            if parsed.errors:
                result.notes.extend(parsed.errors)
            if parsed.calls:
                result.tool_calls = list(parsed.calls)
                result.finish_reason = "tool_calls"
                result.text = _strip_tool_fences(raw, config.TOOL_CALL_FENCE)
            elif parsed.saw_marker:
                # 出现了标记但无法解析：按普通文本返回，并保留诊断（绝不静默转成调用）。
                result.notes.append("tool_call_parse_error")
        return result


__all__ = ["GenerationResult", "ReplyGenerator", "DeltaCallback", "CancelCheck"]
