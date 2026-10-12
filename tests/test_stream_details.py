"""流式细节（T4.4 / D4.4）：on_delta 去重、include_usage、重写指标。"""

from __future__ import annotations

import json

from claude_web.api.chat_adapter import StreamEncoder
from claude_web.models import Usage


def _data(payload: str) -> dict:
    assert payload.startswith("data: ")
    return json.loads(payload[len("data: "):].strip())


def test_usage_chunk_has_empty_choices_and_usage():
    """include_usage 收尾块：choices 为空、usage 非空（OpenAI 约定）。"""
    enc = StreamEncoder("claude-web")
    payload = _data(enc.usage(Usage(prompt_tokens=3, completion_tokens=5, total_tokens=8)))
    assert payload["choices"] == []
    assert payload["usage"]["total_tokens"] == 8


def test_include_usage_flag_helper():
    from claude_web.models import ChatTask
    from claude_web.server import _include_usage

    base = ChatTask(messages=[], model="m", bucket="b")
    assert _include_usage(base) is False
    base.stream_options = {"include_usage": True}
    assert _include_usage(base) is True
    base.stream_options = {"include_usage": False}
    assert _include_usage(base) is False
    base.stream_options = None
    assert _include_usage(base) is False


def test_stream_rewritten_metric_registered():
    """指标名必须在 COUNTERS 里，否则 inc 会抛 ValueError。"""
    from claude_web.metrics import COUNTERS

    assert "stream_rewritten_total" in COUNTERS
