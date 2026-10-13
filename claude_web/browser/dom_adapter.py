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
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, List, Optional, Tuple

from .. import config
from ..errors import (
    AuthenticationRequiredError,
    ChallengeRequiredError,
    ComposerNotFoundError,
    NotReadyError,
    PromptSubmissionError,
    ReplyExtractionError,
    is_page_lost_error,
    is_timeout_error,
    page_lost_reason,
)
from . import selectors
from .diagnostics import DiagnosticsMixin

logger = logging.getLogger(__name__)

#: 用户消息身份指纹保留的字符数（只做相等比较，不落盘、不进日志）。
_USER_TAIL_CHARS = 200

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


@dataclass
class ModalState:
    """「额度用尽 / 升级」类弹窗的探测与关闭结果（doc/design.md 4.3）。

    * ``found``：本次探测是否发现了这类弹窗（与是否成功关掉无关）；
    * ``dismissed``：是否已经把它关掉；
    * ``present``：关闭尝试之后它是否仍在页面上；
    * ``reason``：``none`` / ``closed`` / ``no_close_control`` / ``close_failed`` /
      ``disabled`` / ``probe_failed``（诊断用，不回显页面正文）。

    只含布尔、命中特征与文本长度，不含页面正文。
    """

    found: bool = False
    dismissed: bool = False
    present: bool = False
    reason: str = "none"
    pattern: Optional[str] = None
    selector: Optional[str] = None
    close_kind: Optional[str] = None
    text_len: int = 0

    @property
    def quota_hit(self) -> bool:
        """是否真的出现了「额度用尽 / 升级」弹窗（用于把失败如实归类）。"""
        return self.found or self.present

    def as_detail(self) -> dict:
        return {
            "found": self.found,
            "dismissed": self.dismissed,
            "present": self.present,
            "reason": self.reason,
            "pattern": self.pattern,
            "close_kind": self.close_kind,
        }


@dataclass
class ReplyState:
    """页面上「回复 / 提问」的结构状态（doc/design.md 4.4）。

    * ``reply_count``：assistant 节点条数（原来的唯一口径）；
    * ``user_count``：user 消息条数；
    * ``trailing_reply``：最后一条 assistant 节点是否排在最后一条 user 消息之后
      （= 页面已经在回答眼下最后那条提问）；
    * ``last_user_tail``：最后一条 user 消息的文本尾巴（身份指纹，仅用于相等比较）。

    条数口径单独不够用：真实页面上新增本轮回复时可能同时回收 / 复用旧行，
    条数会停在基线上。结构信号与条数是**两把独立的钥匙**，任一成立即可判定有新回复。
    """

    reply_count: int = 0
    reply_selector: Optional[str] = None
    user_count: int = 0
    user_selector: Optional[str] = None
    trailing_reply: bool = False
    last_user_tail: Optional[str] = None

    def as_detail(self) -> dict:
        """诊断用摘要（只含计数与布尔，不含页面正文）。"""
        return {
            "reply_count": self.reply_count,
            "user_count": self.user_count,
            "trailing_reply": self.trailing_reply,
            "reply_selector": self.reply_selector,
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
                # 区分两类失败（D6.3）：
                #   * 页面已死（标签关闭 / 渲染进程崩溃）→ 不再试下一个选择器：那只会让
                #     每个候选都白等一次超时，最终错误也更模糊。立刻抛 NotReadyError；
                #   * 仅仅「等超时」→ 定位元素时只有它算「没命中」，记 ``:timeout``
                #     后继续试下一个候选（其它异常按类型名记诊断）。
                if is_page_lost_error(exc):
                    raise NotReadyError(
                        page_lost_reason(page),
                        detail={"selector": selector, "error": type(exc).__name__},
                    ) from exc
                reason = "timeout" if is_timeout_error(exc) else type(exc).__name__
                hits.append(f"{selector}:{reason}")
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

    async def stop_generation(self, page: Any) -> bool:
        """点击**已确认的停止控件**以中断页面当前的生成（T2.7）。

        只在探测到 ``generating_state`` 为真时调用；找不到控件返回 False。安全约束
        与额度弹窗一致：只点「停止」类控件，绝不点其它按钮。失败不抛异常。
        """
        element, _ = await self._find_visible(page, config.GENERATING_SELECTORS)
        if element is None:
            return False
        try:
            await _maybe_await(element.click())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DOM] 点击停止控件失败：%s", type(exc).__name__)
            return False

    async def settle_before_submit(self, page: Any, wait_s: float) -> None:
        """提交前确保页面不在生成中（T2.7）。

        上一轮被取消 / 超时后页面可能仍在生成；此时提交新提示词，Enter 可能被站点忽略，
        随后被误报成「提交未生效」。做法：先等 ``wait_s``，仍未结束就点一次停止控件。
        **探测失败（未知）按「可提交」处理**，不阻塞正常请求。``wait_s <= 0`` 时
        完全跳过（连探测都不做）：这是本能力的总开关，也让既有轮询时序不受影响。
        """
        if wait_s <= 0:
            return
        state = await self.generating_state(page)
        if state.generating is not True:
            return
        if wait_s > 0:
            await asyncio.sleep(wait_s)
            state = await self.generating_state(page)
            if state.generating is not True:
                return
        stopped = await self.stop_generation(page)
        logger.info("[生成] 提交前页面仍在生成：%s", "已点停止控件" if stopped else "未找到停止控件")
        await asyncio.sleep(min(wait_s, 1.0))

    # ---- 挡路弹窗（额度用尽 / 升级）----
    async def limit_modal_state(self, page: Any) -> ModalState:
        """探测「额度用尽 / 升级」类弹窗（只回布尔、命中特征与文本长度）。

        探到就在它内部挑一个**关闭类**控件打上标记（见 ``selectors.MODAL_MARKER_ATTR``），
        让调用方能用真实点击关掉它。探测失败按「没有弹窗」处理（与旧行为一致）。
        """
        try:
            info = await page.evaluate(
                selectors.MODAL_PROBE_JS,
                {
                    "container": list(config.MODAL_CONTAINER_SELECTORS),
                    "close": list(config.MODAL_CLOSE_SELECTORS),
                    "close_text": list(config.MODAL_CLOSE_TEXT_PATTERNS),
                    "text": list(config.MODAL_TEXT_PATTERNS),
                    "marker": selectors.MODAL_MARKER_ATTR,
                },
            )
        except Exception:  # noqa: BLE001
            return ModalState(reason="probe_failed")
        if not isinstance(info, dict):
            return ModalState(reason="probe_failed")
        found = bool(info.get("present"))
        return ModalState(
            found=found,
            present=found,
            reason="none" if not found else ("close_ready" if info.get("close") else "no_close_control"),
            pattern=str(info.get("pattern") or "") or None,
            selector=str(info.get("selector") or "") or None,
            close_kind=str(info.get("close_kind") or "") or None,
            text_len=int(info.get("text_len") or 0),
        )

    async def dismiss_limit_modal(self, page: Any) -> ModalState:
        """关掉盖住输入区的「额度用尽 / 升级」弹窗（默认开启，可用配置关闭）。

        为什么需要：额度用尽后站点会弹出 ``role="dialog"`` 的升级提示，它盖住输入区并
        抢走焦点。此时提示词**写进去了**但 Enter 不生效、回复也不出现，桥接随即误报
        「提交未生效 / 生成停滞」——用户看到的是「网页明明没问题，桥却说生成停滞」。

        安全约束（doc/design.md 1.2、4.3）：

        * 只点**关闭 / 先不用**类控件，绝不点升级、购买、订阅等任何按钮；
        * 只处理「可见容器 + 命中配置文案」的弹窗，普通对话框一律不碰；
        * 任何一步失败都不抛异常（关不了弹窗不是请求失败的理由），由调用方看
          :class:`ModalState` 决定如何如实上报。
        """
        state = await self.limit_modal_state(page)
        if not state.found:
            return state
        logger.info(
            "[弹窗] 检测到额度/升级弹窗（命中 %s，容器 %s，关闭控件 %s）",
            state.pattern,
            state.selector,
            state.close_kind or "无",
        )
        if not config.MODAL_DISMISS_ENABLED:
            return replace(state, reason="disabled")
        if state.close_kind is not None:
            await self._click_modal_close(page)
        else:
            await self._press_escape(page)
        after = await self.limit_modal_state(page)
        if after.found:
            # 点击没生效（或只找到「先不用」但站点没关）：退化到 Escape。
            await self._press_escape(page)
            after = await self.limit_modal_state(page)
        if after.found:
            logger.warning("[弹窗] 额度/升级弹窗仍然在页面上（%s）", after.reason)
            return replace(after, found=True, dismissed=False, reason="close_failed")
        logger.info("[弹窗] 已关闭额度/升级弹窗（命中 %s）", state.pattern)
        return replace(state, found=True, dismissed=True, present=False, reason="closed")

    async def _click_modal_close(self, page: Any) -> bool:
        """点击探针标出的关闭控件（真实点击；失败返回 False）。"""
        try:
            element = await page.query_selector(f'[{selectors.MODAL_MARKER_ATTR}="1"]')
        except Exception:  # noqa: BLE001
            return False
        if element is None:
            return False
        try:
            await _maybe_await(element.click())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[弹窗] 点击关闭控件失败：%s", type(exc).__name__)
            return False

    async def _press_escape(self, page: Any) -> bool:
        """按 Escape（关闭控件的退化路径）；失败返回 False。"""
        keyboard = getattr(page, "keyboard", None)
        if keyboard is None:
            return False
        try:
            await _maybe_await(keyboard.press("Escape"))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[弹窗] Escape 关闭失败：%s", type(exc).__name__)
            return False

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
        # 整个填充循环受 FILL_TIMEOUT_MS 约束（T6.2 / D6.2）：此前该配置声明了却
        # 从未被读，填充卡住时只能等更上层的超时，错误归因也更模糊。
        try:
            return await asyncio.wait_for(
                self._fill_with_retries(page, text, attempts), timeout=config.FILL_TIMEOUT_MS / 1000.0
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise PromptSubmissionError(
                "写入输入区超时",
                detail={"attempts": attempts, "timeout_ms": config.FILL_TIMEOUT_MS},
            ) from exc

    async def _fill_with_retries(self, page: Any, text: str, attempts: int) -> None:
        """``fill_composer`` 的重试主体（独立出来便于整体加超时）。"""
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

    async def reply_state(self, page: Any, *, want_tail: bool = False) -> ReplyState:
        """一次页面内查询拿到「条数 + 结构信号」（doc/design.md 4.4）。

        探针本身失败时**退化**为原来的逐选择器计数（结构信号按未知处理），
        这样旧页面 / 替身页面上的行为与改造前一致，不会把探测失败变成新的失败模式。
        """
        try:
            info = await page.evaluate(
                selectors.REPLY_STATE_JS,
                {
                    "reply": list(config.RESPONSE_SELECTORS),
                    "user": list(config.USER_MESSAGE_SELECTORS),
                    "want_tail": bool(want_tail),
                    "tail_chars": _USER_TAIL_CHARS,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DOM] 回复结构探测失败：%s", type(exc).__name__)
            info = None
        if isinstance(info, dict):
            try:
                tail = info.get("last_user_tail")
                return ReplyState(
                    reply_count=int(info.get("reply_count") or 0),
                    reply_selector=info.get("reply_selector"),
                    user_count=int(info.get("user_count") or 0),
                    user_selector=info.get("user_selector"),
                    trailing_reply=bool(info.get("trailing_reply")),
                    last_user_tail=None if tail is None else str(tail),
                )
            except (TypeError, ValueError) as exc:  # noqa: BLE001
                logger.debug("[DOM] 回复结构探测返回异常值：%s", type(exc).__name__)
        return ReplyState(reply_count=await self.reply_count(page))

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


__all__ = [
    "ClaudeDOMAdapter",
    "GeneratingState",
    "ModalState",
    "PageStatus",
    "ReplySnapshot",
    "ReplyState",
]
