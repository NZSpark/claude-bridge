"""会话状态文档迁移：legacy（无版本号 / v0）-> v1。

迁移失败 / 不支持版本时抛 :class:`ValueError`，由 store 层捕获并按空状态继续。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from .schema import STATE_SCHEMA_VERSION, validate_state_document

logger = logging.getLogger("claude_web.session_store")


def migrate_state_document(data: Dict[str, Any]) -> Dict[str, Any]:
    """把无版本号 / v0 文档迁移为 schema v1。"""
    if not isinstance(data, dict):
        raise ValueError("session state root must be an object")
    version = data.get("schema_version")
    legacy_version = data.get("version")
    if version == STATE_SCHEMA_VERSION:
        return validate_state_document(dict(data))
    if version not in (None, 0) or legacy_version not in (None, 0):
        raise ValueError(
            f"unsupported session state schema marker: {version!r}/{legacy_version!r}"
        )

    migrated = dict(data)
    migrated.pop("version", None)
    migrated["schema_version"] = STATE_SCHEMA_VERSION
    sessions = migrated.get("sessions")
    migrated["sessions"] = dict(sessions) if isinstance(sessions, dict) else {}
    logger.info("会话状态文件已迁移到 schema v%d。", STATE_SCHEMA_VERSION)
    return validate_state_document(migrated)
