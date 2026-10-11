"""Bridge 聊天命令（``/bridge ...``）：在对话里查状态、管理当前会话桶。

语义参考姊妹项目 ``deepseek-bridge`` 的 ``bridge_commands.py``，但只落到本项目
**真正具备**的能力上（不硬造上游有、这里没有的东西）：

* **精确匹配**：只有「整条消息就是那一行」才算命令；正文里、多行文本里以及历史
  消息里的 ``/bridge`` 字样一律当普通提问（见 :func:`parse_command`）。
* **只看最后一条**：最后一条必须是 ``user`` 消息。工具循环的后续请求会带着同一批
  历史重发，若回溯「最后一条 user 消息」，旧命令会被反复执行（见 :func:`latest_user_text`）。
* **只作用于当前会话桶**：命令没有「指定别的桶」的参数；桶由请求头 / ``user`` / UA
  决定（与普通请求同一套规则），因此多 Agent 场景不会互相影响。
* **本地应答**：命令由桥自己回答、**不发给 Claude 网页版**，所以页面失效、浏览器
  没起来（普通请求 503）时这些命令依然可用 —— 排障时最需要它们。
* **不泄露**：回复只含状态摘要，不打印配置值、Cookie、令牌或其它桶的信息。
* **不硬造能力**：本项目在**同一条网页会话里续写**（首轮开新对话 + 完整历史播种，
  之后只发增量），没有按桶设置项，因此不提供 ``reseed`` / ``settings`` 命令，而是在
  这类输入上直接说明该用什么命令。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence
from urllib.parse import urlsplit

from . import config
from .errors import InvalidRequestError, NotReadyError
from .models import advertised_models_ids
from .prompting import content_to_text

logger = logging.getLogger(__name__)

#: 命令命名空间。只识别这一个前缀；其它 ``/`` 开头的文本照旧交给网页版。
BRIDGE_COMMAND = "/bridge"
#: 「按播种方式绑定」的开关。
SEED_FLAG = "--seed"
#: ``/bridge help`` 支持的主题。
HELP_TOPICS = ("session",)
#: 会话路径：``https://claude.ai/chat/<会话 ID>``。
_SESSION_PATH_RE = re.compile(r"^/chat/([A-Za-z0-9_-]{8,})$")
#: ``session`` 的子命令。
SESSION_SUBCOMMANDS = ("status", "reset", "link", "unlink", "reseed")
#: 顶层别名：用户习惯直接写 ``/bridge link``，等价于 ``/bridge session link``。
_SESSION_ALIASES = ("reset", "link", "unlink", "reseed")

_USAGE = (
    f"{BRIDGE_COMMAND} 用法：\n"
    f"  {BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]  命令列表 / 分主题帮助\n"
    f"  {BRIDGE_COMMAND} status  桥接与当前会话桶状态摘要\n"
    f"  {BRIDGE_COMMAND} models  本桥对外公布的模型名（同 GET /v1/models）\n"
    f"  {BRIDGE_COMMAND} modal  关闭挡路的额度/升级弹窗（不改变会话状态）\n"
    f"  {BRIDGE_COMMAND} session [status]  当前桶的会话状态摘要\n"
    f"  {BRIDGE_COMMAND} session reset  下一轮开新网页会话并播种完整历史\n"
    f"  {BRIDGE_COMMAND} session link <URL> [{SEED_FLAG}]  绑定到指定网页会话\n"
    f"  {BRIDGE_COMMAND} session unlink  解除当前桶的会话绑定\n"
    f"  （link / unlink / reset 也可省略 session 直接写）"
)


# ==================== 命令清单（帮助的唯一来源）====================


@dataclass(frozen=True)
class CommandSpec:
    """帮助里的一个条目。只列当前版本**真正注册**的命令，避免帮助与实现漂移。"""

    syntax: str
    summary: str
    group: str = ""  # "" = 顶层；"session" = 分主题
    readonly: bool = True


REGISTRY: tuple = (
    CommandSpec(f"{BRIDGE_COMMAND} help [{'|'.join(HELP_TOPICS)}]", "查看命令列表 / 分主题帮助"),
    CommandSpec(f"{BRIDGE_COMMAND} status", "桥接、浏览器与当前会话桶的状态摘要"),
    CommandSpec(f"{BRIDGE_COMMAND} models", "本桥对外公布的模型名（与 GET /v1/models 同源）"),
    CommandSpec(
        f"{BRIDGE_COMMAND} modal",
        "关闭挡路的额度/升级弹窗（只点关闭类控件，不改会话状态）",
        readonly=False,
    ),
    CommandSpec(f"{BRIDGE_COMMAND} session [status]", "当前桶的会话状态摘要", "session"),
    CommandSpec(
        f"{BRIDGE_COMMAND} session reset",
        "下一轮开新网页会话，并把客户端历史播种进去",
        "session",
        readonly=False,
    ),
    CommandSpec(
        f"{BRIDGE_COMMAND} session link <URL> [{SEED_FLAG}]",
        "把当前桶绑定到指定网页会话（{0} 表示那条会话里没有此前记录）".format(SEED_FLAG),
        "session",
        readonly=False,
    ),
    CommandSpec(
        f"{BRIDGE_COMMAND} session unlink",
        "解除当前桶的会话绑定",
        "session",
        readonly=False,
    ),
)


@dataclass(frozen=True)
class BridgeCommand:
    """一条已被识别的 ``/bridge`` 命令。"""

    action: str  # "help" | "status" | "models" | "modal" | "session" | "invalid"
    subaction: str = ""  # help 主题 / session 子命令
    target: str = ""  # session link 的会话地址（原样，执行时再校验归一化）
    seed: bool = False  # session link 是否带 --seed
    reason: str = ""  # 未执行的原因（invalid 时展示给用户）


# ==================== 解析 ====================


def _invalid(reason: str) -> BridgeCommand:
    return BridgeCommand("invalid", reason=f"{reason}\n{_USAGE}")


def parse_command(text: str) -> Optional[BridgeCommand]:
    """整条消息是否是一条 ``/bridge`` 命令；不是则返回 ``None``（照常发给网页版）。

    只有「整条消息就是这一行」才算命令：混在正文里、出现在多行文本里都当普通提问。
    命名空间内**语法已匹配但参数不合法**的写法返回 ``invalid``（带用法）——``/bridge``
    是桥自己保留的前缀，回一条用法提示比把它发给网页版更可解释。其它 ``/`` 开头的
    文本一律不是命令。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    tokens = lines[0].split()
    if not tokens or tokens[0].lower() != BRIDGE_COMMAND:
        return None
    tail = tokens[1:]
    if not tail:
        # 裸 `/bridge` = 命令发现入口（帮助），而不是「未知命令」
        return BridgeCommand("help")
    head, rest = tail[0].lower(), tail[1:]
    if head == "help":
        if len(rest) > 1:
            return _invalid(f"{BRIDGE_COMMAND} help 最多接受一个主题。")
        topic = rest[0].lower() if rest else ""
        if topic and topic not in HELP_TOPICS:
            listed = "、".join(HELP_TOPICS)
            return _invalid(f"没有这个帮助主题：{rest[0]}（可用主题：{listed}）。")
        return BridgeCommand("help", subaction=topic)
    if head == "status":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} status 不接受参数。")
        return BridgeCommand("status")
    if head == "models":
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} models 不接受参数。")
        return BridgeCommand("models")
    if head in ("modal", "popup"):
        if rest:
            return _invalid(f"{BRIDGE_COMMAND} modal 不接受参数。")
        return BridgeCommand("modal")
    if head == "session":
        return _parse_session(rest, aliased=False)
    if head in _SESSION_ALIASES:
        return _parse_session([head, *rest], aliased=True)
    return _invalid(f"未知子命令：{tail[0]}。")


def _parse_session(rest: List[str], *, aliased: bool) -> BridgeCommand:
    if not rest:
        return BridgeCommand("session", subaction="status")
    sub, args = rest[0].lower(), rest[1:]
    if sub not in SESSION_SUBCOMMANDS:
        listed = "、".join(SESSION_SUBCOMMANDS)
        prefix = "" if aliased else "session "
        return _invalid(f"{prefix}没有这个子命令：{rest[0]}（可用：{listed}）。")
    if sub != "link":
        if args:
            return _invalid(f"{BRIDGE_COMMAND} session {sub} 不接受参数。")
        return BridgeCommand("session", subaction=sub)
    # session link <URL> [--seed]：选项与地址分别校验，不做猜测式执行
    seed = False
    positional: List[str] = []
    for token in args:
        if token.lower() == SEED_FLAG:
            seed = True
        elif token.startswith("--"):
            return _invalid(f"session link 不认识选项 {token}（只支持 {SEED_FLAG}）。")
        else:
            positional.append(token)
    if not positional:
        return _invalid("session link 还需要一个会话地址（或会话 ID）。")
    if len(positional) > 1:
        return _invalid(f"会话地址里不能有空格：收到了 {len(positional)} 段参数。")
    return BridgeCommand("session", subaction="link", target=positional[0], seed=seed)


# ==================== 消息侧判定 ====================


def _message_field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def latest_user_text(messages: Optional[Sequence[Any]]) -> Optional[str]:
    """最后一条消息的文本，仅当它是 ``user`` 消息时返回（否则 ``None``）。

    只看最后一条：历史里的旧命令是**已经执行过**的会话内容，不能重放执行。
    """
    if not messages:
        return None
    last = messages[-1]
    if _message_field(last, "role") != "user":
        return None
    content = _message_field(last, "content")
    if isinstance(content, str):
        return content
    try:
        return content_to_text(content)
    except Exception:  # noqa: BLE001 - 命令判定绝不能因为异常把请求打断
        return None


def is_command(messages: Optional[Sequence[Any]]) -> bool:
    """这批消息的最后一条是否会由桥自己应答（``/bridge ...``）。"""
    return parse_command(latest_user_text(messages) or "") is not None


# ==================== 会话地址归一化 ====================


def normalize_session_url(raw: str) -> Optional[str]:
    """把会话地址（或裸会话 ID）归一化成可校验的 URL；不合法返回 ``None``。

    只做「补全」不做「猜测」：完整 URL 原样交给
    :func:`claude_web.completion.lifecycle.validate_link_url` 做 https + 主机
    + 路径三段校验；裸 ID 会按允许主机补成 ``https://<host>/chat/<ID>``。
    """
    text = (raw or "").strip().strip("`\"'<>")
    if not text:
        return None
    if "://" in text:
        return text
    if not _SESSION_PATH_RE.fullmatch(f"/chat/{text}"):
        return None
    host = next(
        (h.strip() for h in config.LINK_ALLOWED_HOSTS if h.strip() and h.strip() != "*"),
        "claude.ai",
    )
    return f"https://{host}/chat/{text}"


def _host_ok(url: str) -> bool:
    """快速判断 URL 是否至少指向允许的主机（用于给出更友好的提示）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() != "https":
        return False
    host = (parts.hostname or "").lower()
    allowed = [h.strip().lower() for h in config.LINK_ALLOWED_HOSTS if h.strip()]
    if "*" in allowed:
        return bool(host)
    return any(host == item or host.endswith("." + item) for item in allowed)


# ==================== 执行 ====================


async def handle_command(
    messages: Optional[Sequence[Any]], service: Any, bucket: str
) -> Optional[str]:
    """识别并执行聊天命令，返回桥自己给出的应答；不是命令时返回 ``None``。

    调用方拿到应答后直接回给客户端，**不会**把这条消息发给网页版（因此命令在页面
    失效 / 浏览器未初始化时依然可用）。
    """
    text = latest_user_text(messages)
    if text is None:
        return None
    command = parse_command(text)
    if command is None:
        return None
    try:
        return await _execute(command, service, bucket)
    except Exception as exc:  # noqa: BLE001
        # 失败要可解释，但不把内部堆栈 / 敏感信息回给客户端（细节留在日志里）
        logger.warning("[命令] 执行 %s 失败：%r", BRIDGE_COMMAND, exc, exc_info=True)
        return f"[Bridge] 命令未执行：内部错误（{type(exc).__name__}）。请查看服务日志。"
    finally:
        logger.info("[命令] bucket=%s 已处理 %s", bucket, text.splitlines()[0][:120])


async def _execute(command: BridgeCommand, service: Any, bucket: str) -> str:
    if command.action == "help":
        return _help_reply(command.subaction)
    if command.action == "status":
        return _status_reply(service, bucket)
    if command.action == "models":
        return _models_reply()
    if command.action == "modal":
        return await _modal_reply(service, bucket)
    if command.action == "invalid":
        return f"[Bridge] 命令未执行：{command.reason}"
    if command.action == "session":
        return await _session_reply(command, service, bucket)
    return f"[Bridge] 命令未执行：未知命令。\n{_USAGE}"  # 防御：注册表与执行分支必须同步


async def _session_reply(command: BridgeCommand, service: Any, bucket: str) -> str:
    sub = command.subaction
    if sub == "status":
        return _session_status_reply(service, bucket)
    if sub == "reseed":
        return "\n".join(
            [
                "[Bridge] 不需要单独的 reseed：本项目首轮就开新对话并**播种完整历史**，",
                "之后在同一网页会话里**只发增量**，因此上下文默认一直是连贯的。",
                f" 怀疑网页端上下文丢了 / 想强制重播一遍：`{BRIDGE_COMMAND} session reset`",
                "  （下一轮换成一条新网页会话并重新播种完整历史）。",
                f" 想固定在一条已有会话上：`{BRIDGE_COMMAND} session link <URL> --seed`。",
            ]
        )
    if sub == "reset":
        await service.lifecycle.reset(bucket)
        logger.info("[命令] bucket=%s 已按 %s session reset 重置。", bucket, BRIDGE_COMMAND)
        return "\n".join(
            [
                f"[Bridge] 已重置会话桶 {bucket}：下一轮会开一条**新的网页会话**，",
                "并把客户端传来的完整历史播种进去（上下文不丢）。",
                " 影响范围：只影响这个桶；其它桶不动。",
                " 注意：这不是删除客户端的对话历史，也不清除 Claude 账户里的远程会话记录。",
                " HTTP 等价入口：POST /session/reset",
            ]
        )
    if sub == "unlink":
        had = bool(service.lifecycle.status(bucket).get("linked_url"))
        service.lifecycle.unlink(bucket)
        logger.info("[命令] bucket=%s 已按 %s session unlink。", bucket, BRIDGE_COMMAND)
        if not had:
            return f"[Bridge] 会话桶 {bucket} 本来就没有绑定网页会话。"
        return "\n".join(
            [
                f"[Bridge] 已解除会话桶 {bucket} 的会话绑定。",
                " 当前页面与上下文保持不变（不会被清空），只是不再被钉在那条会话上。",
                f" 下一轮会新开一条网页会话并播种完整历史：`{BRIDGE_COMMAND} session reset`。",
            ]
        )
    if sub == "link":
        return await _link_reply(command, service, bucket)
    return f"[Bridge] 命令未执行：未知会话子命令。\n{_USAGE}"


async def _link_reply(command: BridgeCommand, service: Any, bucket: str) -> str:
    url = normalize_session_url(command.target)
    if url is None or not _host_ok(url):
        allowed = "、".join(config.LINK_ALLOWED_HOSTS) or "（未配置）"
        return "\n".join(
            [
                f"[Bridge] 命令未执行：这不是一条可绑定的会话地址：{command.target}",
                f" 只接受 https + 允许的站点（{allowed}）+ 形如 /chat/<会话 ID> 的路径；",
                " 也可以只发会话 ID（会自动补全成完整地址）。",
            ]
        )
    try:
        normalized = await service.lifecycle.link(bucket, url, seed=command.seed)
    except (InvalidRequestError, NotReadyError) as exc:
        return "\n".join(
            [
                f"[Bridge] 命令未执行：无法绑定到该会话（{_one_line(exc.message)}）。",
                " 可能原因：地址非法 / 主机不在允许列表内 / 浏览器未就绪或页面尚未登录。",
                " 只读命令（status / models）在浏览器不可用时依然可用。",
            ]
        )
    logger.info("[命令] bucket=%s 已按 %s session link 绑定到 %s。", bucket, BRIDGE_COMMAND, normalized)
    lines = [f"[Bridge] 已把会话桶 {bucket} 绑定到：{normalized}"]
    if command.seed:
        lines.append(" 已标记为「那条会话里没有此前记录」：下一轮把完整历史播种进去（网页端会出现一次重复）。")
    else:
        lines.append(
            " 按「该会话里已有此前上下文」处理：本轮会完整播种一次，之后只发增量。"
        )
    lines.append(" 影响范围：只影响这个桶；页面重建 / 服务重启后仍会回到这条会话。")
    lines.append(f" 解除绑定：`{BRIDGE_COMMAND} unlink`。")
    return "\n".join(lines)


# ==================== 只读回复 ====================


def _help_reply(topic: str) -> str:
    """分主题帮助：只列当前版本真正注册的命令，并标明是否只读。"""
    lines = [f"[Bridge] {BRIDGE_COMMAND} 命令列表（当前版本）："]
    for spec in REGISTRY:
        if topic and spec.group != topic:
            continue
        mark = "只读" if spec.readonly else "改状态"
        lines.append(f"  {spec.syntax}\n    {spec.summary}（{mark}）")
    if not topic:
        lines.append(" 分主题帮助：" + "、".join(f"{BRIDGE_COMMAND} help {item}" for item in HELP_TOPICS))
    lines.append("只读命令不导航网页、不触发生成；改状态的命令只影响当前会话桶。")
    lines.append(
        f"会话地址形如 https://claude.ai/chat/<会话 ID>；也可以只发会话 ID"
        f"（{BRIDGE_COMMAND} link <ID>）。"
    )
    lines.append("提醒：命令必须**单独成一条消息**；正文里提到 /bridge 会照样发给网页版。")
    return "\n".join(lines)


def _models_reply() -> str:
    """本桥对外公布的模型名：与 ``GET /v1/models`` 用同一份数据。"""
    lines = ["[Bridge] 本桥对外公布的模型（与 GET /v1/models 同源）："]
    lines.extend(f"  {model_id}" for model_id in advertised_models_ids())
    lines.append(
        "说明：这些是 API 层的模型名，供客户端配置使用；它们**不强制**切换 Claude "
        "网页端实际选用的模型（那由登录账号与网页端设置决定）。"
    )
    return "\n".join(lines)


async def _modal_reply(service: Any, bucket: str) -> str:
    """关闭当前桶页面上的额度 / 升级弹窗（只点关闭类控件，绝不点升级 / 付费）。

    额度用尽后站点弹出的那个对话框会盖住输入区并抢走焦点：提示词写得进去、Enter 却不
    生效。生成链路每轮开始时会自己关一次（``doc/design.md`` 4.3），这个命令是「现在
    就关掉」的手动入口，也顺便回答「页面上到底有没有这个弹窗」。
    """
    try:
        page = await service.driver.page_for(bucket)
        state = await service.driver.dom.dismiss_limit_modal(page)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[命令] bucket=%s %s modal 失败：%s", bucket, BRIDGE_COMMAND, type(exc).__name__)
        return "\n".join(
            [
                f"[Bridge] 命令未执行：拿不到会话桶 {bucket} 的页面（{type(exc).__name__}）。",
                " 可能原因：浏览器未启动 / 页面已失效。只读命令（status / models）不受影响；",
                " 下一个生成请求会惰性启动浏览器并重试。",
            ]
        )
    if state.found and state.dismissed:
        logger.info("[命令] bucket=%s 已关闭额度/升级弹窗（命中 %s）。", bucket, state.pattern)
        return "\n".join(
            [
                f"[Bridge] 已关闭页面上的额度/升级弹窗（命中：{state.pattern}）。",
                " 只点了关闭类控件，没有点升级 / 付费按钮，也没有改动会话与页面内容。",
                " 注意：这只是关掉提示框；若确实是额度用尽，生成仍会失败并报 usage_limit_reached，",
                "  需要等额度重置或升级后才能继续。",
            ]
        )
    if state.found and not state.dismissed:
        return "\n".join(
            [
                f"[Bridge] 找到了额度/升级弹窗（命中：{state.pattern}）但没能关掉（{state.reason}）。",
                " 可能原因：站点改版换了关闭控件。请在浏览器里手动关一次，",
                f"  并把新选择器加进 {BRIDGE_COMMAND} 的 MODAL_CLOSE_SELECTORS 配置。",
            ]
        )
    return "\n".join(
        [
            f"[Bridge] 会话桶 {bucket} 的页面上没有额度/升级弹窗（不用关）。",
            " 生成链路每轮开始时会自己检查并关掉它；这里没命中就说明当前页面上确实没有。",
        ]
    )


def _status_reply(service: Any, bucket: str) -> str:
    """桥接与当前桶的状态摘要：只读，不导航、不触发生成。"""
    driver = service.driver
    stats = service.lifecycle.status(bucket)
    started = bool(getattr(driver, "started", False))
    start_error = getattr(driver, "start_error", None)
    lines = ["[Bridge] 桥接状态（只读，不打开页面）："]
    if start_error:
        lines.append(f" 浏览器：未就绪（初始化失败：{_one_line(str(start_error))}）")
    elif started:
        lines.append(" 浏览器：已启动")
    else:
        lines.append(" 浏览器：尚未启动（首个请求时惰性启动）")
    lines.append(f" 当前桶：{bucket}")
    lines.append(
        f" 会话分桶：{'开启' if config.SESSION_SCOPING else '关闭'}"
        f"（请求头 {config.SESSION_KEY_HEADER}）"
    )
    names = _bucket_names(driver)
    if names:
        lines.append(f" 在用页面桶：{'、'.join(names)}")
    lines.append(f" 会话绑定：{stats.get('linked_url') or '未绑定'}")
    lines.append(f" 网页会话：{_history_line(stats)}")
    lines.append(
        f" 轮数：{int(stats.get('turns') or 0)}；估算 tokens：{int(stats.get('est_tokens') or 0)}"
    )
    if stats.get("last_error"):
        lines.append(f" 最近错误：{_one_line(str(stats.get('last_error')))}")
    lines.append(" 登录 / 输入区是否就绪要真的打开页面探测：GET /readiness")
    return "\n".join(lines)


def _session_status_reply(service: Any, bucket: str) -> str:
    """当前桶的会话状态摘要（只查当前桶，不提供「列出所有桶」的正文信息）。"""
    stats = service.lifecycle.status(bucket)
    lines = [f"[Bridge] 会话桶 {bucket} 的会话状态："]
    lines.append(f" 会话绑定：{stats.get('linked_url') or '未绑定'}")
    if bucket == config.DEFAULT_SESSION_KEY and stats.get("default_link_url"):
        lines.append(f" 默认绑定（CLAUDE_DEFAULT_LINK_URL）：{stats.get('default_link_url')}")
    lines.append(f" 网页会话：{_history_line(stats)}")
    lines.append(
        f" 轮数：{int(stats.get('turns') or 0)}；估算 tokens：{int(stats.get('est_tokens') or 0)}"
    )
    if stats.get("cap_hit"):
        lines.append(" 上次生成：因上下文长度上限中断（下一轮会换新网页会话并播种历史）")
    if stats.get("last_error"):
        lines.append(f" 最近错误：{_one_line(str(stats.get('last_error')))}")
    lines.append(
        f" 管理：`{BRIDGE_COMMAND} reset`（换新网页会话）、"
        f"`{BRIDGE_COMMAND} link <URL>`、`{BRIDGE_COMMAND} unlink`"
    )
    return "\n".join(lines)


def _bucket_names(driver: Any) -> List[str]:
    lister = getattr(getattr(driver, "pool", None), "buckets", None)
    if not callable(lister):
        return []
    try:
        return sorted(str(name) for name in lister())
    except Exception:  # noqa: BLE001 - 诊断信息不该让命令失败
        return []


def _history_line(stats: dict) -> str:
    if stats.get("pending_reseed"):
        return "已排队重新播种（下一轮重发完整历史）"
    if stats.get("cap_hit"):
        return "上次到上下文上限（下一轮换新网页会话并播种历史）"
    if stats.get("has_history"):
        return "已有上下文（复用该网页会话，只发增量）"
    return "尚无历史（下一轮会把完整历史播种进去）"


def _one_line(text: str, limit: int = 200) -> str:
    """只保留第一行有效内容并限长：回复里不带换行，也不把内部细节整段倒进对话。"""
    rows = [row.strip() for row in str(text or "").splitlines() if row.strip()]
    flat = " ".join(rows[0].split()) if rows else ""
    return flat[:limit] + ("…" if len(flat) > limit else "")


__all__ = [
    "BRIDGE_COMMAND",
    "SEED_FLAG",
    "BridgeCommand",
    "CommandSpec",
    "REGISTRY",
    "handle_command",
    "is_command",
    "latest_user_text",
    "normalize_session_url",
    "parse_command",
]
