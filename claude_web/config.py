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
    """把 ``a||b||c``（或逗号分隔）的候选列表解析为字符串列表。"""
    raw = os.environ.get(key, default)
    parts: List[str] = []
    for chunk in raw.replace("||", "\n").split("\n"):
        for piece in chunk.split(","):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return parts


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

#: 缺省初始会话链接：设置后，**默认会话桶**在未单独绑定时直接进入这条会话，
#: 而不是每次新开对话。
#:
#: 只作用于默认桶是刻意为之：其他桶仍各自新建对话，避免多个桶共用一条网页会话
#: 而互相串线（doc/design.md 5.1「同一会话桶的页面状态由唯一任务顺序控制」）。
#: 需要让某个非默认桶也固定在一条会话上，用 POST /session/link 单独绑定。
#: 非法值（非 HTTPS / 非允许主机 / 非会话路径）会被忽略并打告警，回落到新开对话。
DEFAULT_LINK_URL = env_str("CLAUDE_DEFAULT_LINK_URL", "").strip()


# ==================== 3. 路径 ====================
USER_DATA_DIR = env_str("USER_DATA_DIR", "./user_data")
OUTPUT_DIR = env_str("OUTPUT_DIR", "./output")
SESSION_FILE = Path(env_str("SESSION_FILE", "./user_data/.claude_state"))
TASK_FILE_DIR = Path(env_str("TASK_FILE_DIR", "./user_data/.claude_tasks"))


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
POLL_INTERVAL_S = env_float("POLL_INTERVAL_S", 1.5)
STABLE_POLLS = env_int("STABLE_POLLS", 4)
STALL_POLLS = env_int("STALL_POLLS", 20)
QUIET_POLLS = env_int("QUIET_POLLS", 4)


# ==================== 8. 会话 ====================
SESSION_SCOPING = env_bool("SESSION_SCOPING", True)
SESSION_KEY_HEADER = env_str("SESSION_KEY_HEADER", "X-Claude-Session")
SESSION_SCOPING_BY_UA = env_bool("SESSION_SCOPING_BY_UA", True)
SESSION_KEY_MAX_LEN = env_int("SESSION_KEY_MAX_LEN", 64)
MAX_SESSION_BUCKETS = env_int("MAX_SESSION_BUCKETS", 4)
BUCKET_LOCK_TIMEOUT_S = env_float("BUCKET_LOCK_TIMEOUT_S", 120.0)
BUCKET_IDLE_TTL_S = env_float("BUCKET_IDLE_TTL_S", 900.0)
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
CONTEXT_LIMIT_PATTERNS = env_list(
    "CONTEXT_LIMIT_PATTERNS",
    "context limit||conversation is too long||maximum length||"
    "对话长度上限||已达长度限制",
)


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
