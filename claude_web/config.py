"""配置加载与全部可调参数（doc/design.md 第 8 节）。

所有可调参数集中在这里，默认值即 ``.env.example`` 中列出的那一套。
其余模块通过 ``config.<NAME>`` **在运行时取属性**（而不是 ``from config import NAME``），
这样测试可以直接 ``patch.object(config, "NAME", value)`` 生效。

约定：``.env.example``、本模块、``README.md`` 三者构成配置契约，
``tests/test_config_drift.py`` 会核对它们是否漂移。
"""

import ipaddress
import logging
import os
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

# ==================== 0. 配置加载 (.env) ====================
# 用一个极简 .env 解析器，避免为读取配置引入额外依赖：
#   * 已存在的真实环境变量优先于 .env（便于临时覆盖 / CI）；
#   * 支持 `KEY=value`、`#` 注释、空行、值两侧引号。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[配置] 读取 %s 失败，将使用默认值：%s", path, exc)
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file(ENV_FILE)


def env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


def env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(key: str, default: str) -> List[str]:
    """把 ``a||b||c`` 的候选列表解析为字符串列表。

    只用 ``||`` 作分隔符（T3.5 / D3.5）：早先还按逗号切分，会把合法的 CSS 选择器
    切坏——例如 ``:is(a,b)`` 会变成 ``:is(a`` 与 ``b)`` 两个无效选择器，于是
    ``query_selector_all`` 静默命中不到任何节点。逗号在选择器 / 文案里都是合法字符，
    绝不能当分隔符。
    """
    raw = os.environ.get(key, default)
    parts: List[str] = []
    for piece in raw.split("||"):
        piece = piece.strip()
        if piece:
            parts.append(piece)
    return parts


def env_path(key: str, default: str) -> Path:
    """解析路径配置：相对路径一律以 ``PROJECT_ROOT`` 为基准（T3.5 / D3.5）。

    早先直接用 ``os.environ`` 的原始字符串（如 ``./user_data``），于是路径随**当前工作
    目录**漂移：换个目录启动就找不到登录态 / 状态文件，表现为「莫名要求重新登录」。
    以项目根为基准后，从任何 cwd 启动都指向同一份数据；``~`` 仍会展开；绝对路径原样保留。
    """
    raw = (os.environ.get(key, default) or "").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


# ==================== 1. 服务监听 ====================
HOST = env_str("HOST", "127.0.0.1")
PORT = env_int("PORT", 8003)


def is_loopback_host(host: str) -> bool:
    """HOST 是否只对本机可见（纯函数，便于单测与启动告警）。

    空串/``localhost`` 视为回环（与默认值 ``127.0.0.1`` 等价）；其余主机名一律
    按“可能对外”处理——安全告警宁可多喊一声，也不要默认放行。
    """
    raw = (host or "").strip().lower()
    if not raw or raw in ("localhost", "localhost.localdomain"):
        return True
    if raw.startswith("[") and raw.endswith("]"):  # [::1]
        raw = raw[1:-1]
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return raw.split(".")[0] in ("localhost", "ip6-localhost")


# ==================== 2. 目标站点 ====================
CLAUDE_WEBSITE = env_str("CLAUDE_WEBSITE", "https://claude.ai/new")
#: 每桶新开对话的落点（可由 CLAUDE_WEBSITE 覆盖）。
HOME_URL = CLAUDE_WEBSITE
#: /session/link 允许绑定的主机（子域自动允许；"*" = 不限制）。
LINK_ALLOWED_HOSTS = env_list("LINK_ALLOWED_HOSTS", "claude.ai")
#: 允许列表为 "*" 时启动要打强告警（等于对任意 HTTPS 主机开放绑定）。
if "*" in LINK_ALLOWED_HOSTS:
    logger.warning(
        "[配置] LINK_ALLOWED_HOSTS 含 '*'：/session/link 可绑定任意 HTTPS 主机，"
        "这会削弱绑定 URL 的防导航滥用保护，仅应在完全可信的环境使用。"
    )

#: 缺省初始会话链接：设置后，**默认会话桶**在未单独绑定时直接进入这条会话，
#: 而不是让桥自己新建一条（桥自建时会复用同一条）。
#:
#: 只作用于默认桶是刻意为之：其他桶仍在**各自**的网页会话里续写，避免多个桶共用
#: 一条网页会话而互相串线（doc/design.md 5.1「同一会话桶的页面状态由唯一任务顺序控制」）。
#: 需要让某个非默认桶也固定在一条会话上，用 POST /session/link 单独绑定。
#: 非法值（非 HTTPS / 非允许主机 / 非会话路径）会被忽略并打告警，回落到桥自建会话。
DEFAULT_LINK_URL = env_str("CLAUDE_DEFAULT_LINK_URL", "").strip()


# ==================== 3. 路径 ====================
# 相对路径一律以 PROJECT_ROOT 为基准解析（T3.5 / D3.5）：换 cwd 启动不再丢登录态。
USER_DATA_DIR = env_path("USER_DATA_DIR", "./user_data")
OUTPUT_DIR = env_path("OUTPUT_DIR", "./output")
SESSION_FILE = env_path("SESSION_FILE", "./user_data/.claude_state")
TASK_FILE_DIR = env_path("TASK_FILE_DIR", "./user_data/.claude_tasks")


# ==================== 4. 运行模式 / 调试 ====================
HEADLESS = env_bool("HEADLESS")
# 浏览器通道：留空 = Playwright 自带的 Chromium；填 "chrome" / "msedge" 则使用本机
# 已安装的真实浏览器。
#
# 注意：换浏览器**不会**让未登录的新 profile 通过站点校验。真正起作用的是
# 「已登录 + 已获得站点放行 cookie 的 profile」，见下面的 BROWSER_PROFILE_DIRECTORY。
BROWSER_CHANNEL = env_str("BROWSER_CHANNEL", "").strip()
# 浏览器启动方式：playwright = Playwright 直接启动；chrome-cdp = 启动普通 Chrome 后通过 CDP 连接。
# 后者不使用 Playwright 的浏览器启动参数，但 Cloudflare 仍可能要求人工完成安全校验。
BROWSER_LAUNCH_MODE = env_str("BROWSER_LAUNCH_MODE", "playwright").strip().lower()
# chrome-cdp 模式的本机调试端口；只监听 127.0.0.1，不应暴露到局域网或公网。
BROWSER_CDP_PORT = env_int("BROWSER_CDP_PORT", 9222)
# Chrome / Chromium 的用户目录内要使用的子 profile（例如 "Default" / "Profile 1"）。
# 配合 USER_DATA_DIR 指向**你已有登录态的那个用户目录的副本**才有意义。
# 留空 = 不传 --profile-directory（使用目录内的默认 profile）。
BROWSER_PROFILE_DIRECTORY = env_str("BROWSER_PROFILE_DIRECTORY", "").strip()
# 是否启用 Chromium 渲染进程沙箱。
#
# Playwright 默认**关闭**沙箱（传 ``--no-sandbox``），于是 Chrome 顶部会挂一条
# "unsupported command-line flag: --no-sandbox" 提示，渲染进程隔离也真的被弱化。
# 那条提示**只是警告**：不影响页面加载，也不会阻止登录页 / 安全校验页出现。
# Linux 容器里以 root 运行、内核不允许沙箱时需要设回 false。
BROWSER_SANDBOX = env_bool("BROWSER_SANDBOX", True)
# 是否使用操作系统钥匙串（macOS Keychain）而不是 Playwright 默认的 mock keychain。
#
# 背景：Playwright 默认追加 ``--password-store=basic --use-mock-keychain``，在 macOS 上
# 会用 mock key 加密 cookie —— 你日常 Chrome 用真实 Keychain 加密的 cookie 因此**无法
# 解密**，站点会认为会话无效（claude.ai 会踢成 ``/logout?involuntary=1``）。
#
# 开启后去掉这两个默认参数，用真实 Keychain 读取你自己 profile 里的凭据；
# 系统可能弹一次钥匙串授权，属预期。仅在你确实要复用已有 profile 时开启。
BROWSER_USE_OS_KEYCHAIN = env_bool("BROWSER_USE_OS_KEYCHAIN")
DEBUG = env_bool("CLAUDE_DEBUG")
SAVE_DEBUG_ARTIFACTS = env_bool("SAVE_DEBUG_ARTIFACTS")
ENABLE_DEBUG_DOM = env_bool("ENABLE_DEBUG_DOM")


# ==================== 5. 鉴权（可选）====================
#: 保护生成类 API 与管理端点的 Bearer Token；留空 = 不校验。
BRIDGE_TOKEN = env_str("BRIDGE_TOKEN", "")
#: /session/reset 的额外令牌保护；留空 = 不校验。
RESET_TOKEN = env_str("RESET_TOKEN", "")


# ==================== 6. 浏览器 ====================
CLAUDE_BROWSER_TIMEOUT = env_float("CLAUDE_BROWSER_TIMEOUT", 30.0)
READY_TIMEOUT_MS = env_int("READY_TIMEOUT_MS", 15000)
FILL_TIMEOUT_MS = env_int("FILL_TIMEOUT_MS", 10000)
FILL_RETRIES = env_int("FILL_RETRIES", 3)
MAX_PAGE_POOL = env_int("MAX_PAGE_POOL", 4)


# ==================== 7. 回复生成 / 结束检测 ====================
#: 单轮绝对超时（秒）。
RESPONSE_TIMEOUT_S = env_float("CLAUDE_TIMEOUT", 300.0)
#: 到达绝对超时但页面仍在生成时的额外延长额度（秒）；0 = 不延长。
RESPONSE_TIMEOUT_EXTEND_S = env_float("RESPONSE_TIMEOUT_EXTEND_S", 300.0)
#: 明确可安全重试的最大次数（默认保守）。
MAX_UPSTREAM_RETRIES = env_int("CLAUDE_RETRIES", 1)
RETRY_BACKOFF_S = env_float("RETRY_BACKOFF_S", 1.0)
#: 页面**静止**（没有在生成、正文也没有增长）时的轮询间隔（秒）。
POLL_INTERVAL_S = env_float("POLL_INTERVAL_S", 1.5)
#: 页面**活跃**（自述仍在生成 / 正文刚增长）时的轮询间隔（秒）。
#:
#: 只影响「多快看到页面上的变化」，不影响判定语义——结束 / 停滞阈值都是**时长**
#: （见 SETTLE_S / STALL_S），因此放快轮询不会削弱任何保护。
POLL_INTERVAL_ACTIVE_S = env_float("POLL_INTERVAL_ACTIVE_S", 0.4)
#: 页面自述已结束（``data-is-streaming=false`` / 无停止控件）后，内容静默多久算完成。
#: 这是「客户端等多久才拿到收尾」的主要项（时长，不随轮询间隔变化）。
SETTLE_S = env_float("SETTLE_S", 2.5)
#: 完全探不到生成信号（两路信号都拿不到）时的兜底静默时长，比 SETTLE_S 更保守。
SETTLE_NO_SIGNAL_S = env_float("SETTLE_NO_SIGNAL_S", 6.0)
#: 页面静止多久、且输入区里仍留着本轮提示词 → 判定「提交没生效」并快速失败。
SUBMIT_LOST_S = env_float("SUBMIT_LOST_S", 6.0)
#: 页面静止多久且正文无增长 → 判定停滞（快速失败，不再干等到绝对超时）。
STALL_S = env_float("STALL_S", 30.0)
#: 提交前若页面仍在生成：先等这么久（秒）；仍未结束就点一次停止控件（T2.7）。
PRE_SUBMIT_WAIT_S = env_float("PRE_SUBMIT_WAIT_S", 3.0)


# ==================== 8. 会话 ====================
SESSION_SCOPING = env_bool("SESSION_SCOPING", True)
SESSION_KEY_HEADER = env_str("SESSION_KEY_HEADER", "X-Claude-Session")
SESSION_SCOPING_BY_UA = env_bool("SESSION_SCOPING_BY_UA", True)
SESSION_KEY_MAX_LEN = env_int("SESSION_KEY_MAX_LEN", 64)
MAX_SESSION_BUCKETS = env_int("MAX_SESSION_BUCKETS", 4)
BUCKET_LOCK_TIMEOUT_S = env_float("BUCKET_LOCK_TIMEOUT_S", 120.0)
#: 管理类操作（reset / link / unlink / modal）取桶锁的**短超时**：桶正忙时快速
#: 返回 ``session_busy``，而不是让管理命令也排队上百秒、更不会强行关掉在途任务的页面。
ADMIN_LOCK_TIMEOUT_S = env_float("ADMIN_LOCK_TIMEOUT_S", 10.0)
BUCKET_IDLE_TTL_S = env_float("BUCKET_IDLE_TTL_S", 900.0)
#: 后台空闲回收任务的间隔（秒）；0 = 不启动周期回收（只按需回收）。
REAP_INTERVAL_S = env_float("REAP_INTERVAL_S", 60.0)
#: 内存里缓存的会话状态上限（超出按最久未用逐出）；状态本就落盘，逐出内存副本安全。
MAX_SESSION_STATE_CACHE = env_int("MAX_SESSION_STATE_CACHE", 64)
NEW_SESSION_ON_START = env_bool("CLAUDE_NEW_SESSION")
TOOL_RESULT_MAX_CHARS = env_int("TOOL_RESULT_MAX_CHARS", 20000)
#: 播种 prompt 里的「环境说明」：告诉模型本机就是工作目录，不必向用户索要地址 /
#: 让用户手动执行（参考 chatgpt-bridge 的 SEED_ENV_NOTE）。
SEED_ENV_NOTE = env_str(
    "SEED_ENV_NOTE",
    "[环境说明] 项目就在本机的当前工作目录里，你可以直接下命令（如 git status / "
    "git add / git commit / git log / ls / cat）来完成查看与修改，"
    "无需向用户索要仓库地址，也无需用户手动执行。",
)
#: 工具说明里是否输出每个工具的完整 JSON Schema（默认 false = 紧凑的 name(必填参数) 形态）。
TOOLS_INSTRUCTION_VERBOSE = env_bool("TOOLS_INSTRUCTION_VERBOSE")
#: 工具说明里单条描述的最大字符数（0 = 不限制）。
TOOLS_DESC_MAX_CHARS = env_int("TOOLS_DESC_MAX_CHARS", 200)
#: 默认会话桶键。
DEFAULT_SESSION_KEY = "default"


# ==================== 9. 上下文预算 ====================
CLAUDE_MAX_CONTEXT_CHARS = env_int("CLAUDE_MAX_CONTEXT_CHARS", 100000)
SYSTEM_MAX_CHARS = env_int("SYSTEM_MAX_CHARS", 8000)


# ==================== 10. API ====================
ENABLE_RESPONSES_API = env_bool("ENABLE_RESPONSES_API")
STREAM_KEEPALIVE_S = env_float("STREAM_KEEPALIVE_S", 10.0)
TOOL_CALL_BUFFER = env_bool("TOOL_CALL_BUFFER", True)
TOOL_CALL_FENCE = env_str("TOOL_CALL_FENCE", "tool_call")


# ==================== 11. 任务快照 ====================
TASK_SNAPSHOT_ENABLED = env_bool("TASK_SNAPSHOT_ENABLED", True)
TASK_GOAL_MAX_CHARS = env_int("TASK_GOAL_MAX_CHARS", 2000)
TASK_KEEP_MESSAGES = env_int("TASK_KEEP_MESSAGES", 8)


# ==================== 12. DOM 选择器（候选，真实探测后确认）====================
INPUT_SELECTORS = env_list(
    "INPUT_SELECTORS",
    'div[contenteditable="true"][role="textbox"]'
    '||div.ProseMirror[contenteditable="true"]'
    '||div[contenteditable="true"]||textarea',
)
SEND_BUTTON_SELECTORS = env_list(
    "SEND_BUTTON_SELECTORS",
    'button[aria-label*="Send"]||button[data-testid*="send"]||button[type="submit"]',
)
RESPONSE_SELECTORS = env_list(
    "RESPONSE_SELECTORS",
    'div[data-testid="assistant-message"]||[data-message-author-role="assistant"]'
    '||div.font-claude-message||div[class*="font-claude-message"]',
)
# 用户消息节点（只用于「本轮提问是否已经落到页面上」的结构信号，不参与正文读取）。
USER_MESSAGE_SELECTORS = env_list(
    "USER_MESSAGE_SELECTORS",
    'div[data-testid="user-message"]||[data-message-author-role="user"]',
)
GENERATING_SELECTORS = env_list(
    "GENERATING_SELECTORS",
    'button[aria-label*="Stop"]||button[data-testid*="stop"]||[data-is-streaming="true"]',
)
NEW_CHAT_SELECTORS = env_list(
    "NEW_CHAT_SELECTORS",
    'a[href="/new"]||button[aria-label*="New chat"]||a[aria-label*="New chat"]',
)
AUTH_INDICATORS = env_list(
    "AUTH_INDICATORS",
    'input[type="password"]||a[href*="/login"]||button[data-testid*="login"]',
)
CHALLENGE_INDICATORS = env_list(
    "CHALLENGE_INDICATORS",
    'iframe[title*="challenge"]||div[class*="cf-challenge"]',
)
# 站点安全校验页的文案特征（小写子串，同时用于 <title> 与页面可见文本）。
# 只用于**识别并如实上报** challenge_required；本项目不自动勾选 / 绕过任何验证。
CHALLENGE_TEXT_PATTERNS = env_list(
    "CHALLENGE_TEXT_PATTERNS",
    "performing security verification||security verification||verify you are human||"
    "checking your browser||just a moment||attention required||"
    "请稍候||安全验证",
)
# 扫描挑战文案时要**剔除**的子树：命中这些容器里的文本属于历史内容（会话正文、
# 消息列表、会话链接），不是「站点在拦你」的证据。
#
# 事故背景（doc/cloudfare.md 3.4）：一个引用本项目 README 的会话，正文整段引用了
# "performing security verification"，而旧实现扫的是整页 innerText、且只排除助手回复
# 节点，于是对**每个**请求都误报 challenge_required —— 用户明明在同一页面上对话正常。
CHALLENGE_EXCLUDE_SELECTORS = env_list(
    "CHALLENGE_EXCLUDE_SELECTORS",
    'div[data-testid="assistant-message"]||div[data-testid="user-message"]'
    "||[data-message-author-role]||div.cds-user-message-body||div.font-claude-message"
    '||div[class*="font-claude-message"]||[data-testid="conversation-turn"]'
    '||[data-testid="chat-message"]||a[href*="/chat/"]',
)
CONTEXT_LIMIT_PATTERNS = env_list(
    "CONTEXT_LIMIT_PATTERNS",
    "context limit||conversation is too long||maximum length||"
    "对话长度上限||已达长度限制",
)
# 回复节点里要剔除的**页面外壳**（doc/design.md 4.4）。
#
# assistant 消息节点不只是模型正文，实测（2026-10-12 在真实页面逐节点核对）还包含：
#   * ``sr-only`` 的无障碍标题，内容形如 ``Claude responded: <整段正文>``
#     —— 不剔除就会让客户端看到「同一段正文出现两遍」；
#   * 动作栏（Copy / Read aloud / 点赞 / 重试）；
#   * 时间戳 ``<time>``（``just now`` / ``13 minutes ago``）；
#   * 推广行（``Claude works directly with your codebase`` + Install 按钮）。
#
# 改版后若发现新的外壳混进正文，把选择器加进来即可（无需改代码）。
REPLY_CHROME_SELECTORS = env_list(
    "REPLY_CHROME_SELECTORS",
    "script||style||noscript||template||time||button||[role=\"button\"]"
    "||[role=\"toolbar\"]||[role=\"status\"]||[aria-hidden=\"true\"]||.sr-only"
    "||[class*=\"sr-only\"]||[class*=\"visually-hidden\"]||[class*=\"screen-reader\"]"
    "||[data-testid*=\"message-actions\"]||[data-testid*=\"action-bar\"]"
    "||[data-testid*=\"growth\"]||[data-testid*=\"promo\"]||[data-testid*=\"upsell\"]",
)

# ---- 额度用尽 / 升级弹窗（doc/design.md 4.3）----
#
# 额度用尽后 claude.ai 会弹出「Upgrade to keep chatting / You hit your 5-hour message
# limit」这类 ``role="dialog"`` 弹窗。它盖住输入区、抢走焦点，于是填充看似成功而 Enter
# 不生效，或者回复迟迟不来——桥接会误报「提交未生效 / 生成停滞」。这个开关让适配器在
# 提交前把这类弹窗关掉（只点关闭 / 先不用类控件，绝不点升级或付费按钮）。
MODAL_DISMISS_ENABLED = env_bool("MODAL_DISMISS_ENABLED", True)
#: 弹窗容器候选选择器。
MODAL_CONTAINER_SELECTORS = env_list(
    "MODAL_CONTAINER_SELECTORS",
    '[role="dialog"]||[aria-modal="true"]||[data-testid*="modal"]||[data-testid*="dialog"]',
)
#: 弹窗内「关闭」类控件的候选选择器（优先级最高）。
MODAL_CLOSE_SELECTORS = env_list(
    "MODAL_CLOSE_SELECTORS",
    'button[aria-label="Close"]||button[aria-label*="close"]||button[aria-label*="dismiss"]'
    '||button[data-testid*="close"]||button[data-testid*="dismiss"]',
)
#: 关闭控件的文案特征（小写子串）；用于「先不用 / 稍后再说」这类没有 aria-label 的按钮。
#: ⚠️ 只放「不花钱就能关掉」的措辞：升级 / 购买 / 订阅类按钮绝不能被它匹配到。
MODAL_CLOSE_TEXT_PATTERNS = env_list(
    "MODAL_CLOSE_TEXT_PATTERNS",
    "not now||maybe later||no thanks||dismiss||close||稍后||以后再说||暂不||先不用||关闭",
)
#: 命中这些文案（小写子串）的可见弹窗才算「额度用尽 / 升级」弹窗。
MODAL_TEXT_PATTERNS = env_list(
    "MODAL_TEXT_PATTERNS",
    "message limit||usage limit||limit resets||resets at||upgrade to keep||"
    "upgrade for higher limits||额度||已达到上限||升级套餐",
)


def conversation_exclude_selectors() -> list:
    """**会话内容**子树（助手回复、用户消息、会话列表等）——不是页面自身文案的证据。

    两套配置都尊重：``CHALLENGE_EXCLUDE_SELECTORS`` 覆盖用户消息 / 会话列表，
    ``RESPONSE_SELECTORS`` 可能被部署方改过，因此取并集而不是二选一。

    两个「在整页文本里找一句特征文案」的扫描共用这一份清单：挑战页扫描与「会话到顶」
    扫描。用户完全可能在会话里引用同样的字样——实测（2026-10-12，CDP 逐节点核对）：
    一个把本仓库 ``.env.example`` 里的
    ``CONTEXT_LIMIT_PATTERNS=context limit||conversation is too long||...``
    读进会话的任务，会让「会话到顶」扫描**每轮**都命中，把一个完全正常的会话误报成
    ``context_limit_reached``（同一类事故见 doc/cloudfare.md 3.4）。
    """
    merged: list = []
    for selector in [*CHALLENGE_EXCLUDE_SELECTORS, *RESPONSE_SELECTORS]:
        text = str(selector).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


def challenge_exclude_selectors() -> list:
    """兼容别名：挑战页扫描与「会话到顶」扫描用同一份会话剔除清单。"""
    return conversation_exclude_selectors()


# ==================== 13. 日志 ====================
LOG_LEVEL = env_str("LOG_LEVEL", "INFO")


# ==================== 14. 模型别名 ====================
# 桥接层公开的**逻辑模型别名**。首期只公布一个 ``claude-web``：网页版没有稳定
# 且经真实探测确认的模型选择契约，不能在 /v1/models 里虚构整套 Claude 型号。
# 需要按别名映射到网页上的模型标签时，用 ``MODEL_ALIASES=alias=UI标签,alias2=...``
# 显式配置（未验证的映射不默认公开）。
DEFAULT_MODEL_ALIAS = "claude-web"


def _parse_model_aliases(raw: str) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        alias, _, label = chunk.partition("=")
        alias = alias.strip()
        if alias:
            aliases[alias] = label.strip()
    return aliases


MODEL_ALIASES: Dict[str, str] = _parse_model_aliases(env_str("MODEL_ALIASES", ""))
if DEFAULT_MODEL_ALIAS not in MODEL_ALIASES:
    MODEL_ALIASES[DEFAULT_MODEL_ALIAS] = ""

#: 兼容导出：部分调用方按 ``config.SUPPORTED_MODELS`` 读取别名清单。
SUPPORTED_MODELS = list(MODEL_ALIASES.keys())
