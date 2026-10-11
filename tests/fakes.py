"""离线测试替身：Fake 页面 / 元素 / 浏览器 Driver。

用轻量替身驱动真实的 :class:`~claude_web.browser.dom_adapter.ClaudeDOMAdapter`，
从而在不启动 Playwright、不联网的前提下覆盖整条生成链路。
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from claude_web import config
from claude_web.browser import selectors
from claude_web.browser.dom_adapter import ClaudeDOMAdapter
from claude_web.driver import Readiness
from claude_web.page_pool import PagePool


class FakeElement:
    def __init__(self, text: str = "", visible: bool = True) -> None:
        self.text = text
        self.visible = visible
        self.clicks = 0

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        return self.text

    async def fill(self, text: str) -> None:
        self.text = text

    async def click(self) -> None:
        self.clicks += 1


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def press(self, key: str) -> None:
        if key == "Enter":
            self.page.submitted = True

    async def insert_text(self, text: str) -> None:
        self.page.composer.text = text


class FakePage:
    """可脚本化的假页面。

    * ``snapshots``：提交后每轮轮询看到的回复正文（递增）；
    * ``generating_plan``：每轮 ``is_generating`` 的返回值（最后一项重复）。
    """

    def __init__(
        self,
        *,
        snapshots: Optional[List[str]] = None,
        generating_plan: Optional[List[bool]] = None,
        body_text: str = "",
        conversation_text: str = "",
        challenge_indicator: bool = False,
        authenticated: bool = True,
        composer_available: bool = True,
        url: str = "https://claude.ai/new",
        title_text: str = "Claude",
    ) -> None:
        self.snapshots = snapshots if snapshots is not None else ["Hello from Claude"]
        self.generating_plan = generating_plan if generating_plan is not None else [False]
        #: 页面**自身**的文本（会被挑战文案扫描）。
        self.body_text = body_text
        #: 会话正文 / 输入区文本：探针会把它剔除，不属于「站点在拦你」的证据。
        self.conversation_text = conversation_text
        #: 是否有**可见**的挑战页节点命中（隐藏的 Turnstile iframe 不算）。
        self.challenge_indicator = challenge_indicator
        self.authenticated = authenticated
        self.composer_available = composer_available
        self.url = url
        self.title_text = title_text
        self.composer = FakeElement("")
        self.keyboard = FakeKeyboard(self)
        self.submitted = False
        self.poll = 0
        self.closed = False
        self.new_chat_opened = False
        self.goto_calls: List[str] = []

    # ---- Playwright 接口子集 ----
    def is_closed(self) -> bool:
        return self.closed

    async def title(self) -> str:
        return self.title_text

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append(url)
        self.url = url

    async def close(self) -> None:
        self.closed = True

    async def query_selector_all(self, selector: str) -> List[FakeElement]:
        selector = selector.strip()
        if self._is_input(selector):
            return [self.composer] if self.composer_available else []
        if self._is_response(selector):
            return [FakeElement(text) for text in self._current_snapshots()]
        return []

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        if expression == selectors.PAGE_PROBE_JS:
            words = (arg or {}).get("challenge_text") or []
            # 真实探针只扫描**剔除了会话正文与输入区**之后的页面文本；
            # 这里用 body_text 表示那部分，conversation_text 不参与扫描。
            body = self.body_text.lower()
            matched = next((str(w).lower() for w in words if str(w).lower() in body), "")
            return {
                "url": self.url,
                "ready": "complete",
                "title": self.title_text,
                "composer": self.composer_available,
                "auth": not self.authenticated,
                "challenge": self.challenge_indicator,
                "challenge_text": bool(matched),
                "challenge_text_pattern": matched,
                "body_text_len": len(self.body_text),
                "password": not self.authenticated,
            }
        if expression == selectors.GENERATING_JS:
            value = self._generating_now()
            self.poll += 1
            return value
        if expression == selectors.BODY_TEXT_JS:
            return self.body_text
        if expression == selectors.COMPLETE_TEXT_JS:
            return getattr(arg, "text", "")
        return None

    # ---- 内部 ----
    def _current_snapshots(self) -> List[str]:
        if not self.submitted:
            return []
        index = min(self.poll, len(self.snapshots) - 1)
        return [self.snapshots[index]]

    def _generating_now(self) -> bool:
        if not self.submitted:
            return False
        index = min(self.poll, len(self.generating_plan) - 1)
        return bool(self.generating_plan[index])

    @staticmethod
    def _is_input(selector: str) -> bool:
        return "contenteditable" in selector or "textarea" in selector

    @staticmethod
    def _is_response(selector: str) -> bool:
        return "assistant" in selector or "claude-message" in selector


class FakeDriver:
    """与 :class:`~claude_web.driver.ClaudeWebDriver` 接口一致的替身。"""

    def __init__(self, page_factory: Callable[[], FakePage], *, dom: Optional[ClaudeDOMAdapter] = None) -> None:
        self.dom = dom or ClaudeDOMAdapter()
        self.pool = PagePool(closer=self._close)
        self._factory = page_factory
        self.closed_pages: List[Any] = []
        self.ready = Readiness(ok=True, browser=True, page=True, authenticated=True, composer=True)

    async def _close(self, page: Any) -> None:
        self.closed_pages.append(page)
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass

    async def page_for(self, bucket: str) -> FakePage:
        async def _create() -> FakePage:
            return self._factory()

        return await self.pool.acquire(bucket, _create)

    async def open_new_conversation(self, page: FakePage) -> None:
        page.new_chat_opened = True

    async def readiness(self) -> Readiness:
        return self.ready

    async def close(self) -> None:
        await self.pool.close_all()


def make_service(page: FakePage, tmp_path, **overrides):
    """构造一个用替身驱动的 :class:`~claude_web.service.ChatService`。"""
    from claude_web.completion.generator import ReplyGenerator
    from claude_web.completion.lifecycle import SessionLifecycle
    from claude_web.driver import ClaudeWebDriver  # noqa: F401  (类型参考)
    from claude_web.service import ChatService
    from claude_web.session.store import SessionStore
    from claude_web.tasks import TaskStore

    driver = overrides.pop("driver", None) or FakeDriver(lambda: page)
    store = overrides.pop("store", None) or SessionStore(tmp_path / "state.json")
    tasks = overrides.pop("tasks", None) or TaskStore(tmp_path / "tasks")
    config_patch = overrides.pop("locks", None)
    service = ChatService(
        driver=driver,
        generator=ReplyGenerator(driver.dom),
        lifecycle=SessionLifecycle(driver, store=store),
        tasks=tasks,
        **({"locks": config_patch} if config_patch is not None else {}),
        **overrides,
    )
    return service


__all__ = ["FakeElement", "FakeKeyboard", "FakePage", "FakeDriver", "make_service", "config"]
