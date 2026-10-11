"""新回复识别（doc/design.md 4.4）。

回归事故（2026-10-12）：真实页面上 Claude 已经回答，桥却报
``提交后未识别到本轮回复（页面无新增 assistant 节点）``。

原因：``read_new`` 只比较 assistant 节点**条数**与提交前的基线。只要页面在渲染本轮
回复的同时回收 / 复用旧行（条数停在基线上），``count <= baseline`` 就永远成立，于是
一路轮询到停滞。

这里覆盖三件事：

* 条数停在基线上，但页面上已经出现本轮提问的回答 → **必须认出**（靠结构信号）；
* 条数没涨、页面上也没有新提问（还是上一条回复）→ **不能**把旧正文当成新回复；
* 结构信号读不到时（旧页面 / 替身）→ 行为退回只看条数，与改造前一致。
"""

from __future__ import annotations

import asyncio

import pytest

from claude_web.browser.dom_adapter import ClaudeDOMAdapter
from claude_web.completion.extractor import ReplyExtractor
from claude_web.models import ChatTask, NormalizedMessage
from tests.fakes import FakePage, make_service

BUCKET = "reply-detect"


def _task(text: str) -> ChatTask:
    return ChatTask(
        messages=[NormalizedMessage(role="user", content=text)],
        model="claude-web",
        bucket=BUCKET,
        stream=False,
    )


def _next_turn(page: FakePage, reply: str) -> None:
    """模拟客户端下一轮：页面上先前的回复已归档，等待本轮新回复。"""
    page.snapshots = [reply]
    page.submitted = False
    page.poll = 0


def test_flat_reply_count_still_detects_new_reply(tmp_path):
    """条数停在基线上，但「提问变多 + 其后已有 assistant 节点」必须认出回复。"""
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    first = asyncio.run(service.run(_task("第一轮")))
    assert first.result.text == "答复一"

    _next_turn(page, "答复二")
    # 页面复用同一行：提交前后 assistant 条数都是 1（条数口径失灵）。
    page.flat_reply_rows = 1
    outcome = asyncio.run(service.run(_task("第二轮")))
    assert outcome.result.text == "答复二"


def test_flat_reply_count_does_not_return_the_previous_reply(tmp_path):
    """条数没涨、提问也没变多时：页面上那条是**上一条回复**，不能当成本轮回复。"""
    page = FakePage(snapshots=["答复一"])
    extractor = ReplyExtractor(ClaudeDOMAdapter())
    page.submitted = True
    page.snapshots = ["答复一"]
    page.flat_reply_rows = 1
    baseline = asyncio.run(extractor.baseline(page))

    read = asyncio.run(extractor.read_new(page, baseline))
    assert read.is_new is False
    assert read.text == ""


def test_dropped_user_count_is_not_treated_as_a_new_turn(tmp_path):
    """用户消息条数变少（页面回收旧行）不算本轮提问：宁可不认，也不能返回旧正文。"""
    page = FakePage(snapshots=["答复一"], user_messages=["很旧的一轮"])
    page.flat_reply_rows = 1
    extractor = ReplyExtractor(ClaudeDOMAdapter())
    baseline = asyncio.run(extractor.baseline(page))

    page.submitted = True
    page.user_messages = []  # 旧行被回收：条数反而变少
    read = asyncio.run(extractor.read_new(page, baseline))
    assert read.is_new is False


def test_missing_structural_signal_falls_back_to_count(monkeypatch):
    """探针不可用（旧页面 / 替身）时退回只看条数：与改造前行为一致。"""
    page = FakePage(snapshots=["答复一"])
    extractor = ReplyExtractor(ClaudeDOMAdapter())

    async def _no_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(page, "evaluate", _no_probe)
    baseline = asyncio.run(extractor.baseline(page))
    page.submitted = True
    read = asyncio.run(extractor.read_new(page, baseline))
    # 旧口径：条数 0 → 1，仍然认出新回复
    assert baseline.count == 0
    assert read.is_new is True
    assert read.structural is False


def test_structural_signal_is_marked_in_read():
    """条数停在基线上、靠结构信号认出回复时，要在读取结果上打标记（诊断用）。"""
    page = FakePage(snapshots=["答复一"], user_messages=["基线那一轮"])
    page.flat_reply_rows = 1  # 提交前后都是 1 条（页面复用了同一行）
    extractor = ReplyExtractor(ClaudeDOMAdapter())
    baseline = asyncio.run(extractor.baseline(page))
    assert (baseline.count, baseline.user_count) == (1, 1)

    page.filled_texts.append("本轮提示词")  # 本轮提问落到页面上
    page.submitted = True
    read = asyncio.run(extractor.read_new(page, baseline))
    assert read.is_new is True
    assert read.structural is True
    assert read.text == "答复一"


def test_changed_user_fingerprint_detects_reply_when_counts_match():
    """提问条数一样（页面回收了旧行）时，靠最后一条提问的指纹变化仍能认出本轮。"""
    page = FakePage(snapshots=["答复一"], user_messages=["基线那一轮"])
    page.flat_reply_rows = 1
    extractor = ReplyExtractor(ClaudeDOMAdapter())
    baseline = asyncio.run(extractor.baseline(page))

    page.user_messages = ["本轮提示词"]  # 旧行被回收：条数不变，内容换了
    page.submitted = True
    read = asyncio.run(extractor.read_new(page, baseline))
    assert read.user_count == baseline.user_count == 1
    assert read.is_new is True
    assert read.structural is True


@pytest.mark.parametrize("prompt", ["第二轮", "第二轮"])
def test_same_prompt_twice_is_still_detected_by_user_count(tmp_path, prompt):
    """客户端把同一条指令重发（文本完全相同）时，靠「提问条数变多」仍能认出。"""
    page = FakePage(snapshots=["答复一"])
    service = make_service(page, tmp_path)
    asyncio.run(service.run(_task(prompt)))
    _next_turn(page, "答复二")
    page.flat_reply_rows = 1
    outcome = asyncio.run(service.run(_task(prompt)))
    assert outcome.result.text == "答复二"
