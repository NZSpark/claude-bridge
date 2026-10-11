"""会话状态数据模型与 schema 版本（doc/design.md 5.4、7）。

只持久化**最少量必要元数据**：桶标识、轮数、体积、错误摘要与可选绑定 URL。
绝不写入登录 Cookie 或完整浏览器存储。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

#: 当前磁盘状态文档的 schema 版本。
#: v1 是 ClaudeBridge 的首版（与 ChatGPTBridge 的 v2 编号无关，各自独立演进）。
STATE_SCHEMA_VERSION = 1

#: 会话桶 payload 中允许持久化的字段（其余字段加载时丢弃）。
_SESSION_FIELDS = frozenset(
    {
        "has_history",
        "turns",
        "est_tokens",
        "cap_hit",
        "cap_failures",
        "last_error",
        "updated_at",
        "linked_url",
        "pending_reseed",
    }
)


def empty_state_document() -> Dict[str, Any]:
    return {"schema_version": STATE_SCHEMA_VERSION, "sessions": {}}


def validate_state_document(data: Dict[str, Any]) -> Dict[str, Any]:
    """校验并规范化一个 v1 状态文档（不改动会话 payload 语义）。"""
    if not isinstance(data, dict):
        raise ValueError("session state root must be an object")
    version = data.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        raise ValueError(f"unsupported session state schema_version: {version!r}")
    sessions = data.get("sessions")
    if sessions is None:
        data["sessions"] = {}
    elif not isinstance(sessions, dict):
        raise ValueError("session state sessions must be an object")
    else:
        data["sessions"] = {
            key: value
            for key, value in sessions.items()
            if isinstance(key, str) and isinstance(value, dict)
        }
    return data


def session_payload_only(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: payload[key] for key in _SESSION_FIELDS if key in payload}


@dataclass
class SessionState:
    """单个会话桶的状态。"""

    has_history: bool = False
    turns: int = 0
    est_tokens: int = 0
    cap_hit: bool = False
    # 连续「到顶」失败次数：累计到阈值后改用更小的播种预算，避免死循环。
    cap_failures: int = 0
    last_error: Optional[str] = None
    updated_at: int = 0
    # 用户通过 /session/link 绑定的网页会话 URL（None = 未绑定）。
    linked_url: Optional[str] = None
    # 排队的一次性「下一轮重新播种」。
    pending_reseed: bool = False

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "SessionState":
        state = cls()
        if not isinstance(payload, dict):
            return state
        for name in ("has_history", "cap_hit", "pending_reseed"):
            if name in payload:
                setattr(state, name, bool(payload.get(name)))
        for name in ("turns", "est_tokens", "updated_at", "cap_failures"):
            try:
                setattr(state, name, int(payload.get(name) or 0))
            except (TypeError, ValueError):
                setattr(state, name, 0)
        last_error = payload.get("last_error")
        state.last_error = last_error if isinstance(last_error, str) else None
        linked = payload.get("linked_url")
        state.linked_url = linked.strip() if isinstance(linked, str) and linked.strip() else None
        return state
