"""生成循环的时延行为（doc/design.md 4.4）。

这里把 ``asyncio.sleep`` 与 ``time.monotonic`` 同时指向**虚拟时间轴**，于是
「轮询间隔序列」与「客户端一共等了多久」可以在毫秒内断言，而判定仍按真实秒数比较。

回归的背景：旧实现用「连续轮询次数」当结束 / 停滞阈值，页面在第 2 轮就写完了，
客户端却要等满 ``QUIET_POLLS × POLL_INTERVAL_S``（默认 6s）才收到收尾；一次实测
10.8s 的短回复里有 9.0s 花在这种固定尾部等待上。
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, Dict, List

import pytest

from claude_web import config
from claude_web.browser import selectors
from claude_web.completion import generator as generator_module
from claude_web.completion.generator import ReplyGenerator
from claude_web.errors import ContextLimitError, PromptSubmissionError, ReplyNotFoundError
from tests.fakes import FakePage


class VirtualClock:
    """记录每次睡眠并驱动 ``time.monotonic`` 的虚拟时钟。"""

    def __init__(self) -> None:
        self.total = 0.0
        self.intervals: List[float] = []

    async def sleep(self, seconds: float) -> None:
        self.total += float(seconds)
        self.intervals.append(float(seconds))

    def monotonic(self) -> float:
        return self.total


@pytest.fixture
def clock(monkeypatch) -> VirtualClock:
    virtual = VirtualClock()
    monkeypatch.setattr(generator_module, "asyncio", types.SimpleNamespace(sleep=virtual.sleep))
    monkeypatch.setattr(
        generator_module, "time", types.SimpleNamespace(monotonic=virtual.monotonic)
    )
    return virtual


class CountingPage(FakePage):
    """统计每种 DOM 探针调用次数的假页面。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.evaluate_calls: Dict[str, int] = {}

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        self.evaluate_calls[expression] = self.evaluate_calls.get(expression, 0) + 1
        return await super().evaluate(expression, arg)

    def count(self, expression: str) -> int:
        return self.evaluate_calls.get(expression, 0)


def _defaults(monkeypatch, **overrides) -> None:
    values = {
        "POLL_INTERVAL_S": 1.5,
        "POLL_INTERVAL_ACTIVE_S": 0.4,
        "SETTLE_S": 2.5,
        "SETTLE_NO_SIGNAL_S": 6.0,
        "SUBMIT_LOST_S": 6.0,
        "STALL_S": 30.0,
        "RESPONSE_TIMEOUT_S": 300.0,
        "RESPONSE_TIMEOUT_EXTEND_S": 0.0,
        "MAX_UPSTREAM_RETRIES": 0,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(config, name, value)


def test_streaming_polls_fast_and_settles_after_settle_window(clock, monkeypatch):
    """页面在第 2 轮就落定：剩下的等待只应是 SETTLE_S，而不是固定 6 秒尾部。"""
    _defaults(monkeypatch)
    page = FakePage(
        snapshots=["正在执行。", "做完了：一切正常。"],
        generating_plan=[True, False],
    )
    result = asyncio.run(ReplyGenerator().generate(page, "把结论给我。"))

    assert result.text == "做完了：一切正常。"
    # 首段正文在第一个**活跃**间隔内到达（不是等满一个静止间隔）。
    assert clock.intervals[0] == pytest.approx(0.4)
    # 页面在第 2 轮（虚拟 0.8s）落定；收尾只按静默时长决定。
    assert clock.total == pytest.approx(0.8 + config.SETTLE_S, abs=0.3)
    # 旧实现固定 6 轮 × 1.5s = 9.0s。
    assert clock.total < 4.0


def test_idle_polling_keeps_stall_window_in_wall_clock(clock, monkeypatch):
    """静止期用基础间隔：停滞窗口仍是 STALL_S 秒，不因轮询变快而缩短。"""
    _defaults(monkeypatch, STALL_S=30.0)
    page = FakePage(snapshots=[], generating_plan=[False])
    with pytest.raises(ReplyNotFoundError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert 30.0 <= clock.total <= 32.0
    # 静止以后不应再出现「活跃间隔」。
    assert all(interval == pytest.approx(1.5) for interval in clock.intervals[1:])


def test_lost_submission_fails_within_submit_window(clock, monkeypatch):
    """提交没生效：按 SUBMIT_LOST_S 快速失败，而不是等满 30s 停滞窗口。"""
    _defaults(monkeypatch, SUBMIT_LOST_S=6.0, STALL_S=30.0)
    page = FakePage(snapshots=[], generating_plan=[False], submit_lands=False)
    with pytest.raises(PromptSubmissionError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert clock.total < 8.0


def test_context_limit_scan_runs_only_when_page_is_static(clock, monkeypatch):
    """「到顶」扫描要读整页文本：流式期间一次都不做。"""
    _defaults(monkeypatch)
    page = CountingPage(
        snapshots=["a", "ab", "abc", "abcd"],
        generating_plan=[True, True, True, False],
        body_text="XX",
    )
    asyncio.run(ReplyGenerator().generate(page, "hi"))
    # 只有页面静止（第 4 轮之后）那一次扫描。
    assert page.count(selectors.BODY_TEXT_JS) == 1


def test_context_limit_still_detected_once_page_is_static(clock, monkeypatch):
    _defaults(monkeypatch)
    page = CountingPage(
        snapshots=["a", "ab"],
        generating_plan=[True, False],
        body_text="已达长度限制，请开启新对话",
    )
    with pytest.raises(ContextLimitError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))


def test_settle_does_not_fire_while_page_still_generating(clock, monkeypatch):
    """页面自述仍在生成时，静默时长累计不上：正文不变也不会提前收尾。"""
    _defaults(monkeypatch, SETTLE_S=2.5)
    page = FakePage(
        snapshots=["思考中…"],
        generating_plan=[True, True, True, True, False],
    )
    result = asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert result.text == "思考中…"
    # 前 4 轮都在生成中（每轮 0.4s），落定后才开始计入静默时长。
    assert clock.total == pytest.approx(4 * 0.4 + config.SETTLE_S, abs=0.9)
