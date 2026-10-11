"""会话状态存储的兼容门面（doc/design.md 7）。

真实实现位于 :mod:`claude_web.session.store`；本模块**只做委托**，
不持有第二份状态，避免出现两个互不一致的事实来源。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .session.schema import SessionState  # noqa: F401  (re-export)
from .session.store import SessionStore, default_store, get_store

logger = logging.getLogger("claude_web.session_store")

#: 兼容别名：旧调用方按 ``session_store._state`` 读取失败时会回落到磁盘。
_state = default_store


def state(key: str) -> SessionState:
    return default_store.state(key)


def load_state(key: str) -> Dict[str, Any]:
    return default_store.state(key).to_payload()


def save_state(key: str, state_obj: SessionState) -> None:
    default_store.save_state(key, state_obj)


def update_state(key: str, **fields: Any) -> SessionState:
    return default_store.update(key, **fields)


def reset_bucket(key: str) -> SessionState:
    return default_store.reset(key)


def snapshot() -> Dict[str, Any]:
    return default_store.snapshot()


def buckets() -> List[str]:
    return default_store.buckets()


def store(path: Optional[Path] = None) -> SessionStore:
    return SessionStore(path)


__all__ = [
    "SessionStore",
    "SessionState",
    "default_store",
    "get_store",
    "state",
    "load_state",
    "save_state",
    "update_state",
    "reset_bucket",
    "snapshot",
    "buckets",
    "store",
]
