"""OpenAI 兼容的数据模型与内部统一模型（doc/design.md 第 3、7 节）。

分三层：

1. **外部协议模型**：``ChatCompletionRequest`` / ``ChatCompletionResponse`` 等，
   直接对应 OpenAI Chat Completions 的字段；对未知字段宽松接收（``extra="allow"``），
   避免兼容客户端因为多传参数而收到 422。
2. **内部统一模型**：``NormalizedMessage`` / ``ToolSpec`` / ``ChatTask``，
   是网页驱动层与各协议适配器共同依赖的中间表示。
3. **任务快照**：``TaskSnapshot``，见 doc/design.md 9.3。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

# ==================== 1. 外部协议模型 ====================


class FunctionCall(BaseModel):
    name: str
    # OpenAI 的 arguments 是 JSON 字符串；这里以字符串对外暴露。
    arguments: str = "{}"


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:16]}")
    type: str = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    """OpenAI 请求中的消息；宽松接收以避免校验失败。"""

    model_config = ConfigDict(extra="allow")

    role: str
    # content 可能是字符串，也可能是内容分片数组（[{"type": "text", ...}]）。
    content: Optional[Any] = None
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "claude-web"
    messages: List[ChatMessage] = Field(default_factory=list)
    stream: Optional[bool] = False
    stream_options: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None
    # 网页版无法控制生成参数；以下字段仅作兼容占位（接收但不生效）。
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Any] = None
    # 会话分桶标识（OpenAI 的 `user` 字段）。
    user: Optional[str] = None


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    """保守 usage。

    doc/design.md 3.3：没有可信 token 来源时，不得伪造精确值。这里统一采用
    ``estimated=True`` 的字符估算约定，全项目只有这一处定义。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = "claude-web"
    choices: List[Choice]
    usage: Usage = Field(default_factory=Usage)


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "claude-web-bridge"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: List[ModelCard]


def advertised_models() -> List[Dict[str, Any]]:
    """本桥对外公布的模型清单（唯一来源，供 /v1/models 与 /diagnostics 共用）。"""
    from . import config

    cards: List[Dict[str, Any]] = []
    for alias, ui_label in config.MODEL_ALIASES.items():
        card: Dict[str, Any] = {"id": alias, "ui_label": ui_label}
        cards.append(card)
    if not cards:
        cards.append({"id": config.DEFAULT_MODEL_ALIAS, "ui_label": ""})
    return cards


def model_is_supported(model: Optional[str]) -> bool:
    """请求的 model 是否为已知别名；未知别名不直接报错（宽松兼容），仅用于诊断。"""
    if not model:
        return True
    return model in advertised_models_ids()


def advertised_models_ids() -> List[str]:
    from . import config

    ids = [card["id"] for card in advertised_models()]
    return ids or [config.DEFAULT_MODEL_ALIAS]


# ==================== 2. 内部统一模型 ====================


@dataclass
class NormalizedMessage:
    """归一化后的单条对话消息（网页驱动层只看这个）。"""

    role: str
    content: str = ""
    name: Optional[str] = None
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_id: Optional[str] = None


@dataclass
class ToolSpec:
    """客户端声明的一个工具（从 OpenAI ``tools`` 数组归一化而来）。"""

    name: str
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatTask:
    """一次 Chat Completions 请求的内部表示。"""

    messages: List[NormalizedMessage]
    model: str
    bucket: str
    stream: bool = False
    tools: List[ToolSpec] = field(default_factory=list)
    tool_choice: Optional[Any] = None


# ==================== 3. 任务快照（doc/design.md 9.3）====================

#: 任务状态机：queued -> preparing -> submitted -> generating -> completed
#: 异常路径：preparing/submitted/generating -> recovering -> completed/failed
TASK_STATES = ("queued", "preparing", "submitted", "generating", "recovering", "completed", "failed")


@dataclass
class TaskSnapshot:
    """不可丢失的任务快照：提交状态一旦可能成功，就不允许无条件重发。"""

    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    bucket: str = ""
    model: str = ""
    started_at: float = field(default_factory=time.time)
    state: str = "queued"
    goal: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    submitted: bool = False
    last_reply: str = ""
    recoveries: int = 0
    last_error: Optional[str] = None

    def to_payload(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "TaskSnapshot":
        snap = cls()
        if not isinstance(payload, dict):
            return snap
        for name in ("task_id", "bucket", "model", "state", "goal", "last_reply"):
            value = payload.get(name)
            if isinstance(value, str):
                setattr(snap, name, value)
        messages = payload.get("messages")
        snap.messages = list(messages) if isinstance(messages, list) else []
        try:
            snap.started_at = float(payload.get("started_at") or snap.started_at)
        except (TypeError, ValueError):
            pass
        snap.submitted = bool(payload.get("submitted"))
        try:
            snap.recoveries = int(payload.get("recoveries") or 0)
        except (TypeError, ValueError):
            snap.recoveries = 0
        err = payload.get("last_error")
        snap.last_error = err if isinstance(err, str) else None
        if snap.state not in TASK_STATES:
            snap.state = "queued"
        return snap
