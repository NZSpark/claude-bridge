"""FastAPI 应用、路由、鉴权与错误映射（doc/design.md 3、9、10）。

本模块只负责 HTTP 契约：请求校验、鉴权、请求 ID、错误到状态码的映射。
它**不直接查询 DOM**，所有浏览器交互都通过 :class:`~claude_web.service.ChatService`。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from . import config
from .api.chat_adapter import (
    StreamEncoder,
    build_completion_response,
    build_sse_headers,
    estimate_usage,
    to_task,
)
from .api.responses_adapter import (
    build_response_object,
    response_completed_event,
    response_created_event,
    response_events,
    response_text_delta_event,
    store_thread,
    to_task_from_responses,
)
from .api.streaming_adapter import sse_comment, sse_error
from .bridge_commands import handle_command
from .browser.selectors import SELECTOR_VERSION
from .errors import (
    BridgeError,
    ErrorCode,
    InternalError,
    InvalidRequestError,
    UnauthorizedError,
)
from .logging_setup import current_request_id, log_context, new_request_id, redact
from .metrics import metrics
from .models import ChatCompletionRequest, ChatTask, ModelCard, advertised_models
from .prompting import delta_piece
from .service import ChatService, service

logger = logging.getLogger(__name__)


# ==================== 请求辅助 ====================


def _sanitize_key(raw: Optional[str]) -> str:
    """桶键清洗：只保留安全字符并限制长度。"""
    if not raw:
        return config.DEFAULT_SESSION_KEY
    cleaned = "".join(ch for ch in str(raw) if ch.isalnum() or ch in "-_.@:")
    cleaned = cleaned.strip("-_.")[: config.SESSION_KEY_MAX_LEN]
    return cleaned or config.DEFAULT_SESSION_KEY


def _session_key(request: Request, body: Optional[Dict[str, Any]]) -> str:
    """按优先级选择会话桶（doc/design.md 5.1）。"""
    if not config.SESSION_SCOPING:
        return config.DEFAULT_SESSION_KEY
    header = request.headers.get(config.SESSION_KEY_HEADER)
    raw = header
    if not raw and isinstance(body, dict):
        user = body.get("user")
        raw = user if isinstance(user, str) else None
    if not raw and config.SESSION_SCOPING_BY_UA:
        raw = request.headers.get("user-agent")
    return _sanitize_key(raw)


def _check_bridge_auth(request: Request) -> None:
    """校验可选的 Bearer Token（保护生成类 API 与管理端点）。"""
    token = config.BRIDGE_TOKEN
    if not token:
        return
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise UnauthorizedError("缺少 Bearer Token")
    provided = header[7:].strip()
    if not secrets.compare_digest(provided, token):
        raise UnauthorizedError("Bearer Token 无效")


def _check_reset_token(request: Request) -> None:
    token = config.RESET_TOKEN
    if not token:
        return
    provided = request.headers.get("x-reset-token", "")
    if not secrets.compare_digest(provided, token):
        raise UnauthorizedError("重置令牌无效", code=ErrorCode.UNAUTHORIZED)


async def _read_json(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise InvalidRequestError("请求体必须是合法 JSON") from exc
    if not isinstance(body, dict):
        raise InvalidRequestError("请求体必须是 JSON 对象")
    return body


def _error_response(exc: BridgeError, request_id: str) -> JSONResponse:
    return JSONResponse(
        exc.error_body(),
        status_code=exc.http_status,
        headers={"X-Request-Id": request_id},
    )


# ==================== 应用 ====================


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.is_loopback_host(config.HOST):
        logger.warning(
            "\n[启动告警] HOST=%s 不是回环地址：任何能访问该地址的人都能复用你的 Claude "
            "网页登录态与额度，且本服务不对此做保护。请改回 127.0.0.1 并使用 SSH 端口转发 / VPN。",
            config.HOST,
        )
    logger.info(
        "[启动] ClaudeBridge 监听 %s:%s（selector_version=%s）",
        config.HOST,
        config.PORT,
        SELECTOR_VERSION,
    )
    if not config.BRIDGE_TOKEN:
        logger.warning("[启动] 未设置 BRIDGE_TOKEN：仅适合本机使用，请勿暴露到公网。")
    if not config.ENABLE_RESPONSES_API:
        logger.info("[启动] Responses API 未启用（ENABLE_RESPONSES_API=false）。")
    # 启动期配置体检（T3.6 / D3.6）：旧键静默失效、跨键约束冲突都只打日志，
    # 不拒绝启动——默认值本身就命中一条（锁超时 < 单轮超时），否则默认配置都跑不起来。
    for problem in config.config_warnings():
        logger.warning("[启动配置] %s", problem)
    # 生效配置摘要（不含密钥）：出事后第一时间能核对「当时到底跑的是什么值」。
    logger.info("[启动配置] 生效值：%s", " | ".join(config.summary_lines()))
    reaper = asyncio.create_task(_reap_idle_loop())
    try:
        yield
    finally:
        reaper.cancel()
        try:
            await reaper
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        try:
            await service.driver.close()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title="ClaudeBridge", version="0.1.0", lifespan=lifespan)


async def _reap_idle_loop() -> None:
    """周期性关闭空闲超时的页面（T2.3）。

    页面池的 ``reap_idle`` / ``mark_busy`` 此前没有任何调用点：``BUCKET_IDLE_TTL_S``
    形同虚设，而 ``MAX_PAGE_POOL`` 的 LRU 会驱逐**正在生成**的页面。这里把回收接上；
    忙碌标记在 :meth:`ChatService.run` 里设置，因此回收 / 驱逐都会跳过在用页面。
    """
    interval = config.REAP_INTERVAL_S
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            reaped = await service.driver.reap_idle()
            if reaped:
                logger.info("[页面池] 空闲回收：关闭 %s 个页面（%s）", len(reaped), ", ".join(reaped))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("[页面池] 空闲回收失败：%s", type(exc).__name__)


@app.exception_handler(BridgeError)
async def _bridge_error_handler(request: Request, exc: BridgeError) -> JSONResponse:
    return _error_response(exc, current_request_id())


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("[服务] 未处理异常：%s", type(exc).__name__)
    # 内部 bug 用 500（InternalError），不要伪装成 503「未就绪」——那会让客户端
    # 当成登录 / 页面问题去重试（T3.1）。
    safe = InternalError("服务内部错误", detail={"error": type(exc).__name__})
    return _error_response(safe, current_request_id())


@app.middleware("http")
async def _request_context(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or new_request_id()
    with log_context(request_id=request_id):
        response = await call_next(request)
    response.headers.setdefault("X-Request-Id", request_id)
    return response


# ---- 基础端点 ----


@app.get("/")
async def root() -> Dict[str, Any]:
    return {
        "service": "ClaudeBridge",
        "version": "0.1.0",
        "endpoints": ["/v1/models", "/v1/chat/completions", "/healthz", "/readiness"],
    }


@app.get("/healthz")
async def healthz() -> Dict[str, str]:
    """进程存活 ≠ 能处理请求；可用性请查 /readiness。"""
    return {"status": "ok"}


@app.get("/readiness")
async def readiness() -> JSONResponse:
    report = await service.driver.readiness()
    return JSONResponse(report.to_payload(), status_code=200 if report.ok else 503)


@app.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    return metrics.snapshot()


@app.get("/diagnostics")
async def diagnostics() -> Dict[str, Any]:
    """汇总浏览器、会话桶、选择器与任务状态；**默认不返回正文**。"""
    report = await service.driver.readiness()
    return {
        "selector_version": SELECTOR_VERSION,
        "browser": report.to_payload(),
        "buckets": {
            "pages": service.driver.pool.buckets(),
            "active": service.locks.active_buckets(),
        },
        "sessions": service.lifecycle.store.snapshot(),
        "locks": service.locks.active_buckets(),
        "tasks": service.tasks.recent(),
        "metrics": metrics.snapshot(),
        "config": {
            "session_scoping": config.SESSION_SCOPING,
            "max_session_buckets": config.MAX_SESSION_BUCKETS,
            "enable_responses_api": config.ENABLE_RESPONSES_API,
            "auth_enabled": bool(config.BRIDGE_TOKEN),
        },
    }


@app.get("/v1/models")
async def list_models() -> Dict[str, Any]:
    cards = [
        ModelCard(id=card["id"], owned_by="claude-web-bridge").model_dump()
        for card in advertised_models()
    ]
    return {"object": "list", "data": cards}


# ---- 会话管理 ----


@app.post("/session/reset")
async def reset_session(request: Request) -> Dict[str, Any]:
    _check_bridge_auth(request)
    _check_reset_token(request)
    body = await _read_json(request)
    bucket = _sanitize_key(body.get("session") or body.get("bucket") or _session_key(request, body))
    await service.admin_reset(bucket)
    return {"status": "reset", "session": bucket}


@app.post("/session/link")
async def link_session(request: Request) -> Dict[str, Any]:
    _check_bridge_auth(request)
    body = await _read_json(request)
    bucket = _sanitize_key(body.get("session") or body.get("bucket") or _session_key(request, body))
    url = str(body.get("url") or "")
    seed = bool(body.get("seed"))
    normalized = await service.admin_link(bucket, url, seed=seed)
    return {"status": "linked", "session": bucket, "url": redact(normalized), "seed": seed}


@app.post("/session/unlink")
async def unlink_session(request: Request) -> Dict[str, Any]:
    _check_bridge_auth(request)
    body = await _read_json(request)
    bucket = _sanitize_key(body.get("session") or body.get("bucket") or _session_key(request, body))
    service.admin_unlink(bucket)
    return {"status": "unlinked", "session": bucket}


@app.get("/session/{bucket}")
async def session_status(bucket: str) -> Dict[str, Any]:
    return service.lifecycle.status(_sanitize_key(bucket))


# ---- 生成端点 ----


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _check_bridge_auth(request)
    body = await _read_json(request)
    bucket = _session_key(request, body)
    with log_context(session_key=redact(bucket)):
        try:
            parsed = ChatCompletionRequest(**body)
        except ValidationError as exc:
            raise InvalidRequestError(
                "请求字段校验失败", detail={"errors": exc.error_count()}
            ) from exc
        task = to_task(parsed, bucket)
        # 桥内命令（/bridge ...）：本地应答，**不**发给网页版，因此浏览器没起来也能用。
        command_reply = await handle_command(parsed.messages, service, bucket)
        if command_reply is not None:
            if parsed.stream:
                return StreamingResponse(
                    _stream_bridge_reply(task, command_reply),
                    media_type="text/event-stream",
                    headers=build_sse_headers(),
                )
            return JSONResponse(
                build_completion_response(task, text=command_reply, finish_reason="stop").model_dump()
            )
        if parsed.stream:
            return StreamingResponse(
                _stream_chat_completion(task, service, request),
                media_type="text/event-stream",
                headers=build_sse_headers(),
            )
        outcome = await _run_with_disconnect_guard(request, service, task)
        response = build_completion_response(
            task,
            text=outcome.result.text,
            tool_calls=outcome.result.tool_calls,
            finish_reason=outcome.result.finish_reason,
            prompt_text=outcome.conversation.prompt,
        )
        return JSONResponse(response.model_dump())


@app.post("/v1/responses")
async def responses(request: Request):
    if not config.ENABLE_RESPONSES_API:
        raise InvalidRequestError("Responses API 未启用", code=ErrorCode.NOT_FOUND)
    _check_bridge_auth(request)
    body = await _read_json(request)
    bucket = _session_key(request, body)
    with log_context(session_key=redact(bucket)):
        task = to_task_from_responses(body, bucket)
        response_id = f"resp_{new_request_id().replace('-', '')}"
        # 与 Chat Completions 同一套命令，避免两条协议行为分叉。
        command_reply = await handle_command(task.messages, service, bucket)
        if command_reply is not None:
            if task.stream:
                return StreamingResponse(
                    _stream_bridge_responses(task, command_reply, response_id),
                    media_type="text/event-stream",
                    headers=build_sse_headers(),
                )
            return JSONResponse(
                build_response_object(task, text=command_reply, response_id=response_id)
            )
        if task.stream:
            return StreamingResponse(
                _stream_responses(task, service, request, response_id),
                media_type="text/event-stream",
                headers=build_sse_headers(),
            )
        outcome = await _run_with_disconnect_guard(request, service, task)
        store_thread(
            response_id,
            task.messages,
            outcome.result.text,
            tool_calls=outcome.result.tool_calls,
        )
        payload = build_response_object(
            task,
            text=outcome.result.text,
            tool_calls=outcome.result.tool_calls,
            response_id=response_id,
        )
        return JSONResponse(payload)


if config.DEBUG and config.ENABLE_DEBUG_DOM:

    @app.get("/debug/dom")
    async def debug_dom() -> Dict[str, Any]:
        """仅在调试模式 + 显式开启时注册；只返回布尔/计数，不回显正文。"""
        page = await service.driver.page_for(config.DEFAULT_SESSION_KEY)
        groups = {
            "input": config.INPUT_SELECTORS,
            "send": config.SEND_BUTTON_SELECTORS,
            "response": config.RESPONSE_SELECTORS,
            "generating": config.GENERATING_SELECTORS,
        }
        detail, healthy = await service.driver.dom.selector_diagnostics(page, groups)
        probe = await service.driver.dom.page_probe(page, config)
        return {"selector_version": SELECTOR_VERSION, "selectors": detail, "healthy": healthy, "page": probe}


# ==================== 非流式请求的断连保护 ====================

#: 检测客户端断连的轮询间隔（秒）。断连检测本身很轻，放快一点能更快释放桶锁。
_DISCONNECT_POLL_S = 0.5


async def _run_with_disconnect_guard(request: Request, svc: ChatService, task: ChatTask):
    """非流式请求也带断连检测（T2.4 / D2.4）。

    流式路径靠消费生成器时读 :meth:`Request.is_disconnected` 来发现断连；非流式路径
    此前是**裸 await**：客户端断开后桶锁会一直持到绝对超时（默认 300s），期间别的
    请求拿不到这个桶，页面也白跑。

    做法与流式一致——同一个取消机制：后台跑 ``svc.run``，并发轮询 ``is_disconnected``，
    命中就置取消标志。生成循环读到取消后抛 :class:`GenerationCancelledError`（499），
    桶锁在 ``service.run`` 的 ``finally`` 里立即释放。
    """
    cancel = asyncio.Event()

    async def watch_disconnect() -> None:
        try:
            while not cancel.is_set():
                if await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(_DISCONNECT_POLL_S)
        except asyncio.CancelledError:  # pragma: no cover - 正常收尾
            raise
        except Exception:  # noqa: BLE001 - 断连检测失败不该拖垮请求
            return

    run_task = asyncio.create_task(svc.run(task, cancel=cancel.is_set))
    watcher = asyncio.create_task(watch_disconnect())
    try:
        return await run_task
    finally:
        # 只发取消、**不 await**：断连探测里可能正卡在 ``request.is_disconnected()``
        # 的 ``receive()``（请求体已读完时它可能不再返回，且不一定响应取消）。
        # 若在这里 await watcher，成功路径会被它拖死（实测会让整条非流式路由挂住）。
        # 取消标志已置位，任务被丢弃即可，不会泄漏有意义的状态。
        watcher.cancel()


# ==================== 流式实现 ====================

#: SSE 保活间隔：从配置读取（T4.1 / D4.1），两条流式路径共用。
#: 此前是写死的 ``_KEEPALIVE = 10.0``，导致 ``STREAM_KEEPALIVE_S`` 声明了却从未生效。
def _keepalive_s() -> float:
    return max(0.1, float(config.STREAM_KEEPALIVE_S))


def _include_usage(task: ChatTask) -> bool:
    """客户端是否要求流式附带 usage（``stream_options.include_usage``）。

    此前该字段声明了却从未被消费（T4.4 / D4.4）。
    """
    opts = task.stream_options
    if not isinstance(opts, dict):
        return False
    return bool(opts.get("include_usage"))


async def _stream_bridge_reply(task: ChatTask, reply: str) -> AsyncIterator[str]:
    """桥内命令的本地 SSE：没有上游生成，直接一次性吐出应答。"""
    encoder = StreamEncoder(task.model)
    yield encoder.role()
    for chunk in encoder.content(reply):
        yield chunk
    yield encoder.final("stop")
    yield encoder.done()


async def _stream_chat_completion(
    task: ChatTask, svc: ChatService, request: Request
) -> AsyncIterator[str]:
    """Chat Completions SSE：合成流 + 结束块 + ``[DONE]``（doc/design.md 3.5）。"""
    encoder = StreamEncoder(task.model)
    queue: "asyncio.Queue" = asyncio.Queue()
    cancel = asyncio.Event()

    async def on_delta(text: str) -> None:
        await queue.put(("delta", text))

    async def produce() -> None:
        try:
            outcome = await svc.run(
                task, on_delta=on_delta, cancel=lambda: cancel.is_set()
            )
            await queue.put(("result", outcome))
        except BridgeError as exc:
            await queue.put(("error", exc))
        except asyncio.CancelledError:  # pragma: no cover - 客户端断开
            raise
        except Exception as exc:  # noqa: BLE001
            await queue.put(("error", InternalError("流式生成失败", detail={"error": type(exc).__name__})))
        finally:
            await queue.put(("end", None))

    producer = asyncio.create_task(produce())
    sent = ""
    rewritten = False
    try:
        yield encoder.role()
        while True:
            if await request.is_disconnected():
                cancel.set()
                break
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=_keepalive_s())
            except (asyncio.TimeoutError, TimeoutError):
                yield sse_comment()
                continue
            if kind == "delta":
                piece, reset = delta_piece(sent, payload)
                if reset is not None:
                    # 网页重写了回复：无法撤回已发送内容，停止继续发送，避免重复错误内容。
                    if not rewritten:
                        metrics.inc("stream_rewritten_total")
                    rewritten = True
                elif piece:
                    sent = payload
                    for chunk in encoder.content(piece):
                        yield chunk
            elif kind == "result":
                outcome = payload
                if outcome.result.has_tool_calls:
                    for chunk in encoder.tool_calls(outcome.result.tool_calls):
                        yield chunk
                    yield encoder.final("tool_calls")
                else:
                    final = outcome.result.text
                    if not rewritten:
                        for chunk in encoder.content(final[len(sent):] if final.startswith(sent) else final):
                            yield chunk
                    yield encoder.final(outcome.result.finish_reason)
                if _include_usage(task):
                    # OpenAI 约定：usage chunk 在 finish_reason 之后、[DONE] 之前
                    # （T4.4 / D4.4）。
                    yield encoder.usage(
                        estimate_usage(outcome.conversation.prompt, outcome.result.text)
                    )
                yield encoder.done()
                return
            elif kind == "error":
                yield sse_error(payload.message, payload.code.value)
                yield encoder.done()
                return
            elif kind == "end":
                return
    finally:
        if not producer.done():
            cancel.set()
            try:
                await asyncio.wait_for(producer, timeout=5)
            except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
                producer.cancel()


async def _stream_bridge_responses(
    task: ChatTask, reply: str, response_id: str
) -> AsyncIterator[str]:
    """桥内命令在 Responses 协议下的本地事件序列（不触发上游生成）。"""
    for event in response_events(response_id, task.model, text=reply):
        yield event


async def _stream_responses(
    task: ChatTask, svc: ChatService, request: Request, response_id: str
) -> AsyncIterator[str]:
    """Responses API 真流式：复用 chat 路径的「队列 + 保活 + 断连 + 错误事件」骨架。

    此前是伪流式——先 ``await svc.run(task)`` 拿到完整结果再一次性吐事件，客户端在
    整个生成期间收不到任何增量，也拿不到保活（T4.1 / D4.1）。现在改为：

    * 生成期间逐段发 ``response.output_text.delta``；
    * 空闲超过 ``STREAM_KEEPALIVE_S`` 发 SSE 注释保活；
    * 客户端断开即置取消标志，桶锁在 ``service.run`` 的 ``finally`` 里释放；
    * 失败发 ``sse_error`` + 收尾，不把内部异常伪装成空回复。
    """
    queue: "asyncio.Queue" = asyncio.Queue()
    cancel = asyncio.Event()

    async def on_delta(text: str) -> None:
        await queue.put(("delta", text))

    async def produce() -> None:
        try:
            outcome = await svc.run(
                task, on_delta=on_delta, cancel=lambda: cancel.is_set()
            )
            await queue.put(("result", outcome))
        except BridgeError as exc:
            await queue.put(("error", exc))
        except asyncio.CancelledError:  # pragma: no cover - 客户端断开
            raise
        except Exception as exc:  # noqa: BLE001
            await queue.put(
                ("error", InternalError("流式生成失败", detail={"error": type(exc).__name__}))
            )
        finally:
            await queue.put(("end", None))

    producer = asyncio.create_task(produce())
    sent = ""
    rewritten = False
    try:
        yield response_created_event(response_id, task.model)
        while True:
            if await request.is_disconnected():
                cancel.set()
                break
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=_keepalive_s())
            except (asyncio.TimeoutError, TimeoutError):
                yield sse_comment()
                continue
            if kind == "delta":
                piece, reset = delta_piece(sent, payload)
                if reset is not None:
                    # 网页重写了回复：无法撤回已发出的增量，停止再发，避免重复错误内容。
                    # 与 chat 路径同一语义；最终文本在 ``response.completed`` 里如实给出。
                    if not rewritten:
                        metrics.inc("stream_rewritten_total")
                    rewritten = True
                elif piece:
                    sent = payload
                    yield response_text_delta_event(response_id, piece)
            elif kind == "result":
                outcome = payload
                store_thread(
                    response_id,
                    task.messages,
                    outcome.result.text,
                    tool_calls=outcome.result.tool_calls,
                )
                final = outcome.result.text
                # 未发生重写时才补齐增量没覆盖到的尾部；重写场景增量已不可信，
                # 只把完整最终文本放进 ``response.completed``。
                if (
                    not rewritten
                    and not outcome.result.has_tool_calls
                    and final.startswith(sent)
                ):
                    tail = final[len(sent):]
                    if tail:
                        yield response_text_delta_event(response_id, tail)
                yield response_completed_event(
                    response_id,
                    task.model,
                    text=final,
                    tool_calls=outcome.result.tool_calls,
                )
                return
            elif kind == "error":
                yield sse_error(payload.message, payload.code.value)
                return
            elif kind == "end":
                return
    finally:
        if not producer.done():
            cancel.set()
            try:
                await asyncio.wait_for(producer, timeout=5)
            except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
                producer.cancel()


__all__ = [
    "app",
    "chat_completions",
    "diagnostics",
    "healthz",
    "list_models",
    "lifespan",
    "readiness",
    "reset_session",
    "root",
    "_sanitize_key",
    "_session_key",
]
