"""回复结束判定的纯函数状态机（doc/design.md 4.4）。

把生成等待循环里「是否仍在生成 / 是否落定 / 静止了多久 / 是否卡住 / 超时延长」
抽成纯函数：

* 输入：本轮观测（``reply_seen`` / ``generating`` / ``idle_seconds``）与轮询状态；
* 输出：:class:`EndVerdict`，``action`` 只能是
  ``continue`` / ``finish`` / ``extend`` / ``fail_stalled`` / ``fail_timeout``。

**阈值全部是「时长」而不是「轮询次数」**（2026-10 重构）。原因是轮询次数把
「等多重才算结束」和「多久轮询一次」耦合在一起：为了降低尾部等待（页面早已静止、
客户端还在等下一次轮询）而缩短轮询间隔，会同时把停滞窗口等比缩短。改成时长之后，
轮询间隔只影响「多快看到变化」，不再影响判定语义。

副作用（发增量、记日志、落盘、抛异常）全部留在调用方，便于穷举测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class EndState:
    """轮询循环的可持久状态（每轮 evaluate 后回写）。"""

    #: 本轮之前是否观测到过「页面正在生成」（用于区分「落定」与「从未见到生成信号」）。
    saw_generating: bool = False


@dataclass
class IdleClock:
    """页面「静止时长」累加器。

    「静止」= 页面没有在生成，正文也没有增长。任何一种活动都会把计时归零：

    * ``generating is True``：页面自述仍在生成（可能在思考、也可能在下一段输出之间）；
    * ``content_changed``：读到的正文与上一轮不同。

    返回的是**本轮静止了多久**（秒），由调用方用单调时钟提供 ``now``，
    因此这个类可以在测试里直接用假时间驱动。
    """

    since: Optional[float] = None

    def update(
        self, now: float, *, generating: Optional[bool], content_changed: bool
    ) -> float:
        if self.since is None or generating is True or content_changed:
            self.since = now
            return 0.0
        return max(0.0, now - self.since)


@dataclass(frozen=True)
class EndLimits:
    """判定阈值（时长，秒；来自 config，测试可直接构造）。"""

    #: 页面自述已结束（``data-is-streaming=false`` / 无停止控件）时，内容静默多久算完成。
    settle_s: float
    #: 完全探不到生成信号时的兜底静默时长（比 ``settle_s`` 更保守）。
    settle_no_signal_s: float
    #: 静止多久判定停滞（快速失败，不再干等到绝对超时）。
    stall_s: float
    #: 静止多久、且输入区里仍留着本轮提示词 → 判定「提交没生效」。
    submit_lost_s: float
    #: 到达绝对超时但页面仍在生成时的单次延长额度（秒）。
    extend_step_s: float


@dataclass(frozen=True)
class EndVerdict:
    """一轮判定结果；``state`` 是回写后的新对象。"""

    action: str
    state: EndState
    extend_seconds: float = 0.0
    note: str = ""
    #: ``fail_stalled`` 时：确定是「提交没生效」（页面静止、输入区里还留着提示词），
    #: 而不是「提交成功但回复没渲染出来」。调用方据此给出准确的错误类别。
    submit_lost: bool = False
    #: ``finish`` 时：回复是被**硬超时截断**的（页面仍在生成、延长额度已耗尽），
    #: 调用方应据此改用截断语义（``finish_reason=length``）并在 notes 里说明。
    truncated: bool = False

    @property
    def finished(self) -> bool:
        return self.action == "finish"

    @property
    def failed(self) -> bool:
        return self.action in ("fail_stalled", "fail_timeout")


def evaluate_poll(
    state: EndState,
    *,
    limits: EndLimits,
    reply_seen: bool,
    generating: Optional[bool],
    idle_seconds: float = 0.0,
    has_text: bool = False,
    past_deadline: bool = False,
    extend_budget: float = 0.0,
    submit_landed: Optional[bool] = None,
) -> EndVerdict:
    """判定这一轮该继续、收尾、延长还是失败。

    判定顺序（改动会改变真实环境下的收尾时机）：

    1. **收尾**（仅在 ``reply_seen`` 时）：
       * 「页面落定」（``generating is False``、此前观测到过生成中、有正文）且静止满
         ``settle_s``；
       * 或从未观测到生成信号时，静止满更保守的 ``settle_no_signal_s``。
    2. **提交未生效**（快速失败）：``submit_landed is False``（页面静止、输入区里还留着
       本轮提示词）且静止满 ``submit_lost_s`` → ``fail_stalled`` + ``submit_lost``。
    3. **停滞**：静止满 ``stall_s`` → ``fail_stalled``。
    4. **超时**：已到总超时 → 有正文就 ``finish``（返回已读到的内容，绝不重发）；
       页面仍在生成且仍有延长额度就 ``extend``；否则 ``fail_timeout``。

    ``idle_seconds`` 由调用方用 :class:`IdleClock` 累加（页面生成中 / 正文在增长即归零），
    因此「页面早已静止、客户端却一路空等到绝对超时」不会再发生，且轮询快慢不影响判定。
    """
    saw_generating = state.saw_generating or bool(generating)
    idle = generating is not True
    state_after = EndState(saw_generating=saw_generating)

    if reply_seen:
        settled = generating is False and saw_generating and has_text
        if settled and idle_seconds >= limits.settle_s:
            return EndVerdict(
                "finish",
                state_after,
                note=f"页面已落定且内容静默 {idle_seconds:.1f}s",
            )
        # 停止控件与消息级属性都探不到时的兜底：纯文本静止更久才算结束。
        if not saw_generating and idle and has_text and idle_seconds >= limits.settle_no_signal_s:
            return EndVerdict(
                "finish",
                state_after,
                note=f"未观测到生成信号，内容静默 {idle_seconds:.1f}s",
            )

    # 提交没生效的强证据：页面静止、没有新增回复，而输入区里仍留着本轮提示词。
    if submit_landed is False and idle_seconds >= limits.submit_lost_s:
        return EndVerdict(
            "fail_stalled",
            state_after,
            note=f"提交未生效：提示词仍留在输入区、页面已静止 {idle_seconds:.1f}s",
            submit_lost=True,
        )

    if idle_seconds >= limits.stall_s:
        return EndVerdict(
            "fail_stalled",
            state_after,
            note=f"页面连续静止 {idle_seconds:.1f}s 且正文无增长",
        )

    if past_deadline:
        # 优先级（doc/design.md 4.4，T1.6）：页面仍在生成且还有延长额度时**先延长**，
        # 而不是因为「已有正文」就把仍在继续生成的回复当完整回复返回（那会截断长回复）。
        if generating and extend_budget > 0:
            wait = min(extend_budget, max(1.0, limits.extend_step_s))
            return EndVerdict("extend", state_after, extend_seconds=wait)
        if has_text:
            # 到硬上限：页面可能仍在生成，已有正文只能作为**截断**结果返回（绝不重发）。
            truncated = generating is True
            note = (
                "超时（页面仍在生成，延长额度已耗尽），返回已读到的部分内容，标记为截断"
                if truncated
                else "超时但已读到内容，直接返回，不重发"
            )
            return EndVerdict("finish", state_after, note=note, truncated=truncated)
        return EndVerdict("fail_timeout", state_after)

    return EndVerdict("continue", state_after)


__all__ = ["EndState", "EndLimits", "EndVerdict", "IdleClock", "evaluate_poll"]
