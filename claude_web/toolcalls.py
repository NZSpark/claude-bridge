"""工具调用模拟（doc/design.md 第 6 节）。

Claude 网页输出不会自动等于 OpenAI 的 ``tool_calls``。这里的做法：

1. :func:`format_tools_instruction` 把客户端声明的工具（名称 / 描述 / 参数 Schema）
   转成提示词附加内容，并约定用**专用围栏** ``tool_call`` 承载结构化调用；
   规则里明确禁止「自造通用工具名」与「退回 XML / DSL 标记」（参考
   ``deepseek-bridge`` 的 toolcalls.py：模型在长会话 / 新会话里最容易犯这两个错）；
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
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import config
from .models import FunctionCall, ToolCall, ToolSpec

#: 参数键的候选名（不同客户端 / 模型习惯不同）。
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARG_KEYS = ("arguments", "args", "parameters", "input")

_DESC_DEFAULT_LIMIT = 200

# ==================== 注入块标题（生成与判定必须同源）====================
# 这些标题是「去重判定」（prompting.build_prompt：入站内容已带同类说明时不再追加）
# 与「泄漏检测」（测试断言注入块没被当成正文回给客户端）的唯一依据。
# 措辞与结构对齐 chatgpt-bridge 的 toolcalls.py：英文标题 + 全英文正文，实测对
# 网页版的指令遵从度明显优于中文模版式说明。
TOOLCALL_HEADER = "[Tool Calling Instructions]"
EMPHASIS_HEADER = "[Output Format Emphasis]"
#: 同一命令被反复调用且每次都是空输出时的点名提醒（防死循环）。
REPEAT_HEADER = "[Repeated Empty Tool Call]"


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


def _truncate_description(desc: str, limit: int) -> str:
    """按句末 / 词边界截断描述，避免出现 ``whichever is hit firs…`` 这类半截词。"""
    desc = (desc or "").strip()
    if not limit or len(desc) <= limit:
        return desc
    window = desc[:limit]
    cut = max(window.rfind(". "), window.rfind("。"), window.rfind("; "))
    if cut >= limit // 2:
        return window[: cut + 1] + " …"
    space = window.rfind(" ")
    if space >= limit // 2:
        window = window[:space]
    return window.rstrip() + " …"


def _example_arg_value(schema: Any) -> Any:
    """示例里给某个参数用的占位值：按声明类型给一个 JSON 合法的最小值。

    历史 bug：示例一律用字符串占位（``"offset": "..."``），而真实参数是整数——
    模型照抄即得到类型错误的参数；尖括号占位符（``<command>``）则会被原样发进 shell。
    """
    kind = schema.get("type") if isinstance(schema, dict) else None
    if kind in ("integer", "number"):
        return 0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return "..."


def _tool_example_call(tools: Sequence[ToolSpec], fence: str) -> str:
    """按第一个工具的真实名字 / 参数键生成一个围栏示例块。

    示例必须带**真实工具名与参数键**（否则模型不认），而值必须是「一眼就知道要替换」
    的占位符——但类型要与声明一致，不能一律用字符串。优先用 ``required``（必填项最
    容易漏），没有 required 才退回 properties 的前几个键。
    """
    first = tools[0] if tools else None
    name = getattr(first, "name", None) or "tool_name"
    params = getattr(first, "parameters", None)
    params = params if isinstance(params, dict) else {}
    properties = params.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = [key for key in _required_params(params)][:4]
    keys = required or list(properties.keys())[:4]
    example_args = {key: _example_arg_value(properties.get(key)) for key in keys}
    payload = json.dumps({"name": name, "arguments": example_args}, ensure_ascii=False)
    return f"```{fence}\n{payload}\n```"


def format_tool_call_emphasis(tools: Optional[Sequence[ToolSpec]] = None, *, fence: Optional[str] = None) -> str:
    """新会话 / 重置后的播种 prompt 开头再强调一次的格式块。

    新桶没有「示范过正确格式」的历史轮次，模型最容易在这时候无视工具或退回原生
    XML / DSL 标记，所以把要求与完整格式再点一遍。示例与主注入块共用
    :func:`_tool_example_call`，保证两处永远是同一种（可解析的）形态。
    """
    fence = fence or config.TOOL_CALL_FENCE
    if tools:
        example_call = _tool_example_call(tools, fence)
    else:
        payload = json.dumps({"name": "...", "arguments": {"param": "..."}}, ensure_ascii=False)
        example_call = f"```{fence}\n{payload}\n```"
    return "\n".join(
        [
            f"{EMPHASIS_HEADER} This is a new session (or one that was just reset); the",
            "following rules stay in effect for this whole session:",
            "You MUST use the provided tools whenever the task needs real action or data;",
            "never fabricate tool output.",
            "You do not run anything yourself: the user's local client executes the tool calls you",
            "write and sends their real output back as your next message; never reply that you",
            "have no tools.",
            f"To call a tool, output ONE fenced code block labelled exactly `{fence}`:",
            example_call,
            '(Replace the "..." placeholders above with the real values; never copy the example',
            "literally.)",
            f"The fence label must be `{fence}` - not json, not text, not empty - and the JSON must",
            "sit inside the fence; a call written as plain text will NOT be executed.",
            "Keep the JSON on one line: escape double quotes as \\\" and line breaks as \\n; for",
            "shell commands prefer single quotes inside the command so they never clash with the",
            "JSON quotes.",
            "Use the exact tool names given in the tool instructions; never invent tool names.",
            "Output EXACTLY ONE tool_call block per reply; when you call a tool, output only that",
            "block - no explanation, no preamble.",
            "Never emit XML / DSL tags - only the fenced block is executed.",
        ]
    )


def format_repeat_call_hint(repeats: Sequence[tuple]) -> str:
    """同一命令已重复调用（且每次都是空输出）时的点名提醒，用于打断死循环。

    :param repeats: ``[(命令签名, 出现次数)]``，来自 ``prompting._repeated_empty_calls``。
    """
    lines = [
        f"{REPEAT_HEADER} You have already issued the SAME tool call more than once, and it",
        "returned EMPTY output every time:",
    ]
    for signature, count in repeats:
        lines.append(f"- called {count}x, always empty: {signature}")
    lines += [
        "An empty result means the command SUCCEEDED and printed nothing. Running it again",
        "will NEVER produce output - repeating it is an infinite loop and the task will fail.",
        "Do NOT repeat any call listed above. Either use a DIFFERENT command that prints the",
        "state you need, or stop and summarise what you already know as the final answer.",
    ]
    return "\n".join(lines)


def format_tools_instruction(
    tools: Sequence[ToolSpec],
    *,
    fence: Optional[str] = None,
    verbose: bool = False,
) -> str:
    """构造注入到提示词里的工具调用协议与工具清单。

    措辞按 chatgpt-bridge 的实测结论校准（此前的中文模版式说明实测不工作）：

    1. **切断退路**：说清「直接凭知识作答＝任务失败」，并禁止伪造工具输出；
    2. **给具体到参数的示例**：只给格式模板不够；且占位值要按声明类型给
       （旧写法 ``{"name": "<工具名>"}`` 会被模型原样照抄，``<`` 直接发进 shell）；
    3. **载体只有一种**：全篇只说 ``tool_call`` 围栏，不再给出其它写法的“负向示范”；
    4. **不要声称执行能力属于模型**：既不说“你有工具”，也不说“你没有访问”——
       两种说法都会把模型推向核对自己的能力状态，进而拒答；只说「你的文本会被
       本机客户端执行、执行结果作为下一条消息回来」；
    5. 指令要短、聚焦，过长的 meta 说明会稀释核心要求。

    实测（Pi + claude.ai）：模型很容易**无视工具、直接凭知识作答**，因此这里的
    命令式要求 + 具体示例 + 规则编号必须一起给。
    """
    if not tools:
        return ""
    fence = fence or config.TOOL_CALL_FENCE
    example_call = _tool_example_call(tools, fence)
    lines: List[str] = [
        TOOLCALL_HEADER,
        "You are a language model: the only thing you produce is text, and you cannot run",
        "anything yourself. This conversation is read by a small local client running on the",
        "user's own computer - when you write a tool call in the format below, that client",
        "executes it there for real and returns the real output to you as your next message.",
        "No tool has to be installed or mounted on your side, and the tools below will never",
        "appear in your built-in tool list - that is expected and normal, because the client",
        "takes the call out of your reply text.",
        "Writing a tool call is therefore not fabrication and not a false claim about your",
        "capabilities: you are writing the command and the client runs it.",
        "That is the only way you take real action here. If the task needs real action or data",
        "and you answer from your own knowledge instead, the task FAILS. Never fabricate tool",
        "output.",
        "",
        f"A tool call is ONE fenced code block labelled exactly `{fence}`, containing one JSON",
        "object and nothing else:",
        example_call,
        '(Replace the "..." placeholders above with the real values. Do NOT copy the example',
        "literally.)",
        "",
        "Tools the client (not you) can execute - use these exact names:",
    ]
    verbose = verbose or config.TOOLS_INSTRUCTION_VERBOSE
    for spec in tools:
        desc = _truncate_description(spec.description, config.TOOLS_DESC_MAX_CHARS)
        if verbose:
            lines.append(f"- {spec.name}: {desc}")
            if spec.parameters:
                lines.append(
                    "  parameters (JSON Schema): " + json.dumps(spec.parameters, ensure_ascii=False)
                )
            continue
        required = _required_params(spec.parameters)
        properties = spec.parameters.get("properties") if isinstance(spec.parameters, dict) else None
        keys = ",".join(required) if required else ",".join(list(properties or {})[:4])
        signature = f"{spec.name}({keys})" if keys else spec.name
        lines.append(f"- {signature}: {desc}" if desc else f"- {signature}")
    lines += [
        "",
        "Rules:",
        f"1. The fence label MUST be exactly `{fence}` - not json, not text, not empty - and the",
        "   JSON must sit INSIDE the fence. A block labelled anything else, or a call written",
        "   as plain text instead of a fenced block, will NOT be executed.",
        "2. `arguments` must be a JSON object using the tool's own parameter names, with values",
        "   of the declared type (numbers and booleans unquoted, strings in double quotes).",
        "3. Keep the JSON on ONE line inside the fence: escape double quotes as \\\" and line",
        "   breaks as \\n; never put a raw line break inside a JSON string.",
        "4. Paste command / script / file text into the JSON string verbatim - the fenced block",
        "   preserves it exactly. A shell command always travels as the `command` value inside",
        "   the tool_call JSON: never reply with the bare command in a `bash` style code block.",
        "   Prefer single quotes inside the command (e.g. git commit -m 'msg') so they never",
        "   clash with the JSON quotes.",
        "5. Output EXACTLY ONE tool_call block per reply - never two or more; the client",
        "   processes a single call at a time. If you need several tools, issue one call, wait",
        "   for its result, then issue the next in your next reply.",
        "6. When you call a tool, output ONLY the single fenced block: no explanation, no",
        "   preamble, no text before or after it.",
        "7. Never decline a task on the grounds that you \"have no tools\" or that the tools are",
        "   not in your own tool list: you are not the one that runs them - the client executes",
        "   whatever you write, and such a refusal means the task FAILED. Never offer the user",
        "   instructions or a patch to apply themselves instead of calling a tool.",
        "8. If a tool result is EMPTY (e.g. shows \"(no output)\"), the command SUCCEEDED and",
        "   genuinely printed nothing. That is a valid result, not a failure: move on to the",
        "   NEXT command or give the final answer. Never re-run the exact same command.",
        "9. Never emit XML / DSL tags (native tool markers of that shape are not executed): the",
        f"   fenced `{fence}` block above is the only carrier this client accepts.",
    ]
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


def _marker_pattern(fence: str) -> re.Pattern:
    """无围栏形态的标记正则：``tool_call`` / ``tool-call`` 都认。

    不能用 ``\\b`` 收尾：中文 / 全角字符在 Python ``re`` 里算 ``\\w``，
    会让 ``tool_call丨`` 这种边界匹配失败（参考 deepseek-bridge）。
    """
    body = re.escape(fence).replace("_", "[-_]")
    return re.compile(rf"{body}(?![A-Za-z0-9_])", re.IGNORECASE)


#: 只由噪声字符组成的行（markdown 引用符号、竖线、围栏渲染残留等）。
_NOISE_LINE_RE = re.compile(r"^[\s>|｜丨`\-—…]+$")

#: 兜底的 ```json 围栏：模型不听话时会把调用装进 json 代码块。
_JSON_FENCE_RE = re.compile(r"```[ \t]*json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

#: DSML 风格：``<|DSML|calls>{...}</|DSML|calls>``（竖线数量不固定）。
_DSML_BLOCK_RE = re.compile(
    r"<\s*[｜|]{1,4}\s*DSML\s*[｜|]{1,4}[^>]*>(.*?)<\s*/\s*[｜|]{1,4}\s*DSML\s*[｜|]{1,4}",
    re.DOTALL | re.IGNORECASE,
)

#: DSML **结构化**形态（开启标签 / 闭标签 / 参数标签；闭标签可能缺失）。
_DSML_BAR = r"[｜|]{1,4}"
_DSML_INVOKE_OPEN_RE = re.compile(rf"<\s*{_DSML_BAR}\s*DSML\s*{_DSML_BAR}\s*invoke\b([^>]*)>", re.IGNORECASE)
_DSML_INVOKE_CLOSE_RE = re.compile(rf"<\s*/\s*{_DSML_BAR}\s*DSML\s*{_DSML_BAR}\s*invoke\s*>", re.IGNORECASE)
_DSML_PARAM_OPEN_RE = re.compile(rf"<\s*{_DSML_BAR}\s*DSML\s*{_DSML_BAR}\s*parameter\b([^>]*)>", re.IGNORECASE)
_DSML_PARAM_CLOSE_RE = re.compile(rf"<\s*/\s*{_DSML_BAR}\s*DSML\s*{_DSML_BAR}\s*parameter\s*>", re.IGNORECASE)

#: 参数值的 JSON 标量识别（``string="true"`` 时不参与）。
_DSML_SCALAR_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null", re.IGNORECASE)

#: 模型偶尔不用声明的工具名，而用 bash / shell 这类通用名；能唯一对应才映射。
_DSML_GENERIC_NAMES = {
    "bash", "sh", "shell", "terminal", "command", "cmd", "exec", "execute",
    "run_command", "run_commands", "execute_command",
}
_SHELL_NAME_KEYWORDS = ("shell", "exec", "bash", "command", "term")

#: 裸 JSON 里出现 ``"tool_uses"`` 键（DSML 标签丢失后的残留形态）。
_TOOL_USES_RE = re.compile(r"tool_uses\s*\"?\s*:", re.IGNORECASE)


def _iter_balanced_spans(text: str) -> Iterable[Tuple[int, int]]:
    """扫描顶层、括号平衡的 ``{...}`` 区间（能正确处理字符串与转义）。"""
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
                    yield start, i + 1
                    start = -1


def _iter_balanced_objects(text: str) -> Iterable[str]:
    """从文本里提取顶层 ``{...}`` 片段（跳过字符串里的花括号）。"""
    for start, end in _iter_balanced_spans(text):
        yield text[start:end]


def _payload_entries(data: Any, *, allow_bare_object: bool) -> List[Any]:
    """从一段已解析的 JSON 里取出「调用条目」列表。

    兼容 ``{"tool_calls": [...]}`` / ``{"tool_uses": [...]}``（DSML 与部分网页版
    形态）/ 裸数组 / 单个裸对象（仅当 ``allow_bare_object``）。
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("tool_calls", "tool_uses"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data] if allow_bare_object else []
    return []


def _looks_like_call(entry: Any) -> bool:
    """宽容路径的形状闸：必须有可用的工具名。

    宽容路径（无围栏 / json 围栏 / tool_uses 裸对象）不要求“看起来像调用”之外
    的条件，否则普通 JSON（配置片段等）会被当成调用条目并刷出诊断。
    """
    if not isinstance(entry, dict):
        return False
    nested = entry.get("function")
    if isinstance(nested, dict) and isinstance(nested.get("name"), str):
        return True
    return any(isinstance(entry.get(key), str) and entry.get(key).strip() for key in _NAME_KEYS)


def _dsml_blocks(open_re: re.Pattern, close_re: re.Pattern, text: str) -> Iterable[Tuple[str, str]]:
    """按 DSML 开标签切出 ``(属性串, 块体)``。

    闭标签缺失 / 错位（回复被截断、DOM 吞标签）时，退化为取到下一个开标签或文末。
    """
    pos = 0
    while True:
        opened = open_re.search(text, pos)
        if not opened:
            return
        start = opened.end()
        closed = close_re.search(text, start)
        nxt = open_re.search(text, start)
        if closed and (nxt is None or closed.start() < nxt.start()):
            yield opened.group(1), text[start : closed.start()]
            pos = closed.end()
        elif nxt:
            yield opened.group(1), text[start : nxt.start()]
            pos = nxt.start()
        else:
            yield opened.group(1), text[start:]
            return


def _dsml_attr(attrs: str, key: str) -> Optional[str]:
    """从开标签的属性串里取 ``key="value"``。"""
    match = re.search(rf'(?:^|\s){re.escape(key)}\s*=\s*"([^"]*)"', attrs)
    return match.group(1) if match else None


def _dsml_param_value(raw: str, string_attr: Optional[str]) -> Any:
    """DSML parameter 内容 -> Python 值（``string="true"`` 原样保留）。"""
    value = raw.strip()
    if (string_attr or "").strip().lower() == "true":
        return value
    if value[:1] in "{[" or _DSML_SCALAR_RE.fullmatch(value):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _resolve_dsml_name(name: str, allowed: Optional[set]) -> str:
    """把 DSML invoke 的工具名对齐到客户端声明的名字；对不上就原样返回。"""
    if not name or not allowed:
        return name
    if name in allowed:
        return name
    lowered = name.strip().lower()
    for candidate in allowed:
        if candidate.lower() == lowered:
            return candidate
    if lowered in _DSML_GENERIC_NAMES:
        hits = [c for c in allowed if any(k in c.lower() for k in _SHELL_NAME_KEYWORDS)]
        if len(hits) == 1:
            return hits[0]
    return name


def _parse_dsml_invokes(text: str, allowed: Optional[set]) -> List[Dict[str, Any]]:
    """解析 DSML 结构化工具调用（invoke / parameter 形态）。"""
    calls: List[Dict[str, Any]] = []
    for attrs, body in _dsml_blocks(_DSML_INVOKE_OPEN_RE, _DSML_INVOKE_CLOSE_RE, text):
        name = _dsml_attr(attrs, "name")
        if not name:
            continue
        arguments: Dict[str, Any] = {}
        for pattrs, pvalue in _dsml_blocks(_DSML_PARAM_OPEN_RE, _DSML_PARAM_CLOSE_RE, body):
            pname = _dsml_attr(pattrs, "name")
            if not pname:
                continue
            arguments[pname] = _dsml_param_value(pvalue, _dsml_attr(pattrs, "string"))
        calls.append({"name": _resolve_dsml_name(name, allowed), "arguments": arguments})
    return calls


def _iter_bare_marker_blocks(text: str, marker_re: re.Pattern) -> Iterable[Tuple[int, int, str]]:
    """产出无围栏形态的 ``(标记起点, JSON 结束位置, JSON 文本)``。

    网页 DOM 把 ``` 渲染成 ``<pre>`` 后，``inner_text`` 常退化成：

    .. code-block:: text

        > tool_call
        {"name": "read_file", "arguments": {"path": "a.txt"}}

    因此标记与 JSON 之间可能夹着 ``>``、竖线、反引号等噪声行，需要跳过它们再找 JSON。
    """
    pos = 0
    while True:
        match = marker_re.search(text, pos)
        if not match:
            return
        segment = text[match.end() :]
        cursor = 0
        for line in segment.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped or _NOISE_LINE_RE.match(stripped):
                cursor += len(line)
            else:
                break
        probe = segment[cursor:]
        found = False
        for start, end in _iter_balanced_spans(probe):
            yield match.start(), match.end() + cursor + end, probe[start:end]
            pos = match.end() + cursor + end
            found = True
            break
        if not found:
            pos = match.end()


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
    # OpenAI 原生形态：{"function": {"name": ..., "arguments": ...}}
    nested = payload.get("function")
    if isinstance(nested, dict) and not isinstance(payload.get("name"), str):
        merged = dict(nested)
        for key in ("id", "type", "tool_call_id"):
            if key in payload and key not in merged:
                merged[key] = payload[key]
        payload = merged
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


def _consume(
    raw: str,
    allowed: Optional[set],
    result: ParsedToolCalls,
    *,
    allow_bare_object: bool,
    strict: bool = True,
) -> None:
    """解析一段 JSON 文本，把其中的调用条目追加到 ``result``。

    ``strict=False`` 用于宽容路径：不像工具调用的普通 JSON 被静默跳过
    （不刷诊断），但一旦有 name 就会完整走工具名校验。
    """
    raw = raw.strip()
    if not raw:
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    for entry in _payload_entries(data, allow_bare_object=allow_bare_object):
        if not isinstance(entry, dict):
            result.errors.append("工具调用条目必须是 JSON 对象")
            continue
        if not strict and not _looks_like_call(entry):
            continue
        call, error = _make_call(entry, allowed)
        if error:
            result.errors.append(error)
        elif call is not None:
            result.calls.append(call)


def _consume_fenced(body: str, allowed: Optional[set], result: ParsedToolCalls) -> None:
    """围栏内容：先按整体 JSON 解析，失败再退化为逐段平衡括号扫描。"""
    try:
        json.loads(body)
    except json.JSONDecodeError:
        for chunk in _iter_balanced_objects(body):
            _consume(chunk, allowed, result, allow_bare_object=True)
        return
    _consume(body, allowed, result, allow_bare_object=True)


def _parse_bare_marker_calls(
    text: str, marker_re: re.Pattern, allowed: Optional[set], result: ParsedToolCalls
) -> None:
    """无围栏形态：``tool_call`` 标记 + 紧随其后的平衡 JSON 对象。"""
    for _start, _end, obj in _iter_bare_marker_blocks(text, marker_re):
        result.saw_marker = True
        _consume(obj, allowed, result, allow_bare_object=True, strict=False)


def parse_tool_calls(
    text: str,
    *,
    allowed_names: Optional[Iterable[str]] = None,
    fence: Optional[str] = None,
) -> ParsedToolCalls:
    """解析回复文本里的结构化工具调用。

    按可信度依次尝试（前一步拿到调用就不再继续）：

    1. ``tool_call`` 围栏内的 JSON（本项目的正式约定）；
    2. ``<|DSML|calls>{...}</|DSML|calls>``（网页版偶发的 XML 包裹）；
    3. DSML 结构化 ``invoke / parameter`` 形态；
    4. ```json 围栏（模型不听话时的兜底）；
    5. 带 ``tool_uses`` 键的裸 JSON 对象（DSML 标签丢失后的残留）；
    6. **无围栏**的 ``tool_call`` 标记 + 平衡 JSON 对象——网页 DOM 把 ``` 渲染成
       ``<pre>`` 后 ``inner_text`` 的真实形态，实测会真的发生。

    所有路径共用同一套校验：工具名必须在 ``allowed_names`` 内，``arguments``
    必须是 JSON 对象；不合法只产生诊断，不会被静默转成可执行调用。
    宽容路径（4/5/6）额外要求条目“看起来像调用”（有可用 name），
    避免把回复里的普通 JSON 当成调用。
    """
    fence = fence or config.TOOL_CALL_FENCE
    allowed = set(allowed_names) if allowed_names is not None else None
    result = ParsedToolCalls()
    if not text:
        return result
    marker_re = _marker_pattern(fence)

    # 1) 正式围栏
    for match in _fence_pattern(fence).finditer(text):
        result.saw_marker = True
        body = match.group(1).strip()
        if not body:
            result.errors.append("工具围栏为空")
            continue
        before = len(result.calls) + len(result.errors)
        _consume_fenced(body, allowed, result)
        if len(result.calls) + len(result.errors) == before:
            result.errors.append("工具围栏内容不是合法 JSON")

    # 2) DSML 风格 XML 包裹的 JSON
    if not result.calls:
        for match in _DSML_BLOCK_RE.finditer(text):
            result.saw_marker = True
            _consume(match.group(1), allowed, result, allow_bare_object=True, strict=False)

    # 3) DSML 结构化 invoke / parameter
    if not result.calls:
        dsml_calls = _parse_dsml_invokes(text, allowed)
        if dsml_calls:
            result.saw_marker = True
            for entry in dsml_calls:
                call, error = _make_call(entry, allowed)
                if error:
                    result.errors.append(error)
                elif call is not None:
                    result.calls.append(call)

    # 4) ```json 围栏兜底
    if not result.calls:
        for match in _JSON_FENCE_RE.finditer(text):
            _consume(match.group(1), allowed, result, allow_bare_object=True, strict=False)

    # 5) 只剩 {"tool_uses": [...]} 的裸对象
    if not result.calls and _TOOL_USES_RE.search(text):
        result.saw_marker = True
        for obj in _iter_balanced_objects(text):
            _consume(obj, allowed, result, allow_bare_object=True, strict=False)
            if result.calls:
                break

    # 6) 无围栏标记 + 平衡 JSON
    if not result.calls and marker_re.search(text):
        _parse_bare_marker_calls(text, marker_re, allowed, result)

    return result


def strip_tool_call_blocks(text: str, *, fence: Optional[str] = None) -> str:
    """去掉承载工具调用的块（围栏与无围栏两种形态），保留其余正文。"""
    fence = fence or config.TOOL_CALL_FENCE
    staged = _fence_pattern(fence).sub("", text)
    pieces: List[str] = []
    cursor = 0
    for start, end, _obj in _iter_bare_marker_blocks(staged, _marker_pattern(fence)):
        pieces.append(staged[cursor:start])
        cursor = end
    pieces.append(staged[cursor:])
    return "".join(pieces).strip()


def to_tool_call_models(calls: Sequence[ToolCall]) -> List[ToolCall]:
    """兼容 facade：解析结果已是 :class:`ToolCall`，直接返回副本。"""
    return list(calls)
