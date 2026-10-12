"""任务快照（doc/design.md 9.3）。

任务快照至少包含任务 ID、桶键、目标模型、开始时间、当前状态、提交状态、
最近一次回复快照与恢复次数。语义约束：

* 状态机 ``queued -> preparing -> submitted -> generating -> completed``，
  异常路径 ``... -> recovering -> completed/failed``；
* **提交操作一旦可能成功**，页面丢失 / 状态不确定时不得无条件重发；
* 快照只保存目标、最近进展与状态，不保存登录凭据。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .models import TaskSnapshot

logger = logging.getLogger(__name__)


class TaskStore:
    """按桶保存任务快照；文件系统落盘，读取失败时安全降级为空。"""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self._explicit_dir = Path(directory) if directory is not None else None

    @property
    def directory(self) -> Path:
        return self._explicit_dir or Path(config.TASK_FILE_DIR)

    def _path(self, bucket: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in bucket) or "default"
        return self.directory / f"{safe}.json"

    def enabled(self) -> bool:
        return bool(config.TASK_SNAPSHOT_ENABLED)

    def save(self, snapshot: TaskSnapshot) -> None:
        if not self.enabled():
            return
        path = self._path(snapshot.bucket or config.DEFAULT_SESSION_KEY)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot.to_payload(), ensure_ascii=False, indent=2), "utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[任务] 快照写入失败：%s", type(exc).__name__)

    def load(self, bucket: str) -> Optional[TaskSnapshot]:
        if not self.enabled():
            return None
        path = self._path(bucket)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return TaskSnapshot.from_payload(data)

    def clear(self, bucket: str) -> None:
        path = self._path(bucket)
        try:
            path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """诊断用：最近的任务摘要（只含状态与计数，不含正文）。"""
        out: List[Dict[str, Any]] = []
        directory = self.directory
        if not directory.exists():
            return out
        for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                snap = TaskSnapshot.from_payload(json.loads(path.read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                continue
            out.append(
                {
                    "task_id": snap.task_id,
                    "bucket": snap.bucket,
                    "state": snap.state,
                    "submitted": snap.submitted,
                    "recoveries": snap.recoveries,
                    "last_error": snap.last_error,
                    "goal_chars": len(snap.goal),
                    "reply_chars": len(snap.last_reply),
                    "started_at": snap.started_at,
                }
            )
        return out

    @staticmethod
    def new(bucket: str, goal: str = "", messages: Optional[List[dict]] = None) -> TaskSnapshot:
        # 只保留最近 TASK_KEEP_MESSAGES 条（T6.2 / D6.2）：此前该配置声明了却从未被读，
        # 快照会把整段历史都落盘。0 = 不限制。
        kept = list(messages or [])
        limit = config.TASK_KEEP_MESSAGES
        if limit and len(kept) > limit:
            kept = kept[-limit:]
        return TaskSnapshot(
            bucket=bucket,
            started_at=time.time(),
            state="queued",
            goal=goal,
            messages=kept,
        )


#: 进程级默认任务存储。
default_task_store = TaskStore()
