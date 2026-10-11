"""消息归一化与提示词构造（doc/design.md 3.2、5.2、5.3）。

首期采用「新会话 + 完整历史播种」策略：每次任务都把 API 请求里的完整消息历史
转写成 Claude 网页输入框可提交的文本，因此这里必须：

* 把 ``system`` / ``developer`` 归一化为**显式标记的前缀**（不声称 API 原生语义）；
* 明确标记每条消息的角色边界（``[user]`` / ``[assistant]`` / ``[tool result]``）；
* 在独立字符预算内裁剪历史（优先保留当前任务与最新轮次，且绝不丢弃最后一条 user 消息）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from . import config
from .errors import InvalidRequestError, UnsupportedInputError
from .models import ChatMessage, NormalizedMessage, ToolSpec

#: 归一化后允许的内部角色。
_INTERNAL_ROLES = ("system", "user", "assistant", "tool")

#: 允许的文本内容分片类型（其余一律按不支持处理）。
_TEXT_PART_TYPES = {"text", "input_text", "output_text"}

_ROLE_MARKERS = {
    "system": "[system]",
    "user": "[user]",
    "assistant": "[assistant]",
    "tool": "[tool result]",
}

_PREAMBLE = (
    "[桥接说明] 下面是一段对话历史的完整重放（由本地桥接层从 API 请求重建）。"
    "请把它视为已经发生的上下文，不要复述历史，直接针对最后一条 [user] 消息作答。"
)


def content_to_text(content: Any) -> str:
    """把字符串或内容分片数组归一化为纯文本。

    不支持的片段类型（图片 / 音频 / 文件）抛 :class:`UnsupportedInputError`，
    而不是静默丢弃（doc/design.md 1.2：首期不承诺支持这些输入）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, dict):
        return content_to_text(content.get("text", content))
    if isinstance(content, (list, tuple)):
        pieces: List[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
                continue
            if not isinstance(part, dict):
                raise UnsupportedInputError(f"不支持的消息内容分片类型：{type(part).__name__}")
            part_type = str(part.get("type") or "text")
            if part_type not in _TEXT_PART_TYPES:
                raise UnsupportedInputError(f"当前版本不支持内容类型：{part_type}")
            pieces.append(str(part.get("text") or ""))
        return "".join(pieces)
    raise UnsupportedInputError(f"不支持的消息内容类型：{type(content).__name__}")


def _tool_calls_to_payload(tool_calls: Any) -> List[dict]:
    payload: List[dict] = []
    for call in tool_calls or []:
        if isinstance(call, dict):
            fn = call.get("function") or {}
            payload.append(
                {
                    "id": call.get("id"),
                    "name": fn.get("name") if isinstance(fn, dict) else None,
                    "arguments": fn.get("arguments") if isinstance(fn, dict) else None,
                }
            )
        else:  # pydantic ToolCall
            payload.append(
                {
                    "id": getattr(call, "id", None),
                    "name": getattr(getattr(call, "function", None), "name", None),
                    "arguments": getattr(getattr(call, "function", None), "arguments", None),
                }
            )
    return payload


def normalize_messages(messages: Sequence[ChatMessage]) -> List[NormalizedMessage]:
    """把 OpenAI 请求消息归一化为内部统一表示。

    * ``system`` / ``developer`` -> ``system``；
    * ``function``（legacy）-> ``tool``；
    * 未知角色抛 :class:`InvalidRequestError`；
    * 不支持的内容类型抛 :class:`UnsupportedInputError`。
    """
    if not messages:
        raise InvalidRequestError("messages 不能为空")

    normalized: List[NormalizedMessage] = []
    for raw in messages:
        role = str(getattr(raw, "role", "") or "").strip().lower()
        if role in ("system", "developer"):
            role = "system"
        elif role == "function":
            role = "tool"
        if role not in _INTERNAL_ROLES:
            raise InvalidRequestError(f"不支持的消息角色：{getattr(raw, 'role', None)!r}")
        normalized.append(
            NormalizedMessage(
                role=role,
                content=content_to_text(getattr(raw, "content", None)),
                name=getattr(raw, "name", None),
                tool_calls=_tool_calls_to_payload(getattr(raw, "tool_calls", None)),
                tool_call_id=getattr(raw, "tool_call_id", None),
            )
        )
    return normalized


def extract_system_text(messages: Sequence[NormalizedMessage]) -> str:
    """把 system / developer 内容合并成前缀文本（受 ``SYSTEM_MAX_CHARS`` 约束）。"""
    parts = [m.content for m in messages if m.role == "system" and m.content.strip()]
    if not parts:
        return ""
    text = "\n\n".join(parts)
    limit = config.SYSTEM_MAX_CHARS
    if limit and len(text) > limit:
        head = text[: max(0, limit - 20)]
        text = head + "\n…（system 提示已截断）"
    return text


def estimate_tokens(text: str) -> int:
    """保守的 token 估算（约 3 字符/token，混排中英文时偏保守）。

    仅用于预算与客户端兼容，**不对外宣称是精确 token 数**（doc/design.md 3.3）。
    """
    return max(0, len(text) // 3)


def _render_message(message: NormalizedMessage) -> str:
    marker = _ROLE_MARKERS.get(message.role, f"[{message.role}]")
    body = message.content or ""
    if message.role == "assistant" and message.tool_calls:
        calls = json.dumps(message.tool_calls, ensure_ascii=False)
        body = (body + "\n" if body else "") + f"[发起工具调用] {calls}"
    if message.role == "tool":
        limit = config.TOOL_RESULT_MAX_CHARS
        if limit and len(body) > limit:
            body = body[:limit] + f"\n…（工具结果已截断，原长 {len(body)} 字符）"
    return f"{marker}\n{body}"


@dataclass
class PromptBuild:
    """一次播种的结果与诊断（截断情况必须对外可见）。"""

    text: str
    truncated: bool = False
    included_messages: int = 0
    total_messages: int = 0
    est_tokens: int = 0
    notes: List[str] = field(default_factory=list)


def build_prompt(
    messages: Sequence[NormalizedMessage],
    *,
    tools: Optional[Sequence[ToolSpec]] = None,
    include_preamble: bool = True,
    extra_sections: Optional[Sequence[str]] = None,
) -> PromptBuild:
    """把归一化消息构建为网页输入框文本。

    预算策略（doc/design.md 5.3）：先扣除 system 前缀与工具说明，再从最新往最旧
    累加历史；**最后一条 user 消息永远完整保留**，即使因此超出预算（并在 notes 里标注）。
    """
    system_text = extract_system_text(messages)
    tool_text = ""
    if tools:
        from .toolcalls import format_tools_instruction

        tool_text = format_tools_instruction(tools)

    budget = config.CLAUDE_MAX_CONTEXT_CHARS
    fixed_parts: List[str] = []
    if include_preamble:
        fixed_parts.append(_PREAMBLE)
    if system_text:
        fixed_parts.append(_ROLE_MARKERS["system"] + "\n" + system_text)
    fixed = "\n\n".join(fixed_parts)
    trailer_parts: List[str] = []
    if tool_text:
        trailer_parts.append(tool_text)
    trailer_parts.extend(extra_sections or [])
    trailer = "\n\n".join(trailer_parts)

    reserved = len(fixed) + len(trailer) + 2
    history = [m for m in messages if m.role != "system"]

    # 从最新一条往前累加，直到预算用尽。
    rendered: List[str] = []
    used = 0
    truncated = False
    indexed = list(enumerate(history))
    newest_user_index = max(
        (i for i, m in indexed if m.role == "user"),
        default=(len(history) - 1 if history else -1),
    )
    for idx, message in reversed(indexed):
        block = _render_message(message)
        block_len = len(block) + 2
        is_last_user = idx == newest_user_index
        if not is_last_user and budget and (reserved + used + block_len) > budget:
            truncated = True
            break
        rendered.append(block)
        used += block_len
    rendered.reverse()

    # 预算对「最新一条 user」不设上限，但若它自身就超过预算需在 notes 里说明。
    if newest_user_index >= 0:
        newest_block_len = len(_render_message(history[newest_user_index])) + 2
        if budget and reserved + newest_block_len > budget:
            truncated = True

    included = len(rendered)
    body = "\n\n".join(rendered)
    text = "\n\n".join(part for part in (fixed, body, trailer) if part)
    notes: List[str] = []
    if truncated:
        notes.append(f"上下文已裁剪：保留最近 {included}/{len(history)} 条历史消息")
    return PromptBuild(
        text=text,
        truncated=truncated,
        included_messages=included,
        total_messages=len(history),
        est_tokens=estimate_tokens(text),
        notes=notes,
    )


def normalize_tools(tools: Optional[Sequence[Any]]) -> List[ToolSpec]:
    """把 OpenAI ``tools`` 数组归一化为 :class:`ToolSpec` 列表。"""
    specs: List[ToolSpec] = []
    for entry in tools or []:
        if not isinstance(entry, dict):
            continue
        fn = entry.get("function") if entry.get("type") == "function" else entry
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        params = fn.get("parameters")
        specs.append(
            ToolSpec(
                name=name,
                description=str(fn.get("description") or ""),
                parameters=params if isinstance(params, dict) else {},
            )
        )
    return specs


def seed_goal(messages: Sequence[NormalizedMessage]) -> str:
    """提取任务目标（最后一条 user 消息），用于任务快照（doc/design.md 9.3）。"""
    for message in reversed(list(messages)):
        if message.role == "user" and message.content.strip():
            text = message.content.strip()
            limit = config.TASK_GOAL_MAX_CHARS
            return text[:limit] if limit else text
    return ""


def delta_piece(previous: str, current: str) -> Tuple[str, Optional[str]]:
    """计算流式增量。

    返回 ``(piece, reset_to)``：

    * 正常前缀增长：``(新增文本, None)``；
    * 内容被重写（非前缀变化）：``("", 新的完整文本)``，调用方应重置已发送缓冲；
    * 无变化：``("", None)``。
    """
    if current == previous:
        return "", None
    if current.startswith(previous):
        return current[len(previous):], None
    # 非前缀增长：无法可靠表达为增量（引用折叠 / 代码块调整），要求重置。
    return "", current
