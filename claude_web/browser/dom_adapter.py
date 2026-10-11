"""Claude 网页 UI 探测与 DOM 操作适配（doc/design.md 4.2、4.3、4.4）。

适配器把「页面对象」当鸭子类型使用（只依赖 ``query_selector_all`` /
``query_selector`` / ``evaluate`` / ``keyboard`` / ``goto`` 等最小接口），
因此离线测试可以用轻量替身驱动整个包，不需要真实浏览器。

所有选择器都从 :mod:`claude_web.config` **在运行时**读取，候选按顺序回退；
全部落空时抛带 ``composer_not_found`` / ``reply_extraction_failed`` 语义的异常。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple

from .. import config
from ..errors import (
    AuthenticationRequiredError,
    ChallengeRequiredError,
    ComposerNotFoundError,
    PromptSubmissionError,
    ReplyExtractionError,
)
from . import selectors
from .diagnostics import DiagnosticsMixin

logger = logging.getLogger(__name__)

#: 安全校验页的判定不走单独常量：文案特征集中在
#: :data:`claude_web.config.CHALLENGE_TEXT_PATTERNS`，标题与可见文本共用同一份
#: 口径，避免两处清单漂移。
#:
#: 为什么不能只看选择器：Cloudflare / Turnstile 的校验页标题常常就是站点名、
#: 正文里才有 "Performing security verification"，而 ``iframe`` / ``cf-challenge``
#: 这类选择器在新版页面上常常一个都不命中 —— 只看选择器会把「被安全校验挡住」
#: 误诊成「未登录 / 改版」。（本项目**只识别并如实上报**，不自动勾选 / 绕过。）


@dataclass
class PageStatus:
    """页面可观测状态（doc/design.md 4.2 的桥接层内部状态）。"""

    url: str = ""
    ready: str = ""
    authenticated: bool = True
    challenged: bool = False
    composer: bool = False
    #: 定位输入区时的命中明细（诊断用，逐条选择器的命中数）。
    composer_hits: List[str] = field(default_factory=list)
    #: 判定为挑战页的依据（``title`` / ``selector`` / ``text``），便于诊断。
    challenge_reason: Optional[str] = None
    #: 命中的具体证据：**配置里的**文案特征或选择器名（不含页面正文，可安全进日志）。
    challenge_evidence: Optional[str] = None
    #: 命中了挑战特征、但输入区可用：只作诊断提示，**不**当作挑战页报错。
    #:
    #: 这是为了修掉一个真实误报：一个（正常的）会话正文里引用了
    #: "performing security verification"，旧实现就每轮都报 challenge_required，
    #: 而用户明明能在同一个页面上正常对话。
    challenge_suspect: bool = False

    @property
    def usable(self) -> bool:
        return self.authenticated and not self.challenged and self.composer


@dataclass
class ReplySnapshot:
    """一次回复读取结果。"""

    text: str
    count: int
    selector: Optional[str] = None


@dataclass
class GeneratingState:
    """「页面是否仍在生成本轮回复」的多信号判定结果（doc/design.md 4.2、4.4）。

    两个独立信号：

    * ``streaming``：最后一条回复节点自身的 ``data-is-streaming``（页面自述；
      ``None`` = 该页面上没有这个属性，或还没有任何回复节点）；
    * ``stop_control``：控件级信号（可见的停止按钮 / 全局流式标记）。

    ``generating`` 是两者取并集的结论；两者都探不到时按「没有生成迹象」处理
    （与旧行为一致），只有探测本身抛异常才是 ``None``（未知）。
    """

    generating: Optional[bool] = None
    streaming: Optional[bool] = None
    stop_control: bool = False
    selector: Optional[str] = None

    @property
    def reason(self) -> str:
        """判定依据（日志 / 诊断用，不含页面正文）。"""
        if self.generating is None:
            return "unknown"
        if self.generating:
            return "stop_control" if self.stop_control else "reply_attr"
        return "no_signal"

    def as_detail(self) -> dict:
        return {
            "generating": self.generating,
            "streaming": self.streaming,
            "stop_control": self.stop_control,
            "selector": self.selector,
            "reason": self.reason,
        }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ClaudeDOMAdapter(DiagnosticsMixin):
    """把 Claude 网页 DOM 的读 / 写收敛到一处。"""

    # ---- 通用查找 ----
    async def _find_visible(
        self, page: Any, candidates: Iterable[str]
    ) -> Tuple[Optional[Any], List[str]]:
        hits: List[str] = []
        for selector in candidates:
            selector = str(selector).strip()
            if not selector:
                continue
            try:
                elements = await page.query_selector_all(selector)
            except Exception as exc:  # noqa: BLE001
                hits.append(f"{selector}:{type(exc).__name__}")
                continue
            hits.append(f"{selector}:{len(elements)}")
            for element in elements:
                try:
                    visible = await _maybe_await(element.is_visible())
                except Exception:  # noqa: BLE001
                    visible = True
                if visible:
                    return element, hits
        return None, hits

    # ---- 状态探测 ----
    async def page_status(self, page: Any) -> PageStatus:
        """探测页面是否已登录、是否遇到挑战、输入区是否可用。"""
        status = PageStatus()
        status.url = str(getattr(page, "url", "") or "")
        try:
            info = await page.evaluate(
                selectors.PAGE_PROBE_JS,
                {
                    "input": list(config.INPUT_SELECTORS),
                    "auth": list(config.AUTH_INDICATORS),
                    "challenge": list(config.CHALLENGE_INDICATORS),
                    "challenge_text": list(config.CHALLENGE_TEXT_PATTERNS),
                    # 扫描挑战文案时要剔除的子树：会话正文（助手回复 / 用户消息 /
                    # 会话列表）不是「站点在拦你」的证据，见 selectors.PAGE_PROBE_JS 注释。
                    "exclude": config.challenge_exclude_selectors(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DOM] 页面状态探测失败：%s", type(exc).__name__)
            info = {}
        raw_hit = False
        if isinstance(info, dict):
            status.url = str(info.get("url") or status.url)
            status.ready = str(info.get("ready") or "")
            patterns = [str(p).lower() for p in config.CHALLENGE_TEXT_PATTERNS if str(p).strip()]
            title = str(info.get("title") or "").lower()
            title_pattern = next((p for p in patterns if p and p in title), None)
            selector_hit = bool(info.get("challenge"))
            text_hit = bool(info.get("challenge_text"))
            raw_hit = selector_hit or bool(title_pattern) or text_hit
            if selector_hit:
                status.challenge_reason = "selector"
                status.challenge_evidence = "|".join(
                    str(s) for s in config.CHALLENGE_INDICATORS
                )[:120]
            elif title_pattern:
                status.challenge_reason = "title"
                status.challenge_evidence = title_pattern
            elif text_hit:
                status.challenge_reason = "text"
                # 只报**命中的配置文案**，不倒页面正文（可能是用户会话内容）。
                hit_pattern = str(info.get("challenge_text_pattern") or "")
                status.challenge_evidence = hit_pattern if hit_pattern in patterns else None
            status.authenticated = not bool(info.get("auth")) and not bool(info.get("password"))
            status.composer = bool(info.get("composer"))
        _, hits = await self._find_visible(page, config.INPUT_SELECTORS)
        status.composer_hits = hits
        if not status.composer:
            status.composer = any(entry.endswith(":1") or not entry.endswith(":0") for entry in hits)
        # 判定顺序很重要：**能打字就说明页面没被挡住**。输入区可用时，即使命中了挑战特征
        # （会话正文提到验证字样、隐藏的 Turnstile iframe 等），也只记一条诊断提示，
        # 不报 challenge_required —— 否则一次误报会让所有请求直接失败（实测事故见
        # doc/cloudfare.md 3.4）。真的被挡住（校验页）时输入区本来就不存在。
        status.challenged = raw_hit and not status.composer
        status.challenge_suspect = raw_hit and status.composer
        return status

    async def ensure_usable(self, page: Any) -> PageStatus:
        """确认页面可交互；否则抛语义化异常。"""
        status = await self.page_status(page)
        if status.challenged:
            raise ChallengeRequiredError(
                "检测到站点安全挑战，请在普通浏览器中完成验证后重试",
                detail={
                    "reason": status.challenge_reason,
                    "evidence": status.challenge_evidence,
                    "composer": status.composer,
                    "composer_hits": status.composer_hits,
                },
            )
        if not status.authenticated:
            raise AuthenticationRequiredError(
                "Claude 网页要求登录；请在持久化浏览器中手动登录一次",
                detail={"composer_hits": status.composer_hits},
            )
        if not status.composer:
            raise ComposerNotFoundError(
                "未找到可操作的输入区（页面可能改版或仍在加载）",
                detail={"composer_hits": status.composer_hits, "selector_version": selectors.SELECTOR_VERSION},
            )
        return status

    async def generating_state(self, page: Any) -> GeneratingState:
        """探测页面是否仍在生成本轮回复（多信号；探测失败返回未知）。"""
        control = list(config.GENERATING_SELECTORS)
        if selectors.GENERATING_FALLBACK not in control:
            control.append(selectors.GENERATING_FALLBACK)
        try:
            info = await page.evaluate(
                selectors.GENERATING_STATE_JS,
                {"reply": list(config.RESPONSE_SELECTORS), "control": control},
            )
        except Exception:  # noqa: BLE001
            return GeneratingState()
        if not isinstance(info, dict):
            return GeneratingState()
        raw_streaming = info.get("streaming")
        streaming = raw_streaming if isinstance(raw_streaming, bool) else None
        stop_control = bool(info.get("stop_control"))
        selector = str(info.get("streaming_selector") or "") or None
        generating = bool(stop_control or streaming is True)
        return GeneratingState(
            generating=generating,
            streaming=streaming,
            stop_control=stop_control,
            selector=selector,
        )

    async def is_generating(self, page: Any) -> Optional[bool]:
        """页面是否仍在生成；探测失败返回 ``None``（未知）。"""
        return (await self.generating_state(page)).generating

    async def detect_context_limit(self, page: Any) -> bool:
        """页面是否出现可识别的「会话到顶」提示（排除回复正文的影响）。"""
        patterns = [p.strip().lower() for p in config.CONTEXT_LIMIT_PATTERNS if p.strip()]
        if not patterns:
            return False
        try:
            # JS 侧先做一次廉价的文案预判（没命中直接返回空串），命中才剔除会话子树。
            # 剔除清单必须包含**用户消息**：实测一个引用了本仓库 .env.example 里
            # ``CONTEXT_LIMIT_PATTERNS=context limit||...`` 的会话，只剔除助手回复时
            # 会让扫描每轮命中，把正常会话误报成 context_limit_reached。
            text = await page.evaluate(
                selectors.BODY_TEXT_JS,
                {
                    "selectors": config.conversation_exclude_selectors(),
                    "patterns": patterns,
                },
            )
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(text, str) or not text:
            return False
        lowered = text.lower()
        return any(pattern in lowered for pattern in patterns)

    # ---- 输入区 ----
    async def find_composer(self, page: Any) -> Optional[Any]:
        element, _ = await self._find_visible(page, config.INPUT_SELECTORS)
        return element

    async def fill_composer(self, page: Any, text: str) -> None:
        """把文本写入输入区并**校验读回内容**。

        超长提示词一次填充未必成功，因此：重新定位输入框 → ``fill`` → 读回校验，
        失败再退化为「点击 + 键盘插入」；仍失败则抛 :class:`PromptSubmissionError`。

        退化路径前会先**清空输入区**：``insert_text`` 是插入（追加）语义，不清空
        会把同一段提示词叠成多份 —— 既让读回校验更难通过，也会在提交时把重复
        内容真的发给站点。
        """
        attempts = max(1, config.FILL_RETRIES)
        last_error: Optional[BaseException] = None
        for attempt in range(attempts):
            element = await self.find_composer(page)
            if element is None:
                if attempt == attempts - 1:
                    raise ComposerNotFoundError("填充前找不到输入区")
                await asyncio.sleep(0.3 * (attempt + 1))
                continue
            try:
                await _maybe_await(element.fill(text))
                if await self._read_back(page, text):
                    return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            # 退化路径：先清空（避免叠加），点击聚焦后用键盘插入。
            try:
                await self._clear_composer(page, element)
                await _maybe_await(element.click())
                keyboard = getattr(page, "keyboard", None)
                if keyboard is not None:
                    await _maybe_await(keyboard.insert_text(text))
                    if await self._read_back(page, text):
                        return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
            await asyncio.sleep(0.3 * (attempt + 1))
        raise PromptSubmissionError(
            "无法把提示词写入输入区（多次重试后读回仍为空/不一致）",
            detail={"attempts": attempts, "error": type(last_error).__name__ if last_error else None},
        )

    async def _clear_composer(self, page: Any, element: Any) -> None:
        """尽力清空输入区（不抛异常；失败只记 debug 日志）。"""
        try:
            await _maybe_await(element.fill(""))
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DOM] 清空输入区失败（fill）：%s", type(exc).__name__)
        try:
            await _maybe_await(element.click())
            keyboard = getattr(page, "keyboard", None)
            if keyboard is not None:
                await _maybe_await(keyboard.press("ControlOrMeta+a"))
                await _maybe_await(keyboard.press("Backspace"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DOM] 清空输入区失败（键盘）：%s", type(exc).__name__)

    #: 读回校验的探针长度（字符数）。
    READ_BACK_PROBE_CHARS = 120

    @staticmethod
    def _normalize_text(text: str) -> str:
        """把文本归一化成「单空格分隔」，用于跨 DOM 结构比较。

        输入区是 tiptap / ProseMirror：源码里的每个换行都会被渲染成独立段落，
        读回时表现为 ``\\n\\n``（实测 3944 字符的多行提示词读回 4068 字符）。
        因此忽略空白口径差异，否则**任何多行提示词都会被误判成写入失败**。
        """
        return " ".join(str(text or "").split())

    def _read_back_probes(self, wanted: str) -> List[str]:
        """从归一化文本里取等距探针（首 / 中 / 尾），用于分段校验。

        只看开头太脆：站点可能对开头做自动格式改写（``- `` 变列表、``` 变代码块），
        文本内容仍在，但严格前缀会比较失败。
        """
        size = self.READ_BACK_PROBE_CHARS
        if len(wanted) <= size:
            return [wanted]
        stride = max(1, (len(wanted) - size) // 2)
        probes: List[str] = []
        for index in range(3):
            probe = wanted[index * stride : index * stride + size].strip()
            if probe and probe not in probes:
                probes.append(probe)
        return probes or [wanted[:size]]

    async def _read_back(self, page: Any, expected: str) -> bool:
        """读回输入区内容，判断是否与期望一致（空内容视为失败）。"""
        if not self._normalize_text(expected):
            # 空提示词没有可校验内容（只用于清空场景）。
            return True
        return bool(await self.composer_contains(page, expected))

    async def composer_contains(self, page: Any, expected: str) -> Optional[bool]:
        """输入区里现在是否仍含有 ``expected`` 这段文本。

        与 :meth:`_read_back` 同一套比较口径，但**区分「输入区是空的」与「输入区不可用」**：

        * ``True``：输入区里就是这段文本（提交没生效的强证据）；
        * ``False``：输入区里没有这段文本（已被站点清空，或读回内容对不上）；
        * ``None``：输入区找不到 / 读不出内容（未知，不能作为下结论的依据）。
        """
        wanted = self._normalize_text(expected)
        if not wanted:
            return False
        element = await self.find_composer(page)
        if element is None:
            return None
        try:
            value = await _maybe_await(element.inner_text())
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(value, str):
            return None
        actual = self._normalize_text(value)
        if not actual:
            return False
        if actual.startswith(wanted):
            return True
        probes = self._read_back_probes(wanted)
        hits = sum(1 for probe in probes if probe in actual)
        return hits >= (1 if len(probes) <= 2 else len(probes) - 1)

    async def submit(self, page: Any) -> None:
        """提交当前输入：优先真实键盘 Enter，其次点击发送按钮。

        只负责「动作」，是否真的提交成功由调用方通过回复节点确认。
        """
        keyboard = getattr(page, "keyboard", None)
        if keyboard is not None:
            try:
                await _maybe_await(keyboard.press("Enter"))
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug("[DOM] Enter 提交失败：%s", type(exc).__name__)
        element, hits = await self._find_visible(page, config.SEND_BUTTON_SELECTORS)
        if element is None:
            raise PromptSubmissionError(
                "无法确认提示词已提交（既不能按 Enter，也找不到发送按钮）",
                detail={"send_hits": hits},
            )
        try:
            await _maybe_await(element.click())
        except Exception as exc:  # noqa: BLE001
            raise PromptSubmissionError(
                "点击发送按钮失败",
                detail={"error": type(exc).__name__},
            ) from exc

    # ---- 新对话 ----
    async def open_new_chat(self, page: Any) -> bool:
        """尝试通过「新对话」入口开启干净会话；找不到入口返回 False。"""
        element, _ = await self._find_visible(page, config.NEW_CHAT_SELECTORS)
        if element is None:
            return False
        try:
            await _maybe_await(element.click())
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- 回复读取 ----
    async def _reply_nodes(self, page: Any) -> Tuple[Optional[str], List[Any]]:
        """返回 (命中的选择器, 回复节点列表)；使用**首个有命中的**选择器，避免嵌套重复计数。"""
        for selector in config.RESPONSE_SELECTORS:
            selector = str(selector).strip()
            if not selector:
                continue
            try:
                nodes = await page.query_selector_all(selector)
            except Exception:  # noqa: BLE001
                continue
            if nodes:
                return selector, list(nodes)
        return None, []

    async def reply_count(self, page: Any) -> int:
        _, nodes = await self._reply_nodes(page)
        return len(nodes)

    async def read_reply_text(self, page: Any, index: int = -1) -> ReplySnapshot:
        """读取第 ``index`` 条回复的完整文本（``-1`` = 最后一条）。"""
        selector, nodes = await self._reply_nodes(page)
        if not nodes:
            raise ReplyExtractionError(
                "未找到任何 assistant 回复节点",
                detail={"selector_version": selectors.SELECTOR_VERSION},
            )
        try:
            node = nodes[index]
        except IndexError as exc:
            raise ReplyExtractionError(f"回复索引越界：{index}") from exc
        text = ""
        try:
            # 剔除页面外壳（无障碍标题 / 动作栏 / 时间戳 / 推广行）：只读整段 innerText
            # 会把「Claude responded: <整段正文>」与 “just now” 一并读成正文。
            text = await page.evaluate(
                selectors.COMPLETE_TEXT_JS,
                {"node": node, "chrome": list(config.REPLY_CHROME_SELECTORS)},
            )
        except Exception:  # noqa: BLE001
            try:
                text = await _maybe_await(node.inner_text())
            except Exception as exc:  # noqa: BLE001
                raise ReplyExtractionError(
                    "回复节点文本读取失败",
                    detail={"error": type(exc).__name__},
                ) from exc
        if not isinstance(text, str):
            text = str(text or "")
        return ReplySnapshot(text=text, count=len(nodes), selector=selector)


__all__ = ["ClaudeDOMAdapter", "GeneratingState", "PageStatus", "ReplySnapshot"]
