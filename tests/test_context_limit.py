"""「会话到顶」判定的真假阳性（doc/design.md 4.4）。

真实事故（2026-10-12，CDP 逐节点核对）：一个把本仓库 ``.env.example`` 里的
``CONTEXT_LIMIT_PATTERNS=context limit||conversation is too long||maximum length||对话长度上限||已达长度限制``
读进会话的任务，让「会话到顶」扫描**每轮**都命中——因为旧实现只剔除助手回复节点，
而那句话在**用户消息**里。会话明明还能正常对话，客户端却收到
``网页会话已达上下文长度上限，请重置该会话桶``（且桶被标记为到顶 → 下一轮重新播种）。

两个修复点在这里各有一条用例：剔除清单必须覆盖会话子树（含用户消息），且**正常收尾
优先于**特征文案。
"""

from __future__ import annotations

import asyncio

import pytest

from claude_web import config
from claude_web.browser import selectors
from claude_web.completion.generator import ReplyGenerator
from claude_web.errors import ContextLimitError, GenerationStalledError, PromptSubmissionError
from tests.fakes import FakePage


class RecordingPage(FakePage):
    """记录 ``BODY_TEXT_JS`` 入参的假页面。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.body_text_args = None

    async def evaluate(self, expression, arg=None):
        if expression == selectors.BODY_TEXT_JS:
            self.body_text_args = arg
        return await super().evaluate(expression, arg)


def test_exclusion_list_covers_user_messages_and_composer():
    """剔除清单必须包含用户消息（事故里的那句话就在用户消息里）。"""
    page = RecordingPage(
        snapshots=[],
        generating_plan=[False],
        body_text="已达长度限制",
        submit_lands=False,
    )
    with pytest.raises(ContextLimitError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert page.body_text_args is not None
    passed = list(page.body_text_args["selectors"])
    assert 'div[data-testid="user-message"]' in passed
    assert 'div[data-testid="assistant-message"]' in passed
    assert passed == config.conversation_exclude_selectors()


def test_body_text_js_prunes_by_clone_not_by_string_surgery():
    """剔枝必须靠在克隆体上剪子树：字符串替换会因空白规范化不同而静默失败。"""
    js = selectors.BODY_TEXT_JS
    for selector in ("[contenteditable=\"true\"]", "textarea", "script", "style"):
        assert selector in js
    assert "cloneNode(true)" in js and ".remove()" in js
    # 不得再用「整页文本 - 子树 innerText」的字符串替换
    assert "split(t).join" not in js and "text.replace(t" not in js


def test_reply_that_settles_normally_is_not_reported_as_context_limit(
    virtual_time, default_timing
):
    """事故复现：页面别处（会话正文引用配置）出现特征文案，成功回复不能被改成错误。"""
    page = FakePage(
        snapshots=["正在执行。", "做完了：一切正常。"],
        generating_plan=[True, False],
        body_text="前面聊过 CONTEXT_LIMIT_PATTERNS=context limit||conversation is too long",
    )
    result = asyncio.run(ReplyGenerator().generate(page, "把结论给我。"))
    assert result.text == "做完了：一切正常。"
    assert result.finish_reason == "stop"


def test_lost_submission_with_notice_reports_context_limit(virtual_time, default_timing):
    """真到顶时输入区会留着提示词：此时应报 context_limit，而不是“提交未生效”。"""
    page = FakePage(
        snapshots=[],
        generating_plan=[False],
        body_text="已达长度限制，请开启新对话",
        submit_lands=False,
    )
    with pytest.raises(ContextLimitError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))


def test_lost_submission_without_notice_still_reports_submit_failure(
    virtual_time, default_timing
):
    page = FakePage(
        snapshots=[], generating_plan=[False], body_text="一切正常", submit_lands=False
    )
    with pytest.raises(PromptSubmissionError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))


def test_notice_still_detected_when_page_never_produces_a_reply(
    virtual_time, default_timing, monkeypatch
):
    """真正到顶的形态：页面不再产出新回复且页面上有提示 → 仍应报 context_limit。"""
    monkeypatch.setattr(config, "STALL_S", 10.0)
    page = FakePage(
        snapshots=[],
        generating_plan=[False],
        body_text="已达长度限制，请开启新对话",
    )
    with pytest.raises(ContextLimitError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))


def test_notice_with_growing_content_does_not_fire(virtual_time, default_timing):
    """正文还在增长时不做该扫描（扫描只在页面静止时进行）。"""
    page = FakePage(
        snapshots=["第一段", "第一段第二段", "第一段第二段第三段", "第一段第二段第三段"],
        generating_plan=[True, True, True, False],
        body_text="已达长度限制",
    )
    result = asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert result.text == "第一段第二段第三段"


def test_stall_without_notice_still_reports_stall(virtual_time, default_timing, monkeypatch):
    """页面静止、有回复节点但正文为空、且没有到顶提示时，报的是停滞而不是到顶。"""
    monkeypatch.setattr(config, "STALL_S", 10.0)
    page = FakePage(snapshots=[""], generating_plan=[False], body_text="一切正常")
    with pytest.raises(GenerationStalledError):
        asyncio.run(ReplyGenerator().generate(page, "hi"))
