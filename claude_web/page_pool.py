"""页面池、LRU 与空闲回收（doc/design.md 4.1、5.1、7）。

* 页面（浏览器标签）是有成本的资源，用 ``MAX_PAGE_POOL`` 限制上限；
* 超出上限时回收**最久未用**的页面；空闲超过 ``BUCKET_IDLE_TTL_S`` 的页面也会被关闭；
* 关闭页面**不等于**丢上下文：会话元数据仍在 :mod:`claude_web.session.store` 里，
  下次打开时按需重新播种（doc/design.md 5.2）。

池子不感知 Playwright：页面由 ``factory`` 创建，关闭由 ``closer`` 回调完成，
便于离线测试注入替身。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import config

logger = logging.getLogger(__name__)


@dataclass
class _Entry:
    page: Any
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False


class PagePool:
    def __init__(
        self,
        *,
        max_pages: Optional[int] = None,
        idle_ttl_s: Optional[float] = None,
        closer: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> None:
        self._entries: Dict[str, _Entry] = {}
        self._max_pages = max_pages
        self._idle_ttl_s = idle_ttl_s
        self._closer = closer

    @property
    def max_pages(self) -> int:
        return self._max_pages if self._max_pages is not None else config.MAX_PAGE_POOL

    @property
    def idle_ttl_s(self) -> float:
        return self._idle_ttl_s if self._idle_ttl_s is not None else config.BUCKET_IDLE_TTL_S

    def get(self, bucket: str) -> Optional[Any]:
        entry = self._entries.get(bucket)
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        return entry.page

    def put(self, bucket: str, page: Any) -> None:
        self._entries[bucket] = _Entry(page=page)

    def mark_busy(self, bucket: str, busy: bool) -> None:
        entry = self._entries.get(bucket)
        if entry is not None:
            entry.busy = busy

    def buckets(self) -> List[str]:
        return sorted(self._entries.keys())

    def size(self) -> int:
        return len(self._entries)

    def _evictable(self, exclude: str) -> List[Tuple[str, _Entry]]:
        return sorted(
            ((key, entry) for key, entry in self._entries.items() if key != exclude and not entry.busy),
            key=lambda item: item[1].last_used,
        )

    async def _close_entry(self, key: str, entry: _Entry) -> None:
        self._entries.pop(key, None)
        if self._closer is None or entry.page is None:
            return
        try:
            await self._closer(entry.page)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[页面池] 关闭页面失败（bucket=%s）：%s", key, type(exc).__name__)

    async def evict_if_needed(self, *, exclude: str) -> None:
        limit = self.max_pages
        if not limit:
            return
        while len(self._entries) >= limit:
            candidates = self._evictable(exclude)
            if not candidates:
                return
            key, entry = candidates[0]
            await self._close_entry(key, entry)

    async def acquire(self, bucket: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """取页面；不存在时用 ``factory`` 创建（必要时先回收最久未用的页面）。"""
        existing = self.get(bucket)
        if existing is not None:
            return existing
        await self.evict_if_needed(exclude=bucket)
        page = await factory()
        self.put(bucket, page)
        return page

    async def reap_idle(self, now: Optional[float] = None) -> List[str]:
        """关闭空闲超时的页面，返回被回收的桶名。"""
        ttl = self.idle_ttl_s
        if not ttl:
            return []
        now = now if now is not None else time.monotonic()
        reaped: List[str] = []
        for key, entry in list(self._entries.items()):
            if entry.busy:
                continue
            if now - entry.last_used >= ttl:
                await self._close_entry(key, entry)
                reaped.append(key)
        return reaped

    async def close_all(self) -> None:
        for key, entry in list(self._entries.items()):
            await self._close_entry(key, entry)

    async def drop(self, bucket: str) -> None:
        entry = self._entries.get(bucket)
        if entry is not None:
            await self._close_entry(bucket, entry)


__all__ = ["PagePool"]
