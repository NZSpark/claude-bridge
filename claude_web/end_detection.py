"""回复结束判定的纯函数状态机（doc/design.md 4.4）。

把生成等待循环里「是否仍在生成 / 是否落定 / 静默计数 / 卡死计数 / 超时延长」
抽成纯函数：

* 输入：本轮观测（``reply_seen`` / ``generating``）与轮询状态；
* 输出：:class:`EndVerdict`，``action`` 只能是
  ``continue`` / ``finish`` / ``extend`` / ``fail_stalled`` / ``fail_timeout``。

副作用（发增量、记日志、落盘、抛异常）全部留在调用方，便于穷举测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class EndState:
    """轮询循环的可持久状态（每轮 evaluate 后回写）。"""

    quiet_count: int = 0
    stable_count: int = 0
    stalled: int = 0
    saw_generating: bool = False


@dataclass(frozen=True)
class EndLimits:
    """判定阈值（来自 config，测试可直接构造）。"""

    quiet_polls: int
    stable_polls: int
    stall_limit: int
    extend_step_s: float


@dataclass(frozen=True)
class EndVerdict:
    """一轮判定结果；``state`` 是回写后的新对象。"""

    action: str
    state: EndState
    extend_seconds: float = 0.0
    note: str = ""
    #: ``fail_stalled`` 时：确定是「提交没生效」（页面空闲、输入区里还留着提示词），
    #: 而不是「提交成功但回复没渲染出来」。调用方据此给出准确的错误类别。
    submit_lost: bool = False

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
    normalized: str = "",
    last_normalized: str = "",
    last_len: int = -1,
    past_deadline: bool = False,
    extend_budget: float = 0.0,
    has_partial_text: bool = False,
    submit_landed: Optional[bool] = None,
) -> EndVerdict:
    """判定这一轮该继续、收尾、延长还是失败。

    判定顺序（改动会改变真实环境下的收尾时机）：

    1. **提交未生效**（快速失败）：``submit_landed is False``（页面空闲、输入区里还留着
       本轮提示词）且已空转 ``quiet_polls`` 轮 → ``fail_stalled`` + ``submit_lost``。
    2. **收尾**（仅在 ``reply_seen`` 时）：
       * 「页面落定」（``generating is False``、此前观测到过生成中、有正文）且内容
         静默满 ``quiet_polls`` 轮；
       * 或从未观测到生成信号时，内容稳定满 ``max(stable_polls, quiet_polls)`` 轮。
    3. **无增长**：连续 ``stall_limit`` 轮页面空闲（``generating`` 不为真）且正文没有
       增长 → ``fail_stalled``。
    4. **超时**：已到总超时 → 有正文就 ``finish``（返回已读到的内容，绝不重发）；
       页面仍在生成且仍有延长额度就 ``extend``；否则 ``fail_timeout``。

    ``stalled`` 只看**本轮观测**（当前 ``generating`` / 正文是否增长），不看
    ``saw_generating`` 闩锁：早先修复前它一旦被置位就再也不会计数，于是「提交没生效 /
    回复没渲染出来」的页面只能一路空等到绝对超时（实测客户端等到 300s 报
    ``generation_timeout``，而页面早就静止了 —— 见 doc/design.md 4.4）。
    """
    quiet = state.quiet_count
    stable = state.stable_count
    stalled = state.stalled
    saw_generating = state.saw_generating or bool(generating)
    # 页面是否**当前**仍在生成：只认本轮观测，不用闩锁值。
    idle = generating is not True
    # 本轮正文是否比上一轮更长/变了（``reply_seen`` 为假时不算「在长」）。
    content_changed = normalized != last_normalized or len(normalized) != last_len

    if reply_seen:
        if content_changed or generating is True:
            quiet = 0
        else:
            quiet += 1

        settled = generating is False and saw_generating and bool(normalized)
        if settled and quiet >= limits.quiet_polls:
            return EndVerdict(
                "finish",
                EndState(quiet, stable, stalled, saw_generating),
                note=f"页面已落定且内容静默 {quiet} 次",
            )

        # 停止控件始终探测不到时的兜底：纯文本稳定（多等静默窗口）。
        if not saw_generating and idle:
            same_text = bool(normalized) and normalized == last_normalized
            stable = stable + 1 if same_text else 0
            if stable >= max(limits.stable_polls, limits.quiet_polls):
                return EndVerdict(
                    "finish",
                    EndState(quiet, stable, stalled, saw_generating),
                    note=f"未观测到生成信号，内容稳定 {stable} 次",
                )

    # 无增长计数：只要正文没增长且页面当前不在生成，就算一轮空转。
    growing = reply_seen and content_changed
    if growing or not idle:
        stalled = 0
    else:
        stalled += 1
    state_after = EndState(quiet, stable, stalled, saw_generating)

    # 提交没生效的强证据：页面空闲、没有新增回复，而输入区里仍留着本轮提示词。
    if submit_landed is False and stalled >= max(1, limits.quiet_polls):
        return EndVerdict(
            "fail_stalled",
            state_after,
            note="提交未生效：提示词仍留在输入区且页面没有新增回复",
            submit_lost=True,
        )

    if stalled >= max(1, limits.stall_limit):
        return EndVerdict(
            "fail_stalled",
            state_after,
            note=f"页面连续 {stalled} 次空闲且正文无增长",
        )

    if past_deadline:
        if has_partial_text:
            return EndVerdict("finish", state_after, note="超时但已读到内容，直接返回，不重发")
        if extend_budget > 0 and generating:
            wait = min(extend_budget, max(1.0, limits.extend_step_s))
            return EndVerdict("extend", state_after, extend_seconds=wait)
        return EndVerdict("fail_timeout", state_after)

    return EndVerdict("continue", state_after)


__all__ = ["EndState", "EndLimits", "EndVerdict", "evaluate_poll"]
