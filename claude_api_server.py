"""ClaudeBridge 入口（薄封装）。

实现位于 ``claude_web`` 包：

    claude_web/config.py            配置加载（.env）与全部可调参数
    claude_web/models.py            OpenAI 兼容与内部统一模型
    claude_web/prompting.py         消息归一化与提示词构造
    claude_web/toolcalls.py         工具注入与结构化解析
    claude_web/driver.py            Playwright 浏览器生命周期
    claude_web/browser/             Claude 网页 UI 探测与 DOM 操作
    claude_web/completion/          生成、回复提取、会话生命周期
    claude_web/api/                 协议适配（Chat Completions / Responses / SSE）
    claude_web/server.py            FastAPI 应用与路由
    claude_web/service.py           请求流水线（桶锁 + 会话 + 生成 + 快照）

本文件仅做两件事：重新导出历史公开名字，以及在直接运行时启动服务。

.. warning::
   下面导出的 **配置常量是加载时的快照**。运行期各模块都通过
   ``claude_web.config.<NAME>`` 取属性，因此改写 ``claude_api_server.PORT`` 之类
   的名字不会生效；需要覆盖请改 ``config`` 模块属性或用环境变量 / ``.env``。
"""

from claude_web import config
from claude_web.config import (  # noqa: F401
    DEFAULT_MODEL_ALIAS,
    HOST,
    MODEL_ALIASES,
    PORT,
    SESSION_KEY_HEADER,
    SESSION_SCOPING,
    env_bool,
    env_float,
    env_int,
    env_list,
    env_str,
    is_loopback_host,
)
from claude_web.logging_setup import configure_logging  # noqa: E402
from claude_web.models import (  # noqa: F401
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ChoiceMessage,
    FunctionCall,
    ModelCard,
    ModelListResponse,
    ToolCall,
    Usage,
    advertised_models,
)
from claude_web.prompting import build_prompt, estimate_tokens, normalize_messages  # noqa: F401
from claude_web.server import (  # noqa: F401
    app,
    chat_completions,
    diagnostics,
    healthz,
    lifespan,
    list_models,
    readiness,
    reset_session,
    root,
)
from claude_web.toolcalls import format_tools_instruction, parse_tool_calls  # noqa: F401

configure_logging()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
