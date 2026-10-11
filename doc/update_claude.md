# ClaudeBridge 改进建议（整体重审版）

> 2026-10-12，基于提交 `1d83cfa` 重新审视整个项目，取代此前按提交追加的分段评审。
> 本次用 grep 对当前代码重新核对了 P0/P1 结论；未通读的部分在第 7 节单列。
> 说明：请求里写的路径是 `doc/doc/update_claude.md`，该目录不存在，按笔误处理，仍更新 `doc/update_claude.md`。

## 1. 现状概览

- 规模：约 12.9k 行（含文档），测试约 3.5k 行。最大的文件：`toolcalls.py` 714、`design.md` 686、`dom_adapter.py` 580、`prompting.py` 535、`bridge_commands.py` 508、`server.py` 498。
- 基线：`pytest` 全绿（e2e 默认跳过），`ruff check` 通过。
- 架构：协议适配（chat / responses）→ `ChatService` → `SessionLifecycle` / `ReplyGenerator` → `ClaudeDOMAdapter`，分层清晰；`end_detection` 是纯函数状态机，可用虚拟时钟单测。
- 近期主线：复用网页会话只发增量；结束判定改为时长阈值 + 多信号；修复「提交未生效」「会话到顶误报」「回复条数口径漏判」三类线上事故；回复正文剔除页面外壳。方向正确，且都带回归测试。

## 2. 已解决（不再作为待办）

- 生成停滞被 `saw_generating` 闩锁关掉、一路等到绝对超时：已改为只看本轮观测。
- 提交没生效快速失败（`PromptSubmissionError`：页面静止且输入区仍留着提示词）。
- 「会话到顶」误报：整页扫描只在即将判定失败时做一次，剔除清单含用户消息与输入区，剔除改为克隆体剪枝。
- 只看 assistant 条数导致 `reply_not_found` 漏判：新增结构信号（最后一条提问已变且其后有 assistant 节点）。
- 回复正文混入无障碍标题、时间戳、动作栏：DOM 侧 `REPLY_CHROME_SELECTORS` + Python 侧窄口径兜底。
- 复用会话的播种标记：`service._run_locked` 已把 `page` 与 `system_digest` 传给 `note_success`，「每轮重新播种」的担忧不成立。
- 测试辅助集中到 `tests/fakes.py`（`VirtualClock`）与 `tests/conftest.py`（`virtual_time`、`default_timing`）。

## 3. P0：安全与正确性（本次已用代码复核，仍未修复）

### 3.1 管理 / 诊断端点缺少鉴权
`server.py` 里只有生成端点与 `/session/reset|link|unlink` 调用了 `_check_bridge_auth`。设置了 `BRIDGE_TOKEN` 后，下列端点仍可匿名访问：`GET /diagnostics`（桶名、会话元数据、任务摘要、配置开关）、`GET /session/{bucket}`、`GET /metrics`、`GET /readiness`。另请确认 `/debug/dom` 默认关闭。
建议：`/diagnostics`、`/session/{bucket}` 一律鉴权；`/healthz` 保持开放；`/metrics`、`/readiness` 用配置项（如 `PROTECT_OBSERVABILITY`）控制；`design.md` 端点表补一列鉴权。

### 3.2 聊天命令绕过 `RESET_TOKEN`
`POST /session/reset` 需要 `BRIDGE_TOKEN` 与 `X-Reset-Token`，但 `/v1/chat/completions` 里的 `/bridge session reset|link|unlink` 只过了 `_check_bridge_auth`，`handle_command` 直接调用 `lifecycle.reset/link/unlink`。配置了 `RESET_TOKEN` 的部署里，持有 `BRIDGE_TOKEN` 的聊天客户端能绕过这层保护。
建议：对 `CommandSpec.readonly=False` 的命令复用重置令牌策略，或增加 `ALLOW_CHAT_ADMIN_COMMANDS`（默认只开放只读命令），并补鉴权矩阵测试。

### 3.3 `X-Request-Id` 未清洗
`_request_context` 直接采用客户端传来的 `x-request-id` 写入日志上下文与响应头。建议白名单字符 + 长度上限（如 64），非法时重新生成。

### 3.4 未处理异常被伪装成 503「未就绪」
`_unhandled_handler` 把内部 bug 包成 `NotReadyError`，客户端会误判为登录 / 页面问题并重试。建议新增 `InternalError`（HTTP 500），`NotReadyError` 只用于浏览器、登录、页面不可用。

### 3.5 取消语义用错异常
`generator.generate` 在 `cancel()` 为真时抛 `GenerationStalledError`（约第 232 行），会被 `_record_failure` 计入失败、写入会话 `last_error`、污染超时统计。建议新增 `GenerationCancelledError`，在 service 里单独计数、不标记会话错误。

### 3.6 `_fill` 的 `except BaseException`
`generator.py` 约第 147 行用 `BaseException` 做提交前重试，会吞掉 `CancelledError` / `KeyboardInterrupt` 的语义。应只捕获 `Exception`，让取消直接冒泡。

### 3.7 `_same_conversation` 前缀比较
`lifecycle._same_conversation` 用 `startswith(target_url[: max(10, len-1)])`，会话 ID 只差最后一位时被判为同一条，可能把请求发进错误的会话。建议解析 URL，精确比较路径中的会话 ID。

### 3.8 流式「网页重写回复」时静默截断
检测到 `reset` 后停止发送，但仍以 `finish_reason=stop` 结束，客户端无法得知被截断。至少增加 `stream_rewritten_total` 指标，并在最终块或 `bridge_notes` 里说明。

## 4. P1：可靠性与可维护性

### 4.1 空闲回收与忙碌保护是死代码
全仓库只有 `driver.reap_idle()` 对 `pool.reap_idle()` 的包装，`server.lifespan` 没有任何周期任务调用，`mark_busy` 也无调用点。因此 `BUCKET_IDLE_TTL_S` 不生效，LRU 可能回收正在生成的页面。建议 lifespan 起周期任务，并在生成前后 `mark_busy(True/False)`。

### 4.2 旧配置键静默失效
`STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS` 已被 `SETTLE_S`、`SETTLE_NO_SIGNAL_S`、`SUBMIT_LOST_S`、`STALL_S`、`POLL_INTERVAL_ACTIVE_S` 取代。README 已有迁移说明，但已有部署的 `.env` 里残留旧键会被直接忽略，默认窗口（约 6 秒判提交未生效、30 秒判停滞）可能与原先调过的值不同。建议启动时发现旧键就打警告，并校验 `SETTLE_NO_SIGNAL_S >= SETTLE_S`、`STALL_S > SUBMIT_LOST_S`，把生效值写进摘要日志。

### 4.3 误判窗口与长时间思考
- 提交成功但站点清空输入区有延迟时，`composer_contains` 仍为真，可能约 `SUBMIT_LOST_S` 后被误判为提交未生效。建议触发快路径前再读一次输入区与回复结构状态确认。
- 长时间思考期间若两路生成信号都探不到，约 `STALL_S`（30 秒）就会报 `generation_stalled`。需实测消息级 `data-is-streaming` 在思考阶段是否稳定为真，必要时让停滞窗口在「曾见生成中」后放宽。
- `PromptSubmissionError` 发生在提交之后，按 `design.md` 9.3 不得无条件重发；需在文档写明客户端能否安全重试。
- `service._record_failure` 只对 `generation_timeout` / `generation_stalled` 计 `request_timeout_total`，建议为 `prompt_submission_failed`、`context_limit_reached`、`reply_not_found` 各自计数。

### 4.4 回复结构信号的边界
- `read_new` 在条数没涨时，需要 `trailing_reply` 且「提问已变」才认定新回复；若 `USER_MESSAGE_SELECTORS` 被站点改版打空，结构信号整体失效，退回只看条数。建议在 `/diagnostics` 暴露两路选择器命中数，并在连续失败时给出明确提示。
- `user_count` 相等时靠文本尾巴指纹比较；两轮提问末 200 字符相同（例如重复发同一句话）时无法区分。可再叠加提问节点的 DOM 序号或 `data-*` 标识（若页面提供）。
- 每轮轮询的 DOM 往返在增加（生成状态、回复状态、输入区）。可合并成一次 `page.evaluate`，减少延迟并避免读到不一致的中间态。

### 4.5 页面外壳剔除的边界
- 文本兜底按前缀 `Claude said:` 等剥离，正文本身以这类前缀开头（引用）会被误剥；末行恰好是 `10:30`、`3 days ago` 的代码 / 日志类回复会丢最后一行。建议仅在 DOM 侧确认存在 `sr-only` / `<time>` 外壳时才启用兜底。
- 默认清单里的 `button`、`[role=status]`、`[aria-hidden=true]` 可能误伤公式的可视层或代码块头部。需用含公式、表格、代码块的真实回复各跑一次对比。
- 流式中无障碍标题与正文长度不同步时，中间态可能被当成「网页重写」。建议补一条流式用例。

### 4.6 `/readiness` 与 `/diagnostics` 会触碰浏览器
高频探活会与生成任务争用同一页面。建议加 5 秒 TTL 缓存、桶忙碌时返回上次结果，并标明数据新鲜度。

### 4.7 Responses API 的「流式」不是流式
`_stream_responses` 先 `await svc.run(task)` 完成再一次性输出事件，没有保活，也不处理断开与取消。长任务（Codex CLI 等）容易触发客户端空闲超时。建议复用 chat 路径的队列 + keepalive + 断开检测。

### 4.8 会话存储与任务快照
- 每次 `save_state` 都 `load_document()` 再整体写回；`state()` 返回可变缓存对象，忘记 `save_state` 就只改了内存；`STATE_FILE_LOCK` 是线程锁，在 asyncio 里持锁做同步 IO 会阻塞事件循环。建议内存持有文档、变更时原子写盘，对外只暴露 `update(**fields)`，必要时 `asyncio.to_thread`。
- `TaskStore` 每桶只存一个文件，且只写 `preparing/submitted/completed/failed`，设计文档里的 `generating`、`recovering` 未落实；要么实现恢复，要么精简文档与字段。

### 4.9 异常处理过宽
`claude_web` 里约 49 处 `except Exception` / `BaseException`，集中在 `dom_adapter.py`（17）与 `driver.py`（9）。浏览器侧不可预测，多数合理，但建议收敛成 `PLAYWRIGHT_ERRORS` 元组常量，其余显式失败，并保证吞错处至少有一条 debug 日志。

### 4.10 聊天命令的小问题
- `/bridge status` 依赖 `getattr(driver, 'started')`，属性缺失时静默显示未启动；建议 driver 显式提供只读属性。
- `_link_reply` 失败文案说「只读命令在浏览器不可用时依然可用」，而 `reset` / `unlink` 同样不依赖浏览器。
- 命令回复里的绑定 URL 未脱敏，而 `/session/link` 返回的是脱敏后的，两条入口不一致。
- 命令调用未进 metrics（建议 `bridge_command_total{action}`）。
- `_session_status_reply` 的管理提示写 `reset|link|unlink`，与 `session ...` 写法不统一。

### 4.11 `toolcalls.py` 体量
714 行单文件，围栏与无围栏两种解析并存。建议拆成「提取」与「校验 / 类型强转」两层，并补随机化测试（嵌套代码块、重复调用、半截 JSON），避免解析回归。

## 5. P2：工程化与体验

- **CI**：仓库没有 `.github/workflows`。加最小 CI：`ruff check`、`pytest`（跳过 e2e）、Python 版本矩阵；e2e 做成手动触发 workflow 或 Makefile 目标，并在 `doc/tasks.md` 记录最近一次真实探测日期。
- **mypy**：`pyproject.toml` 注释提到 mypy 配置，但没有 `[tool.mypy]`。补上（先对 `errors.py`、`session/`、`models.py`、`end_detection.py` 开启）或删注释。
- **依赖**：`requirements.txt` 无版本约束，与 `pyproject.toml` 重复。以 `pyproject.toml` 为准，README 改用 `pip install -e .[dev]` 或 `uv`，并生成锁文件。
- **ruff**：目前只选 `E4/E7/E9/F/I`。可逐步启用 `E501`、`B`、`UP`、`SIM`，或统一 `ruff format`。
- **目录整洁**：根目录存在 `user_data_chrome.bak-*`、`pre-refresh-*` 等含登录 Cookie 的 profile 备份（已被忽略，不会提交）。建议统一放到 `~/.cache/claudebridge/`，README 说明清理方法；`.gitignore` 中 `.freebuff/` 重复；根目录 `main.py` 与 `claude_api_server.py` 并存请确认是否都需要；`session_store.py` 是纯委托门面，若无调用方可删除。
- **测试补强**：`generator.generate` 端到端时序（重写、取消、停滞、上下文上限）；复用会话回归（页面被换、系统提示变化、`link --seed`、URL 精确比较）；鉴权矩阵（设置 `BRIDGE_TOKEN` 后逐端点 401/200）；并发（同桶串行、跨桶并行、`SessionBusyError`）；`tests/e2e` 增加「会话里含特征文案但可正常对话」「条数停在基线但页面已回答」两条真实用例。
- **可观测性**：指标增加每桶队列等待、播种 vs 增量次数、重写次数、取消次数、选择器回退命中、结构信号认回复次数（`structural=True`）；日志用 `request_id` + `bucket` 贯穿；`/metrics` 可提供 Prometheus 文本格式。
- **文档**：`design.md` 已达 686 行，建议把 4.4（结束判定与回复识别）拆成独立文档，并在 README 给出「故障现象 → 日志关键字 → 处理」对照表（`prompt_submission_failed`、`generation_stalled`、`context_limit_reached`、`reply_not_found`、`challenge_required`）。
- **提交习惯**：此前两次 `git add -A` 把无关改动并入文档提交，提交信息与内容不符。建议按主题分提交，提交前先看 `git status` 与 `git diff --stat`。

## 6. 建议实施顺序

1. 鉴权补齐（3.1）、聊天命令令牌（3.2）、请求 ID 清洗（3.3）、内部错误码（3.4）：改动小、风险低。
2. 取消语义（3.5）、`_fill` 异常范围（3.6）、`_same_conversation` 精确比较（3.7）、流式重写提示（3.8）。
3. 旧配置键警告与时长参数校验（4.2）、失败类别指标（4.3）。
4. 页面池回收与 `mark_busy`（4.1）、Responses 保活（4.7）。
5. DOM 探测合并与结构信号诊断（4.4）、外壳剔除边界用例（4.5）。
6. 会话存储与任务快照取舍（4.8）、异常收敛（4.9）。
7. CI、依赖、目录整理、文档拆分（第 5 节）。

## 7. 待核实（未通读，不下结论）

- `toolcalls.py`：嵌套代码块、重复调用、参数类型强转的边界。
- `prompting.py`：`CLAUDE_MAX_CONTEXT_CHARS` 超限时是否会截掉最新的 tool 结果；用户消息里出现工具说明标题时不再追加工具说明的副作用。
- `dom_adapter.py`：选择器回退命中是否进 metrics；`detect_context_limit` 克隆体 `innerText` 退化为 `textContent` 后，隐藏节点文本也会被读到（现在只在失败点才扫，影响面小）。
- `tools/bench_pipeline.py`、`tests/test_extraction_chrome.py`：只看过 diff 与提交说明。
- `driver.py`：浏览器启动、Cookie 与 profile 管理、崩溃恢复路径。
