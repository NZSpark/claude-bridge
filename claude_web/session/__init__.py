"""会话状态、并发锁与持久化（doc/design.md 5、7）。

对外入口是 :mod:`claude_web.session.store`（以及兼容门面
:mod:`claude_web.session_store`）。本包不依赖 HTTP 与浏览器层。
"""

from .lock import STATE_FILE_LOCK
from .schema import (
    STATE_SCHEMA_VERSION,
    SessionState,
    empty_state_document,
    session_payload_only,
    validate_state_document,
)
from .store import SessionStore, default_store

__all__ = [
    "STATE_FILE_LOCK",
    "STATE_SCHEMA_VERSION",
    "SessionState",
    "empty_state_document",
    "session_payload_only",
    "validate_state_document",
    "SessionStore",
    "default_store",
]
