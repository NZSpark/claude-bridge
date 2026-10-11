"""结束判定状态机（doc/design.md 4.4、11.1）。"""

from __future__ import annotations

from claude_web.end_detection import EndLimits, EndState, evaluate_poll

LIMITS = EndLimits(quiet_polls=2, stable_polls=2, stall_limit=3, extend_step_s=10.0)


def test_finishes_when_settled_and_quiet():
    state = EndState(saw_generating=True)
    # 第一轮：内容刚出现，generating=True -> continue
    v = evaluate_poll(state, limits=LIMITS, reply_seen=True, generating=True, normalized="Hi")
    assert v.action == "continue"
    state = v.state
    # 第二轮：内容不变但仍在生成 -> continue
    v = evaluate_poll(state, limits=LIMITS, reply_seen=True, generating=True,
                      normalized="Hi", last_normalized="Hi", last_len=2)
    assert v.action == "continue"
    state = v.state
    # 第三轮：生成结束，静默计数 1 < quiet_polls
    v = evaluate_poll(state, limits=LIMITS, reply_seen=True, generating=False,
                      normalized="Hi", last_normalized="Hi", last_len=2)
    assert v.action == "continue"
    # 第四轮：静默计数 2 >= quiet_polls -> finish
    v = evaluate_poll(v.state, limits=LIMITS, reply_seen=True, generating=False,
                      normalized="Hi", last_normalized="Hi", last_len=2)
    assert v.action == "finish"
    assert "静默" in v.note


def test_content_change_resets_quiet_counter():
    state = EndState(saw_generating=True, quiet_count=1)
    v = evaluate_poll(state, limits=LIMITS, reply_seen=True, generating=False,
                      normalized="Hi there", last_normalized="Hi", last_len=2)
    assert v.action == "continue"
    assert v.state.quiet_count == 0


def test_fallback_stable_when_no_generating_signal():
    state = EndState()
    # 第一轮：正文刚出现，尚不稳定。
    v = evaluate_poll(state, limits=LIMITS, reply_seen=True, generating=None, normalized="Answer")
    assert v.action == "continue"
    # 第二轮：内容与上轮相同，稳定计数 1 < max(stable_polls, quiet_polls)。
    v = evaluate_poll(v.state, limits=LIMITS, reply_seen=True, generating=None,
                      normalized="Answer", last_normalized="Answer", last_len=6)
    assert v.action == "continue"
    # 第三轮：稳定计数达标 -> finish（停止控件始终探测不到时的兜底）。
    v = evaluate_poll(v.state, limits=LIMITS, reply_seen=True, generating=None,
                      normalized="Answer", last_normalized="Answer", last_len=6)
    assert v.action == "finish"
    assert not v.state.saw_generating


def test_stall_fails_without_any_signal():
    state = EndState()
    for _ in range(2):
        v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=None)
        assert v.action == "continue"
        state = v.state
    v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=None)
    assert v.action == "fail_stalled"


def test_stall_still_counts_after_generating_was_seen():
    """回归：页面一旦出现过生成信号，无增长计时不能被闩锁关掉。

    旧实现在这里会一路 ``continue`` 到绝对超时：实测客户端等了 RESPONSE_TIMEOUT_S 秒
    （快照 goal=「命令超时，重新发送」、reply_chars=0）才拿到 ``generation_timeout``，
    而页面早就静止了。
    """
    state = EndState(saw_generating=True)
    for _ in range(LIMITS.stall_limit - 1):
        v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=False)
        assert v.action == "continue"
        state = v.state
    v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=False)
    assert v.action == "fail_stalled"
    assert v.submit_lost is False


def test_stall_fires_when_reply_node_stays_empty():
    """新回复节点出现了但正文一直为空、页面也不在生成：同样按卡死收尾。"""
    state = EndState(saw_generating=True)
    for _ in range(LIMITS.stall_limit - 1):
        v = evaluate_poll(
            state, limits=LIMITS, reply_seen=True, generating=False,
            normalized="", last_normalized="", last_len=0,
        )
        assert v.action == "continue"
        state = v.state
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=False,
        normalized="", last_normalized="", last_len=0,
    )
    assert v.action == "fail_stalled"
    assert v.submit_lost is False


def test_lost_submission_reports_submit_lost_before_stall_limit():
    """输入区里还留着本轮提示词 = 提交没生效：不等卡死窗口，按静默窗口就失败。"""
    state = EndState()
    for _ in range(LIMITS.quiet_polls - 1):
        v = evaluate_poll(
            state, limits=LIMITS, reply_seen=False, generating=False, submit_landed=False
        )
        assert v.action == "continue"
        state = v.state
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=False, generating=False, submit_landed=False
    )
    assert v.action == "fail_stalled"
    assert v.submit_lost is True
    assert "输入区" in v.note


def test_unknown_submission_state_keeps_normal_stall_window():
    """输入区读不出来时不能诬告「提交没生效」：仍然等完整的卡死窗口。"""
    state = EndState()
    for _ in range(LIMITS.quiet_polls):
        v = evaluate_poll(
            state, limits=LIMITS, reply_seen=False, generating=False, submit_landed=None
        )
        assert v.action == "continue"
        state = v.state
    assert v.submit_lost is False


def test_growing_text_resets_stall_counter():
    state = EndState(saw_generating=True, stalled=LIMITS.stall_limit - 1)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=False,
        normalized="abc", last_normalized="ab", last_len=2,
    )
    assert v.action == "continue"
    assert v.state.stalled == 0


def test_generating_keeps_stall_counter_at_zero():
    """页面当前确实在生成时不算空转（即使还没有正文）。"""
    state = EndState(saw_generating=True, stalled=LIMITS.stall_limit - 1)
    v = evaluate_poll(state, limits=LIMITS, reply_seen=False, generating=True)
    assert v.action == "continue"
    assert v.state.stalled == 0


def test_timeout_with_partial_text_finishes():
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=True,
        normalized="partial", past_deadline=True, extend_budget=0.0, has_partial_text=True,
    )
    assert v.action == "finish"
    assert "超时" in v.note


def test_timeout_extends_while_generating_with_budget():
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=True,
        normalized="x", past_deadline=True, extend_budget=30.0, has_partial_text=False,
    )
    assert v.action == "extend"
    assert v.extend_seconds == 10.0


def test_timeout_fails_without_text_or_budget():
    state = EndState(saw_generating=True)
    v = evaluate_poll(
        state, limits=LIMITS, reply_seen=True, generating=True,
        normalized="x", past_deadline=True, extend_budget=0.0, has_partial_text=False,
    )
    assert v.action == "fail_timeout"
