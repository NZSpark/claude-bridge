"""失败类别计数（T3.4 / D3.4）。

此前 /metrics 只能看到笼统的 request_error_total，无法验证 D3.3 的「归因收窄」
到底把错误分对了类没有。本文件逐码驱动真实 service.run 路径，断言
「对应计数器 +1，且其它类别计数保持不变」。

全部使用 FakePage / FakeDriver，不联网、不启动浏览器。
"""

from __future__ import annotations

import asyncio

import pytest

from claude_web import config
from claude_web.errors import (
    ContextLimitError,
    PromptSubmissionError,
    ReplyNotFoundError,
    UsageLimitError,
)
from claude_web.metrics import MetricsRegistry
from claude_web.models import ChatTask, NormalizedMessage
from tests.fakes import FakePage, make_service

BUCKET = "metrics"

#: 本文件关心的分类计数器（与 service._FAILURE_COUNTERS 的值集一致）。
CLASSIFIED = (
    "usage_limit_reached_total",
    "prompt_submission_failed_total",
    "reply_not_found_total",
    "context_limit_reached_total",
)


def _task() -> ChatTask:
    return ChatTask(
        messages=[NormalizedMessage(role="user", content="hi")],
        model="claude-web",
        bucket=BUCKET,
        stream=False,
    )


def _service(page, tmp_path):
    """每个测试用独立的 metrics 注册表：进程级单例会在测试间串味。"""
    return make_service(page, tmp_path, metrics=MetricsRegistry())


def _counters(service) -> dict:
    return service.metrics.snapshot()["counters"]


def _assert_only(counters: dict, expected: str) -> None:
    """断言只有 expected 非零，其余分类计数器为 0（防误增）。"""
    for name in CLASSIFIED:
        want = 1 if name == expected else 0
        assert counters[name] == want, f"{name} 期望 {want}，实际 {counters[name]}"


def test_usage_limit_increments_its_own_counter(tmp_path, monkeypatch):
    """提交落空 + 额度弹窗：归因为 usage_limit_reached，只增它自己的计数。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(
        snapshots=[],
        generating_plan=[False],
        submit_lands=False,
        limit_modal="message limit",
    )
    service = _service(page, tmp_path)
    with pytest.raises(UsageLimitError):
        asyncio.run(service.run(_task()))
    counters = _counters(service)
    _assert_only(counters, "usage_limit_reached_total")
    # 笼统错误计数照常累加，分类计数是它之上的细分。
    assert counters["request_error_total"] == 1


def test_prompt_submission_failure_increments_its_own_counter(tmp_path, monkeypatch):
    """提交没生效且无额度弹窗：归因为 prompt_submission_failed。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(snapshots=[], generating_plan=[False], submit_lands=False)
    service = _service(page, tmp_path)
    with pytest.raises(PromptSubmissionError):
        asyncio.run(service.run(_task()))
    counters = _counters(service)
    _assert_only(counters, "prompt_submission_failed_total")


def test_reply_not_found_increments_its_own_counter(tmp_path, monkeypatch):
    """页面始终没给出回复：归因为 reply_not_found。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(snapshots=[], generating_plan=[False])
    service = _service(page, tmp_path)
    with pytest.raises(ReplyNotFoundError):
        asyncio.run(service.run(_task()))
    counters = _counters(service)
    _assert_only(counters, "reply_not_found_total")


def test_context_limit_increments_both_counters(tmp_path, monkeypatch):
    """会话到顶：分类计数 + 既有的 request_context_limit_total 都要 +1。"""
    monkeypatch.setattr(config, "MAX_UPSTREAM_RETRIES", 0)
    page = FakePage(
        snapshots=[],
        generating_plan=[False],
        body_text="已达长度限制",
        submit_lands=False,
    )
    service = _service(page, tmp_path)
    with pytest.raises(ContextLimitError):
        asyncio.run(service.run(_task()))
    counters = _counters(service)
    _assert_only(counters, "context_limit_reached_total")
    # 既有计数不应因为新增分类计数而丢失。
    assert counters["request_context_limit_total"] == 1


def test_success_increments_no_failure_counter(tmp_path):
    """对照：正常成功不增任何失败类别计数。"""
    page = FakePage(snapshots=["答复一"])
    service = _service(page, tmp_path)
    asyncio.run(service.run(_task()))
    counters = _counters(service)
    for name in CLASSIFIED:
        assert counters[name] == 0, f"成功路径不应增 {name}"
    assert counters["request_success_total"] == 1
