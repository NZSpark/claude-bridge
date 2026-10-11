"""请求处理流水线（doc/design.md 2 的链路、5.1、9.3）。

把「取桶锁 -> 准备会话 -> 生成 -> 任务快照 -> 指标/状态更新」收敛到一处，
让 :mod:`claude_web.server` 只负责 HTTP 契约（校验、鉴权、错误到状态码）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

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
        self.lifecycle.note_success(task.bucket)
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
