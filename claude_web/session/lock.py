"""并发控制：状态文件锁 + 桶级异步锁（doc/design.md 5.1、7）。

* :data:`STATE_FILE_LOCK` 保护会话状态文件的「读改写」（跨线程）。
* :class:`BucketLockManager` 保证**同一会话桶的请求串行**，不同桶在资源允许时并行；
  等待超时后抛 :class:`~claude_web.errors.SessionBusyError`（映射为 HTTP 503），
  而不是无限排队把请求拖到客户端自己超时。
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional

from .. import config
from ..errors import SessionBusyError

#: 保护状态文件「读改写」的进程内锁。
STATE_FILE_LOCK = threading.Lock()


class _Bucket:
    """单个会话桶的运行期锁与占用计数。"""

    __slots__ = ("lock", "active", "last_used")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.active = 0
        self.last_used = time.monotonic()


class BucketLockManager:
    """桶级并发协调器。

    * ``acquire(key)`` 是异步上下文管理器：进入前等待该桶空闲；
    * 等待超过 ``BUCKET_LOCK_TIMEOUT_S`` 抛 :class:`SessionBusyError`；
    * 桶数超过 ``MAX_SESSION_BUCKETS`` 时，按最久未用回收**空闲**桶的锁
      （占用中的桶绝不回收，避免把在途任务的状态丢掉）。
    """

    def __init__(
        self,
        *,
        max_buckets: Optional[int] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._buckets: Dict[str, _Bucket] = {}
        self._max_buckets = max_buckets
        self._timeout_s = timeout_s

    @property
    def max_buckets(self) -> int:
        return self._max_buckets if self._max_buckets is not None else config.MAX_SESSION_BUCKETS

    @property
    def timeout_s(self) -> float:
        return self._timeout_s if self._timeout_s is not None else config.BUCKET_LOCK_TIMEOUT_S

    def _bucket(self, key: str) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            self._evict_if_needed(exclude=key)
            bucket = _Bucket()
            self._buckets[key] = bucket
        bucket.last_used = time.monotonic()
        return bucket

    def _evict_if_needed(self, *, exclude: str) -> None:
        limit = self.max_buckets
        if not limit or len(self._buckets) < limit:
            return
        for key in sorted(self._buckets, key=lambda k: self._buckets[k].last_used):
            if key == exclude:
                continue
            bucket = self._buckets[key]
            if bucket.active == 0 and not bucket.lock.locked():
                del self._buckets[key]
                return

    @asynccontextmanager
    async def acquire(
        self, key: str, *, timeout_s: Optional[float] = None
    ) -> AsyncIterator[None]:
        """获取桶锁；超时抛 :class:`SessionBusyError`。

        ``timeout_s`` 可为本次调用覆盖默认超时：管理类操作（reset / link / …）
        用**短超时**，桶正忙时快速返回 ``session_busy``，而不是把管理命令也排队
        上百秒。
        """
        key = key or config.DEFAULT_SESSION_KEY
        bucket = self._bucket(key)
        timeout = self.timeout_s if timeout_s is None else timeout_s
        try:
            if timeout and timeout > 0:
                await asyncio.wait_for(bucket.lock.acquire(), timeout=timeout)
            else:
                await bucket.lock.acquire()
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise SessionBusyError(
                f"会话桶 {key} 正忙，等待超过 {timeout:.0f}s",
                detail={"bucket": key, "timeout_s": timeout},
            ) from exc
        bucket.active += 1
        try:
            yield
        finally:
            bucket.active -= 1
            bucket.last_used = time.monotonic()
            bucket.lock.release()

    def active_buckets(self) -> Dict[str, int]:
        return {key: bucket.active for key, bucket in self._buckets.items()}

    def reset(self) -> None:
        """清空全部桶锁（仅测试 / 关闭时使用；调用方需确保无在途任务）。"""
        self._buckets.clear()


__all__ = ["STATE_FILE_LOCK", "BucketLockManager"]
