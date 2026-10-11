"""生成循环的时延行为（doc/design.md 4.4）。

这里把 ``asyncio.sleep`` 与 ``time.monotonic`` 同时指向**虚拟时间轴**，于是
「轮询间隔序列」与「客户端一共等了多久」可以在毫秒内断言，而判定仍按真实秒数比较。

回归的背景：旧实现用「连续轮询次数」当结束 / 停滞阈值，页面在第 2 轮就写完了，
客户端却要等满 ``QUIET_POLLS × POLL_INTERVAL_S``（默认 6s）才收到收尾；一次实测
10.8s 的短回复里有 9.0s 花在这种固定尾部等待上。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

import pytest

from claude_web import config
from claude_web.browser import selectors
from claude_web.completion.generator import ReplyGenerator
from claude_web.errors import (
    GenerationStalledError,
    PromptSubmissionError,
    ReplyNotFoundError,
    UsageLimitError,
)
from tests.fakes import FakePage, VirtualClock

#: 真实页面上额度用尽时的弹窗文案（2026-10-12 用 CDP 逐节点核对）。
LIMIT_MODAL_TEXT = (
    "Upgrade to keep chatting. You hit your 5-hour message limit. "
    "It resets at 2:10 PM, or you can upgrade for higher limits."
)


@pytest.fixture
def clock(virtual_time: VirtualClock) -> VirtualClock:
    """本文件里统一叫 ``clock``（实现见 conftest 的 ``virtual_time``）。"""
    return virtual_time


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


def test_streaming_polls_fast_and_settles_after_settle_window(
    clock: VirtualClock, monkeypatch, default_timing
):
    """页面在第 2 轮就落定：剩下的等待只应是 SETTLE_S，而不是固定 6 秒尾部。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
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


def test_idle_polling_keeps_stall_window_in_wall_clock(
    clock: VirtualClock, monkeypatch, default_timing
):
    """静止期用基础间隔：停滞窗口仍是 STALL_S 秒，不因轮询变快而缩短。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(snapshots=[], generating_plan=[False])
    with pytest.raises(ReplyNotFoundError) as excinfo:
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    # 失败详情要留下当时的条数现场：下次出这种事能直接从错误 / 日志里看出是「条数口径」
    # 失灵还是页面真的没回（doc/design.md 4.4）。
    assert excinfo.value.detail["reply_count"] == 0
    assert excinfo.value.detail["reply_baseline"] == 0
    # 页面确实收到了本轮提问（提问条数 0→1），但始终没有 assistant 节点：
    # ``trailing_reply`` 为 False 就是它与「条数口径失灵」（已被结构信号救回）的区别。
    assert excinfo.value.detail["user_count"] == 1
    assert excinfo.value.detail["trailing_reply"] is False
    assert 30.0 <= clock.total <= 32.0
    # 静止以后不应再出现「活跃间隔」。
    assert all(interval == pytest.approx(1.5) for interval in clock.intervals[1:])


def test_lost_submission_fails_within_submit_window(
    clock: VirtualClock, monkeypatch, default_timing
):
    """提交没生效：按 SUBMIT_LOST_S 快速失败，而不是等满 30s 停滞窗口。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(snapshots=[], generating_plan=[False], submit_lands=False)
    with pytest.raises(PromptSubmissionError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert clock.total < 8.0


def test_usage_limit_modal_is_reported_instead_of_stall(
    clock: VirtualClock, monkeypatch, default_timing
):
    """额度用尽：提示词没发出去（提交落空）时，要如实报额度用尽，而不是「提交未生效」。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(
        snapshots=[],
        generating_plan=[False],
        submit_lands=False,
        limit_modal=LIMIT_MODAL_TEXT,
    )
    with pytest.raises(UsageLimitError) as excinfo:
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert excinfo.value.code.value == "usage_limit_reached"
    assert excinfo.value.http_status == 429
    # 提交前就把挡路的弹窗关掉（只点关闭类控件），而不是等它自己消失。
    assert page.modal_clicks == 1
    assert page.modal_dismissed is True
    # 走的是 SUBMIT_LOST_S 快速失败，不是停滞窗口。
    assert clock.total < 8.0


def test_stalled_round_with_quota_modal_names_the_real_cause(
    clock: VirtualClock, monkeypatch, default_timing
):
    """回复节点一直空白 + 页面有额度弹窗：归因为额度用尽，而不是「生成停滞」。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(
        snapshots=[""], generating_plan=[True, False], limit_modal=LIMIT_MODAL_TEXT
    )
    with pytest.raises(UsageLimitError) as excinfo:
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert excinfo.value.detail["seen_at_submit"] is True
    assert excinfo.value.detail["pattern"] == "message limit"


def test_empty_reply_node_without_modal_is_still_a_stall(
    clock: VirtualClock, monkeypatch, default_timing
):
    """对照：同样是静止不动，但页面没有额度弹窗时，仍然按停滞快速失败。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(snapshots=[""], generating_plan=[True, False])
    with pytest.raises(GenerationStalledError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert page.modal_clicks == 0
    assert 30.0 <= clock.total <= 32.0


def test_successful_run_does_no_full_page_scan(
    clock: VirtualClock, monkeypatch, default_timing
):
    """「到顶」扫描要读整页可见文本（强制布局）：正常成功的轮次一次都不做。

    （旧实现按固定轮询次数扫整页；那时一次仅几十字符的短回复也会扫一次全页。）
    """
    page = CountingPage(
        snapshots=["a", "ab", "abc", "abcd"],
        generating_plan=[True, True, True, False],
        body_text="已达长度限制，请开启新对话",
    )
    result = asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert result.text == "abcd"
    assert page.count(selectors.BODY_TEXT_JS) == 0


def test_settle_does_not_fire_while_page_still_generating(
    clock: VirtualClock, monkeypatch, default_timing
):
    """页面自述仍在生成时，静默时长累计不上：正文不变也不会提前收尾。"""
    page = FakePage(
        snapshots=["思考中…"],
        generating_plan=[True, True, True, True, False],
    )
    result = asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert result.text == "思考中…"
    # 前 4 轮都在生成中（每轮 0.4s），落定后才开始计入静默时长。
    assert clock.total == pytest.approx(4 * 0.4 + config.SETTLE_S, abs=0.9)
