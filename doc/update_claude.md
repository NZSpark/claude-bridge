# ClaudeBridge 改进建议

> 基于 2026-10-12 对仓库（初稿基于提交 `4e3cdf1`，5.2 节补充了 `c8719d3` 中一并提交的生成判定改动）的静态阅读与测试结果。已阅读：`server.py`、`service.py`、`completion/generator.py`、`completion/lifecycle.py`、`driver.py`、`page_pool.py`、`tasks.py`、`session/store.py`、`session_store.py`、`config.py`（部分）、`pyproject.toml`、`.gitignore`、`doc/design.md`、`doc/tasks.md`。未逐行读完的模块（`toolcalls.py`、`prompting.py`、`dom_adapter.py`、`bridge_commands.py`）只在「待核实」中提及，不下结论。

## 1. 现状概览

- 规模：约 8400 行 Python（`claude_web` 约 5400 行，`tests` 约 2900 行）。
- 质量基线：`pytest` 全绿（e2e 默认跳过），`ruff check` 通过。
- 架构：分层清晰（协议适配 → ChatService → 生命周期/生成器 → DOM 适配），设计文档与实现基本对应，有配置漂移测试守护 `.env.example` / `config.py` / 文档。
- 最近一次改动把默认策略从「每轮新开对话 + 全量重放」改为「复用网页会话 + 只发增量」，这是最大的行为变化，也是风险最集中的地方（见 P0）。

## 2. P0：正确性与安全（建议优先处理）

### 2.1 管理/诊断端点缺少鉴权
`server.py` 里只有 `/session/reset|link|unlink` 与生成端点调用了 `_check_bridge_auth`。以下端点在设置了 `BRIDGE_TOKEN` 时仍然无需 Token 即可访问：

- `GET /diagnostics`：返回桶名、会话元数据、最近任务摘要、配置开关。
- `GET /session/{bucket}`：返回任意桶状态。
- `GET /metrics`、`GET /readiness`。

建议：`/diagnostics`、`/session/{bucket}` 一律走 `_check_bridge_auth`；`/healthz` 保持开放；`/metrics`、`/readiness` 可用配置项 `PROTECT_OBSERVABILITY` 控制。`/v1/models` 是否开放同理按需。

### 2.2 `X-Request-Id` 未清洗
`_request_context` 直接采用客户端传来的 `x-request-id` 并写入日志上下文和响应头。建议复用 `_sanitize_key` 式的白名单与长度限制（如 64 字符），防止日志注入和超长头。

### 2.3 未处理异常被伪装成 503「未就绪」
`_unhandled_handler` 把任何内部 bug 都包装成 `NotReadyError`。客户端会据此重试或误判为登录问题。建议新增 `InternalError`（HTTP 500，`ErrorCode.INTERNAL`），并保留类型名到 detail，`NotReadyError` 只用于浏览器/登录/页面不可用。

### 2.4 流式「网页重写回复」时静默截断
`_stream_chat_completion` 检测到 `reset` 后只置 `rewritten=True` 并停止继续发送，但最后仍以 `finish_reason=stop` 结束，客户端无法知道内容被截断。建议：

- 在最终块前发送一个带说明的 SSE 注释/错误事件，或把 `finish_reason` 置为 `length` 之外的明确值并附 `bridge_notes`；
- 至少在 metrics 中计数 `stream_rewritten_total`。

### 2.5 取消语义用错异常
`generator.generate` 在 `cancel()` 为真时抛 `GenerationStalledError`。这会被 `ChatService._record_failure` 计入失败并写入会话 `last_error`、`request_timeout_total`，污染统计，也会让会话状态显示为出错。建议新增 `GenerationCancelledError`（或沿用 `asyncio.CancelledError`），在 service 中单独计数、不标记会话错误。

### 2.6 复用会话模式下的状态一致性（需重点回归）
`lifecycle.prepare` 用内存字典 `_seeded_page[bucket] is page` 判断是否需要重新播种，`note_success` 负责记账。风险点：

- 进程重启后字典为空，会触发一次全量播种，符合预期；但**用户在浏览器里手动关闭/刷新该标签页、或页面被池回收后重建**时，是否都能走到 `drop()` + `_forget_seed()`，需要用测试固化。
- `_same_conversation` 用「前缀一致」比较 URL，`target_url[: max(10, len-1)]` 会让不同会话 ID 只差最后一位时被判为同一条。建议改为解析 URL 后精确比较路径中的会话 ID。
- 已核实（二次阅读）：`service._run_locked` 现在以 `note_success(bucket, page=conversation.page, system_digest=conversation.system_digest)` 调用，播种标记会被正确写入，「每轮重新播种」的担忧不成立。仍缺的是针对该路径的回归测试（页面被换、系统提示变化各一条）。

## 3. P1：可靠性与可维护性

### 3.1 空闲页面回收没有调度者
`PagePool.reap_idle()` 存在，但 `server.lifespan` 里没有任何后台任务调用它，`mark_busy` 在已读代码中也未被使用。结果是 `BUCKET_IDLE_TTL_S` 实际可能不生效，`MAX_PAGE_POOL` 只靠 LRU 兜底。建议在 lifespan 里起一个周期任务（如每 60 秒），并在生成期间对桶 `mark_busy(True/False)`，避免 LRU 把正在生成的页面回收掉。

### 3.2 任务快照形同虚设
`TaskStore` 每桶只存一个文件，每次任务覆盖前一次；设计文档定义的状态机包含 `generating`、`recovering`，`recoveries` 字段也存在，但 `service.py` 只写了 `preparing/submitted/completed/failed`，没有 `generating` 与任何恢复路径。要么落实恢复（提交后只读页面现有回复），要么把文档与字段精简到实际使用的部分。`/diagnostics` 的 `tasks.recent` 也因此最多只有「桶数」条记录。

### 3.3 会话状态存储的读改写成本与共享可变对象
- 每次 `save_state` 都 `load_document()`（读盘 + 解析 + 迁移判断）再整体写回；高频小更新会放大磁盘 IO。建议内存持有完整文档，仅在变更时原子写盘。
- `state()` 返回缓存里的可变对象，调用方直接改属性（如 `state.has_history = True`）后若忘记 `save_state` 就只改了内存，进程重启后丢失。建议对外只暴露 `update(**fields)`。
- `STATE_FILE_LOCK` 是线程锁，在 asyncio 里持锁做同步磁盘 IO 会阻塞事件循环；量小时可接受，但与流式 keepalive 同进程时值得用 `asyncio.to_thread` 包起来。

### 3.4 异常处理过宽
仓库内有 48 处 `except Exception` / `BaseException` / `noqa: BLE001`。多数是有意为之（浏览器侧不可预测），但：

- `generator._fill` 用 `except BaseException` 重试，会吞掉 `CancelledError` / `KeyboardInterrupt` 的重试语义，应只捕获 `Exception`，让取消直接冒泡。
- 建议把「浏览器侧可恢复异常」收敛成一个元组常量（如 `PLAYWRIGHT_ERRORS`），其余让它们显式失败。

### 3.5 `diagnostics` / `readiness` 会触碰浏览器
`readiness()` 会驱动真实页面检测，若被监控系统高频轮询，会与正在生成的任务争用同一页面。建议：给 readiness 加缓存（如 5 秒 TTL）、在桶忙碌时返回上一次结果，并在 diagnostics 里标明数据新鲜度。

### 3.6 Responses API 的「流式」不是流式
`_stream_responses` 先 `await svc.run(task)` 完成后才一次性输出事件，且该路径没有保活注释，也不处理客户端断开与取消。对 Codex CLI 这类长任务容易触发客户端空闲超时。建议复用 chat 路径的 queue + keepalive + disconnect 检测，至少先补上保活。

### 3.7 配置在导入期固化
`config.py` 把环境变量读成模块常量，测试靠 monkeypatch 改模块属性，运行期无法热更新，且读取点散落各处。可保持现状，但建议：

- 增加启动时的一次性配置校验与摘要日志（不含密钥），如 `MAX_SESSION_BUCKETS` 与 `MAX_PAGE_POOL` 的关系、`STABLE_POLLS × POLL_INTERVAL_S` 的最小等待；
- 长期可迁移到 `pydantic-settings`，让校验与文档生成同源。

## 4. P2：工程化与体验

### 4.1 CI 与工具链
- 仓库没有 `.github/workflows`。建议加最小 CI：`ruff check`、`pytest`（e2e 跳过）、Python 3.13/3.14 矩阵。
- `pyproject.toml` 注释提到「mypy 配置」，但实际没有 `[tool.mypy]` 也未列入 dev 依赖；要么补上（先对 `claude_web/errors.py`、`session/`、`models.py` 这类纯逻辑模块开启），要么删掉注释。
- `requirements.txt` 无版本约束，与 `pyproject.toml` 的上下界重复。建议以 `pyproject.toml` 为准，README 改为 `uv pip install -e .[dev]`，或用 `uv lock` 生成锁文件。
- ruff 只选了 `E4/E7/E9/F/I`，行宽 100 但 `E501` 未启用，`driver.py` 中有不少超长行（如 `_find_chrome_executable`）。可启用 `E501`、`B`、`UP`、`SIM` 并逐步清理，或用 `ruff format` 统一。

### 4.2 目录整洁
- 仓库根目录存在 `user_data_chrome.bak-*`、`user_data_chrome.pre-refresh-*` 多份 profile 备份（含登录 Cookie，已被 `.gitignore` 的 `user_data*/` 忽略，不会提交）。建议统一放到 `~/.cache/claudebridge/` 或 `./.state/`，并在 README 中说明清理方法，降低误打包风险。
- `.gitignore` 里 `.freebuff/` 重复出现两次。
- 根目录 `main.py` 与 `claude_api_server.py` 并存，请确认是否都需要；若 `main.py` 只是转发，可删除以减少入口歧义。
- `session_store.py` 是纯委托门面，`_state = default_store` 注释为「兼容别名」。若内部已无调用方，建议删除并更新测试，避免未来出现第二份事实来源。

### 4.3 测试补强
现有测试覆盖了配置、会话、流式、工具调用解析、桥内命令，但以下场景值得补：

- `generator.generate` 的端到端时序（用 `tests/fakes.py` 的假页面）：回复重写、取消、停滞、上下文上限。
- 复用会话模式的回归：页面被换、系统提示变化、`/link --seed`、URL 精确比较。
- 鉴权矩阵：设置 `BRIDGE_TOKEN` 后，每个端点的 401/200 表格化测试（能防止 2.1 这类遗漏复发）。
- 并发：同桶串行、跨桶并行、`BUCKET_LOCK_TIMEOUT_S` 触发 `SessionBusyError`。
- 为 `tests/e2e` 增加一个手动触发的 workflow 或 Makefile 目标，并在 `doc/tasks.md` 记录最近一次真实探测日期。

### 4.4 可观测性
- 指标增加：每桶队列等待时间、播种次数 vs 增量次数、重写次数、取消次数、选择器回退命中。
- 日志用 `request_id` + `bucket` 贯穿；`/metrics` 可提供 Prometheus 文本格式（`Accept: text/plain`），便于接入现成监控。

### 4.5 文档
- `README.md` 仍写「每次任务新对话 + 完整历史播种」，而代码已改为默认复用会话；`doc/design.md` 与 `doc/tasks.md` 已更新，README 的能力表与「多轮历史」一行需要同步。
- 在 README 增加「已知限制与故障排查」：安全校验页（403）、登录失效、选择器漂移、上下文上限的识别与处理流程。
- `doc/design.md` 3.1 的端点表建议补上鉴权列。

## 5. 建议实施顺序

1. 鉴权补齐（2.1）、请求 ID 清洗（2.2）、内部错误码（2.3）：改动小，风险低。
2. 取消语义（2.5）、`_fill` 异常范围（3.4）、流式重写提示（2.4）。
3. 复用会话的状态一致性核对与测试（2.6，含 URL 精确比较）。
4. 页面池回收与 `mark_busy`（3.1）、Responses 保活（3.6）。
5. 会话存储内存化（3.3）与任务快照取舍（3.2）。
6. CI、依赖与目录整理、README 同步（第 4 节）。

## 5.1 第二轮补充核实（已读 `bridge_commands.py`、`service.py` 接线、`reap_idle` 调用链）

### A. 聊天命令绕过 `RESET_TOKEN`（P0）
`POST /session/reset` 需要 `BRIDGE_TOKEN` 与 `X-Reset-Token` 双重校验，但 `/v1/chat/completions` 里的 `/bridge session reset|link|unlink` 只过了 `_check_bridge_auth`，在 `handle_command` 内直接调用 `service.lifecycle.reset/link/unlink`，没有任何 `RESET_TOKEN` 检查。配置了 `RESET_TOKEN` 的部署里，任何持有 `BRIDGE_TOKEN` 的聊天客户端都能重置或改绑会话，等于绕过了这层保护。建议：命令执行前对「改状态」类命令（`CommandSpec.readonly=False`）复用同一套重置令牌策略（例如增加配置项 `ALLOW_CHAT_ADMIN_COMMANDS`，默认只开放只读命令），并补一条鉴权矩阵测试。

### B. `reap_idle` 确认无人调用（P1，证实 3.1）
全仓库只有 `driver.reap_idle()` 对 `pool.reap_idle()` 的包装，没有任何调用点；`mark_busy` 同样没有调用点。空闲回收与「忙碌页面不被 LRU 回收」目前都是死代码。

### C. `bridge_commands.py` 评价
优点：必须整条消息单行匹配；只看最后一条且必须是 `user`，工具循环重发历史不会重放旧命令；非流式与流式（含 Responses）共用 `handle_command`；执行失败只回异常类型名，不泄露堆栈。可改进点：
- `/bridge status` 的浏览器状态依赖 `driver.started` / `start_error` 的 `getattr`，属性缺失时会静默显示为未启动，建议在 driver 上显式提供只读属性。
- `_link_reply` 失败提示写「只读命令在浏览器不可用时依然可用」，但 `reset` / `unlink` 实际也不依赖浏览器，文案可更准确。
- 命令回复里的绑定 URL 未经 `redact`，而 HTTP 端点 `/session/link` 返回的是脱敏后的 URL，两条入口行为不一致。
- 命令调用次数未进入 metrics（建议 `bridge_command_total{action}`），排障时看不到被调用情况。
- `/bridge` 前缀被保留，用户想向 Claude 提问 `/bridge` 开头的内容会被截获；README 已有提醒，可在 `help` 里再注明规避方式。
- `_session_status_reply` 末尾的管理提示写的是 `/bridge reset|link|unlink`，与 `REGISTRY` 中的 `session` 写法不完全一致（别名可用，但文案建议统一）。

### D. 仍成立的结论
`lifecycle._same_conversation` 的前缀比较问题（2.6）、`generator` 取消抛 `GenerationStalledError`（2.5）、`_fill` 的 `except BaseException`（3.4）本轮未发现已修复迹象，保持原优先级。

## 5.2 第三轮补充：`c8719d3` 中一并提交的「提交未生效 / 多信号生成判定」改动\n\n验证：`pytest` 全部通过（e2e 默认跳过），`ruff check` 通过。本节只依据该提交的 diff，未逐行通读 `dom_adapter.py` 全文。\n\n### 做得好的地方\n- `end_detection.evaluate_poll` 的 `stalled` 不再受 `saw_generating` 闩锁影响，只看本轮观测，修复了「页面早已静止、客户端仍等到 `generation_timeout`」的问题；状态机仍是纯函数，便于单测。\n- 新增 `PromptSubmissionError` / `submit_lost` 快路径：页面空闲、无新回复、输入区仍留着本轮提示词时，空转满 `quiet_polls` 轮即失败。\n- 生成中判定改为「消息级 `data-is-streaming` + 控件级停止按钮」取并集；探测抛异常才记为未知。\n- 增加不含正文的 `[生成]` 收尾日志，排障时能直接看出结束原因、轮询次数与正文字符数。\n\n### 风险与建议\n- **误判窗口（P1）**：提交成功但站点清空输入区有延迟时，`composer_contains` 仍为真，可能在约 `quiet_polls × POLL_INTERVAL_S`（默认约 6 秒）后被判为提交未生效。建议触发快路径前再读一次输入区与回复计数确认，或把窗口放宽为可配置项。\n- **重试语义（P1）**：`PromptSubmissionError` 发生在提交之后，按 design.md 9.3 不得无条件重发。需要在文档里写明客户端是否可以安全重试，以及与「提交没生效」的区别是否对客户端可见。\n- **长时间思考的误报（P1）**：`stalled` 现在包含「页面空闲且正文无增长」，若长时间思考期间两路生成信号都探不到，约 `STALL_POLLS × POLL_INTERVAL_S`（默认约 30 秒）就会报 `generation_stalled`。建议核实消息级信号在思考阶段是否稳定为真，必要时让停滞窗口随「已见到正文 / 曾见生成中」放宽。\n- **指标缺口（P2）**：`service._record_failure` 只对 `generation_timeout` / `generation_stalled` 计 `request_timeout_total`，建议为 `prompt_submission_failed` 单独计数，便于观察站点改版或输入区选择器失效。\n- **每轮多一次 DOM 往返（P2）**：未见到回复前每轮都调用 `composer_contains`，可只在页面空闲时才检查。\n- **测试（P2）**：`end_detection` 与浏览器适配层已有新增用例；`generator.generate` 层面 `submit_lost` 路径的集成时序测试是否存在未核实，建议用 `tests/fakes.py` 补一条。\n- **流程（P2）**：这批改动被 `git add -A` 带入了只写「文档」的提交，提交信息与内容不符。建议以后按主题拆分提交。\n\n### 与前文结论的关系\n2.5（取消抛 `GenerationStalledError`）、3.4（`_fill` 的 `except BaseException`）在该提交后仍成立，优先级不变；2.6 中关于 `_same_conversation` 的结论也未受影响。\n\n## 6. 待核实（本次未读完，不下结论）

- `toolcalls.py`（约 714 行）：围栏/无围栏两种解析形态的边界，如嵌套代码块、重复调用、参数类型强转。
- `prompting.py`（约 473 行）：`CLAUDE_MAX_CONTEXT_CHARS` 超限时的截断策略是否会截掉最新的 tool 结果。
- `browser/dom_adapter.py`：选择器回退命中后是否记录到 metrics，以及 `detect_context_limit` 的误报率；`generating_state` / `composer_contains` 的实现细节只看过 diff，未通读。
- `bridge_commands.py`：已在 5.1 核实（仅最后一条 user 消息触发；各协议共用同一入口）。
