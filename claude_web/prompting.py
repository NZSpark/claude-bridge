"""消息归一化与提示词构造（doc/design.md 3.2、5.2、5.3）。

首期采用「新会话 + 完整历史播种」策略：每次任务都把 API 请求里的完整消息历史
转写成 Claude 网页输入框可提交的文本，因此这里必须：

* 把 ``system`` / ``developer`` 归一化为**显式标记的前缀**（不声称 API 原生语义）；
* 标记每条消息的角色边界（``[系统指令]`` / ``[你之前的回复]`` / ``[工具执行结果]``）；
* 在独立字符预算内裁剪历史（优先保留当前任务与最新轮次，且绝不丢弃最后一条 user 消息）。

角色标签刻意采用**对话口吻而非对称标记**（参考 ``deepseek-bridge`` 的 prompting.py）：
用户说的话原样给出、不套 ``[user]``，模型自己的历史回复标为 ``[你之前的回复]``。
这更接近模型在网页对话界面里习惯的语境，也减少它把标记当成待复述内容的概率。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

from . import config
from .errors import InvalidRequestError, UnsupportedInputError
from .models import ChatMessage, NormalizedMessage, ToolSpec

#: 归一化后允许的内部角色。
_INTERNAL_ROLES = ("system", "user", "assistant", "tool")

#: 允许的文本内容分片类型（其余一律按不支持处理）。
_TEXT_PART_TYPES = {"text", "input_text", "output_text"}

#: 工具结果标签模板（``call_id`` 占位符带前导空格）；缺失时退化为 ``[工具执行结果]``。
_TOOL_LABEL = "[工具执行结果{call_id}]"

#: 角色标签（``user`` 为空 = 原样给出，不加任何标记）。
_ROLE_MARKERS = {
    "system": "[系统指令]",
    "user": "",
    "assistant": "[你之前的回复]",
    "tool": _TOOL_LABEL.format(call_id=""),
}

#: 播种头部（``deepseek-bridge`` 的「上下文重建」风格）。
#:
#: 不用「这是一个新会话」的措辞：``/session/link`` 绑定既有会话时同样是播种。
_PREAMBLE = (
    "[上下文重建] 下面是本任务此前的对话记录（由本地桥接层从 API 请求重建）。"
    "请把它当作已经发生的上下文，据此继续，不要从头重做，不要复述历史，"
    "直接回答最后一条用户消息。"
)

#: 历史被裁剪时插入到可见正文里的省略说明。
#:
#: 只写进 :class:`PromptBuild.notes` 是不够的：模型看不到 notes，会把被裁掉的历史
#: 当成「本来就没有」，于是默默重做已完成的工作。
#: 历史被裁剪时插入到可见正文里的省略说明。
#:
#: 只写进 :class:`PromptBuild.notes` 是不够的：模型看不到 notes，会把被裁掉的历史
#: 当成「本来就没有」，于是默默重做已完成的工作。
_OMISSION_NOTE = "（更早的对话因长度限制已省略，如需请向我确认。）"

#: 播种头部的标题（与去重判定共用同一份常量）。
CONTEXT_REBUILD_HEADER = "[上下文重建]"
ENV_NOTE_HEADER = "[环境说明]"

#: 工具结果「命令执行完成但没有任何输出」时的替代文本。
#:
#: 背景（chatgpt-bridge 的用户实测死循环）：客户端把空 stdout 渲染成 ``(no output)`` /
#: ``Took 0.0s``，模型把它理解成「命令没生效」，于是把**同一条命令原样再发一遍**，
#: 客户端再执行一次、又是一样的空输出——死循环。因此空输出必须被显式说明，
#: 而不是把 ``(no output)`` 原样交给模型。
EMPTY_TOOL_RESULT_NOTE = (
    "（本命令已执行完毕、退出正常，但**没有任何输出**：空输出是有效的正常结果，"
    "既不是失败，也不代表工具异常或命令未生效。）\n"
    "请直接给出**下一条指令**或最终结论；不要重复执行同一条命令——"
    "同样的命令只会再次得到空输出。若确实需要看到内容，请换一条会打印状态的命令。"
)

#: 空输出的等价写法（均为括号 / 尖括号包裹形式，避免把真实输出里的普通句子误判）。
_EMPTY_TOOL_OUTPUT_MARKERS = (
    "(no output)",
    "(no stdout)",
    "(empty output)",
    "(empty)",
    "(no output produced)",
    "(空输出)",
    "(无输出)",
    "<no output>",
    "[no output]",
)

#: 客户端外壳行：命令回显、耗时、退出码、token 统计、空 ``Output:`` 块、分隔线。
_TOOL_CHROME_RES = (
    re.compile(r"^took\s+[\d.]+\s*s$", re.IGNORECASE),
    re.compile(r"^wall time:\s*\S.*$", re.IGNORECASE),
    re.compile(r"^chunk id:\s*\S+$", re.IGNORECASE),
    re.compile(r"^process exited with code\s+\d+$", re.IGNORECASE),
    re.compile(r"^original token count:\s*\d+$", re.IGNORECASE),
    re.compile(r"^output:\s*$", re.IGNORECASE),
    re.compile(r"^[-=_*~#]{3,}$"),
)
#: 命令回显（``$ git status --short``）：只在**首行**识别，避免误判以 ``$`` 开头的输出。
_TOOL_ECHO_RE = re.compile(r"^\$\s+\S.*$")


def _is_tool_chrome_or_marker(line: str) -> bool:
    if line.lower() in _EMPTY_TOOL_OUTPUT_MARKERS:
        return True
    return any(pattern.match(line) for pattern in _TOOL_CHROME_RES)


def _is_empty_tool_output(raw: str) -> bool:
    """工具结果是否等价于「命令执行完成，但确实没有任何输出」。

    去掉外壳行（命令回显 / 耗时 / 退出码 / token 统计 / 分隔线）与空标记后是否什么都不剩。
    这样 ``git status --short``（工作区干净）与空白内容都算空，而任何含真实内容的输出
    仍逐字节保留——Pi / Codex 的 read 结果会原样作为 edit 工具的 oldText。
    """
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return True
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if lines and _TOOL_ECHO_RE.match(lines[0]):
        lines = lines[1:]
    return all(_is_tool_chrome_or_marker(line) for line in lines)


def _tool_call_signature(call: Any) -> str:
    """把一条 assistant 工具调用归一化成「命令签名」（用于识别重复调用）。"""
    if isinstance(call, dict):
        name = call.get("name")
        args = call.get("arguments")
    else:  # pydantic ToolCall
        fn = getattr(call, "function", None)
        name = getattr(fn, "name", None)
        args = getattr(fn, "arguments", None)
    if not name:
        return ""
    if isinstance(args, str):
        try:
            args = json.dumps(json.loads(args), ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            pass
    elif isinstance(args, (dict, list)):
        args = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return f"{name}|{args if args is not None else ''}"


def _repeated_empty_calls(
    messages: Sequence[NormalizedMessage], empty_call_ids: Sequence[Optional[str]]
) -> List[tuple]:
    """找出「同一命令已调用 ≥2 次」且本轮结果为空的那几条，返回 ``[(签名, 次数)]``。"""
    signature_of: dict = {}
    counts: dict = {}
    for message in messages:
        for call in message.tool_calls or []:
            signature = _tool_call_signature(call)
            if not signature:
                continue
            counts[signature] = counts.get(signature, 0) + 1
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            if call_id:
                signature_of[str(call_id)] = signature
    out: List[tuple] = []
    for call_id in empty_call_ids:
        if not call_id:
            continue
        matched = signature_of.get(str(call_id))
        if matched and counts.get(matched, 0) >= 2:
            pair = (matched, counts[matched])
            if pair not in out:
                out.append(pair)
    return out


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
    marker = _ROLE_MARKERS.get(message.role)
    if marker is None:
        marker = f"[{message.role}]"
    body = message.content or ""
    if message.role == "assistant" and message.tool_calls:
        calls = json.dumps(message.tool_calls, ensure_ascii=False)
        body = (body + "\n" if body else "") + f"[发起工具调用] {calls}"
    if message.role == "tool":
        # 空输出必须**显式**说明，否则模型会以为命令没生效而反复重发同一条命令。
        if _is_empty_tool_output(body):
            body = EMPTY_TOOL_RESULT_NOTE
        else:
            limit = config.TOOL_RESULT_MAX_CHARS
            if limit and len(body) > limit:
                body = body[:limit] + f"\n…（工具结果已截断，原长 {len(body)} 字符）"
        # 带上 call id，模型才能把结果对回到具体的那次调用（参考 deepseek-bridge）。
        marker = _TOOL_LABEL.format(
            call_id=f" {message.tool_call_id}" if message.tool_call_id else ""
        )
    return f"{marker}\n{body}" if marker else body


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
    from .toolcalls import (
        TOOLCALL_HEADER,
        format_repeat_call_hint,
        format_tool_call_emphasis,
        format_tools_instruction,
    )

    system_text = extract_system_text(messages)
    inbound = "\n".join(
        [system_text] + [_render_message(m) for m in messages if m.role != "system"]
    )
    tool_text = ""
    # 去重只看**入站消息**：客户端可能已内联一份说明，再追加一份会互相干扰。
    if tools and TOOLCALL_HEADER not in inbound:
        tool_text = format_tools_instruction(tools)
    # 新会话 / 重置后的第一轮：把格式强调再放一次（模型最容易在这时候无视工具）。
    emphasis = format_tool_call_emphasis(tools) if (tool_text and include_preamble) else ""

    budget = config.CLAUDE_MAX_CONTEXT_CHARS
    fixed_parts: List[str] = []
    if include_preamble:
        fixed_parts.append(_PREAMBLE)
    if emphasis:
        fixed_parts.append(emphasis)
    if include_preamble and config.SEED_ENV_NOTE:
        fixed_parts.append(config.SEED_ENV_NOTE)
    if system_text:
        fixed_parts.append(_ROLE_MARKERS["system"] + "\n" + system_text)
    fixed = "\n\n".join(fixed_parts)
    trailer_parts: List[str] = list(extra_sections or [])
    trailer = "\n\n".join(trailer_parts)

    # 工具说明现在插在正文**内部**（紧贴本轮任务），因此也要计入预留预算。
    reserved = len(fixed) + len(trailer) + len(tool_text) + 2
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
    if truncated:
        # 省略说明放在历史**之前**，紧跟在播种头部后面。
        rendered.insert(0, _OMISSION_NOTE)
    # 工具说明的位置（chatgpt-bridge T1.1 实测结论）：**紧贴本轮任务之前**，而不是
    # 整段历史之前，也不是整段 prompt 的末尾——隔着上千行历史时模型倾向于直接凭
    # 知识作答；贴到任务前才有近因效应，同时仍满足「工具说明先于任务」的约定。
    if tool_text:
        rendered.insert(max(0, len(rendered) - 1), tool_text)
    # 死循环防护：同一命令反复发出且每次都是空输出时，直接在末尾点名（近因效应）。
    empty_call_ids = [
        m.tool_call_id
        for m in messages
        if m.role == "tool" and _is_empty_tool_output(m.content or "")
    ]
    if empty_call_ids:
        repeats = _repeated_empty_calls(messages, empty_call_ids)
        if repeats:
            rendered.append(format_repeat_call_hint(repeats))
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
