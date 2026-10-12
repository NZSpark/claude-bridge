"""会话状态的持久化与内存缓存（doc/design.md 5.4、7）。

设计要点：

* **单一事实来源**：状态只在这里读写；兼容门面 :mod:`claude_web.session_store`
  只做委托，不形成第二份实现。
* **文件级串行**：整文件读改写由 :data:`~claude_web.session.lock.STATE_FILE_LOCK`
  保护，避免并发写互相覆盖。
* **内存缓存有界**：缓存上限 ``MAX_SESSION_STATE_CACHE``（超限按最久未用逐出）；
  状态本就落盘，逐出内存副本是安全的。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import config
from .lock import STATE_FILE_LOCK
from .migration import migrate_state_document
from .schema import (
    SessionState,
    empty_state_document,
    session_payload_only,
    validate_state_document,
)

logger = logging.getLogger("claude_web.session_store")


class SessionStore:
    """会话桶元数据的持久化存储。"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._explicit_path = Path(path) if path is not None else None
        self._cache: Dict[str, SessionState] = {}
        self._cache_order: List[str] = []
        self._warned_bad_state = False

    # ---- 路径 ----
    @property
    def path(self) -> Path:
        return self._explicit_path or Path(config.SESSION_FILE)

    # ---- 文档读写 ----
    def load_document(self) -> Dict[str, Any]:
        """读取整个状态文档；文件缺失 / 损坏时返回空文档（并只告警一次）。"""
        path = self.path
        with STATE_FILE_LOCK:
            if not path.exists():
                return empty_state_document()
            try:
                raw = path.read_text(encoding="utf-8")
                data = json.loads(raw) if raw.strip() else {}
            except Exception as exc:  # noqa: BLE001
                if not self._warned_bad_state:
                    logger.warning("[会话] 状态文件损坏，将按空状态继续：%s", exc)
                    self._warned_bad_state = True
                return empty_state_document()
        try:
            migrated = migrate_state_document(data)
        except ValueError as exc:
            if not self._warned_bad_state:
                logger.warning("[会话] 状态文件版本不受支持，将按空状态继续：%s", exc)
                self._warned_bad_state = True
            return empty_state_document()
        # 发生迁移时立刻把新格式写回，避免每次启动都重复迁移。
        if migrated != data:
            self.save_document(migrated)
        return migrated

    def save_document(self, document: Dict[str, Any]) -> None:
        document = validate_state_document(dict(document))
        path = self.path
        with STATE_FILE_LOCK:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp.replace(path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[会话] 状态文件写入失败：%s", exc)

    # ---- 单桶状态 ----
    def state(self, key: str) -> SessionState:
        """取某个桶的状态；内存未命中时从磁盘恢复。"""
        key = key or config.DEFAULT_SESSION_KEY
        cached = self._cache.get(key)
        if cached is not None:
            self._touch(key)
            return cached
        document = self.load_document()
        payload = (document.get("sessions") or {}).get(key) or {}
        state = SessionState.from_payload(payload)
        self._remember(key, state)
        return state

    def save_state(self, key: str, state: SessionState) -> None:
        key = key or config.DEFAULT_SESSION_KEY
        state.updated_at = int(time.time())
        document = self.load_document()
        sessions = document.get("sessions") or {}
        sessions[key] = session_payload_only(state.to_payload())
        document["sessions"] = sessions
        self.save_document(document)
        self._remember(key, state)

    def update(self, key: str, **fields: Any) -> SessionState:
        """局部更新某个桶的状态字段并落盘。"""
        state = self.state(key)
        for name, value in fields.items():
            if hasattr(state, name):
                setattr(state, name, value)
        self.save_state(key, state)
        return state

    def reset(self, key: str) -> SessionState:
        """重置某个桶的**对话状态**（下一轮重新开对话并完整播种）。

        刻意**保留**会话绑定（``linked_url``）与 ``pending_reseed``：reset 的语义是
        「换一条新网页会话重新播种」，不是「解除绑定」。旧实现新建一个空
        :class:`SessionState`，会顺手把 ``linked_url`` 清掉，于是绑定桶 reset 后
        **悄悄变成未绑定**——用户以为还钉在那条会话上。要解绑请用 :meth:`update`
        显式置空（``/session/unlink`` 或 ``/bridge unlink``）。
        """
        current = self.state(key)
        state = SessionState(
            has_history=False,
            turns=0,
            est_tokens=0,
            cap_hit=False,
            linked_url=current.linked_url,
            pending_reseed=current.pending_reseed,
        )
        self.save_state(key, state)
        return state

    def snapshot(self) -> Dict[str, Any]:
        """诊断用快照（不含正文，只含元数据）。"""
        document = self.load_document()
        sessions = {
            key: session_payload_only(value)
            for key, value in (document.get("sessions") or {}).items()
            if isinstance(value, dict)
        }
        return {"schema_version": document.get("schema_version"), "sessions": sessions}

    def buckets(self) -> List[str]:
        return sorted((self.load_document().get("sessions") or {}).keys())

    def _remember(self, key: str, state: SessionState) -> None:
        self._cache[key] = state
        self._touch(key)
        limit = config.MAX_SESSION_STATE_CACHE
        if limit and len(self._cache_order) > limit:
            evicted = self._cache_order.pop(0)
            if evicted != key:
                self._cache.pop(evicted, None)

    def _touch(self, key: str) -> None:
        if key in self._cache_order:
            self._cache_order.remove(key)
        self._cache_order.append(key)


#: 进程级默认存储。
default_store = SessionStore()


def get_store() -> SessionStore:
    return default_store
