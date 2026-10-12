"""请求处理流水线（doc/design.md 2 的链路、5.1、9.3）。

把「取桶锁 -> 准备会话 -> 生成 -> 任务快照 -> 指标/状态更新」收敛到一处，
让 :mod:`claude_web.server` 只负责 HTTP 契约（校验、鉴权、错误到状态码）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config
from .completion.generator import DeltaCallback, GenerationResult, ReplyGenerator
from .completion.lifecycle import Conversation, SessionLifecycle
from .driver import ClaudeWebDriver
from .driver import driver as default_driver
from .errors import BridgeError, ContextLimitError, NotReadyError, SessionBusyError
from .metrics import metrics as default_metrics
from .models import ChatTask
from .session.lock import BucketLockManager
from .tasks import TaskStore, default_task_store

logger = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    """一次任务的结果（协议适配器所需的全部信息）。"""

    result: GenerationResult
    conversation: Conversation
    bucket: str
    notes: list = field(default_factory=list)


class ChatService:
    def __init__(
        self,
        *,
        driver: Optional[ClaudeWebDriver] = None,
        generator: Optional[ReplyGenerator] = None,
        lifecycle: Optional[SessionLifecycle] = None,
        locks: Optional[BucketLockManager] = None,
        tasks: Optional[TaskStore] = None,
        metrics=None,
    ) -> None:
        self.driver = driver or default_driver
        self.generator = generator or ReplyGenerator(self.driver.dom)
        self.lifecycle = lifecycle or SessionLifecycle(self.driver)
        self.locks = locks or BucketLockManager()
        self.tasks = tasks or default_task_store
        self.metrics = metrics or default_metrics

    async def run(
        self,
        task: ChatTask,
        *,
        on_delta: Optional[DeltaCallback] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> RunOutcome:
        """执行一次任务；所有失败都以 :class:`~claude_web.errors.BridgeError` 抛出。"""
        self.metrics.inc("request_total")
        try:
            async with self.locks.acquire(task.bucket):
                return await self._run_locked(task, on_delta=on_delta, cancel=cancel)
        except SessionBusyError:
            self.metrics.inc("session_busy_total")
            raise

    # ---- 管理操作（reset / link / unlink / modal）----
    #
    # 为什么统一入口 + 短桶锁（doc/design.md 5.1）：这些操作会关掉页面（reset）、
    # 导航到别的会话（link）或操纵页面（modal）。不取桶锁就对正在生成的桶执行，
    # 会直接打断在途请求：页面被 drop / goto，客户端拿到失败或错误内容。
    # 统一入口也消除了「HTTP 端点 / 聊天命令 / Responses 路径」三条入口各自为政。
    @asynccontextmanager
    async def _admin_lock(self, bucket: str):
        try:
            async with self.locks.acquire(bucket, timeout_s=config.ADMIN_LOCK_TIMEOUT_S):
                yield
        except SessionBusyError:
            self.metrics.inc("session_busy_total")
            raise

    async def admin_reset(self, bucket: str) -> None:
        """取桶锁后重置会话桶（保留会话绑定，见 T1.3）。"""
        async with self._admin_lock(bucket):
            await self.lifecycle.reset(bucket)

    async def admin_link(self, bucket: str, url: str, *, seed: bool = False) -> str:
        """取桶锁后把桶绑定到会话 URL。"""
        async with self._admin_lock(bucket):
            return await self.lifecycle.link(bucket, url, seed=seed)

    def admin_unlink(self, bucket: str) -> None:
        """解除桶的会话绑定。纯内存 / 状态文件操作，不碰页面，无需桶锁。"""
        self.lifecycle.unlink(bucket)

    async def admin_dismiss_modal(self, bucket: str):
        """取桶锁后关闭页面上的额度 / 升级弹窗，返回 :class:`ModalState`。"""
        async with self._admin_lock(bucket):
            page = await self.driver.page_for(bucket)
            return await self.driver.dom.dismiss_limit_modal(page)

    async def _run_locked(
        self,
        task: ChatTask,
        *,
        on_delta: Optional[DeltaCallback],
        cancel: Optional[Callable[[], bool]],
    ) -> RunOutcome:
        snapshot = self.tasks.new(bucket=task.bucket, goal=self.lifecycle.goal(task.messages))
        snapshot.model = task.model
        snapshot.state = "preparing"
        self.tasks.save(snapshot)

        # 准备阶段失败（未登录 / 页面不可用）尚未提交，可安全报告为未就绪。
        conversation = await self.lifecycle.prepare(
            task.bucket, task.messages, task.tools
        )
        snapshot.state = "submitted"
        snapshot.submitted = True
        self.tasks.save(snapshot)

        try:
            with self.metrics.timer("browser_generation_latency"):
                result = await self.generator.generate(
                    conversation.page,
                    conversation.prompt,
                    tools=task.tools,
                    on_delta=on_delta,
                    cancel=cancel,
                )
        except BridgeError as exc:
            return self._record_failure(snapshot, task.bucket, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[任务] 未预期错误：%s", type(exc).__name__)
            wrapped = NotReadyError("上游执行出现未预期错误", detail={"error": type(exc).__name__})
            return self._record_failure(snapshot, task.bucket, wrapped)

        snapshot.state = "completed"
        snapshot.last_reply = result.text
        self.tasks.save(snapshot)
        # 传页面与系统提示摘要：只有本轮真的成功，网页会话里才确实有了这段上下文
        # （下一轮才能只发增量），也才知道客户端是否换过系统提示。
        self.lifecycle.note_success(
            task.bucket,
            page=conversation.page,
            system_digest=conversation.system_digest,
            history_len=conversation.history_len,
            history_digest=conversation.history_digest,
        )
        if result.has_tool_calls:
            self.metrics.inc("tool_call_total")
        self.metrics.inc("request_success_total")
        notes = list(conversation.notes) + list(result.notes)
        return RunOutcome(result=result, conversation=conversation, bucket=task.bucket, notes=notes)

    def _record_failure(self, snapshot, bucket: str, exc: BridgeError) -> "RunOutcome":
        code = exc.code.value
        snapshot.state = "failed"
        snapshot.last_error = code
        self.tasks.save(snapshot)
        self.lifecycle.note_error(bucket, code)
        self.metrics.inc("request_error_total")
        if isinstance(exc, ContextLimitError):
            self.lifecycle.note_context_limit(bucket)
            self.metrics.inc("request_context_limit_total")
        if code in ("generation_timeout", "generation_stalled"):
            self.metrics.inc("request_timeout_total")
        raise exc


#: 进程级默认服务（测试可替换注入）。
service = ChatService()
