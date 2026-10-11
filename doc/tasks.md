# ClaudeBridge 实施记录

本文件记录 [doc/design.md](design.md) 的落地情况、关键决策与**待真实页面验证**的事项。
设计提到「进入实现阶段后，应将真实网页探测结果、客户端协议测试和关键架构决策同步
补充到 doc/tasks.md 与 doc/e2e_test_design.md」——本文件即该落点。

## 模块映射

| 设计建议模块 | 实现位置 | 状态 |
| --- | --- | --- |
| 启动入口 | [claude_api_server.py](../claude_api_server.py)、[main.py](../main.py) | ✅ 薄封装 |
| 配置 | [claude_web/config.py](../claude_web/config.py) | ✅ 与 `.env.example` 契约一致 |
| 异常与错误码 | [claude_web/errors.py](../claude_web/errors.py) | ✅ 设计 9.1 全部类别 + 9.2 映射 |
| 内部/协议模型 | [claude_web/models.py](../claude_web/models.py) | ✅ 含任务快照 |
| 消息归一化/提示词 | [claude_web/prompting.py](../claude_web/prompting.py) | ✅ 角色标记 + 字符预算 |
| 工具调用 | [claude_web/toolcalls.py](../claude_web/toolcalls.py) | ✅ 围栏解析 + 声明集合校验 |
| FastAPI 应用 | [claude_web/server.py](../claude_web/server.py) | ✅ 路由 / 鉴权 / 错误映射 |
| 请求流水线 | [claude_web/service.py](../claude_web/service.py) | ➕ 新增（桶锁 + 会话 + 生成 + 快照），让 server 保持薄 |
| 浏览器生命周期 | [claude_web/driver.py](../claude_web/driver.py) | ✅ 惰性 Playwright + readiness |
| 选择器 | [claude_web/browser/selectors.py](../claude_web/browser/selectors.py) | ⚠️ 候选，待真实探测 |
| DOM 适配 | [claude_web/browser/dom_adapter.py](../claude_web/browser/dom_adapter.py) | ⚠️ 候选选择器 + 诊断 |
| DOM 诊断 | [claude_web/browser/diagnostics.py](../claude_web/browser/diagnostics.py) | ✅ 只回布尔/计数 |
| 会话生命周期 | [claude_web/completion/lifecycle.py](../claude_web/completion/lifecycle.py) | ✅ 复用网页会话 + 首轮完整播种 + 绑定 |
| 生成协调 | [claude_web/completion/generator.py](../claude_web/completion/generator.py) | ✅ 结束判定 + 工具缓冲 |
| 回复提取 | [claude_web/completion/extractor.py](../claude_web/completion/extractor.py) | ✅ 只取新增回复 |
| 会话状态 | [claude_web/session/](../claude_web/session/) + [session_store.py](../claude_web/session_store.py) 门面 | ✅ 单一事实来源 |
| 页面池 | [claude_web/page_pool.py](../claude_web/page_pool.py) | ✅ LRU + 空闲回收 |
| 任务快照 | [claude_web/tasks.py](../claude_web/tasks.py) | ✅ |
| 结束检测 | [claude_web/end_detection.py](../claude_web/end_detection.py) | ✅ 纯函数状态机 |
| 指标 | [claude_web/metrics.py](../claude_web/metrics.py) | ✅ 进程内、无正文 |
| 日志 | [claude_web/logging_setup.py](../claude_web/logging_setup.py) | ✅ 关联 id + 脱敏 |
| 协议适配 | [claude_web/api/](../claude_web/api/) | ✅ Chat + SSE + Responses 子集 |

## 分阶段实施状态

### 阶段 A：基础浏览器原型
- ✅ 持久化浏览器启动、readiness 探测、页面池、关闭流程。
- ✅ 输入区定位（候选回退 + 读回校验）、发送（Enter 优先，退化为按钮）、新回复定位。
- ✅ Fake Page 单元测试覆盖输入/提交/提取链路。
- ⚠️ **待验证**：真实页面上的输入框 / 发送 / 回复节点选择器、生成状态信号。

### 阶段 B：OpenAI Chat Completions
- ✅ 内部消息模型与请求校验（未知字段宽松接收）。
- ✅ 提示词构建、首轮完整播种、增量续写、字符预算与裁剪标注。
- ✅ 非流式端点、`/v1/models`、标准错误对象。
- ✅ SSE 合成流（首块 role、增量、结束块、`[DONE]`）。
- ✅ `/healthz`、`/readiness`、`/metrics`。

### 阶段 C：多会话与可靠性
- ✅ 桶优先级（头 > user > UA > 默认）、键清洗、异步桶锁、页面池与空闲回收。
- ✅ 任务快照、停滞/超时区分、失败分类。
- ✅ `session/reset`、可选 `session/link`（HTTPS + 主机白名单 + 落点校验）、`unlink`。
- ✅ 日志脱敏、可选 `BRIDGE_TOKEN`、`/debug/dom` 开关。

### 阶段 D：工具调用与客户端兼容性
- ✅ 工具说明注入、围栏解析、参数形状校验、未声明工具拒绝、多调用支持。
- ✅ 工具模式整段缓冲，避免内部标记泄漏。
- ⚠️ **待验证**：用目标客户端（Pi / Codex CLI）跑一次真实工具往返，记录支持的字段范围。

### 阶段 E：Responses API 与文档
- ✅ Responses API 子集适配器（默认关闭）。
- ✅ 安装 / 首次登录 / 诊断 / 限制说明（README）。
- ⚠️ **待验证**：Responses 事件序列在目标客户端上的接受程度；未验证前保持默认关闭。

## 关键决策

1. **默认「复用网页会话 + 首轮完整播种」**：同一桶首轮开新对话并播种完整历史，
   之后只发增量（输入短、延迟低、网页端不再堆重复对话）；页面被换掉 / 进程重启后
   内存里的播种标记失效，自动回到完整播种，不依赖客户端与网页上一轮严格同步。
2. **错题本式错误词汇表**：网页驱动层、会话层、API 层共用 `errors.ErrorCode`，
   HTTP 状态由 `http_status_for` 集中决定，避免各层各写一套。
3. **提交后不无条件重发**：只在填充输入框（无副作用）阶段重试；提交后只做
   「读页面现有回复」式恢复（`completion/generator.py` 顶部注释）。
4. **合成流**：流式由回复快照增量编码，不宣称上游 token 级流；检测到网页重写回复时
   停止继续发送，而不是重复错误内容。
5. **模型别名保守公开**：`/v1/models` 只公布配置过的别名（默认 `claude-web`），
   未经验证的网页模型标签不公开。
6. **配置契约三方一致**：`.env.example` ↔ `config.py` ↔ 文档，由
   `tests/test_config_drift.py` 守护。

## 待真实页面探测确认（doc/design.md 14）

| 项目 | 现状 | 影响 |
| --- | --- | --- |
| 输入区选择器 | 候选（contenteditable / ProseMirror / textarea） | 改版会导致 `composer_not_found` |
| 发送动作 | Enter 优先，候选按钮回退 | 提交失败会报 `prompt_submission_failed` |
| 回复节点选择器 | 候选（assistant-message / font-claude-message） | 提取失败会报 `reply_extraction_failed` |
| 生成中信号 | 停止按钮 / `data-is-streaming` | 探测不到时退化为「内容稳定」判定 |
| 新对话入口 | `a[href="/new"]` / aria-label 候选 | 找不到时退化为重新导航首页 |
| 会话 URL 形态 | 绑定校验只做主机 + 非会话路径拒绝 | 绑定后需人工确认落点 |
| 模型选择器 | 未启用 | 别名暂不映射到 UI 模型标签 |

真实页面探测完成后，应把确认过的选择器与生成信号更新到 `config.py` 默认值与本表。

## 真实探测记录

### 2026-10-11 首次联网探测（`tests/e2e/probe.py`）

探测目标：一条真实的 `https://claude.ai/chat/<id>` 会话链接；持久化 profile 为全新
（未登录）。结果：

| 观测项 | 结果 |
| --- | --- |
| 导航状态 | `403` |
| 落点 | 仍是目标路径（未跳转到登录页） |
| 挑战判定 | `challenged: true`，依据 `title` |
| 输入区 / 回复节点 | 全部 0 命中 |
| 尝试过的浏览器 | Playwright 自带 Chromium 与本机真实 Chrome（`BROWSER_CHANNEL=chrome`）均为 403 |

结论：**该链接暂不可验证为可用**——站点在边缘层返回安全校验页，而不是把请求交给
真实会话页。这属于「环境/登录态未就绪」，不是选择器失效。

由此产生的实现变化：

1. 挑战识别不再只看选择器：新增 `CHALLENGE_TEXT_PATTERNS`（同时匹配标题与可见文本，
   覆盖 `Performing security verification` 这类正文特征），并在诊断中回传
   `challenge_reason`（`selector` / `title` / `text`）。
2. 新增 `BROWSER_CHANNEL`：留空用自带 Chromium，填 `chrome` / `msedge` 用本机真实浏览器。
3. 新增 `CLAUDE_DEFAULT_LINK_URL`（缺省初始会话链接，**仅默认桶**生效）。

合规约定（重申 doc/design.md 1.2 与 4.1）：本项目只**识别并如实上报**安全校验，
**不自动勾选 / 绕过**任何验证。校验必须由人在有头窗口中完成：

```bash
# 打开有头窗口并在检测到校验时等待人工完成（不代勾选）
BROWSER_CHANNEL=chrome .venv/bin/python -m tests.e2e.probe "<url>" --wait-human 300
```

人工通过后会话进入持久化 profile，后续探测与桥接服务无需再次校验。

