"""会话生命周期（doc/design.md 4.2、5.2、5.4）。

首期默认策略是「**新会话 + 完整历史播种**」：为每个任务打开干净的新对话，把 API
请求里的完整消息历史重放进去。这样请求可重放、状态可恢复，且不依赖客户端与网页
上一次会话严格同步。

唯一的例外是用户显式 ``/link`` 绑定的会话 URL：绑定的桶直接回到那条会话
（页面重建 / 重启都回到它），不再新开对话，除非用户 ``/link --seed``。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence
from urllib.parse import urlparse

from .. import config
from ..errors import InvalidRequestError, NotReadyError, page_alive
from ..models import NormalizedMessage, ToolSpec
from ..prompting import PromptBuild, build_prompt, extract_system_text, seed_goal
from ..session.store import SessionStore, default_store

logger = logging.getLogger(__name__)

#: 明确的非会话路径（不允许绑定）。
_NON_CONVERSATION_PATHS = ("/", "/new", "/login", "/logout", "/signup")


def validate_link_url(url: str) -> str:
    """校验会话绑定 URL：必须是 HTTPS、预期官方主机、且指向会话页。

    doc/design.md 5.4：绑定后该桶的 prompt 会真的发到这条会话里，因此绝不能把
    持久化浏览器会话导航到任意网站。
    """
    raw = (url or "").strip()
    if not raw:
        raise InvalidRequestError("绑定 URL 不能为空")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise InvalidRequestError("绑定 URL 必须是 https://")
    host = (parsed.hostname or "").lower()
    if not host:
        raise InvalidRequestError("绑定 URL 缺少主机名")
    allowed = [h.strip().lower() for h in config.LINK_ALLOWED_HOSTS if h.strip()]
    if "*" not in allowed:
        ok = any(host == a or host.endswith("." + a) for a in allowed)
        if not ok:
            raise InvalidRequestError(
                "绑定 URL 的主机不在允许列表内",
                detail={"allowed": allowed},
            )
    path = (parsed.path or "").rstrip("/") or "/"
    if path.lower() in _NON_CONVERSATION_PATHS:
        raise InvalidRequestError("绑定 URL 指向的不是会话页面")
    # 去掉查询串与片段，避免把一次性参数写进会话状态。
    normalized = f"https://{host}{parsed.path.rstrip('/')}"
    return normalized


@dataclass
class Conversation:
    """一次任务使用的页面与已构建的提示词。"""

    bucket: str
    page: Any
    prompt: str
    #: 本轮是否开了新对话 / 重新导航到了绑定会话（对客户端可见的诊断）。
    created_new: bool = False
    #: 本轮是否播种了完整历史（False = 只发了增量）。
    seeded: bool = True
    #: 本轮发送的系统提示摘要（下一轮系统提示变了就重新带上它）。
    system_digest: str = ""
    #: 本轮同步进网页会话的客户端历史指纹（T1.4a，成功后记录进会话状态）。
    history_len: int = 0
    history_digest: str = ""
    linked_url: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    prompt_build: Optional[PromptBuild] = None


def _digest(text: str) -> str:
    """系统提示的短摘要（只用于比「有没有变」，不落盘、不对外暴露）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def history_fingerprint(messages: Sequence[NormalizedMessage]) -> str:
    """客户端历史的**低敏指纹**（T1.4a）：只用于「有没有变」，不暴露正文。

    对每条消息取「角色 + 正文长度 + 正文前缀摘要」，再整体做一次 SHA-256。
    不把正文写进状态文件，也不把内容回给客户端；不同历史碰撞概率可忽略。
    """
    hasher = hashlib.sha256()
    for message in messages:
        content = message.content or ""
        head = hashlib.sha256(content[:200].encode("utf-8")).hexdigest()[:8]
        hasher.update(f"{message.role}:{len(content)}:{head}\n".encode("utf-8"))
    return hasher.hexdigest()[:24]


def _conversation_key(url: str) -> Optional[tuple]:
    """把会话 URL 解析成可精确比较的键 ``(host, path)``；无法解析时返回 ``None``。

    旧实现用 ``startswith(target[:len-1])`` 做前缀比对，会把「会话 ID 只差最后一位」
    的两条会话误判成同一条（例如 ``…5d9c8d`` 与 ``…5d9c8e``），于是复用到了错误的
    会话上下文。这里改为解析 URL、按主机 + 规范化路径**精确**比较（T3.7 / D3.7）：
    忽略查询串与片段、忽略末尾斜杠，其它一律要求逐字相同。
    """
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/") or "/"
    if not host:
        return None
    return host, path


def _same_conversation(current_url: str, target_url: str) -> bool:
    """当前页面是否已经在目标会话上（解析 URL、精确比较主机 + 会话路径）。"""
    current = _conversation_key(current_url)
    target = _conversation_key(target_url)
    if current is None or target is None:
        return False
    return current == target


class SessionLifecycle:
    def __init__(self, driver: Any, store: Optional[SessionStore] = None) -> None:
        self.driver = driver
        self.store = store or default_store
        self._default_link_checked: Optional[str] = None
        self._default_link_value: Optional[str] = None
        #: bucket -> 已含本桶上下文的页面对象（内存标记；页面被换掉就失效）。
        #:
        #: 不能只看持久化的 ``has_history``：进程重启 / 页面重建后网页端上下文已经
        #: 不在了，但状态文件里的 has_history 仍是 True——那种情况下必须重新播种。
        self._seeded_page: dict = {}
        #: bucket -> 上一次成功发送时的系统提示摘要。增量模式默认不再重发系统提示，
        #: 但客户端换了系统提示（换了任务 / 换了工作目录）时必须重新带上。
        self._seeded_system: dict = {}

    def default_link_url(self) -> Optional[str]:
        """``CLAUDE_DEFAULT_LINK_URL`` 的校验结果（非法值忽略并告警一次）。

        只在默认桶生效，避免多个桶共用同一条网页会话导致内容串线。
        """
        raw = (config.DEFAULT_LINK_URL or "").strip()
        if not raw:
            return None
        if raw == self._default_link_checked:
            return self._default_link_value
        try:
            self._default_link_value = validate_link_url(raw)
        except InvalidRequestError as exc:
            logger.warning("[会话] 忽略非法的 CLAUDE_DEFAULT_LINK_URL：%s", exc.message)
            self._default_link_value = None
        self._default_link_checked = raw
        return self._default_link_value

    # ---- 准备 ----
    async def prepare(
        self,
        bucket: str,
        messages: Sequence[NormalizedMessage],
        tools: Optional[Sequence[ToolSpec]] = None,
        *,
        link_url: Optional[str] = None,
    ) -> Conversation:
        """为新任务准备页面与提示词。

        默认**复用**该桶已有的网页会话：只有当「桶上还没有历史 / 被要求重新播种 /
        页面被换掉（上下文已丢）」时才开新对话并播种完整历史，其余情况只发增量。
        （旧行为是每轮都新开对话 + 全量重放，用户侧表现就是「每发一条消息就多一个
        网页会话」，且历史越长越慢。）
        """
        state = self.store.state(bucket)
        default_url = self.default_link_url() if bucket == config.DEFAULT_SESSION_KEY else None
        target_url = link_url or state.linked_url or default_url

        page = await self.driver.page_for(bucket)
        if not page_alive(page):
            # 页面失效：换一具可用句柄（网页端上下文已丢，必须重新播种）。
            await self.driver.pool.drop(bucket)
            page = await self.driver.page_for(bucket)
            self._forget_seed(bucket)

        # 需要开新对话 / 重新播种的三种情形：
        # * 桶上还没有历史（首次使用、刚 reset、刚轮转）；
        # * 排队了一次性重新播种（/link --seed）；
        # * 页面被换掉：新页面里没有此前的上下文（内存标记与当前页面对不上）。
        need_seed = (
            not state.has_history
            or state.pending_reseed
            or self._seeded_page.get(bucket) is not page
        )

        system_digest = _digest(extract_system_text(messages))
        include_system = need_seed or self._seeded_system.get(bucket) != system_digest
        prompt_build = build_prompt(
            messages, tools=tools, seed=need_seed, include_system=include_system
        )
        notes = list(prompt_build.notes)
        if not need_seed and include_system:
            notes.append("系统提示已变化：本轮重新带上系统指令")
        # 历史一致性观测（T1.4a）：复用会话时，核对客户端历史是否**以上一次同步进去
        # 的那批历史为前缀**。对不上就说明客户端历史与网页上下文已分叉（回滚 /
        # 重新生成 / 换任务 / 压缩）。本步骤**只观测**：打日志 + notes，不改行为；
        # 等真实任务验证无误判后再切换为触发重播种（T1.4b）。
        if (
            not need_seed
            and state.history_len > 0
            and state.history_digest
            and len(messages) >= state.history_len
        ):
            current = history_fingerprint(messages[: state.history_len])
            if current != state.history_digest:
                logger.warning(
                    "[会话] bucket=%s 客户端历史与网页上下文可能已分叉"
                    "（已同步 %s 条的历史前缀指纹对不上）",
                    bucket,
                    state.history_len,
                )
                notes.append("客户端历史与网页上下文可能已分叉（仅记录，未重播种）")
        created_new = False

        if target_url:
            # 绑定模式：只在本轮需要播种、或页面根本不在那条会话上时才导航
            # （每轮都 goto 一次会把页面反复重建，也会把用户的手动滚动打断）。
            current = str(getattr(page, "url", "") or "")
            if need_seed or not _same_conversation(current, target_url):
                try:
                    await page.goto(target_url, wait_until="domcontentloaded")
                except Exception as exc:  # noqa: BLE001
                    raise NotReadyError(
                        "无法导航到绑定的会话 URL", detail={"error": type(exc).__name__}
                    ) from exc
                created_new = True
            notes.append("已进入绑定会话" + ("（播种完整历史）" if need_seed else "（复用，只发增量）"))
        elif need_seed:
            await self.driver.open_new_conversation(page)
            created_new = True
            notes.append("新对话 + 完整历史播种")
        else:
            notes.append("复用当前网页会话 + 只发增量")

        await self.driver.dom.ensure_usable(page)
        return Conversation(
            bucket=bucket,
            page=page,
            prompt=prompt_build.text,
            created_new=created_new,
            seeded=need_seed,
            system_digest=system_digest,
            history_len=len(messages),
            history_digest=history_fingerprint(messages),
            linked_url=target_url,
            notes=notes,
            prompt_build=prompt_build,
        )

    # ---- 播种标记（内存）----
    def _forget_seed(self, bucket: str) -> None:
        self._seeded_page.pop(bucket, None)
        self._seeded_system.pop(bucket, None)

    def _remember_seed(self, bucket: str, page: Any) -> None:
        """记住「这个页面上的网页会话已经含有本桶上下文」，下一轮可直接发增量。"""
        if page is not None:
            self._seeded_page[bucket] = page

    # ---- 状态更新 ----
    def note_success(
        self,
        bucket: str,
        *,
        est_tokens: int = 0,
        page: Any = None,
        system_digest: str = "",
        history_len: int = 0,
        history_digest: str = "",
    ) -> None:
        state = self.store.state(bucket)
        state.has_history = True
        state.turns += 1
        state.est_tokens += max(0, est_tokens)
        state.cap_hit = False
        state.cap_failures = 0
        state.last_error = None
        state.pending_reseed = False
        # 记录「这一轮同步进网页会话的客户端历史」指纹，供下一轮一致性观测（T1.4a）。
        if history_len:
            state.history_len = int(history_len)
            state.history_digest = history_digest or ""
        self.store.save_state(bucket, state)
        # 只有本轮真的成功发送，网页会话里才确实有了这段上下文。
        self._remember_seed(bucket, page)
        if system_digest:
            self._seeded_system[bucket] = system_digest

    def note_error(self, bucket: str, code: str) -> None:
        state = self.store.state(bucket)
        state.last_error = code
        self.store.save_state(bucket, state)

    def note_context_limit(self, bucket: str) -> None:
        """兼容 facade：处理「到顶」时的状态更新。"""
        state = self.store.state(bucket)
        state.cap_hit = True
        state.cap_failures += 1
        state.has_history = False
        state.turns = 0
        state.last_error = "context_limit_reached"
        self.store.save_state(bucket, state)

    async def reset(self, bucket: str) -> None:
        """重置桶状态并关闭其页面，下一轮重新开对话并完整播种。"""
        self.store.reset(bucket)
        self._forget_seed(bucket)
        await self.driver.pool.drop(bucket)

    # ---- 绑定 ----
    async def link(self, bucket: str, url: str, *, seed: bool = False) -> str:
        """把桶绑定到会话 URL，并校验导航落点。"""
        normalized = validate_link_url(url)
        page = await self.driver.page_for(bucket)
        try:
            await page.goto(normalized, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            raise NotReadyError(
                "导航到绑定 URL 失败", detail={"error": type(exc).__name__}
            ) from exc
        actual = str(getattr(page, "url", "") or "")
        # 落点校验同样改为精确比较（T3.7 / D3.7）：只容忍站点自身的查询串 / 末尾斜杠
        # 差异，绝不接受「另一个会话 ID」。
        if actual and not _same_conversation(actual, normalized):
            raise NotReadyError("导航落点与绑定 URL 不一致", detail={"mismatch": True})
        await self.driver.dom.ensure_usable(page)
        self.store.update(bucket, linked_url=normalized, pending_reseed=bool(seed))
        # 绑定即换了一条网页会话：本桶的上下文要重新播种（除非这条会话里已有记录，
        # 但那只有用户知道——用 --seed 显式声明；默认仍按已播种处理，等价于旧行为）。
        self._forget_seed(bucket)
        return normalized

    def unlink(self, bucket: str) -> None:
        self.store.update(bucket, linked_url=None, pending_reseed=False)
        self._forget_seed(bucket)

    def status(self, bucket: str) -> dict:
        state = self.store.state(bucket)
        return {
            "bucket": bucket,
            "linked_url": state.linked_url,
            "default_link_url": self.default_link_url() if bucket == config.DEFAULT_SESSION_KEY else None,
            "has_history": state.has_history,
            "turns": state.turns,
            "est_tokens": state.est_tokens,
            "cap_hit": state.cap_hit,
            "last_error": state.last_error,
            "pending_reseed": state.pending_reseed,
        }

    @staticmethod
    def goal(messages: Sequence[NormalizedMessage]) -> str:
        return seed_goal(messages)


__all__ = ["Conversation", "SessionLifecycle", "validate_link_url"]
