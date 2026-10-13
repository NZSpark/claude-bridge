"""Token 速度计算（doc/tokenspeed.md）。

本文件覆盖 :mod:`claude_web.completion.token_speed` 的纯函数：给定规模与时长，
断言速度公式、修正项与端到端填充。全部用固定数值，不依赖真实耗时。
"""

from __future__ import annotations

from claude_web.completion.token_speed import TokenSpeed, apply_e2e, compute_speed

# ==================== compute_speed ====================


def test_basic_prefill_and_decode_rates():
    """无修正时：prefill = prompt_tokens / ttft，decode = reply_tokens / decode。"""
    speed = compute_speed(
        prompt_chars=3000,
        reply_chars=300,
        prompt_tokens=1000,
        reply_tokens=100,
        ttft_s=2.0,
        decode_s=10.0,
    )
    assert speed.prefill_tps == 1000 / 2.0
    assert speed.decode_tps == 100 / 10.0
    assert speed.total_s == 12.0
    # 无修正时，修正量与原始量一致。
    assert speed.ttft_adj_s == 2.0
    assert speed.decode_adj_s == 10.0


def test_settle_credit_is_subtracted_from_decode_only():
    """落定等待只从 decode 扣，不影响 prefill。"""
    speed = compute_speed(
        prompt_chars=0,
        reply_chars=0,
        prompt_tokens=1000,
        reply_tokens=100,
        ttft_s=2.0,
        decode_s=10.0,
        settle_credit_s=2.5,
        saw_generating=True,
    )
    assert speed.decode_adj_s == 7.5
    assert speed.ttft_adj_s == 2.0
    assert speed.decode_tps == 100 / 7.5
    assert speed.settle_credit_s == 2.5


def test_poll_credit_is_subtracted_from_ttft_only():
    """半轮询周期只从 prefill 扣，不影响 decode。"""
    speed = compute_speed(
        prompt_chars=0,
        reply_chars=0,
        prompt_tokens=1000,
        reply_tokens=100,
        ttft_s=2.0,
        decode_s=10.0,
        poll_credit_s=0.2,
    )
    assert speed.ttft_adj_s == 1.8
    assert speed.prefill_tps == 1000 / 1.8
    assert speed.decode_adj_s == 10.0
    assert speed.poll_credit_s == 0.2


def test_zero_tokens_gives_zero_rate():
    """没有正文时速度是 0，而不是除零炸掉。"""
    speed = compute_speed(
        prompt_chars=0,
        reply_chars=0,
        prompt_tokens=0,
        reply_tokens=0,
        ttft_s=1.0,
        decode_s=1.0,
    )
    assert speed.prefill_tps == 0.0
    assert speed.decode_tps == 0.0


def test_zero_duration_is_guarded():
    """时长为 0 时分母有下限保护，不产生 inf。"""
    speed = compute_speed(
        prompt_chars=0,
        reply_chars=0,
        prompt_tokens=100,
        reply_tokens=100,
        ttft_s=0.0,
        decode_s=0.0,
    )
    assert speed.prefill_tps > 0
    assert speed.decode_tps > 0
    assert speed.prefill_tps != float("inf")


def test_credit_larger_than_duration_clamps_to_zero():
    """扣减量超过时长时，修正后时长夹到 0（不出现负数）。"""
    speed = compute_speed(
        prompt_chars=0,
        reply_chars=0,
        prompt_tokens=100,
        reply_tokens=100,
        ttft_s=0.1,
        decode_s=0.1,
        settle_credit_s=5.0,
        poll_credit_s=5.0,
    )
    assert speed.ttft_adj_s == 0.0
    assert speed.decode_adj_s == 0.0


def test_negative_inputs_are_clamped():
    """负数输入被夹到 0，不出现负速度。"""
    speed = compute_speed(
        prompt_chars=-10,
        reply_chars=-10,
        prompt_tokens=-10,
        reply_tokens=-10,
        ttft_s=-1.0,
        decode_s=-1.0,
    )
    assert speed.prompt_tokens == 0
    assert speed.reply_tokens == 0
    assert speed.ttft_s == 0.0
    assert speed.decode_s == 0.0
    assert speed.prefill_tps == 0.0


def test_as_detail_has_no_body_fields():
    """诊断字典只含数值与布尔，绝不含正文。"""
    speed = compute_speed(
        prompt_chars=300,
        reply_chars=30,
        prompt_tokens=100,
        reply_tokens=10,
        ttft_s=1.0,
        decode_s=1.0,
    )
    detail = speed.as_detail()
    assert "prompt_chars" not in detail and "reply_chars" not in detail
    assert detail["prompt_tokens"] == 100
    assert detail["reply_tokens"] == 10
    assert detail["streamed"] is False


# ==================== apply_e2e ====================


def test_apply_e2e_fills_and_marks_streamed():
    speed = compute_speed(
        prompt_chars=0,
        reply_chars=0,
        prompt_tokens=100,
        reply_tokens=100,
        ttft_s=1.0,
        decode_s=1.0,
    )
    apply_e2e(speed, ttft_s=2.0, decode_s=4.0)
    assert speed.streamed is True
    assert speed.e2e_ttft_s == 2.0
    assert speed.e2e_decode_tps == 100 / 4.0


def test_apply_e2e_ignores_none_speed():
    """没有 speed 对象（未读到正文）时安全跳过。"""
    apply_e2e(None, ttft_s=1.0, decode_s=1.0)  # 不应抛异常


def test_apply_e2e_with_zero_reply_tokens():
    speed = TokenSpeed(reply_tokens=0)
    apply_e2e(speed, ttft_s=1.0, decode_s=1.0)
    assert speed.e2e_decode_tps == 0.0
    assert speed.streamed is True
