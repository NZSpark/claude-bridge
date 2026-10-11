"""浏览器启动参数与安全校验页识别（doc/design.md 4.2、4.3）。

说明：本项目**不**做 UA 伪装 / 反检测定制（见 ``browser_launch_args`` 注释），
因此这里断言的是「只叠加显式配置的 profile 目录」这一契约。
"""

from __future__ import annotations

import asyncio

import pytest

from claude_web import config
from claude_web.browser.dom_adapter import ClaudeDOMAdapter
from claude_web.driver import browser_launch_args
from claude_web.errors import ChallengeRequiredError
from tests.fakes import FakeElement, FakeKeyboard, FakePage


def test_launch_args_default_has_no_fingerprint_customization(monkeypatch):
    # 显式清空，避免依赖机器上是否存在 .env（否则本机会因为 .env 里的
    # BROWSER_PROFILE_DIRECTORY 而多出一个 --profile-directory 参数）。
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIRECTORY", "")
    assert browser_launch_args() == ["--no-first-run", "--no-default-browser-check"]


def test_launch_args_include_profile_directory_when_configured(monkeypatch):
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIRECTORY", "Profile 1")
    args = browser_launch_args()
    assert "--profile-directory=Profile 1" in args
    # 仍然不引入 UA / 反检测参数
    assert not any("user-agent" in a.lower() for a in args)


def test_ignore_default_args_off_by_default(monkeypatch):
    from claude_web.driver import browser_ignore_default_args

    monkeypatch.setattr(config, "BROWSER_USE_OS_KEYCHAIN", False)
    assert browser_ignore_default_args() is None


def test_ignore_default_args_drops_mock_keychain(monkeypatch):
    """复用已有 profile 时必须能去掉 mock keychain，否则 cookie 解不开。"""
    from claude_web.driver import browser_ignore_default_args

    monkeypatch.setattr(config, "BROWSER_USE_OS_KEYCHAIN", True)
    dropped = browser_ignore_default_args()
    assert "--use-mock-keychain" in dropped
    assert "--password-store=basic" in dropped


def test_launch_options_enable_sandbox_by_default(monkeypatch):
    """默认开启沙箱：去掉 Playwright 默认的 --no-sandbox（同时消除 Chrome 顶部提示）。"""
    from claude_web.driver import browser_launch_options

    monkeypatch.setattr(config, "BROWSER_SANDBOX", True)
    opts = browser_launch_options()
    assert opts["chromium_sandbox"] is True
    assert not any("no-sandbox" in a for a in opts["args"])


def test_launch_options_can_disable_sandbox(monkeypatch):
    from claude_web.driver import browser_launch_options

    monkeypatch.setattr(config, "BROWSER_SANDBOX", False)
    assert browser_launch_options()["chromium_sandbox"] is False


def test_launch_options_carry_channel_and_args(monkeypatch):
    from claude_web.driver import browser_launch_options

    monkeypatch.setattr(config, "BROWSER_CHANNEL", "chrome")
    monkeypatch.setattr(config, "BROWSER_PROFILE_DIRECTORY", "Default")
    monkeypatch.setattr(config, "BROWSER_USE_OS_KEYCHAIN", True)
    opts = browser_launch_options()
    assert opts["channel"] == "chrome"
    assert "--profile-directory=Default" in opts["args"]
    assert "--use-mock-keychain" in opts["ignore_default_args"]


def test_page_usable_reason_detects_dead_page():
    """页面已关闭时必须给出原因，否则探测会把失败误报成成功。"""
    from claude_web.errors import page_usable_reason

    dead = FakePage()
    dead.closed = True
    assert asyncio.run(page_usable_reason(dead)) is not None


def test_page_usable_reason_accepts_live_page():
    from claude_web.errors import page_usable_reason

    assert asyncio.run(page_usable_reason(FakePage())) is None


def _status(page: FakePage):
    dom = ClaudeDOMAdapter()
    return asyncio.run(dom.page_status(page))


def test_challenge_detected_by_title():
    # 真被挡住时输入区不存在；判定同时要求「命中文案」与「打不了字」。
    status = _status(FakePage(title_text="Just a moment...", composer_available=False))
    assert status.challenged is True
    assert status.challenge_reason == "title"
    assert status.challenge_evidence == "just a moment"


def test_challenge_detected_by_body_text():
    """站点安全校验页的标题可能只是站点名，正文才是线索。"""
    page = FakePage(
        title_text="claude.ai",
        body_text="Performing security verification\nVerifying you are not a bot",
        composer_available=False,
    )
    status = _status(page)
    assert status.challenged is True
    assert status.challenge_reason == "text"
    assert status.challenge_evidence == "performing security verification"


def test_normal_page_is_not_flagged_as_challenge():
    status = _status(FakePage())
    assert status.challenged is False
    assert status.challenge_reason is None
    assert status.composer is True
    assert status.usable is True


# ---- 误报回归：页面能用时不能被判成挑战页 ----
#
# 事故现象：用户能在网页里正常对话，bridge 却对每个请求报
# 「检测到站点安全挑战，请在普通浏览器中完成验证后重试」。


PHRASE = "performing security verification"


def test_conversation_text_never_counts_as_challenge():
    """会话正文（回复 / 用户消息）里的验证字样不是「站点在拦你」的证据。"""
    page = FakePage(conversation_text=f'# {PHRASE}\n请在普通浏览器里完成验证')
    status = _status(page)
    assert status.challenged is False
    assert status.challenge_reason is None
    assert status.challenge_suspect is False


def test_usable_page_with_challenge_phrase_is_only_a_suspect():
    """能打字就不算被挡住：命中文案只记诊断提示，不报 challenge_required。"""
    page = FakePage(body_text=f"{PHRASE} support article", composer_available=True)
    status = _status(page)
    assert status.challenged is False
    assert status.challenge_suspect is True
    assert status.challenge_reason == "text"
    assert status.usable is True
    # 关键：整条链路不再抛异常（此前就是这个误报把正常页面打成失败）。
    asyncio.run(ClaudeDOMAdapter().ensure_usable(page))


def test_hidden_challenge_selector_does_not_block_usable_page():
    """隐藏的 Turnstile iframe / cf-challenge 容器不能把正常页面判成挑战页。"""
    page = FakePage(challenge_indicator=False, composer_available=True)
    assert _status(page).challenged is False
    # 反过来：可见命中 + 没有输入区 = 真的是挑战页
    blocked = FakePage(challenge_indicator=True, composer_available=False)
    status = _status(blocked)
    assert status.challenged is True
    assert status.challenge_reason == "selector"
    with pytest.raises(ChallengeRequiredError):
        asyncio.run(ClaudeDOMAdapter().ensure_usable(blocked))


def test_challenge_error_detail_carries_evidence():
    """错误详情要能告诉用户「凭什么判定」：线索 + 输入区命中情况。"""
    page = FakePage(title_text="Attention Required!", composer_available=False)
    with pytest.raises(ChallengeRequiredError) as excinfo:
        asyncio.run(ClaudeDOMAdapter().ensure_usable(page))
    detail = excinfo.value.detail
    assert detail["reason"] == "title"
    assert detail["evidence"] == "attention required"
    assert detail["composer"] is False


# ---- 输入区写入与读回校验（真实站点是 tiptap / ProseMirror）----
#
# 回归背景：站点输入区把源码里的每个 ``\n`` 渲染成独立段落，读回时是 ``\n\n``。
# 过去的读回校验做严格前缀 / 子串比较，于是**任何多行提示词**都被误判成
# 「写入失败」（实测 3944 字符读回 4068 字符），任务以 prompt_submission_failed
# 结束，而提示词其实已经在输入框里。

LONG_PROMPT = "".join(f"[user] 第 {i} 行 example line {i}\n" for i in range(40))


class _TiptapComposer(FakeElement):
    """模拟 tiptap：写入的每个换行在 DOM 里变成段落分隔（``\n`` → ``\n\n``）。"""

    async def fill(self, text: str) -> None:
        self.text = text.replace("\n", "\n\n")


class _AppendingKeyboard(FakeKeyboard):
    """真实 ``insert_text`` 是插入语义：光标处追加，不清空原有内容。"""

    async def insert_text(self, text: str) -> None:
        self.page.composer.text += text


class _KeyboardOnlyComposer(FakeElement):
    """只能靠键盘插入的输入区：非空 ``fill`` 直接抛错（清空仍然可用）。"""

    async def fill(self, text: str) -> None:
        if text == "":
            self.text = ""
            return
        raise RuntimeError("fill unsupported")


def _read_back(page: FakePage, expected: str) -> bool:
    return asyncio.run(ClaudeDOMAdapter()._read_back(page, expected))


def test_read_back_accepts_paragraph_normalized_newlines():
    page = FakePage()
    page.composer = _TiptapComposer()
    asyncio.run(page.composer.fill(LONG_PROMPT))
    assert _read_back(page, LONG_PROMPT) is True


def test_read_back_rejects_empty_composer():
    assert _read_back(FakePage(), "hello") is False


def test_read_back_rejects_unrelated_draft():
    """输入框里只是旧草稿时必须判失败，不能因为「非空」就放行。"""
    page = FakePage()
    page.composer = FakeElement("完全无关的另一段旧草稿内容" * 5)
    assert _read_back(page, LONG_PROMPT) is False


def test_read_back_tolerates_rewritten_first_line():
    """开头被站点自动格式改写（如 ``- `` 变列表）时，靠分段探针仍能判成功。"""
    page = FakePage()
    written = LONG_PROMPT.replace("[user] 第 0 行", "第 0 行", 1)
    page.composer = FakeElement(written)
    assert _read_back(page, LONG_PROMPT) is True


def test_fill_composer_succeeds_against_tiptap_composer():
    """端到端：多行提示词写入 tiptap 输入区后必须被判定为成功（不再抛异常）。"""
    page = FakePage()
    page.composer = _TiptapComposer()
    asyncio.run(ClaudeDOMAdapter().fill_composer(page, LONG_PROMPT))
    assert page.composer.text.strip()


def test_fill_composer_clears_composer_before_keyboard_fallback():
    """退化到键盘插入前必须清空，否则重试会把提示词叠加成多份提交出去。"""
    page = FakePage()
    page.composer = _KeyboardOnlyComposer("上一轮残留的旧草稿")
    page.keyboard = _AppendingKeyboard(page)
    asyncio.run(ClaudeDOMAdapter().fill_composer(page, LONG_PROMPT))
    assert page.composer.text == LONG_PROMPT


# ---- 生成中探测（多信号）----


def _generating(page: FakePage):
    dom = ClaudeDOMAdapter()
    return asyncio.run(dom.generating_state(page))


def test_generating_reads_reply_streaming_attribute():
    """消息级信号：最后一条回复节点自己的 ``data-is-streaming``。"""
    page = FakePage(generating_plan=[True])
    page.submitted = True
    state = _generating(page)
    assert state.generating is True
    assert state.streaming is True
    assert state.reason == "reply_attr"


def test_generating_is_false_when_reply_finished():
    page = FakePage(generating_plan=[False])
    page.submitted = True
    state = _generating(page)
    assert state.generating is False
    assert state.streaming is False
    assert state.reason == "no_signal"


def test_generating_takes_union_of_signals():
    """两路信号取并集：消息级说写完了、但可见的停止按钮仍在 → 仍按生成中。

    反过来（消息级说正在流、控件探不到）也必须判成生成中，否则站点改版后
    stop 按钮消失就会把「还在写」误判成「已结束」。
    """
    busy_control = FakePage(generating_plan=[False], stop_control_plan=[True])
    busy_control.submitted = True
    state = _generating(busy_control)
    assert state.generating is True
    assert state.reason == "stop_control"

    busy_reply = FakePage(generating_plan=[True], stop_control_plan=[False])
    busy_reply.submitted = True
    assert _generating(busy_reply).generating is True


def test_generating_probe_failure_is_unknown():
    """探测抛异常时只能是 Unknown，不能当成「已结束」。"""

    class _Broken(FakePage):
        async def evaluate(self, expression: str, arg=None):
            raise RuntimeError("probe failed")

    state = _generating(_Broken())
    assert state.generating is None
    assert state.reason == "unknown"


def test_composer_contains_distinguishes_empty_from_unavailable():
    """``None`` = 读不出来（未知），不能当成「输入区里没这段文本」。"""
    dom = ClaudeDOMAdapter()
    page = FakePage()
    assert asyncio.run(dom.composer_contains(page, LONG_PROMPT)) is False
    asyncio.run(page.composer.fill(LONG_PROMPT))
    assert asyncio.run(dom.composer_contains(page, LONG_PROMPT)) is True
    missing = FakePage(composer_available=False)
    assert asyncio.run(dom.composer_contains(missing, LONG_PROMPT)) is None
