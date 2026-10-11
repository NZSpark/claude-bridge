"""统一异常、内部错误类别与到 HTTP 的状态映射（doc/design.md 第 9 节）。

设计要点：

* **单一错误词汇表**：网页驱动层、会话层、API 层都用 :class:`ErrorCode` 的同一组
  字符串，而不是各自定义一套；HTTP 状态由 :func:`http_status_for` 集中决定。
* **不泄漏内部细节**：异常对象只携带可安全外发的 ``message`` 与可选 ``detail``
  （必须是布尔/计数/类别，不能是页面正文、Cookie 或堆栈）。
"""

from __future__ import annotations

import asyncio
import inspect
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    """内部错误类别（doc/design.md 9.1）。"""

    BROWSER_UNAVAILABLE = "browser_unavailable"
    AUTHENTICATION_REQUIRED = "authentication_required"
    CHALLENGE_REQUIRED = "challenge_required"
    COMPOSER_NOT_FOUND = "composer_not_found"
    PROMPT_SUBMISSION_FAILED = "prompt_submission_failed"
    REPLY_NOT_FOUND = "reply_not_found"
    REPLY_EXTRACTION_FAILED = "reply_extraction_failed"
    GENERATION_TIMEOUT = "generation_timeout"
    GENERATION_STALLED = "generation_stalled"
    CONTEXT_LIMIT_REACHED = "context_limit_reached"
    SESSION_CONFLICT = "session_conflict"
    SESSION_BUSY = "session_busy"
    TOOL_CALL_PARSE_ERROR = "tool_call_parse_error"
    UNSUPPORTED_INPUT = "unsupported_input"
    # 请求 / 鉴权层（设计文档 9.2 的“请求结构无效 / 鉴权失败”）
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    NOT_FOUND = "not_found"


#: 内部错误类别 -> HTTP 状态（doc/design.md 9.2）。
_HTTP_STATUS: Dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNSUPPORTED_INPUT: 400,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.BROWSER_UNAVAILABLE: 503,
    ErrorCode.AUTHENTICATION_REQUIRED: 503,
    ErrorCode.CHALLENGE_REQUIRED: 503,
    ErrorCode.SESSION_BUSY: 503,
    ErrorCode.COMPOSER_NOT_FOUND: 502,
    ErrorCode.PROMPT_SUBMISSION_FAILED: 502,
    ErrorCode.REPLY_NOT_FOUND: 502,
    ErrorCode.REPLY_EXTRACTION_FAILED: 502,
    ErrorCode.CONTEXT_LIMIT_REACHED: 502,
    ErrorCode.SESSION_CONFLICT: 502,
    ErrorCode.GENERATION_TIMEOUT: 504,
    ErrorCode.GENERATION_STALLED: 504,
    ErrorCode.TOOL_CALL_PARSE_ERROR: 422,
}


def http_status_for(code: ErrorCode) -> int:
    """错误类别 -> HTTP 状态码（未知类别保守返回 502）。"""
    return _HTTP_STATUS.get(code, 502)


class BridgeError(RuntimeError):
    """Bridge 领域异常的统一基类。"""

    #: 该异常的默认内部错误类别。
    code: ErrorCode = ErrorCode.BROWSER_UNAVAILABLE

    def __init__(
        self,
        message: str,
        *,
        code: Optional[ErrorCode] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        #: 可安全外发的诊断信息（布尔 / 计数 / 类别），禁止放入正文或凭据。
        self.detail: Dict[str, Any] = dict(detail or {})

    @property
    def http_status(self) -> int:
        return http_status_for(self.code)

    def error_body(self) -> Dict[str, Any]:
        """构造 OpenAI 风格的安全错误对象。"""
        payload: Dict[str, Any] = {
            "error": {
                "message": self.message,
                "type": self.code.value,
                "code": self.code.value,
            }
        }
        if self.detail:
            payload["error"]["detail"] = self.detail
        return payload


class ConfigurationError(BridgeError):
    """配置缺失、非法或彼此冲突。"""

    code = ErrorCode.INVALID_REQUEST


class BrowserError(BridgeError):
    """Claude 网页 / Playwright 交互领域错误。"""


class NotReadyError(BrowserError):
    """浏览器或登录状态尚未就绪，无法处理请求。"""

    code = ErrorCode.BROWSER_UNAVAILABLE


class AuthenticationRequiredError(BrowserError):
    code = ErrorCode.AUTHENTICATION_REQUIRED


class ChallengeRequiredError(BrowserError):
    code = ErrorCode.CHALLENGE_REQUIRED


class ComposerNotFoundError(BrowserError):
    code = ErrorCode.COMPOSER_NOT_FOUND


class PromptSubmissionError(BrowserError):
    code = ErrorCode.PROMPT_SUBMISSION_FAILED


class ReplyNotFoundError(BrowserError):
    code = ErrorCode.REPLY_NOT_FOUND


class ReplyExtractionError(BrowserError):
    code = ErrorCode.REPLY_EXTRACTION_FAILED


class GenerationTimeoutError(BrowserError):
    """等待网页版回复超时。与 :class:`GenerationStalledError` 区分。"""

    code = ErrorCode.GENERATION_TIMEOUT


class GenerationStalledError(BrowserError):
    code = ErrorCode.GENERATION_STALLED


class ContextLimitError(BrowserError):
    """网页会话已达上下文长度上限（网页版会停止响应，必须换新会话）。"""

    code = ErrorCode.CONTEXT_LIMIT_REACHED


class SessionConflictError(BrowserError):
    code = ErrorCode.SESSION_CONFLICT


class SessionBusyError(BridgeError):
    """某个会话桶正忙（同桶已有请求在跑且等待超时）。"""

    code = ErrorCode.SESSION_BUSY


class SessionStateError(BridgeError):
    """会话状态读取、写入或迁移失败。"""

    code = ErrorCode.SESSION_CONFLICT


class ToolError(BridgeError):
    """工具调用解析 / 校验领域错误。"""


class ToolCallParseError(ToolError):
    code = ErrorCode.TOOL_CALL_PARSE_ERROR


class UnsupportedInputError(BridgeError):
    code = ErrorCode.UNSUPPORTED_INPUT


class InvalidRequestError(BridgeError):
    code = ErrorCode.INVALID_REQUEST


class UnauthorizedError(BridgeError):
    code = ErrorCode.UNAUTHORIZED


# ==================== 页面存活探测 ====================
#: 页面失效类异常在 Playwright 里的措辞（不 import playwright，保持惰性依赖）。
_PAGE_LOST_TOKENS = (
    "target page, context or browser has been closed",
    "target page or browser has been closed",
    "target crashed",
    "browser has been closed",
    "page has been closed",
    "context has been closed",
)


def is_page_lost_error(exc: BaseException) -> bool:
    """该异常是否表示「页面已经没了」（而不是选择器没命中 / 普通交互失败）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in _PAGE_LOST_TOKENS)


def is_timeout_error(exc: BaseException) -> bool:
    """该异常是否只是「等超时」——定位元素时只有它算「没命中」。"""
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in type(exc).__name__.lower()


def page_alive(page: Any) -> bool:
    """页面是否还活着（快路径）。

    没有 ``is_closed`` 的实现（测试替身 / 旧版对象）按存活处理。
    """
    if page is None:
        return False
    is_closed = getattr(page, "is_closed", None)
    if is_closed is None:
        return True
    try:
        closed = is_closed()
    except Exception:  # noqa: BLE001
        return True
    if inspect.isawaitable(closed):
        return True
    return not closed


def page_url(page: Any) -> str:
    """页面当前 URL（读不到时返回空串）。"""
    try:
        url = getattr(page, "url", "") or ""
    except Exception:  # noqa: BLE001
        return ""
    return url if isinstance(url, str) else ""


#: 判活的「慢路径」探测等待上限（秒）：挂起的渲染进程会让求值永不返回。
PAGE_PROBE_TIMEOUT_S = 5.0


async def page_responds(page: Any, timeout_s: float = PAGE_PROBE_TIMEOUT_S) -> bool:
    """页面是否还能执行 JS（慢路径判活）。

    没有 ``evaluate`` 的实现按可响应处理，与 :func:`page_alive` 一致。
    """
    if page is None:
        return False
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return True
    try:
        probe = evaluate("() => 1")
        if inspect.isawaitable(probe):
            await asyncio.wait_for(probe, timeout=max(0.1, float(timeout_s)))
    except Exception:  # noqa: BLE001
        return False
    return True


def page_lost_reason(page: Any) -> str:
    """给日志 / 错误文案用的可读原因（不回显页面正文）。"""
    if page is None:
        return "标签已失效（没有可用的会话页面）"
    url = page_url(page) or "?"
    if not page_alive(page):
        return f"标签已失效（页面已关闭，URL={url}）"
    return f"标签已失效（页面不可用，URL={url}，疑似渲染进程崩溃或页面已被回收）"


async def page_usable_reason(
    page: Any, timeout_s: float = PAGE_PROBE_TIMEOUT_S
) -> Optional[str]:
    """页面当前是否可用：不可用时返回可读原因，可用时 ``None``。

    先走快路径 :func:`page_alive`（标签被关掉），再走慢路径 :func:`page_responds`
    （渲染进程崩溃 / 挂起时 ``is_closed()`` 仍报 False）。

    用途之一是**防止误报成功**：页面死掉后 ``PageStatus`` 会退回默认值
    （``authenticated=True``、composer 取值陈旧），只看那些字段会把一次失败探测
    报成 ``ok: true``。
    """
    if not page_alive(page):
        return page_lost_reason(page)
    if not await page_responds(page, timeout_s=timeout_s):
        return page_lost_reason(page) + "（页面已无响应：JS 求值报错或超时）"
    return None
