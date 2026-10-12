"""提交并协调回复生成（doc/design.md 3.5、4.4、6）。

流程：

1. 确认页面可用（未登录 / 挑战 / 无输入区都以语义化异常结束）；
2. 记录提交前的回复状态（baseline：assistant 条数 + 最后一条提问的指纹）；
3. 填充输入区（失败可安全重试——此时**尚无副作用**）并提交；
4. 轮询回复快照，用 :func:`claude_web.end_detection.evaluate_poll` 判定结束；
5. 需要工具时**缓冲整段回复**再解析，避免把内部工具标记泄漏到普通文本里。

时延与轮询策略（doc/design.md 4.4）：结束 / 停滞阈值全部是**时长**，由
:class:`~claude_web.end_detection.IdleClock` 累加的「页面静止时长」驱动；轮询间隔
只影响「多快看到变化」：页面活跃时用 ``POLL_INTERVAL_ACTIVE_S``，页面静止后回到
``POLL_INTERVAL_S``，并且只剩收尾等待时只睡到静默阈值那一刻。「会话到顶」的整页扫描
只在**即将判定失败**时做一次（命中就用 ``context_limit_reached`` 代替泛化的失败）。

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
from ..end_detection import EndLimits, EndState, IdleClock, evaluate_poll
from ..errors import (
    AuthenticationRequiredError,
    ChallengeRequiredError,
    ComposerNotFoundError,
    ContextLimitError,
    GenerationCancelledError,
    GenerationStalledError,
    GenerationTimeoutError,
    PromptSubmissionError,
    ReplyNotFoundError,
    UsageLimitError,
)
from ..models import ToolCall, ToolSpec
from ..toolcalls import ParsedToolCalls, parse_tool_calls, strip_tool_call_blocks
from .extractor import ReplyBaseline, ReplyExtractor, ReplyRead, normalize_reply

logger = logging.getLogger(__name__)

#: 内容增量回调：接收**当前完整**的归一化回复文本（由调用方决定如何编码）。
DeltaCallback = Callable[[str], Awaitable[None]]
#: 取消探测：返回 True 表示调用方要求中止本轮。
CancelCheck = Callable[[], bool]

#: 单次睡眠的下限（秒），避免退化成忙轮询。
_MIN_SLEEP_S = 0.05


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
        """页面静止时的轮询间隔。"""
        return self._poll_interval if self._poll_interval is not None else config.POLL_INTERVAL_S

    @property
    def active_poll_interval(self) -> float:
        """页面活跃（在生成 / 正文刚增长）时的轮询间隔；不长于静止间隔。"""
        return min(config.POLL_INTERVAL_ACTIVE_S, self.poll_interval)

    def limits(self) -> EndLimits:
        return EndLimits(
            settle_s=config.SETTLE_S,
            settle_no_signal_s=config.SETTLE_NO_SIGNAL_S,
            stall_s=config.STALL_S,
            submit_lost_s=config.SUBMIT_LOST_S,
            extend_step_s=max(1.0, config.RESPONSE_TIMEOUT_S),
        )

    def _next_interval(
        self,
        *,
        generating: Optional[bool],
        content_changed: bool,
        reply_seen: bool,
        saw_generating: bool,
        idle_seconds: float,
        limits: EndLimits,
    ) -> float:
        """下一轮轮询间隔。

        * 页面活跃 → 短间隔（更快把增量交给客户端）；
        * 页面静止且已读到正文 → 只睡到「静默阈值」那一刻（收尾不再被轮询粒度拖长）；
        * 其余（等回复出现 / 停滞排查）→ 基础间隔，保持与旧版相同的页面压力。
        """
        base = self.poll_interval
        if generating is True or content_changed:
            return self.active_poll_interval
        if reply_seen:
            deadline = limits.settle_s if saw_generating else limits.settle_no_signal_s
            remaining = deadline - idle_seconds
            if remaining > 0:
                return min(base, max(_MIN_SLEEP_S, remaining + _MIN_SLEEP_S))
        return base

    #: 不可重试的写入失败：重试要么必然再次失败，要么会掩盖真实原因（T3.2）。
    #:
    #: * ``ComposerNotFoundError``：输入区**根本不存在**（未登录 / 页面结构变了 /
    #:   正被其它弹层盖住）——再点几次也不会出现，快速失败并把原因如实上报；
    #: * ``AuthenticationRequiredError`` / ``ChallengeRequiredError``：登录态失效或
    #:   人机挑战，重试无效；
    #: * ``PromptSubmissionError``：``fill_composer`` 内部已经跑满 ``FILL_RETRIES``
    #:   并退化过键盘路径，外层再套一层只会把「失败一次」放大成 N 次无意义写入。
    _UNRECOVERABLE_FILL_ERRORS = (
        ComposerNotFoundError,
        AuthenticationRequiredError,
        ChallengeRequiredError,
        PromptSubmissionError,
    )

    async def _fill(self, page, prompt: str) -> None:
        """填充输入框；仅在此阶段允许重试（无副作用）。

        重试边界（T3.2）：``fill_composer`` 自己已经带 ``FILL_RETRIES`` 次重试 +
        键盘退化兜底，因此这里**只**对「瞬时、可恢复」的写入失败再补一层外层重试
        （例如页面正在重排导致的一次性 DOM 抖动）；命中 :attr:`_UNRECOVERABLE_FILL_ERRORS`
        则立即冒泡，不做叠加重试，也不吞掉 ``CancelledError``（T2.5）。
        """
        attempts = max(1, config.MAX_UPSTREAM_RETRIES + 1)
        last: Optional[BaseException] = None
        for attempt in range(attempts):
            try:
                await self.dom.fill_composer(page, prompt)
                return
            except self._UNRECOVERABLE_FILL_ERRORS:
                # 不可恢复：不重试、不包装，直接把语义化错误交给上层。
                raise
            except Exception as exc:  # noqa: BLE001
                # 只捕普通异常：让 CancelledError 正常冒泡，不吞掉协程取消（T2.5）。
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
        # 额度用尽 / 升级弹窗会盖住输入区并抢走焦点：提示词写得进去，Enter 却不生效，
        # 于是先报「提交未生效」，再报「生成停滞」，而真正的原因是额度没了。
        # 提交前先把它关掉（只点关闭类控件，见 doc/design.md 4.3）。
        modal = await self.dom.dismiss_limit_modal(page)
        # 提交前就看到了额度/升级弹窗：失败时据此把原因归到「额度用尽」而不是「停滞」。
        # 弹窗很可能已经被关掉了，所以把命中项也留给失败详情（探测不到时它就是唯一线索）。
        limit_modal_seen = modal.quota_hit
        limit_modal_pattern = modal.pattern if limit_modal_seen else None
        if limit_modal_seen:
            logger.info(
                "[生成] 页面存在额度/升级弹窗（%s），已尝试关闭（dismissed=%s）",
                modal.pattern,
                modal.dismissed,
            )
        baseline: ReplyBaseline = await self.extractor.baseline(page)

        # 上一轮取消 / 失败后页面可能仍在生成：先等一小段，必要时点停止控件，
        # 否则新提示词的 Enter 可能被站点忽略，被误报成「提交未生效」（T2.7）。
        await self.dom.settle_before_submit(page, config.PRE_SUBMIT_WAIT_S)

        await self._fill(page, prompt)
        await self.dom.submit(page)

        timeout = timeout_s if timeout_s is not None else config.RESPONSE_TIMEOUT_S
        deadline = time.monotonic() + max(1.0, timeout)
        extend_budget = config.RESPONSE_TIMEOUT_EXTEND_S

        state = EndState()
        limits = self.limits()
        clock = IdleClock()
        reply_seen = False
        normalized = ""
        last_normalized = ""
        last_len = -1
        polls = 0
        rewritten = False
        emitted = ""
        #: 最近一次回复读取结果（失败时把条数写进日志 / 错误详情，便于下次定位）。
        last_read: Optional[ReplyRead] = None
        #: 「结构信号认出回复」是否已记过一条日志（正常轮次不会触发）。
        structural_logged = False
        #: 下一轮轮询间隔：活跃期用短间隔，静止期用基础间隔（见 _next_interval）。
        interval = self.active_poll_interval
        #: 「会话到顶」扫描是否已做过（每轮任务最多一次，且在失败判定点才做）。
        context_checked = False
        #: 输入区里是否仍留着本轮提示词（``None`` = 未知）。只有确认「还留着」才能在
        #: 页面静止时断定提交没生效，用于快路径报错，避免空等到绝对超时。
        prompt_in_composer: Optional[bool] = None

        def _counts_detail() -> dict:
            """把「条数口径」的现场写进日志 / 错误详情（只有计数，不含页面正文）。"""
            read_count = last_read.count if last_read is not None else None
            read_users = last_read.user_count if last_read is not None else None
            trailing = last_read.trailing if last_read is not None else None
            return {
                "reply_count": read_count,
                "reply_baseline": baseline.count,
                "user_count": read_users,
                "user_baseline": baseline.user_count,
                # 「页面上最后一条回复是不是排在本轮提问之后」：区分「页面根本没渲染回复」
                # 与「渲染了但条数口径没认出来」（后者已被结构信号救回，见 4.4）。
                "trailing_reply": trailing,
            }

        def _log_end(reason: str, *, chars: int = 0) -> None:
            """记一条不含正文的收尾日志：以后「客户端一直等到超时」能直接从日志看出来。"""
            logger.info(
                "[生成] %s：轮询 %s 次，正文 %s 字符，曾经生成中=%s，assistant %s/基线 %s，"
                "提问 %s/基线 %s，回复在提问之后=%s",
                reason,
                polls,
                chars,
                state.saw_generating,
                last_read.count if last_read is not None else "-",
                baseline.count,
                last_read.user_count if last_read is not None else "-",
                baseline.user_count,
                last_read.trailing if last_read is not None else "-",
            )

        while True:
            await asyncio.sleep(interval)
            polls += 1
            now = time.monotonic()
            if cancel is not None and cancel():
                # 取消不是停滞：用独立错误类，调用方据此不计入超时 / 失败统计（T2.5）。
                raise GenerationCancelledError("生成已取消", detail={"polls": polls})

            read = await self.extractor.read_new(page, baseline)
            last_read = read
            if read.is_new:
                reply_seen = True
                normalized = normalize_reply(read.text)
                if read.structural and not structural_logged:
                    # 正常轮次不会走到这里：一旦出现，说明**条数口径当时失灵**，
                    # 而结构信号把这一轮救了回来（下次查日志能直接看到）。
                    structural_logged = True
                    logger.info(
                        "[生成] 本轮回复是靠结构信号认出来的：assistant 条数 %s（基线 %s），提问 %s→%s",
                        read.count,
                        baseline.count,
                        baseline.user_count,
                        read.user_count,
                    )
            content_changed = reply_seen and (
                normalized != last_normalized or len(normalized) != last_len
            )

            probe = await self.dom.generating_state(page)
            generating = probe.generating
            idle_seconds = clock.update(
                now, generating=generating, content_changed=content_changed
            )

            # 还没有任何新回复时才检查「提交到底有没有生效」：真实页面上提交成功会
            # 立刻清空输入区，所以「输入区里还留着本轮提示词」就是提交没生效的强证据。
            # 一旦有新回复（或输入区读不出来）就不再参与判定。
            if not reply_seen:
                contains = await self.dom.composer_contains(page, prompt)
                if contains is not None:
                    prompt_in_composer = contains
            submit_landed = None if prompt_in_composer is None else not prompt_in_composer

            past = time.monotonic() > deadline
            verdict = evaluate_poll(
                state,
                limits=limits,
                reply_seen=reply_seen,
                generating=generating,
                idle_seconds=idle_seconds,
                has_text=bool(normalized),
                past_deadline=past,
                extend_budget=extend_budget,
                submit_landed=submit_landed,
            )
            state = verdict.state
            logger.debug(
                "[生成] 轮询 %s：reply_seen=%s 正文=%s 字符 生成中=%s(%s) 静止=%.1fs",
                polls,
                reply_seen,
                len(normalized),
                generating,
                probe.reason,
                idle_seconds,
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
                interval = self.active_poll_interval
                continue

            interval = self._next_interval(
                generating=generating,
                content_changed=content_changed,
                reply_seen=reply_seen,
                saw_generating=state.saw_generating,
                idle_seconds=idle_seconds,
                limits=limits,
            )

            if verdict.action in ("fail_stalled", "fail_timeout") and not context_checked:
                # 失败归因顺序：额度用尽 / 升级弹窗 > 会话到顶。前者更具体，也更常见
                # （额度没了时页面同样不会再有新内容）。探测只在**即将判定失败**时做一次。
                limit_modal = await self.dom.dismiss_limit_modal(page)
                # 归因收窄（T3.3）：只有在「现在仍能看到弹窗」或「本轮根本没读到新回复」
                # 时才归因额度用尽。若已经读到了正文（说明本轮确实在生成，只是后来停滞 /
                # 超时），不要用提交前那一次弹窗命中把停滞误报成额度问题。
                quota_now = limit_modal.quota_hit
                # 「本轮有实质正文」= 页面确实在生成；此时已有的正文是更可信的现场。
                has_text_now = bool(normalized)
                if quota_now or (limit_modal_seen and not has_text_now):
                    _log_end("失败：网页额度已用尽（页面弹出升级/额度弹窗）", chars=len(normalized))
                    raise UsageLimitError(
                        "网页版额度已用尽：页面弹出了升级/额度提示，本轮没有生成"
                        "（重试无效，请等额度重置或升级后再试）",
                        detail={
                            "polls": polls,
                            "seen_at_submit": limit_modal_seen,
                            **_counts_detail(),
                            **limit_modal.as_detail(),
                            # 关闭后的复探拿不到命中项：回退到提交前那一次的结果。
                            "pattern": limit_modal.pattern or limit_modal_pattern,
                        },
                    )
                # 「会话到顶」的整页扫描只在**即将判定失败**时做一次（doc/design.md 4.4）：
                # 该扫描要读整页可见文本（强制布局），正常轮次一次都不扫；更关键的是，
                # 把它吊在「每次请求都会命中一次」的位置上会带来假阳性——页面别处（会话
                # 正文 / 输入区）出现一句特征文案就能把一次**成功的**请求变成错误，并把桶
                # 标成到顶（下一轮重新播种）。真正到顶时页面不会给出「已落定 + 正文完整」
                # 的信号，而是不再产生新回复，因此一定会在失败判定点被拦到。
                context_checked = True
                if await self.dom.detect_context_limit(page):
                    _log_end("失败：网页会话已达上下文长度上限", chars=len(normalized))
                    raise ContextLimitError(
                        "网页会话已达上下文长度上限，请重置该会话桶",
                        detail={"polls": polls},
                    )

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
                        detail={"polls": polls, "note": verdict.note, **_counts_detail()},
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
            raise ReplyNotFoundError(
                "未识别到本轮回复", detail={"polls": polls, **_counts_detail()}
            )
        _log_end(f"完成（{verdict.note or '正文稳定'}）", chars=len(normalized))

        # 最终快照可能比轮询中读到的更完整，这里再读一次。
        final = await self.extractor.read_new(page, baseline)
        raw = normalize_reply(final.text) or normalized

        result = GenerationResult(
            text=raw,
            raw_text=raw,
            # 硬超时截断（页面仍在生成、延长额度已耗尽）：用兼容的截断语义，
            # 让客户端能区分「完整回复」与「被截断的部分内容」。
            finish_reason="length" if verdict.truncated else "stop",
            polls=polls,
            saw_generating=state.saw_generating,
            rewritten=rewritten,
        )
        if verdict.truncated:
            result.notes.append("回复被硬超时截断：页面仍在生成，延长额度已耗尽")

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
