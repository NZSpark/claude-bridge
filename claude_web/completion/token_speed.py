"""Token 速度统计（设计见 ``doc/tokenspeed.md``）。

本模块只做**纯计算**：给定字符数、token 估算与各段时长，算出 prefill / decode 速度
与端到端口径的速度。所有时长由调用方（``ReplyGenerator.generate``）用
``time.perf_counter()`` 采集后传入，因此本模块可用虚拟时长穷举测试，不依赖真实耗时。

口径说明（务必与 ``doc/tokenspeed.md`` 第 4、8 节一致）：

* token 数是 ``estimate_tokens``（≈ 字符数 / 3）的**估算**，不是精确计数；
* ``prefill_tps`` 用「输入 token / 首字延迟」，与直接 API 的 ``prompt_tokens/TTFT``
  同构，但首字延迟含网页合成 + 轮询粒度，**偏大**；
* ``decode_tps`` 用「输出 token / 首字到落定」，落定等待 ``SETTLE_S`` 已扣除，
  但首字偏晚的误差仍在，**偏小**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: 速度计算里所有分母的下限（秒），避免除零与荒谬的大数。
_MIN_SECONDS = 1e-3


def _rate(tokens: int, seconds: float) -> float:
    """tokens / seconds，分母下限保护；tokens 或时长非正时返回 0。"""
    if tokens <= 0:
        return 0.0
    seconds = max(float(seconds), _MIN_SECONDS)
    return tokens / seconds


@dataclass
class TokenSpeed:
    """一次生成的速度观测。

    所有 ``*_s`` 都是**秒**；``*_tps`` 是 tokens/s。修正后的量（``*_adj_s``）只在
    确实能归因时才与原始量不同（见 :func:`compute_speed`）。
    """

    # ---- 输入规模 ----
    prompt_chars: int = 0
    reply_chars: int = 0
    prompt_tokens: int = 0
    reply_tokens: int = 0

    # ---- 原始时长（桥的观测口径）----
    ttft_s: float = 0.0          # t5 - t3：提交到首个非空回复文本
    decode_s: float = 0.0        # t6 - t5：首字到落定
    total_s: float = 0.0         # t6 - t3：prefill + decode

    # ---- 修正后时长 ----
    ttft_adj_s: float = 0.0
    decode_adj_s: float = 0.0

    # ---- 速度 ----
    prefill_tps: float = 0.0
    decode_tps: float = 0.0

    # ---- 端到端（仅流式路径可填）----
    e2e_ttft_s: Optional[float] = None
    e2e_decode_tps: Optional[float] = None

    # ---- 修正归因（可审计）----
    settle_credit_s: float = 0.0
    poll_credit_s: float = 0.0
    saw_generating: bool = False
    streamed: bool = False
    truncated: bool = False

    def as_detail(self) -> dict:
        """诊断 / 命令输出用的扁平字典（只含数值与布尔，不含正文）。"""
        return {
            "prompt_tokens": self.prompt_tokens,
            "reply_tokens": self.reply_tokens,
            "ttft_s": round(self.ttft_s, 3),
            "decode_s": round(self.decode_s, 3),
            "ttft_adj_s": round(self.ttft_adj_s, 3),
            "decode_adj_s": round(self.decode_adj_s, 3),
            "prefill_tps": round(self.prefill_tps, 1),
            "decode_tps": round(self.decode_tps, 1),
            "e2e_ttft_s": None if self.e2e_ttft_s is None else round(self.e2e_ttft_s, 3),
            "e2e_decode_tps": (
                None if self.e2e_decode_tps is None else round(self.e2e_decode_tps, 1)
            ),
            "settle_credit_s": round(self.settle_credit_s, 3),
            "poll_credit_s": round(self.poll_credit_s, 3),
            "saw_generating": self.saw_generating,
            "streamed": self.streamed,
            "truncated": self.truncated,
        }


def compute_speed(
    *,
    prompt_chars: int,
    reply_chars: int,
    prompt_tokens: int,
    reply_tokens: int,
    ttft_s: float,
    decode_s: float,
    settle_credit_s: float = 0.0,
    poll_credit_s: float = 0.0,
    saw_generating: bool = False,
    truncated: bool = False,
) -> TokenSpeed:
    """把观测值组装成 :class:`TokenSpeed` 并算出速度（纯函数，便于穷举测试）。

    修正规则（**只减能明确归因的部分**）：

    * ``decode_s`` 扣掉落定等待 ``settle_credit_s``：落定要求内容静默 ``SETTLE_S``
      秒，那段不属于解码；
    * ``ttft_s`` 扣掉 ``poll_credit_s``：首字出现后最多要等一个活跃轮询周期才被读到，
      取半周期作为期望值。

    扣减后的时长有下限保护；扣减量原样写回 ``*_credit_s`` 以便审计。
    """
    speed = TokenSpeed(
        prompt_chars=max(0, int(prompt_chars)),
        reply_chars=max(0, int(reply_chars)),
        prompt_tokens=max(0, int(prompt_tokens)),
        reply_tokens=max(0, int(reply_tokens)),
        ttft_s=max(0.0, float(ttft_s)),
        decode_s=max(0.0, float(decode_s)),
        saw_generating=bool(saw_generating),
        truncated=bool(truncated),
    )
    speed.total_s = speed.ttft_s + speed.decode_s

    settle = max(0.0, float(settle_credit_s))
    poll = max(0.0, float(poll_credit_s))
    speed.settle_credit_s = settle
    speed.poll_credit_s = poll

    speed.ttft_adj_s = max(0.0, speed.ttft_s - poll)
    speed.decode_adj_s = max(0.0, speed.decode_s - settle)

    speed.prefill_tps = _rate(speed.prompt_tokens, speed.ttft_adj_s)
    speed.decode_tps = _rate(speed.reply_tokens, speed.decode_adj_s)
    return speed


def apply_e2e(speed: Optional[TokenSpeed], *, ttft_s: float, decode_s: float) -> None:
    """把端到端口径的时长填进已有的 :class:`TokenSpeed`（流式路径专用）。

    * ``ttft_s``：请求进入到**首个含正文的 chunk 写出**的时长（t_first_content - t0）；
    * ``decode_s``：首个正文 chunk 到**收尾（``[DONE]``）**的时长。

    只在 ``speed`` 非 None 时填充，并把 ``streamed`` 置真。非流式路径不调用它。
    """
    if speed is None:
        return
    speed.streamed = True
    speed.e2e_ttft_s = max(0.0, float(ttft_s))
    speed.e2e_decode_tps = _rate(speed.reply_tokens, max(0.0, float(decode_s)))


__all__ = ["TokenSpeed", "compute_speed", "apply_e2e"]
