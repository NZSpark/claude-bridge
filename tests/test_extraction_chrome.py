"""回复正文里的页面外壳（doc/design.md 4.4）。

样本来自 2026-10-12 在**真实页面**上通过 CDP 逐节点核对的 assistant 消息结构：
节点里除了模型正文，还有一个 ``sr-only`` 的无障碍标题（内容就是整段正文，形如
``Claude responded: 收到。``）、动作栏（Copy / Read aloud / 点赞 / 重试）、时间戳
``<time>just now</time>`` 与推广行（``Claude works directly with your codebase``）。

旧行为（真实记录）：客户端收到

.. code-block:: text

    Claude responded: 收到。

    收到。

    




    6 minutes ago

即「同一段正文出现两遍 + 一行时间戳 + 一句页面推广文案」。
"""

from __future__ import annotations

import asyncio

import pytest

from claude_web import config
from claude_web.completion.extractor import normalize_reply
from claude_web.completion.generator import ReplyGenerator
from tests.fakes import FakePage

#: 真实抓到的短回复原文（DOM 剔除前的 innerText）。
SHORT_RAW = "Claude responded: 收到。\n\n收到。\n\n\n\n\n\n6 minutes ago"

#: 多段落正文：无障碍标题把整段正文重复了一遍。
LONG_RAW = (
    "Claude responded: pytest 和 ruff 都通过了。\n\nruff check：All checks passed。\n\n"
    "pytest 和 ruff 都通过了。\n\nruff check：All checks passed。\n\n\n\n13 minutes ago"
)


def test_short_reply_chrome_is_stripped():
    assert normalize_reply(SHORT_RAW) == "收到。"


def test_multiparagraph_body_is_kept_when_a11y_title_repeats_it():
    expected = "pytest 和 ruff 都通过了。\n\nruff check：All checks passed。"
    assert normalize_reply(LONG_RAW) == expected


@pytest.mark.parametrize(
    "stamp",
    ["just now", "now", "13 minutes ago", "2 分钟前", "刚刚", "Yesterday 20:32", "20:32"],
)
def test_trailing_timestamp_line_is_stripped(stamp):
    assert normalize_reply(f"正文第一段。\n\n{stamp}") == "正文第一段。"


def test_timestamp_like_line_in_the_middle_is_kept():
    """只有「最后一行且整行就是时间戳」才剔：正文中间出现的同样文本必须保留。"""
    text = "日志样例：\n\njust now\n\n以上是原文。"
    assert normalize_reply(text) == text


def test_long_last_line_that_is_not_a_timestamp_is_kept():
    text = "结论。\n\n3 minutes ago we saw the same failure and it recovered."
    assert normalize_reply(text) == text


def test_plain_reply_is_untouched():
    text = "第一段。\n\n第二段：\n\n```\ncode()\n```"
    assert normalize_reply(text) == text


def test_repeated_paragraph_without_a11y_label_is_kept():
    """没有无障碍标题前缀时不做折叠：模型有意重复的内容不能被吞掉。"""
    text = "同上。\n\n同上。"
    assert normalize_reply(text) == text


def test_a11y_label_without_duplicated_body_only_loses_the_label():
    """有标题前缀但正文没被重复时，只去掉前缀，正文一个字都不能丢。"""
    assert normalize_reply("Claude responded: 收到。\n\n补充说明。") == "收到。\n\n补充说明。"


def test_chrome_selectors_are_passed_to_the_dom():
    """外壳选择器必须真的传进 DOM 提取（否则只有文本兜底在起作用）。"""
    page = FakePage(snapshots=["ok"], generating_plan=[False])
    asyncio.run(ReplyGenerator().generate(page, "hi"))
    args = page.last_complete_text_args
    assert isinstance(args, dict), "COMPLETE_TEXT_JS 应收到 {node, chrome}"
    assert args["chrome"] == list(config.REPLY_CHROME_SELECTORS)
    assert ".sr-only" in args["chrome"] and "time" in args["chrome"]


def test_end_to_end_reply_has_no_chrome():
    """整条链路（生成 -> 提取 -> 归一化）不再把外壳交给客户端。"""
    page = FakePage(
        snapshots=["Claude responded: 收到。\n\n收到。\n\n\n\njust now"],
        generating_plan=[False],
    )
    result = asyncio.run(ReplyGenerator().generate(page, "hi"))
    assert result.text == "收到。"
