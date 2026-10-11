"""结束判定状态机（doc/design.md 4.4、11.1）。

判定阈值全部是**时长**（秒）：调用方用 :class:`IdleClock` 累加「页面静止了多久」，
状态机只做比较。下面的用例因此不再模拟轮询序列，而是直接给 ``idle_seconds``，
这也顺带证明了「轮询快慢不影响判定语义」。
"""

from __future__ import annotations

from claude_web.end_detection import EndLimits, EndState, IdleClock, evaluate_poll

LIMITS = EndLimits(
    settle_s=2.0,
    settle_no_signal_s=6.0,
    stall_s=30.0,
    submit_lost_s=6.0,
    extend_step_s=10.0,
)


def test_finishes_when_settled_and_quiet():
    state = EndState(saw_generating=True)
    # 页面已落定但静默不足 -> 继续等
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=False,
        idle_seconds=1.9, has_text=True,
    )
    assert v.action == "continue"
    # 静默满 settle_s -> 收尾
    v = evaluate_poll(
        v.state, limits=LIMITS, reply_seen=True, generating=False,
        idle_seconds=2.0, has_text=True,
    )
    assert v.action == "finish"
    assert "静默" in v.note


def test_settled_requires_text_and_reply_node():
    """还没读到任何正文时，即使静止很久也不能算「结束」。"""
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=False, generating=False, idle_seconds=3.0
    )
    assert v.action == "continue"
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=False,
        idle_seconds=3.0, has_text=False,
    )
    assert v.action == "continue"


def test_idle_clock_resets_on_generation_and_on_growth():
    clock = IdleClock()
    assert clock.update(10.0, generating=False, content_changed=False) == 0.0  # 首轮起步
    assert clock.update(11.0, generating=False, content_changed=False) == 1.0
    # 正文增长 -> 归零
    assert clock.update(12.0, generating=False, content_changed=True) == 0.0
    assert clock.update(13.0, generating=False, content_changed=False) == 1.0
    # 页面自述仍在生成（例如分段之间的思考）-> 归零
    assert clock.update(14.0, generating=True, content_changed=False) == 0.0
    assert clock.update(15.0, generating=True, content_changed=False) == 0.0
    assert clock.update(16.0, generating=False, content_changed=False) == 1.0


def test_no_signal_fallback_needs_longer_quiet():
    state = EndState()  # 从未观测到生成信号
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=None,
        idle_seconds=2.0, has_text=True,
    )
    assert v.action == "continue"
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=None,
        idle_seconds=LIMITS.settle_no_signal_s, has_text=True,
    )
    assert v.action == "finish"
    assert "未观测到生成信号" in v.note
    assert v.state.saw_generating is False


def test_stall_fails_after_stall_window_without_any_reply():
    state = EndState()
    v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=False, idle_seconds=29.9)
    assert v.action == "continue"
    v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=False, idle_seconds=30.0)
    assert v.action == "fail_stalled"
    assert v.submit_lost is False


def test_stall_still_counts_after_generating_was_seen():
    """回归：页面一旦出现过生成信号，停滞计时不能被闩锁关掉。

    旧实现在这里会一路 ``continue`` 到绝对超时：实测客户端等了 RESPONSE_TIMEOUT_S 秒
    （快照 goal=「命令超时，重新发送」、reply_chars=0）才拿到 ``generation_timeout``，
    而页面早就静止了。
    """
    state = EndState(saw_generating=True)
    v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=False, idle_seconds=10.0)
    assert v.action == "continue"
    v = evaluate_poll(
        v.state, limits=LIMITS, reply_seen=False, generating=False, idle_seconds=30.0
    )
    assert v.action == "fail_stalled"
    assert v.submit_lost is False


def test_stall_fires_when_reply_node_stays_empty():
    """新回复节点出现了但正文一直为空、页面也不在生成：同样按卡死收尾。"""
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=False,
        idle_seconds=30.0, has_text=False,
    )
    assert v.action == "fail_stalled"
    assert v.submit_lost is False


def test_lost_submission_reports_submit_lost_before_stall_window():
    """输入区里还留着本轮提示词 = 提交没生效：不等卡死窗口，按提交窗口失败。"""
    state = EndState()
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=False, generating=False,
        idle_seconds=5.9, submit_landed=False,
    )
    assert v.action == "continue"
    v = evaluate_poll(
        v.state, limits=LIMITS, reply_seen=False, generating=False,
        idle_seconds=6.0, submit_landed=False,
    )
    assert v.action == "fail_stalled"
    assert v.submit_lost is True
    assert "输入区" in v.note


def test_unknown_submission_state_keeps_normal_stall_window():
    """输入区读不出来时不能诬告「提交没生效」：仍然等完整的卡死窗口。"""
    state = EndState()
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=False, generating=False,
        idle_seconds=6.0, submit_landed=None,
    )
    assert v.action == "continue"
    assert v.submit_lost is False


def test_timeout_with_partial_text_finishes():
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=True, has_text=True,
        past_deadline=True, extend_budget=0.0,
    )
    assert v.action == "finish"
    assert "超时" in v.note


def test_timeout_extends_while_generating_with_budget():
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=True, has_text=False,
        past_deadline=True, extend_budget=30.0,
    )
    assert v.action == "extend"
    assert v.extend_seconds == 10.0


def test_timeout_fails_without_text_or_budget():
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=True, has_text=False,
        past_deadline=True, extend_budget=0.0,
    )
    assert v.action == "fail_timeout"
