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
