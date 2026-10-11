"""统一日志配置与脱敏（doc/design.md 10）。

约定：

* 库代码一律 ``logging.getLogger(__name__)``，不使用 ``print``；
* 级别由 ``LOG_LEVEL`` / ``CLAUDE_DEBUG`` 控制；
* 只在服务入口（``claude_api_server.py``）调用一次 :func:`configure_logging`；
* 每个请求用 :func:`set_request_id` 绑定一个短 id，日志里带 ``[request_id=...]``；
* :func:`redact` 用于任何可能含用户信息（URL、桶键、正文）的日志字段。
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

LOGGER_NAME = "claude_web"
_CORRELATION_FIELDS = ("request_id", "session_key", "task_id", "page_id")
_FORMAT = (
    "%(asctime)s %(levelname)s "
    "[request_id=%(request_id)s session_key=%(session_key)s "
    "task_id=%(task_id)s page_id=%(page_id)s] "
    "%(name)s: %(message)s"
)
_configured = False

_log_context: ContextVar[dict] = ContextVar(
    "claude_log_context",
    default={field: "-" for field in _CORRELATION_FIELDS},
)


class _CorrelationFilter(logging.Filter):
    """把当前关联上下文注入每条日志记录。"""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _log_context.get()
        for field in _CORRELATION_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, context.get(field, "-"))
        return True


def configure_logging(force: bool = False) -> None:
    """按配置设定 ``claude_web`` 命名空间日志级别，并安装关联过滤器（幂等）。"""
    global _configured
    if _configured and not force:
        return
    from . import config

    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(_CorrelationFilter())
        logger.addHandler(handler)
    if config.DEBUG:
        level = logging.DEBUG
    else:
        level = getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO)
    logger.setLevel(level)
    logger.propagate = True
    _configured = True


def new_request_id() -> str:
    """短请求 id：多客户端并发时用于把同一请求的多条日志串起来。"""
    return f"{int(time.time() % 100000):05d}-{uuid.uuid4().hex[:6]}"


def set_log_context(**fields: str) -> None:
    """更新当前异步任务的日志关联字段。"""
    _validate(fields)
    current = dict(_log_context.get())
    for field, value in fields.items():
        current[field] = str(value) if value else "-"
    _log_context.set(current)


def clear_log_context(*fields: str) -> None:
    """清除指定关联字段；不传参数时清除全部字段。"""
    target = fields or _CORRELATION_FIELDS
    current = dict(_log_context.get())
    for field in target:
        current[field] = "-"
    _log_context.set(current)


@contextmanager
def log_context(**fields: str) -> Iterator[None]:
    """临时绑定日志关联字段，退出时恢复调用方原上下文。"""
    _validate(fields)
    previous = _log_context.get()
    current = dict(previous)
    for field, value in fields.items():
        current[field] = str(value) if value else "-"
    token = _log_context.set(current)
    try:
        yield
    finally:
        _log_context.reset(token)


def _validate(fields: dict) -> None:
    unknown = set(fields) - set(_CORRELATION_FIELDS)
    if unknown:
        raise ValueError(f"未知日志关联字段：{sorted(unknown)}")


def set_request_id(request_id: str) -> None:
    set_log_context(request_id=request_id)


def current_request_id() -> str:
    return str(_log_context.get().get("request_id", "-"))


def current_log_context() -> dict:
    return dict(_log_context.get())


def redact(value: str, *, keep: int = 6) -> str:
    """把可能含用户信息的值脱敏为 ``前缀…<hash8>``。

    用于日志里的 URL、会话桶键等（doc/design.md 10：会话 URL 与桶键应脱敏或散列）。
    """
    if not value:
        return "-"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    prefix = value[:keep]
    return f"{prefix}…{digest}"
