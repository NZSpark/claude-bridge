# ClaudeBridge 改进建议（通读版）

> 2026-10-12，基于 `95d09bc`（含 `5263773` 额度弹窗处理）。取代此前按提交追加的分段评审。
> 已通读：`server` `service` `driver` `page_pool` `completion/*` `end_detection` `browser/dom_adapter` `browser/selectors`（JS 片段部分被截断，未读全）`prompting` `toolcalls` `bridge_commands` `config` `session/*` `tasks` `metrics` `errors` `models` `api/*` `logging_setup` 入口文件与 `pyproject.toml`。
> 未通读：`tests/*`、`tools/bench_pipeline.py`、`tests/e2e/*`、README 与其它 doc。测试基线：`1d83cfa` 时 `pytest` / `ruff` 全绿，其后的提交我没有重跑。
> 每条结论都对应本次读到的代码；没把握的写在第 8 节。

## 1. 现状概览

- 分层清晰：协议适配 -> `ChatService`（桶锁 + 会话准备 + 生成 + 快照）-> `SessionLifecycle` / `ReplyGenerator` -> `ClaudeDOMAdapter`。`end_detection` 是纯函数状态机，阈值已改为时长，便于用虚拟时钟单测。
- 近期事故（提交未生效、会话到顶误报、`reply_not_found` 漏判、页面外壳混入正文、额度弹窗）都做了归因、日志与回归测试，方向正确。
- 主要风险已转移：不再是单个判定逻辑，而是 (1) 复用网页会话之后的上下文一致性，(2) 并发与取消语义，(3) 管理面鉴权，(4) 大量写了没用上的配置、指标与状态。

## 2. 已解决 / 经核对不再成立

- 生成停滞被 `saw_generating` 闩锁关掉；提交未生效快速失败；`context_limit` 假阳性（只在失败点扫描、克隆体剪枝）；`reply_not_found` 漏判（结构信号）；页面外壳剔除；额度弹窗识别与关闭（`UsageLimitError`，429）。
- 复用会话的播种标记：`service` 已把 `page` 与 `system_digest` 传给 `note_success`。
- `/bridge status` 依赖的 `driver.started` / `start_error` 现在是显式属性，旧评审里的这条作废。

## 3. P0：会直接造成事故或安全问题

### 3.1 工具说明会被项目自己的内容抑制（本次评审中已实际触发）
`prompting._mentions_tool_instructions` 在所有非 system 消息（含 tool 结果与 assistant 历史）里搜 `[Tool Calling Instructions]`。agent 只要读过本仓库的 `toolcalls.py`、测试或文档（都含这个字面量），此后每一轮、以及任何重新播种，都不再注入工具说明。本次会话里 `cat toolcalls.py` 之后的每一轮请求确实都不再带这段说明，模型只靠之前的上下文继续；一旦换新网页会话就会失效（表现为直接凭知识作答或输出 XML）。用本项目开发本项目必然触发。
建议：只在 system 与 user 角色里判重，且要求标题位于行首；或者不扫文本，改为记录「上一轮已发送的工具清单摘要」，清单没变就不重发。补一条回归测试：tool 结果里含该标题时仍要注入。

### 3.2 绝对超时会截断长回复，页面仍在生成
`end_detection.evaluate_poll` 里 `past_deadline and has_text` 直接 `finish`，排在 `extend` 之前。生成超过 `CLAUDE_TIMEOUT`（默认 300s）且已有部分正文时，回复被截断并以 `finish_reason=stop` 返回，页面还在继续生成；`RESPONSE_TIMEOUT_EXTEND_S` 只在到期时一个字都没有的情况下才起作用，与配置注释相反。
建议：到期时 `generating is True` 且还有延长额度就先 `extend`；真到上限时点停止，返回 `length`（或在 notes 里标明被截断）；补测试。

### 3.3 管理 / 诊断端点缺少鉴权
只有生成端点与 `/session/reset|link|unlink` 调用了 `_check_bridge_auth`。设置 `BRIDGE_TOKEN` 后，`GET /diagnostics`（桶名、会话元数据、任务摘要、配置开关）、`GET /session/{bucket}`、`GET /metrics`、`GET /readiness` 仍可匿名访问。更糟的是 `/readiness`、`/diagnostics` 会调用 `driver.readiness()`，匿名请求就能拉起 Chrome 并占用默认桶页面。
建议：`/diagnostics`、`/session/{bucket}` 一律鉴权；`/healthz` 保持开放；`/metrics`、`/readiness` 由配置项控制；`design.md` 端点表补鉴权列。

### 3.4 聊天命令绕过 `RESET_TOKEN`；管理操作不取桶锁
`POST /session/reset` 要 `BRIDGE_TOKEN` + `X-Reset-Token`，但 `/bridge reset|link|unlink` 只过了 `BRIDGE_TOKEN`，`handle_command` 直接调用 `lifecycle.reset/link/unlink`（`/v1/responses` 同样）。另外这些操作（连同 `/bridge modal`）都不经 `BucketLockManager`：对正在生成的桶 reset 会 `pool.drop` 把页面关掉，link 会 `goto` 打断在途请求。
建议：统一走 `service.admin(bucket)`：先取桶锁（短超时，失败回 busy），再执行；`readonly=False` 的命令套用 `RESET_TOKEN` 策略，或加 `ALLOW_CHAT_ADMIN_COMMANDS`（默认只开放只读命令）。补鉴权矩阵测试。

### 3.5 复用网页会话后的上下文串线
默认 `SESSION_SCOPING_BY_UA=true`：同一种客户端的不同任务共用一个桶。旧行为每轮新开对话，所以安全；现在改为只发增量，换任务时会把新任务的消息发进上一个任务的网页会话。`SessionLifecycle` 只比较系统提示摘要，而且系统提示变了也只是「重新带上系统指令」，并不换会话。`delta_messages` 取最后一条 assistant 之后的消息，没有任何一致性核对，客户端回滚、重新生成、压缩历史时，网页端与客户端的上下文就此分叉。
建议：成功后记录「已同步历史」的指纹（首条 user 摘要、消息条数、末条 assistant 摘要）；下一轮前缀对不上，或系统提示变了，就重新播种到新对话。同时在文档里说明多任务应使用 `X-Claude-Session` 区分桶。

## 4. P1：可靠性与可维护性

### 4.1 驱动与页面池的竞态
- `ClaudeWebDriver.start()` 没有锁：浏览器未启动时两个不同桶的请求同时到来，会各启动一次 Playwright / Chrome。
- `PagePool.acquire` 没有锁：`await factory()` 期间另一个协程可以为同一桶再建一个页面，先建的被覆盖并泄漏；并发的 `evict_if_needed` 也可能让页面数超过 `MAX_PAGE_POOL`。`readiness()` 不取桶锁，正好会和默认桶的生成请求撞上。
建议：`start()` 与 `acquire()` 各加 `asyncio.Lock`（按桶或全局），`readiness` 对忙碌桶返回缓存结果。

### 4.2 空闲回收与忙碌保护是死代码
全仓库只有 `driver.reap_idle()` 对 `pool.reap_idle()` 的包装，`lifespan` 没有周期任务，`mark_busy` 没有任何调用点。结果：`BUCKET_IDLE_TTL_S` 不生效；`MAX_PAGE_POOL` 与 `MAX_SESSION_BUCKETS` 默认都是 4，第 5 个桶进来会 LRU 关掉某个正在生成的页面。
建议：`lifespan` 起周期回收任务；`service._run_locked` 在生成前后 `mark_busy(True/False)`；客户端可任意指定桶名，应限制新桶创建速率或在满池时返回 busy，而不是驱逐他人页面。

### 4.3 取消、断连与「页面上一轮仍在生成」
- 非流式与 Responses 路径没有断连检测：客户端走了，生成仍占着桶锁跑完（最长 300s + 延长额度）。
- 取消时 `generator` 抛 `GenerationStalledError`，被 `_record_failure` 计成失败、写进会话 `last_error`、污染超时统计；`generate._fill` 里的 `except BaseException` 还会吞掉 `CancelledError`。应新增 `GenerationCancelledError`，只捕获 `Exception`。
- 取消、超时、失败后页面可能仍在生成；`prepare` / `generate` 开头不检查 `generating_state`，下一次提交时 Enter 可能被忽略，随即误报「提交未生效」。建议提交前若页面仍在生成：先等一小段，仍未结束就点停止（只点停止，不点其它控件）。
- 失败发生在提交之后，网页会话里已有这条提示词；客户端重试同样的增量会让网页端出现重复。失败后应把该桶标记为「状态不确定」，下一轮按 3.5 的指纹决定是否重播。

### 4.4 Responses API 的兼容缺口
- `_stream_responses` 先 `await svc.run` 再一次性吐事件：没有保活、不处理断开与取消，长任务容易触发客户端空闲超时。
- 错误处理缺失：`StreamingResponse` 已开始后抛出的 `BridgeError` 不能再变成 HTTP 错误，也不会变成 SSE 错误事件（chat 路径有 `sse_error`，这里没有）。
- `store_thread` 只保存 `role` / `content`，丢掉了 `tool_calls` 与 `tool_call_id`，`previous_response_id` 续接工具循环时调用关系会断。
- 事件序列只有 `created` / `output_text.delta` / `function_call_arguments.delta` / `completed`，缺 `output_item.added|done`、`output_text.done` 等；严格的客户端（例如 Codex CLI）可能拒绝，需要用真实客户端做一次兼容探测。
建议：复用 chat 路径的队列 + 保活 + 断连 + 错误事件，再补齐事件集。

### 4.5 提示词体量与默认值
- 增量模式下每一轮都会再插入完整的工具说明（约 3KB），因为判重只扫入站消息而不看「网页会话里是否已有」。50 轮就是约 150KB 重复内容，会加速触顶。建议只在重播种、工具清单变化或每 N 轮重发。
- `SYSTEM_MAX_CHARS` 默认 8000：Codex / Pi 一类客户端的系统提示常常更长，尾部规则会被静默截断（只在末尾留一句「已截断」）。建议调大默认值，或把截断情况写进 notes 与响应头。
- `SEED_ENV_NOTE` 默认文案写死了 git 命令与「项目就在本机」，对非编码场景是噪声；默认留空更稳妥，由部署方配置。
- `tool_choice`（`none` / `required` / 指定函数）被解析进 `ChatTask` 却从未使用；至少 `none` 应当不注入工具说明。

### 4.6 错误语义
- `_unhandled_handler` 把内部 bug 包成 `NotReadyError`（503），客户端会当成登录或页面问题去重试。应新增 `InternalError`（500）。
- `fill_composer` 内部已重试 `FILL_RETRIES`（3），`generator._fill` 外面再重试 `MAX_UPSTREAM_RETRIES + 1`，叠加成六次，而且连 `ComposerNotFound`、鉴权类错误也在重试。只重试可恢复的写入失败。
- 失败归因：`generate` 里只要提交前见过额度弹窗（`limit_modal_seen`），任何失败（包括已有正文后的停滞）都会被报成 `UsageLimitError`。建议只在没有新回复，或弹窗仍在页面上时才归因额度。
- `_record_failure` 只给 `generation_timeout` / `generation_stalled` 计 `request_timeout_total`，其它类别（`usage_limit_reached`、`prompt_submission_failed`、`reply_not_found`、`context_limit_reached`）没有各自的计数。

### 4.7 会话与绑定
- `_same_conversation` 用 `startswith(target[:max(10, len-1)])` 比较，会话 ID 只差最后一位会被当成同一条；`lifecycle.link` 的落点校验也用同样的前缀写法。应解析 URL，精确比较 `/chat/<ID>`。
- `store.reset()` 新建一个空 `SessionState`，**同时清掉了 `linked_url`**；`/session/reset` 与 `/bridge reset` 的回复都没说明这一点，绑定会话的桶 reset 后会悄悄变成未绑定。
- 绑定桶触顶后 `has_history=False`，下一轮仍然 `goto` 同一个已满的会话，会重复失败。
- 未绑定的桶新建了网页会话，但没有保存它的 URL；服务重启或页面被回收后只能新开会话并重播完整历史，可以考虑可选地保存并恢复。

### 4.8 配置与启动
- `SESSION_FILE`、`USER_DATA_DIR`、`TASK_FILE_DIR`、`OUTPUT_DIR` 默认是相对路径，按当前工作目录解析：换个目录启动服务就会换一份浏览器 profile，登录态丢失。应以 `PROJECT_ROOT` 为基准解析。
- `env_list` 同时按 `||` 与逗号切分：含逗号的选择器（`:is(a,b)`、选择器组）会被切坏。选择器类配置只用 `||`。
- `.env` 解析器不支持行内注释；默认 `LINK_ALLOWED_HOSTS` 可设为 `*`，此时登录态浏览器能被导航到任意 https 站点，并把提示词输进去，建议去掉 `*` 或启动时强告警。
- 没有启动期校验：旧键（`STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS`）静默失效；`BUCKET_LOCK_TIMEOUT_S`（120s）小于 `CLAUDE_TIMEOUT`（300s），同桶排队的请求会先超时；`SETTLE_NO_SIGNAL_S >= SETTLE_S`、`STALL_S > SUBMIT_LOST_S` 也没人检查。建议启动时校验并把生效值写进摘要日志（不含密钥）。
- 不支持多 worker（状态全在进程内），文档里应写明；浏览器是惰性启动的，首个请求要承担启动成本，可加 `WARM_START`。

### 4.9 日志与可观测性
- 日志上下文在流式请求里很可能丢失：`log_context` 在路由返回 `StreamingResponse` 时就退出了，而生成发生在之后迭代响应体的阶段，所以最长的那段日志没有 `request_id` / `session_key`。把上下文绑定放进生成器内部，或改成纯 ASGI 中间件。
- `X-Request-Id` 未清洗就写进日志与响应头，需白名单字符 + 长度上限。
- `bridge_commands` 日志直接打印会话 URL、桶键与命令首行，没有走 `redact`；`handle_command` 里 `exc_info=True` 会把完整堆栈写进日志。
- 见第 5 节：多个指标从未被增加，`/metrics` 上的零值会误导排障。

### 4.10 流式细节
- 检测到网页「重写回复」后不再发送，却仍以 `finish_reason=stop` 收尾，客户端拿到的是截断文本且无任何提示。至少加 `stream_rewritten_total` 指标，并在最终块或 `bridge_notes` 里说明。
- `generate` 每轮轮询都调用 `on_delta(normalized)`，即使内容没变，队列里堆满重复项；只在内容变化时回调。
- 流式最后一块没有 `usage`，忽略了 `stream_options.include_usage`。
- 读取已见回复时若节点短暂消失，`read_new` 抛出的 `ReplyExtractionError` 会让整个请求失败；窗口很小，但已经看到正文时应保留上一次文本继续判定。

### 4.11 回复外壳剔除与工具调用解析的边界
- 文本兜底按前缀 `Claude said:` 剥离，正文以此开头（引用）会被误剥；末行恰为 `10:30`、`3 days ago` 的日志或代码类回复会丢最后一行。建议只在 DOM 侧确认存在 `sr-only` / `<time>` 外壳时才启用兜底，并用含公式、表格、代码块的真实回复各比对一次。
- `strip_tool_call_blocks` 只处理围栏与无围栏两种形态；调用来自 DSML / json 围栏 / `tool_uses` 路径时，原始标记会残留在返回的正文里。
- 工具参数只检查「是 JSON 对象」，不核对 `required` 与类型，而文档字符串写的是「参数形状校验」。可加轻量 schema 校验，失败时作为诊断返回。
- `toolcalls.py` 714 行，建议拆成「提取」与「校验 / 强转」两层，并补随机化测试（嵌套代码块、重复调用、半截 JSON）。

### 4.12 状态与快照落盘
- 每次 `save_state` 都 `load_document()` 再整体写回；同步 IO 在 asyncio 里持有线程锁，会阻塞事件循环。建议内存持有文档、变更时原子写盘，必要时 `asyncio.to_thread`；`state()` 返回可变缓存对象，忘记 `save_state` 就只改了内存。
- `TaskSnapshot.last_reply` 把完整回复明文写进 `user_data/.claude_tasks`，`goal` 也含用户消息前 2000 字；应当可配置为不落正文，并在 README 说明。

## 5. 写了但没有用上的东西（契约漂移）

- 指标从未被增加：`request_retry_total` `browser_unavailable_total` `browser_selector_miss_total` `reply_extraction_failure_total` `session_recovery_total` `tool_parse_failure_total`，以及耗时 `request_latency` `reply_extraction_latency`。
- 配置：`STREAM_KEEPALIVE_S`（`server` 用写死的 `_KEEPALIVE`）、`TASK_KEEP_MESSAGES`。
- 状态字段：`SessionState.cap_failures` 只写不读（注释说会换更小的播种预算，没有实现）；`TaskSnapshot.messages`、`recoveries` 与状态 `queued` / `generating` / `recovering` 从未被设置，生产代码里也没有恢复路径使用任务快照（`load` 是否只被测试调用，见第 8 节）。
- 函数：`models.model_is_supported`、`session.lock.WARNED_BAD_STATE`（store 自己有一份标志）、`errors.page_usable_reason` / `page_responds` / `is_page_lost_error` / `is_timeout_error` 在 `claude_web` 里没有调用方。
- 其它：`prompting.py` 里 `_OMISSION_NOTE` 的注释重复了两遍；`/bridge modal` 注册为「改状态」，实际不改会话状态；`session_store.py` 是纯委托门面；`main.py` 与 `claude_api_server.py` 并存且都启动服务。
处理原则：要么接上（页面判活应当用在 `ensure_usable` 与 readiness 里），要么删掉并同步 `design.md`，不要保留「看起来有保护」的空壳。

## 6. P2：工程化与体验

- **CI**：没有 `.github/workflows`。最小方案：`ruff check` + `pytest`（跳过 e2e）+ 版本矩阵；e2e 做手动触发，并在 `doc/tasks.md` 记录最近一次真实探测日期。
- **类型**：`pyproject.toml` 注释提到 mypy，但没有 `[tool.mypy]`。先对 `errors` `session/*` `models` `end_detection` 开启，或删除注释。
- **依赖**：`requirements.txt` 无版本约束且与 `pyproject.toml` 重复，以后者为准并生成锁文件；`requires-python >=3.13` 偏严，代码看起来不依赖 3.13 特性，放宽能让更多人用上。
- **ruff**：目前只选 `E4/E7/E9/F/I`，可逐步加 `B`、`UP`、`SIM`。
- **目录整洁**：根目录有 `user_data_chrome.bak-*`、`pre-refresh-*` 等含登录 Cookie 的 profile 备份（已被忽略，不会提交），建议统一放到用户缓存目录；`.gitignore` 里 `.freebuff/` 重复。
- **测试补强**：`generate` 端到端时序（重写、取消、停滞、超时延长）；复用会话回归（页面被换、系统提示变化、`link --seed`、URL 精确比较）；鉴权矩阵（设置 `BRIDGE_TOKEN` 后逐端点 401 / 200）；同桶串行、跨桶并行、管理操作取锁；tool 结果里含工具说明标题；`tests/e2e` 增加「会话里含特征文案但可正常对话」「条数停在基线但页面已回答」两条真实用例。
- **指标**：每桶排队等待、播种与增量次数、重写次数、取消次数、选择器回退命中、`structural=True` 认回复次数；`/metrics` 可提供 Prometheus 文本格式。
- **文档**：`design.md` 已近 700 行，建议把「结束判定与回复识别」拆成独立文档，并在 README 加「现象 -> 日志关键字 -> 处理」对照表（`prompt_submission_failed` `generation_stalled` `context_limit_reached` `reply_not_found` `challenge_required` `usage_limit_reached`）。
- **提交习惯**：按主题拆分提交，提交前看 `git status` 与 `git diff --stat`。

## 7. 建议实施顺序

1. 3.1 工具说明判重、3.2 超时延长：改动小、影响面最大，各配一条回归测试。
2. 3.3 鉴权补齐、3.4 管理操作取桶锁 + 令牌、4.9 请求 ID 清洗、4.6 内部错误码。
3. 4.1 驱动 / 页面池加锁、4.2 回收与 `mark_busy`。
4. 3.5 历史指纹（先只记录并打日志，再切换为触发重播）、4.3 取消与页面忙碌处理。
5. 4.4 Responses 流式 / 事件集 / 线程续接，配真实客户端探测。
6. 4.5 提示词体量、4.8 配置路径与启动校验、4.7 会话比较与 reset 语义。
7. 第 5 节清理（接上或删除）、4.10 / 4.11 / 4.12。
8. 第 6 节工程化。

## 8. 未覆盖或不确定

- `selectors.GENERATING_STATE_JS` 的实现细节没读全，4.3 中「页面仍在生成」的判定依赖它。
- `TaskStore.load` / `clear`、`session_store.py` 是否有生产调用方，没有做全仓库 grep 确认，第 5 节相关结论按「未见调用」理解。
- 4.9 中日志上下文在流式请求里丢失，是依据 contextvars 与 Starlette 的执行顺序推断的，未在真实请求上验证。
- 4.4 中 Responses 事件集是否满足 Codex CLI，需要真实客户端验证，不是从代码能断定的。
- `tests/*`、`tools/bench_pipeline.py`、README 与其它 doc 没有通读；`5263773` 之后的测试结果没有重跑。
