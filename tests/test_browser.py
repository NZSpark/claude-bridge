"""浏览器启动参数与安全校验页识别（doc/design.md 4.2、4.3）。

说明：本项目**不**做 UA 伪装 / 反检测定制（见 ``browser_launch_args`` 注释），
因此这里断言的是「只叠加显式配置的 profile 目录」这一契约。
"""

from __future__ import annotations

import asyncio

from claude_web import config
from claude_web.browser.dom_adapter import ClaudeDOMAdapter
from claude_web.driver import browser_launch_args
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
    status = _status(FakePage(title_text="Just a moment...", composer_available=False))
    assert status.challenged is True
    assert status.challenge_reason == "title"


def test_challenge_detected_by_body_text():
    """站点安全校验页的标题可能只是站点名，正文才是线索。"""
    page = FakePage(
        title_text="claude.ai",
        body_text="Performing security verification\nVerifying you are not a bot",
    )
    status = _status(page)
    assert status.challenged is True
    assert status.challenge_reason == "text"


def test_normal_page_is_not_flagged_as_challenge():
    status = _status(FakePage())
    assert status.challenged is False
    assert status.challenge_reason is None
    assert status.composer is True
    assert status.usable is True


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
