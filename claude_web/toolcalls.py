"""工具调用模拟（doc/design.md 第 6 节）。

Claude 网页输出不会自动等于 OpenAI 的 ``tool_calls``。这里的做法：

1. :func:`format_tools_instruction` 把客户端声明的工具（名称 / 描述 / 参数 Schema）
   转成提示词附加内容，并约定用**专用围栏** ``tool_call`` 承载结构化调用；
2. :func:`parse_tool_calls` 只识别该围栏内的 JSON，避免把普通 Markdown 代码块或
   用户引用的文本误判成调用；
3. 解析结果必须通过参数形状校验且工具名属于本次请求允许集合，才会序列化为
   合法 ``tool_calls``；无效结构只产生诊断，**不会被静默转成可执行命令**。

桥接层首期只生成并回传工具调用，不自行执行任何模型生成的命令（doc/design.md 6）。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import config
from .models import FunctionCall, ToolCall, ToolSpec

#: 参数键的候选名（不同客户端 / 模型习惯不同）。
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARG_KEYS = ("arguments", "args", "parameters", "input")

_DESC_DEFAULT_LIMIT = 200


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def _required_params(schema: Dict[str, Any]) -> List[str]:
    required = schema.get("required")
    if isinstance(required, list):
        return [str(item) for item in required]
    return []


def format_tools_instruction(
    tools: Sequence[ToolSpec],
    *,
    fence: Optional[str] = None,
    verbose: bool = False,
) -> str:
    """构造注入到提示词里的工具调用协议与工具清单。"""
    if not tools:
        return ""
    fence = fence or config.TOOL_CALL_FENCE
    lines: List[str] = [
        "[工具调用协议]",
        "你可以调用下列工具来完成任务。需要调用工具时，**只输出**一个或多个如下结构，"
        "不要把调用写进普通 Markdown 代码块，也不要用自然语言描述参数：",
        f"```{fence}",
        '{"name": "<工具名>", "arguments": {<JSON 参数>}}',
        "```",
        "规则：一次可以输出多个上述代码块表示并行调用；"
        "`arguments` 必须是合法 JSON 对象，且只包含该工具声明的参数；"
        "任务完成后用普通文本给出最终答复，不要再输出该代码块。",
        "可用工具：",
    ]
    for spec in tools:
        required = _required_params(spec.parameters)
        signature = f"{spec.name}({'必填: ' + ', '.join(required) + ')' if required else '无必填参数'}"
        desc = _truncate(spec.description, _DESC_DEFAULT_LIMIT)
        lines.append(f"- {signature}：{desc}" if desc else f"- {signature}")
        if verbose and spec.parameters:
            lines.append("  参数 Schema: " + json.dumps(spec.parameters, ensure_ascii=False))
    return "\n".join(lines)


@dataclass
class ParsedToolCalls:
    """工具调用解析结果。

    * ``calls``：已通过校验、可直接序列化的调用；
    * ``errors``：每条失败原因（供诊断，不外发正文）；
    * ``saw_marker``：回复里是否出现过工具围栏（用于决定是否需要缓冲 / 纠偏）。
    """

    calls: List[ToolCall] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    saw_marker: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.calls) and not self.errors


def _fence_pattern(fence: str) -> re.Pattern:
    escaped = re.escape(fence)
    # 匹配 ```fence ... ``` ；围栏语言标签允许前后空白与大小写差异。
    return re.compile(rf"```[ \t]*{escaped}[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _iter_balanced_objects(text: str) -> Iterable[str]:
    """从文本里提取顶层 ``{...}`` 片段（跳过字符串里的花括号）。"""
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : i + 1]
                    start = -1


def _first_key(payload: Dict[str, Any], keys: Sequence[str]) -> Optional[str]:
    for key in keys:
        if key in payload:
            return key
    return None


def _coerce_arguments(raw: Any) -> Optional[Dict[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _make_call(payload: Dict[str, Any], allowed: Optional[set]) -> tuple[Optional[ToolCall], Optional[str]]:
    name_key = _first_key(payload, _NAME_KEYS)
    if name_key is None:
        return None, "工具调用缺少 name 字段"
    name = payload.get(name_key)
    if not isinstance(name, str) or not name.strip():
        return None, "工具调用的 name 必须是非空字符串"
    name = name.strip()
    if allowed is not None and name not in allowed:
        return None, f"模型调用了未声明的工具：{name}"
    arg_key = _first_key(payload, _ARG_KEYS)
    raw_args = payload.get(arg_key) if arg_key else {}
    arguments = _coerce_arguments(raw_args)
    if arguments is None:
        return None, f"工具 {name} 的 arguments 不是合法的 JSON 对象"
    call = ToolCall(
        id=f"call_{uuid.uuid4().hex[:16]}",
        type="function",
        function=FunctionCall(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
    )
    return call, None


def parse_tool_calls(
    text: str,
    *,
    allowed_names: Optional[Iterable[str]] = None,
    fence: Optional[str] = None,
) -> ParsedToolCalls:
    """解析回复文本里的结构化工具调用。

    只认 ``tool_call`` 围栏；围栏外的一律按普通文本处理（避免误判 Markdown /
    用户引用）。工具名会与 ``allowed_names`` 比对；未声明的工具只产生诊断。
    """
    fence = fence or config.TOOL_CALL_FENCE
    allowed = set(allowed_names) if allowed_names is not None else None
    result = ParsedToolCalls()
    if not text:
        return result

    for match in _fence_pattern(fence).finditer(text):
        result.saw_marker = True
        body = match.group(1).strip()
        if not body:
            result.errors.append("工具围栏为空")
            continue
        payloads: List[Any]
        try:
            parsed = json.loads(body)
            payloads = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            # 允许围栏内是「若干行 JSON」或前后带说明文字：退化为平衡括号扫描。
            objects = list(_iter_balanced_objects(body))
            if not objects:
                result.errors.append("工具围栏内容不是合法 JSON")
                continue
            payloads = []
            for chunk in objects:
                try:
                    payloads.append(json.loads(chunk))
                except json.JSONDecodeError:
                    result.errors.append("工具围栏里的 JSON 片段无法解析")

        for payload in payloads:
            if not isinstance(payload, dict):
                result.errors.append("工具调用条目必须是 JSON 对象")
                continue
            call, error = _make_call(payload, allowed)
            if error:
                result.errors.append(error)
            elif call is not None:
                result.calls.append(call)
    return result


def to_tool_call_models(calls: Sequence[ToolCall]) -> List[ToolCall]:
    """兼容 facade：解析结果已是 :class:`ToolCall`，直接返回副本。"""
    return list(calls)
