# ClaudeBridge 任务分解（来源：doc/update_chatgpt.md）

> 日期：2026-10-12。依据全项目静态审查结果分解，按故障影响和依赖排序；不把尚未经过真实环境验证的判断当作实测结论。
>
> 范围：涵盖正确性、并发与资源生命周期、API 与正文处理、持久化、配置、可观测性、工程化和文档；鉴权与访问控制不属于本项目改进范围。
>
> 执行约定：每项行为变更单独提交并配回归测试；开始前检查工作区，结束后检查 `git diff --check`、定向测试、`pytest -q` 和 `ruff check .`。若基线本身失败，先记录并区分新增失败，不得将已有失败描述为本次回归。真实浏览器和真实客户端验证单独记录结果，不以 FakePage 测试替代。

## 阶段 0：确认基线与高风险假设

- [ ] **T0.1** 核对静态审查结论与实际调用路径
  - 来源：全篇，优先第 3、4、5、6 节。
  - 做法：对照当前代码核实管理命令入口、桶锁、页面池 busy 标记、取消传播、超时分支和 Responses SSE 路径；记录可直接确认的问题及仍需现场验证的假设。鉴权与访问控制相关内容不纳入执行范围。
  - 验收：后续任务的文件和行为范围与当前代码一致；不依赖未验证假设实施破坏性改动。

## 阶段 1：P0 正确性与管理操作边界

- [x] **T1.1** 修复工具说明判重范围过宽（来源 3.1）
  - 文件：`claude_web/prompting.py`、`tests/test_prompting.py`。
  - 做法：只在适当的 system / user 消息中识别显式工具说明；标题要求位于行首并满足明确边界；assistant 历史和 tool 输出不参与判重。避免普通引用和代码示例误触发。
  - 验收：tool 结果和 assistant 历史包含 `[Tool Calling Instructions]` 时仍正常注入；system 中已有有效说明时不重复注入；代码块、行首及前后缀边界有回归用例。


- [x] **T1.2** 统一管理操作入口并协调桶锁（来源 update 3.2）
  - 文件：`claude_web/server.py`、`claude_web/bridge_commands.py`、`claude_web/service.py`、`claude_web/completion/lifecycle.py`、相关管理命令和会话测试。
  - 做法：提供统一的管理操作入口，在同一桶锁内执行 reset / link / unlink / modal；HTTP、Chat Completions、Responses 的管理命令复用同一业务实现。取锁超时返回明确的 `session_busy`，不可强行关闭在途任务的页面。不在本任务中新增或调整鉴权与访问控制策略。
  - 验收：桶繁忙时 reset / link / unlink / modal 不破坏在途生成；桶空闲时操作保持预期行为；不同协议入口的管理操作结果一致。

- [x] **T1.3** 明确 reset 与会话绑定语义（来源 5.1）
  - 文件：`claude_web/session/store.py`、`claude_web/completion/lifecycle.py`、`claude_web/bridge_commands.py`、`tests/test_sessions.py`、相关文档。
  - 做法：reset 清除对话状态但保留 `linked_url`；unlink 才负责解绑。同步更新 HTTP 和聊天命令回复，避免文案暗示绑定仍在或已经解除但实际状态相反。
  - 验收：绑定桶 reset 后 `status` 仍显示绑定；unlink 后绑定被清除；相关行为与管理锁测试相容。

- [x] **T1.4a** 增加客户端历史一致性指纹与诊断（来源 update 3.3）
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/session/schema.py`、`claude_web/service.py`、`tests/test_conversation_reuse.py`。
  - 做法：成功同步后记录稳定的历史指纹，例如首条 user 摘要、消息序列/条数、末条 assistant 摘要、system 提示摘要和工具清单摘要。下一轮检测有效前缀或摘要不一致时，先记录低敏日志与 notes，不立即改变复用行为。不得记录完整正文作为诊断信息。
  - 验收：指纹生成和比较具有确定性；覆盖相同提示词、回滚、重新生成、换任务、历史压缩、system 提示变化及旧状态缺字段的兼容行为。

- [ ] **T1.4b** 对历史不一致执行安全重新播种（来源 update 3.3）
  - 依赖：T1.4a。
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/service.py`、`tests/test_conversation_reuse.py`、`README.md`。
  - 做法：历史前缀、system 提示或工具清单不一致时，新建对话并完整播种；对无法确定网页是否已收到增量的状态禁止盲目重放。明确 UA 分桶不提供用户或任务隔离保证。
  - 验收：回滚、重新生成、换任务、压缩历史各有回归用例；不会把新任务接入不兼容的旧上下文；README 说明需要隔离时显式设置 `X-Claude-Session`。

- [x] **T1.6** 修正绝对超时、延长预算与截断语义（来源 4.5）
  - 文件：`claude_web/end_detection.py`、`claude_web/completion/generator.py`、`claude_web/api/chat_adapter.py`、`tests/test_end_detection.py`、`tests/test_generation_latency.py`、`README.md`。
  - 做法：绝对 deadline 已到但页面仍在生成且有延长预算时优先延长；预算耗尽后仍未结束则将已有正文标记为截断，支持时返回 `finish_reason=length`，并通过 notes / 错误元数据说明。已经可靠落定的回复不应无故继续等待。
  - 验收：虚拟时钟覆盖到期且仍生成并已有正文、延长预算耗尽、到期时已经静止三种场景；客户端终止语义与真实完成和截断相区分。

## 阶段 2：浏览器并发与任务生命周期

- [x] **T2.1** 让浏览器驱动启动真正幂等（来源 4.1）
  - 文件：`claude_web/driver.py`、`tests/test_browser.py`。
  - 做法：共享 `asyncio.Lock` 保护初始化全过程，正确处理启动中异常、取消和重复调用。
  - 验收：不同桶并发首次访问时 Playwright / 浏览器只初始化一次；启动失败后行为可恢复；已有浏览器不被重复启动。

- [x] **T2.2** 为页面获取、创建和淘汰提供并发保护（来源 4.1）
  - 文件：`claude_web/page_pool.py`、`tests/test_browser.py`。
  - 做法：按桶合并并发创建，保护容量检查与淘汰操作；页面池满且所有候选页面均忙碌时返回明确 busy，不得超过容量或关闭忙碌页面。readiness 不应与生成请求争抢同一页面。
  - 验收：同桶并发 acquire 只创建一个页面；并发创建不突破 `MAX_PAGE_POOL`；满池时忙碌页面保持可用；readiness 繁忙行为可预测。

- [x] **T2.3** 接通 busy 标记及周期空闲回收（来源 4.1）
  - 文件：`claude_web/server.py`（lifespan）、`claude_web/service.py`、`claude_web/page_pool.py`。
  - 做法：在生成任务实际持有页面期间设置 busy，并在 `finally` 释放；应用 lifespan 启动周期 `reap_idle` 任务，关闭时取消并等待其结束。
  - 验收：成功、异常、超时、取消路径都会复位 busy；空闲页面超过 `BUCKET_IDLE_TTL_S` 后回收；生成中的页面绝不会被 LRU / TTL 淘汰。

- [ ] **T2.4** 统一会话锁与页面池容量语义（来源 4.2）
  - 文件：`claude_web/session/lock.py`、`claude_web/page_pool.py`、`claude_web/config.py`、相关测试。
  - 做法：分别定义桶锁、状态缓存与页面池的容量、淘汰候选、繁忙判定和满载错误；限制客户端控制的 `X-Claude-Session` 桶名造成的资源增长。
  - 验收：不可淘汰时不会静默突破限制；请求收到稳定且可诊断的 busy / 容量错误；活跃桶数、页面数和回收行为可测试。

- [x] **T2.5** 保留任务取消语义（来源 4.3）
  - 文件：`claude_web/errors.py`、`claude_web/completion/generator.py`、`claude_web/service.py`、`tests/test_generation_latency.py`。
  - 做法：生成器 `_fill` 不再用捕获 `BaseException` 的方式吞并取消，只捕获正常可恢复异常；定义明确的取消分类，避免把取消记录成普通超时、上游失败或会话 `last_error`。
  - 验收：协程取消可以正常传播；桶锁和 busy 标记在 `finally` 中释放；任务状态、错误字段和指标不会把取消误记成普通故障。

- [ ] **T2.6** 处理非流式与 Responses 客户端断连（来源 4.3）
  - 文件：`claude_web/server.py`、`claude_web/service.py`、相关 API 测试。
  - 做法：统一请求取消机制；对需要在后台运行的非流式任务及 Responses 请求检测断连并传播取消，避免客户端离开后浏览器仍长期生成。
  - 验收：模拟断连后任务能够在约定的检测周期内退出并释放桶锁；正常完成请求不被误取消；取消和故障的统计相互独立。

- [ ] **T2.7** 下一次提交前检查页面生成状态（来源 4.3）
  - 文件：`claude_web/completion/generator.py`、`claude_web/browser/dom_adapter.py`、相关生成测试。
  - 做法：提交前若页面仍在生成，先等待短时间；仍未结束时仅使用已确认的停止控件并再次确认页面可提交，不可盲目点击或重复提交。
  - 验收：上一轮取消后立即开始下一轮不会因残留生成状态误报提交失败；未知页面状态采用保守失败路径。

- [ ] **T2.8** 为提交后失败引入不确定状态（来源 4.3、4.4）
  - 依赖：T1.4a；与 T1.4b 协同。
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/service.py`、任务状态相关测试。
  - 做法：区分准备、已提交、生成中、完成、失败、取消和不确定等阶段；提交可能成功后失败时不得按提交前错误方式自动重试。恢复前先检查网页与指纹，无法判定时显式返回不确定错误。
  - 验收：提交后客户端断连或读取失败，再次请求时不会将相同增量盲目发入网页两次；提交前可恢复的写入错误仍可按白名单重试。

## 阶段 3：错误处理、API 协议与内容正确性

- [x] **T3.1** 区分内部错误与浏览器未就绪（来源 4.7）
  - 文件：`claude_web/errors.py`、`claude_web/server.py`、相关错误响应测试。
  - 做法：新增内部错误类型并映射到 HTTP 500；`_unhandled_handler` 和流式兜底不可将任意程序错误包装为 `NotReadyError`。对外隐藏堆栈和敏感页面内容。
  - 验收：内部错误、浏览器未就绪和客户端错误映射不同；流式开始后的错误按已定义的流内错误协议处理。

- [ ] **T3.2** 收窄重试边界（来源 4.7）
  - 文件：`claude_web/completion/generator.py`、`claude_web/browser/dom_adapter.py`、相关测试。
  - 做法：避免 `fill_composer` 与上层重复叠加重试；明确可恢复写入失败白名单。`ComposerNotFound`、登录状态失效等错误和提交后不确定状态不应无差别重试。
  - 验收：测试断言总重试次数和分类；不可恢复错误快速返回；不会因多层重试重复提交提示词。

- [ ] **T3.3** 收窄额度错误归因（来源 4.7）
  - 文件：`claude_web/completion/generator.py`、`tests/test_generation_latency.py`。
  - 做法：只在当前额度弹窗仍存在或有足够证据确认本轮没有产生新回复时归因为 `UsageLimitError`；已有有效正文之后的停滞仍报告停滞。
  - 验收：旧弹窗不会污染后续请求的错误分类；额度错误、停滞与回复未找到场景分别有回归测试。

- [ ] **T3.4** 完善失败类别及延迟指标（来源 4.7、5.6、6.1）
  - 文件：`claude_web/metrics.py`、`claude_web/service.py`、`claude_web/completion/generator.py`、相关测试。
  - 做法：让 `usage_limit_reached`、`prompt_submission_failed`、`reply_not_found`、`context_limit_reached`、取消及浏览器不可用等指标对应真实终态分支；补齐请求时延与回复提取时延。明确单个请求只计数一次，标签保持低基数。
  - 验收：每个指标有生产调用方和失败分支测试；错误分类互斥或遵循清晰的计数规则；不记录正文或任意桶名作为无限增长的标签。

- [ ] **T3.5** 实现 Responses API 真正的增量流（来源 4.6）
  - 文件：`claude_web/server.py`、`claude_web/api/responses_adapter.py`、`tests/test_responses.py`。
  - 做法：复用 Chat 流式路径的队列、保活、断连取消和错误传递机制；统一使用 `STREAM_KEEPALIVE_S`。若某项行为尚未实现，应继续关闭该功能或明确拒绝，而不是宣称完整兼容。
  - 验收：长生成期间客户端可持续收到事件；无输出时按配置保活；断连能取消任务；开始发送 SSE 后的错误有明确事件与结束行为。

- [ ] **T3.6** 保留 Responses 线程中的工具调用关联（来源 4.6）
  - 文件：`claude_web/api/responses_adapter.py`、线程 / 响应状态存储及 `tests/test_responses.py`。
  - 做法：`store_thread` 保留 assistant `tool_calls`、工具调用 ID、`tool_call_id` 和工具返回之间的关联，不只存 role/content。
  - 验收：使用 `previous_response_id` 续接包含多轮工具调用的对话时，调用与工具结果不会丢失或错配。

- [ ] **T3.7** 补齐 Responses 事件契约并做真实客户端验证（来源 4.6）
  - 依赖：T3.5、T3.6。
  - 文件：`claude_web/api/responses_adapter.py`、`tests/test_responses.py`、`doc/tasks_chatgpt.md` 或独立 E2E 记录。
  - 做法：按声明支持的范围检查 `response.created`、输出项目 added/done、正文 delta/done、工具参数事件、完成及错误事件的顺序与字段；用目标真实客户端验证工具调用往返和长响应。
  - 验收：记录客户端、版本、测试场景、运行日期和结果；文档支持范围与实际验证相符。离线测试通过不得标注为真实客户端验证通过。

- [ ] **T3.8** 修正 SSE 增量与重写语义（来源 6.1）
  - 文件：`claude_web/completion/generator.py`、`claude_web/server.py`、`claude_web/api/chat_adapter.py`、`claude_web/api/streaming_adapter.py`、相关测试。
  - 做法：正文归一化后确实变化才触发 `on_delta`；区分追加、重写、短暂节点消失与提取失败，短暂读取失败时保留上一次有效快照。记录 `stream_rewritten_total`，并为客户端无法撤回的重写提供 notes。实现或明确拒绝 `stream_options.include_usage`。
  - 验收：覆盖空增量、重复快照、重写、短暂 DOM 抖动、中文/emoji、多字节字符、换行、断连、流内异常和 include_usage。

- [ ] **T3.9** 收紧回复外壳清理（来源 6.2）
  - 文件：`claude_web/completion/extractor.py`、`claude_web/browser/dom_adapter.py`、`tests/test_extraction_chrome.py`。
  - 做法：优先根据实际 DOM 外壳节点移除辅助标题、动作栏与时间戳；只有在确认对应外壳存在时才启用文本兜底。避免基于前缀或末行格式猜测性截断。
  - 验收：覆盖公式、表格、代码块、以 `Claude said:` 开头的引用、时间样式结尾、重复段落及真实辅助节点；不确定时宁可保留文本。

- [ ] **T3.10** 完善工具调用提取和 schema 校验（来源 6.3）
  - 文件：`claude_web/toolcalls.py`、`claude_web/prompting.py`、`claude_web/models.py`、`tests/test_toolcalls.py`。
  - 做法：分离调用提取、参数解析、轻量 schema 校验和 OpenAI 模型转换；覆盖 DSML、JSON 围栏及已观察到的 `tool_uses` 形态。验证 `required`、参数类型及异常输入；无效调用仅提供受控诊断，不执行任意命令。
  - 验收：嵌套代码块、转义字符、嵌套对象、重复调用、混杂正文、半截 JSON、缺失 required 和类型不匹配均有测试；未知格式不伪装成成功解析。

- [ ] **T3.11** 落实 `tool_choice` 与提示词预算契约（来源 6.4）
  - 文件：`claude_web/prompting.py`、`claude_web/api/chat_adapter.py`、`claude_web/models.py`、`claude_web/config.py`、相关测试。
  - 做法：至少让 `tool_choice=none` 禁止工具提示；`required` 和指定函数在提示中得到明确约束，无法支持的行为要明确拒绝。记录已注入工具清单摘要；只在新会话、工具清单变化或明确定义的刷新策略下重发完整说明。系统提示截断写入 notes，`SEED_ENV_NOTE` 默认留空。
  - 验收：三类 tool_choice 有正反向测试；提示截断可见；增量轮次不会无条件重复注入大段工具说明；关键规则不被静默丢弃。

## 阶段 4：会话持久化、配置、日志与资源可观测性

- [ ] **T4.1** 使用结构化 URL 比较会话绑定（来源 5.1）
  - 文件：`claude_web/completion/lifecycle.py`、`claude_web/session/store.py`、`tests/test_conversation_reuse.py`。
  - 做法：解析 URL 并校验协议、主机、路径及完整 `/chat/<ID>`，统一实现 `_same_conversation` 和 link 落点校验；处理绑定会话达到上下文上限的情形，不能反复导航到同一个已满会话。
  - 验收：相似前缀但不同 ID 的 URL 不会被判成同一会话；非法主机、路径及边界 URL 均有测试；触顶结果明确。

- [ ] **T4.2** 解决会话状态读改写竞态（来源 5.2）
  - 文件：`claude_web/session/store.py`、`claude_web/session/lock.py`、`tests/test_sessions.py`。
  - 做法：由内存文档及统一 `update(**fields)` 承担状态修改；对整个读改写事务提供正确同步，使用唯一临时文件与原子替换。同步文件 IO 在合适的异步调用路径使用 `asyncio.to_thread`。避免返回可静默修改的共享可变状态。
  - 验收：并发更新不同桶不会相互覆盖；损坏文件、迁移、写盘失败和临时文件竞态有测试；持久化失败不会被报告为成功；明确进程内锁与多 worker 的支持边界。

- [ ] **T4.3** 控制任务快照的敏感内容持久化（来源 5.3）
  - 文件：`claude_web/tasks.py`、`claude_web/config.py`、`README.md`、`.gitignore`、相关测试。
  - 做法：提供默认不落完整回复正文的配置选项，涵盖 `last_reply` 和可能含原文的 `goal`；如需保存正文，必须显式启用并说明目录、隐私影响、保留期限和清理方式。检查备份、临时文件、日志和浏览器 profile 的忽略规则。
  - 验收：关闭正文持久化时文件不包含完整回复；开关行为和默认值有测试及文档；诊断日志不泄露提示词、回复或登录资料。

- [ ] **T4.4** 统一路径和环境列表解析（来源 5.4）
  - 文件：`claude_web/config.py`、`.env.example`、`tests/test_config.py`、`tests/test_config_drift.py`。
  - 做法：`SESSION_FILE`、`USER_DATA_DIR`、`TASK_FILE_DIR`、`OUTPUT_DIR` 等相对路径统一以 `PROJECT_ROOT` 为基准，显式绝对路径保持有效；选择器类列表只使用 `||` 分隔，避免将 `:is(a,b)` 等合法逗号选择器拆开；`LINK_ALLOWED_HOSTS=*` 启动时强告警。
  - 验收：改变启动工作目录不会意外切换状态/profile；逗号选择器原样保留；危险白名单告警明确；示例配置与配置漂移测试一致。

- [ ] **T4.5** 增加启动期配置校验与有效配置摘要（来源 5.4）
  - 文件：`claude_web/config.py`、`claude_web/server.py`、`tests/test_config.py`、`.env.example`。
  - 做法：检测旧键与已弃用设置并发出告警；校验时间关系、容量、端口、重试次数等约束；记录不含密钥的有效配置摘要。
  - 验收：非法配置启动时给出可操作错误或明确警告；旧配置不会静默改变行为；摘要不包含浏览器凭据、用户正文或其他敏感配置值。

- [ ] **T4.6** 完整绑定请求日志上下文并脱敏（来源 5.5）
  - 文件：`claude_web/logging_setup.py`、`claude_web/server.py`、`claude_web/bridge_commands.py`、相关测试。
  - 做法：保证 SSE 迭代和后台生成阶段保留 `request_id` / `session_key`；`X-Request-Id` 限制白名单字符和 64 字符长度，非法值重新生成。管理命令日志统一调用脱敏函数，不记录完整用户命令、敏感 URL 或无必要堆栈。
  - 验收：正常完成、超时、断连和异常日志可通过请求 ID 关联；非法 request ID 不会污染日志；日志脱敏测试覆盖命令和链接。

- [ ] **T4.7** 清点指标并接入真实调用点（来源 5.6、第 7 节）
  - 文件：`claude_web/metrics.py`、`claude_web/service.py`、`claude_web/completion/generator.py`、`claude_web/browser/*`、测试与 `doc/design.md`。
  - 做法：逐一核对 retry、browser unavailable、selector miss、reply extraction failure、session recovery、tool parse failure、request latency、reply extraction latency 等指标的生产调用方。区分未接线指标与真正的零值；无法给出可靠语义的指标应删除并同步文档。
  - 验收：每个保留指标有更新位置和测试；一次故障按定义计数，不重复累计；不暴露正文或产生无限基数标签。

## 阶段 5：消除空壳与工程化改进

- [ ] **T5.1** 清点未使用的配置、字段和辅助函数（来源 7.1）
  - 文件：`claude_web/`、`tests/`、`doc/design.md`、`.env.example`。
  - 做法：全仓库检索生产、测试和文档调用方；重点核对 `STREAM_KEEPALIVE_S`、`TASK_KEEP_MESSAGES`、`SessionState.cap_failures`、`TaskSnapshot.messages / recoveries`、queued / generating / recovering 状态、模型支持与页面判活函数、`TaskStore.load / clear`、`session_store.py` 和两个入口文件。每项二选一：接线并测试，或删除并同步文档。
  - 验收：没有只声明不生效、却被文档描述为有效保护的空壳；兼容入口移除前确认无实际调用方。

- [ ] **T5.2** 增加最小自动化 CI（来源 7.2）
  - 文件：`.github/workflows/*`、`pyproject.toml`、项目测试配置。
  - 做法：在受支持 Python 环境安装依赖并执行 `ruff check .` 和离线 `pytest -q`；真实浏览器 E2E 不默认在 CI 中运行，另设手动触发任务并避免操作开发者的真实登录 profile。
  - 验收：干净环境能够重复安装并通过质量门禁；CI 日志可区分单元测试和 E2E；不得将真实浏览器用例混入默认离线测试。

- [ ] **T5.3** 统一依赖真源与可复现安装（来源 7.3）
  - 文件：`pyproject.toml`、`requirements.txt`、锁文件及安装文档。
  - 做法：明确依赖真源，去除重复和冲突声明，为依赖设置合理版本约束并提供锁定/可复现安装流程。放宽 Python 最低版本前先核对依赖支持和目标环境，不凭源码表面判断决定。
  - 验收：在干净环境安装、导入和基础启动测试通过；依赖声明与文档一致；Python 与浏览器相关版本有可追溯记录。

- [ ] **T5.4** 增强类型检查与 Ruff 规则（来源 7.2）
  - 文件：`pyproject.toml`、`claude_web/errors.py`、`claude_web/session/*`、`claude_web/models.py`、`claude_web/end_detection.py` 及相关模块。
  - 做法：先对错误类型、会话模块、模型和纯状态机函数建立逐步可执行的 mypy 配置；逐步增加 Bugbear 等 Ruff 规则，先修复现有问题再扩大范围。
  - 验收：新增检查有稳定基线和明确范围；注释中的工具配置与实际配置一致；不通过全局忽略掩盖错误。

- [ ] **T5.5** 整理文档分工并记录真实环境验证（来源 7.4、第 10 节）
  - 文件：`README.md`、`doc/design.md`、`doc/tasks.md`、`doc/tasks_chatgpt.md`、`doc/update_chatgpt.md`。
  - 做法：README 保留安装、配置、安全限制、客户端接入与排障；设计文档描述模块边界、状态机和协议约定；审查文档记录证据、优先级和未验证假设；任务清单维护执行状态。补充常见错误的症状、诊断与处理说明。
  - 验收：完成项与实际实现一致；事故记录区分症状、日志证据、根因、提交和回归测试；E2E 记录包含实际日期、目标浏览器/客户端、相关版本和结果，不把计划执行写成已验证。

## 阶段 6：真实环境验证与最终验收

- [ ] **T6.1** 验证重复提示词和虚拟化后的回复识别（来源 2.1、第 9、10 节）
  - 文件：`tests/e2e/`、`doc/tasks_chatgpt.md` 或独立 E2E 记录。
  - 做法：使用真实 Claude 页面测试相同提示词连续提交、消息节点数量不变或下降、旧行被虚拟化回收等情形；优先检查是否存在稳定 DOM 消息 ID 或节点身份。
  - 验收：记录实际环境与结果；不确定时采用保守退化策略；真实探测前后均不破坏用户登录 profile。

- [ ] **T6.2** 验证取消、并发、硬超时和资源回收（来源 10）
  - 文件：`tests/e2e/`、`doc/tasks_chatgpt.md` 或独立 E2E 记录。
  - 做法：实际探测多桶并发、长任务、断连、硬超时、额度上限和浏览器异常退出，检查下一轮提交、锁释放和页面回收。
  - 验收：每个场景记录日期、浏览器/profile 与结果；离线测试通过不代表真实生命周期问题已解决。

- [ ] **T6.3** 验证 Responses 流和工具调用往返（来源 4.6、第 10 节）
  - 依赖：T3.5、T3.6、T3.7。
  - 文件：真实客户端探测脚本或手动步骤、`doc/tasks_chatgpt.md` 或独立 E2E 记录。
  - 做法：用目标真实客户端检查长响应、保活、工具调用往返、错误事件与 `previous_response_id` 续接。
  - 验收：按实际测试结果限定支持范围；未验证事件不得标成完全兼容。

## 推荐实施顺序与依赖

1. **先做 P0：** T1.1、T1.2、T1.3、T1.4a、T1.6。
2. **随后处理会话一致性与资源：** T1.4b、T2.1–T2.8、T4.1、T4.2。
3. **再补协议和正文边界：** T3.1–T3.11；Responses 相关任务按依赖顺序执行。
4. **补可靠性与运维：** T3.4、T4.3–T4.7、T5.1–T5.5。
5. **最后进行真实环境验证：** T6.1–T6.3；测试结果用于收窄或确认对外宣称的支持范围。

## 完成标准

- [ ] 管理操作遵循统一桶锁，不会中断在途任务。
- [ ] 历史一致性、提交后不确定状态与取消语义明确，不会盲目重复提交。
- [ ] 浏览器只初始化一次，页面池遵守容量限制，忙碌页面不会被回收，空闲回收确实生效。
- [ ] 超时、截断、SSE 重写、Responses 事件和工具调用状态符合声明的协议契约。
- [ ] 状态并发写入不会丢失更新；任务快照正文持久化可控；日志和诊断不会泄漏敏感内容。
- [ ] 所有保留的配置、状态字段和指标都有实际用途；无用项已经删除并同步文档。
- [ ] `git diff --check`、定向测试、全量 `pytest -q` 和 `ruff check .` 通过，或已有基线问题被单独、明确记录。
- [ ] 真实浏览器与真实客户端验证有可追溯记录；未执行的 E2E 不标注为通过。
