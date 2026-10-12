"""进程内指标注册表（doc/design.md 3.1 的 /metrics）。

刻意不引入 Prometheus 依赖：指标是进程内、有界、可 JSON 序列化的快照，
只记录计数、耗时、状态与错误类别，不采集正文（doc/design.md 7 的模块边界）。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Dict, Optional

COUNTERS = (
    "request_total",
    "request_success_total",
    "request_error_total",
    "request_timeout_total",
    "request_retry_total",
    "request_context_limit_total",
    "request_cancelled_total",
    "browser_unavailable_total",
    "browser_selector_miss_total",
    "reply_extraction_failure_total",
    "session_busy_total",
    "session_recovery_total",
    "tool_call_total",
    "tool_parse_failure_total",
    # 失败类别计数（T3.4 / D3.4）：按 ErrorCode 分类，便于在 /metrics 上验证
    # 「归因收窄」（D3.3）是否把错误分对了类，而不是只看到一个笼统的 error 总数。
    "usage_limit_reached_total",
    "prompt_submission_failed_total",
    "reply_not_found_total",
    "context_limit_reached_total",
    # 网页重写已流出的回复（T4.4 / D4.4）：此时无法撤回已发送内容，只能停止续发，
    # 最终文本以收尾块为准。计数它便于在 /metrics 上观察重写频率。
    "stream_rewritten_total",
)

LATENCIES = (
    "request_latency",
    "browser_generation_latency",
    "reply_extraction_latency",
)


class MetricsRegistry:
    """线程安全的内存计数器与耗时累加器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._latency_totals: Dict[str, float] = defaultdict(float)
        self._latency_counts: Dict[str, int] = defaultdict(int)
        self._started_at = time.monotonic()

    def inc(self, name: str, value: int = 1) -> int:
        if name not in COUNTERS:
            raise ValueError(f"unknown metric counter: {name}")
        with self._lock:
            self._counters[name] += value
            return self._counters[name]

    def observe(self, name: str, duration_s: float) -> None:
        if name not in LATENCIES:
            raise ValueError(f"unknown latency metric: {name}")
        duration_s = max(0.0, float(duration_s))
        with self._lock:
            self._latency_totals[name] += duration_s
            self._latency_counts[name] += 1

    def timer(self, name: str) -> "MetricTimer":
        if name not in LATENCIES:
            raise ValueError(f"unknown latency metric: {name}")
        return MetricTimer(self, name)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            counters = {name: int(self._counters[name]) for name in COUNTERS}
            latency: Dict[str, Any] = {}
            for name in LATENCIES:
                count = int(self._latency_counts[name])
                total = float(self._latency_totals[name])
                latency[name] = {
                    "count": count,
                    "total_seconds": total,
                    "avg_seconds": total / count if count else 0.0,
                }
            return {
                "counters": counters,
                "latency": latency,
                "uptime_seconds": max(0.0, time.monotonic() - self._started_at),
            }

    def reset(self) -> None:
        """清空运行时采样；用于测试与受控诊断。"""
        with self._lock:
            self._counters.clear()
            self._latency_totals.clear()
            self._latency_counts.clear()
            self._started_at = time.monotonic()


class MetricTimer:
    """退出时记录耗时的上下文管理器。"""

    def __init__(self, registry: MetricsRegistry, name: str) -> None:
        self.registry = registry
        self.name = name
        self.started_at: Optional[float] = None

    def __enter__(self) -> "MetricTimer":
        self.started_at = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.started_at is not None:
            self.registry.observe(self.name, time.monotonic() - self.started_at)


#: 进程级单例：所有模块共用。
metrics = MetricsRegistry()
