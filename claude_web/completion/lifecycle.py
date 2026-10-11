"""会话生命周期（doc/design.md 4.2、5.2、5.4）。

首期默认策略是「**新会话 + 完整历史播种**」：为每个任务打开干净的新对话，把 API
请求里的完整消息历史重放进去。这样请求可重放、状态可恢复，且不依赖客户端与网页
上一次会话严格同步。

唯一的例外是用户显式 ``/link`` 绑定的会话 URL：绑定的桶直接回到那条会话
（页面重建 / 重启都回到它），不再新开对话，除非用户 ``/link --seed``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence
from urllib.parse import urlparse

from .. import config
from ..errors import InvalidRequestError, NotReadyError, page_alive
from ..models import NormalizedMessage, ToolSpec
from ..prompting import PromptBuild, build_prompt, seed_goal
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
    created_new: bool = True
    linked_url: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    prompt_build: Optional[PromptBuild] = None


class SessionLifecycle:
    def __init__(self, driver: Any, store: Optional[SessionStore] = None) -> None:
        self.driver = driver
        self.store = store or default_store
        self._default_link_checked: Optional[str] = None
        self._default_link_value: Optional[str] = None

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
        """为新任务准备页面与提示词。"""
        state = self.store.state(bucket)
        default_url = self.default_link_url() if bucket == config.DEFAULT_SESSION_KEY else None
        target_url = link_url or state.linked_url or default_url

        page = await self.driver.page_for(bucket)
        if not page_alive(page):
            # 页面失效：换一具可用句柄（会话元数据仍在，可重新播种）。
            await self.driver.pool.drop(bucket)
            page = await self.driver.page_for(bucket)

        prompt_build = build_prompt(messages, tools=tools)
        notes = list(prompt_build.notes)
        created_new = True

        if target_url:
            # 绑定模式：导航到目标会话，不新开对话。
            try:
                await page.goto(target_url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                raise NotReadyError(
                    "无法导航到绑定的会话 URL", detail={"error": type(exc).__name__}
                ) from exc
            created_new = False
            notes.append("已进入绑定会话")
        else:
            await self.driver.open_new_conversation(page)
            notes.append("新对话 + 完整历史播种")

        await self.driver.dom.ensure_usable(page)
        return Conversation(
            bucket=bucket,
            page=page,
            prompt=prompt_build.text,
            created_new=created_new,
            linked_url=target_url,
            notes=notes,
            prompt_build=prompt_build,
        )

    # ---- 状态更新 ----
    def note_success(self, bucket: str, *, est_tokens: int = 0) -> None:
        state = self.store.state(bucket)
        state.has_history = True
        state.turns += 1
        state.est_tokens += max(0, est_tokens)
        state.cap_hit = False
        state.cap_failures = 0
        state.last_error = None
        state.pending_reseed = False
        self.store.save_state(bucket, state)

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
        if actual and not actual.startswith(normalized[: max(10, len(normalized) - 1)]):
            raise NotReadyError("导航落点与绑定 URL 不一致", detail={"mismatch": True})
        await self.driver.dom.ensure_usable(page)
        self.store.update(bucket, linked_url=normalized, pending_reseed=bool(seed))
        return normalized

    def unlink(self, bucket: str) -> None:
        self.store.update(bucket, linked_url=None, pending_reseed=False)

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
