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
    def __init__(self, text: str = "", visible: bool = True, on_fill: Optional[Callable[[str], None]] = None) -> None:
        self.text = text
        self.visible = visible
        self.clicks = 0
        self._on_fill = on_fill

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        return self.text

    async def fill(self, text: str) -> None:
        self.text = text
        if self._on_fill is not None:
            self._on_fill(text)

    async def click(self) -> None:
        self.clicks += 1


class FakeKeyboard:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    async def press(self, key: str) -> None:
        if key != "Enter":
            return
        # 真实页面上 Enter 提交成功后输入区会被清空（桥的「提交到底有没有生效」判断
        # 依赖这一点）；提交没生效时提示词原样留在输入区。
        self.page.submitted = bool(self.page.submit_lands)
        if self.page.submit_lands:
            self.page.composer.text = ""

    async def insert_text(self, text: str) -> None:
        self.page.composer.text = text
        self.page.filled_texts.append(text)


class FakePage:
    """可脚本化的假页面。

    * ``snapshots``：提交后每轮轮询看到的回复正文（递增）；
    * ``generating_plan``：每轮 ``generating_state`` 的返回值（最后一项重复）；
    * ``submit_lands``：提交是否真的生效（``False`` = Enter 被忽略、提示词留在输入区）。
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
        submit_lands: bool = True,
        stop_control_plan: Optional[List[bool]] = None,
        url: str = "https://claude.ai/new",
        title_text: str = "Claude",
        user_messages: Optional[List[str]] = None,
        flat_reply_rows: Optional[int] = None,
    ) -> None:
        self.snapshots = snapshots if snapshots is not None else ["Hello from Claude"]
        self.generating_plan = generating_plan if generating_plan is not None else [False]
        #: 控件级信号（可见的停止按钮 / 全局流式标记）的计划值；缺省全为 False。
        self.stop_control_plan = stop_control_plan
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
        #: 每次写入输入区的文本（提交后输入区会被清空，断言「发了什么」看这里）。
        self.filled_texts: List[str] = []
        #: 提交是否真的生效（Enter 被忽略时页面不会新增回复）。
        self.submit_lands = submit_lands
        self.composer = FakeElement("", on_fill=self.filled_texts.append)
        self.keyboard = FakeKeyboard(self)
        self.submitted = False
        self.poll = 0
        self.closed = False
        self.new_chat_opened = False
        self.goto_calls: List[str] = []
        #: 最近一次 ``COMPLETE_TEXT_JS`` 的入参（用于断言外壳选择器确实传下去了）。
        self.last_complete_text_args: Any = None
        #: 提交前页面上已有的用户消息（提交生效后会把本轮提示词追加在后面）。
        self.user_messages: List[str] = list(user_messages or [])
        #: ``None`` = 每轮回复各占一行（正常页面行为）；设为整数 = 页面复用 / 回收行，
        #: assistant 节点条数**恒定**为该值（复现 2026-10-12 的 ``reply_not_found``：
        #: 页面上明明已经有回复，条数却始终等于基线）。
        self.flat_reply_rows = flat_reply_rows

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
            return [FakeElement(text) for text in self._reply_rows()]
        if self._is_user_message(selector):
            return [FakeElement(text) for text in self._user_rows()]
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
        if expression == selectors.GENERATING_STATE_JS:
            value = self._generating_now()
            stop_control = self._stop_control_now()
            self.poll += 1
            return {
                "streaming": value,
                "streaming_selector": (config.RESPONSE_SELECTORS or [None])[0],
                "stop_control": stop_control,
            }
        if expression == selectors.BODY_TEXT_JS:
            return self.body_text
        if expression == selectors.REPLY_STATE_JS:
            opts = arg or {}
            reply_rows = self._reply_rows()
            user_rows = self._user_rows()
            want_tail = bool(opts.get("want_tail"))
            reply_selector = (opts.get("reply") or [None])[0]
            user_selector = (opts.get("user") or [None])[0]
            return {
                "reply_selector": reply_selector if reply_rows or user_rows else None,
                "reply_count": len(reply_rows),
                "user_selector": user_selector if user_rows else None,
                "user_count": len(user_rows),
                # 假页面里回复总是排在最后一条提问之后（真实页面用文档顺序判定）。
                "trailing_reply": bool(reply_rows) and bool(user_rows),
                "last_user_tail": self._tail(user_rows[-1]) if (want_tail and user_rows) else None,
            }
        if expression == selectors.COMPLETE_TEXT_JS:
            # 真实调用形态是 ``{"node": node, "chrome": [...]}``；也容忍直接传节点。
            node = arg.get("node") if isinstance(arg, dict) else arg
            self.last_complete_text_args = arg
            return getattr(node, "text", "")
        return None

    # ---- 内部 ----
    def _current_snapshots(self) -> List[str]:
        if not self.submitted or not self.snapshots:
            return []
        index = min(self.poll, len(self.snapshots) - 1)
        return [self.snapshots[index]]

    def _reply_rows(self) -> List[str]:
        """页面上当前的 assistant 节点。

        ``flat_reply_rows`` 不为空时模拟「页面复用 / 回收行」：条数恒定，只有最后一行的
        内容是本轮的（旧行被回收）——这正是条数口径失灵的现场。
        """
        rows = self._current_snapshots()
        if self.flat_reply_rows is None:
            return rows
        pad = max(0, self.flat_reply_rows - len(rows))
        return ["(旧回复)"] * pad + rows

    def _user_rows(self) -> List[str]:
        """页面上当前的用户消息；提交生效后本轮提示词也在其中。"""
        rows = list(self.user_messages)
        if self.submitted and self.filled_texts:
            rows.append(self.filled_texts[-1])
        return rows

    @staticmethod
    def _tail(text: str) -> str:
        return " ".join(str(text).split())[-200:]

    def _generating_now(self) -> Optional[bool]:
        if not self.submitted:
            return False
        index = min(self.poll, len(self.generating_plan) - 1)
        value = self.generating_plan[index]
        return None if value is None else bool(value)

    def _stop_control_now(self) -> bool:
        if not self.stop_control_plan:
            return False
        index = min(self.poll, len(self.stop_control_plan) - 1)
        return bool(self.stop_control_plan[index])

    @staticmethod
    def _is_input(selector: str) -> bool:
        return "contenteditable" in selector or "textarea" in selector

    @staticmethod
    def _is_response(selector: str) -> bool:
        return "assistant" in selector or "claude-message" in selector

    @staticmethod
    def _is_user_message(selector: str) -> bool:
        return "user-message" in selector or '="user"' in selector


class VirtualClock:
    """虚拟时间轴：记录每次睡眠并驱动 ``time.monotonic``。

    把 ``asyncio.sleep`` 与 ``time.monotonic`` 同时指向它，就能在毫秒内断言「客户端等了
    多少秒」与「轮询间隔序列」，而不需要真的睡几秒（阈值的比较仍按真实秒数进行）。
    """

    def __init__(self) -> None:
        self.total = 0.0
        self.intervals: List[float] = []

    async def sleep(self, seconds: float) -> None:
        self.total += float(seconds)
        self.intervals.append(float(seconds))

    def monotonic(self) -> float:
        return self.total

    def install(self, monkeypatch) -> "VirtualClock":
        """把生成模块里的 ``asyncio.sleep`` / ``time.monotonic`` 换成本虚拟时间轴。"""
        import types

        from claude_web.completion import generator as generator_module

        monkeypatch.setattr(generator_module, "asyncio", types.SimpleNamespace(sleep=self.sleep))
        monkeypatch.setattr(
            generator_module, "time", types.SimpleNamespace(monotonic=self.monotonic)
        )
        return self


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


__all__ = [
    "FakeElement",
    "FakeKeyboard",
    "FakePage",
    "FakeDriver",
    "VirtualClock",
    "make_service",
    "config",
]
