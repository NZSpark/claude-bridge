# ClaudeBridge 改进建议（复评版）

> 2026-10-13，基于 HEAD `cbf5af5` 加工作区未提交改动（`git status` 33 项）。取代 2026-10-12 的通读版（`95d09bc` 时点）。
> 范围：鉴权相关条目（原 3.3 端点鉴权、3.4 的 `RESET_TOKEN` 部分、鉴权矩阵测试）仍划出范围，不做，只在第 3 节列出现状。
> 方法：本次对照 `doc/tasks_claude.md`、`doc/updates_deepseek.md`、提交历史（`45cdf4b` 之后 31 个提交）与关键符号的 grep 复核；`pytest -q --ignore=tests/e2e` 与 `ruff check` 本次实跑全绿。没有重新通读全部模块，没把握的写在第 6 节。

## 1. 现状概览

- 上一版评审的 P0 与大部分 P1 已落地，每项单独提交并带回归测试；`tasks_claude.md` 的勾选与代码基本一致。
- 新增工程化：`.github/workflows/ci.yml`（ruff + mypy + pytest）、`[tool.mypy]`（覆盖 `errors`、`session/*`、`models`、`end_detection`）。
- 剩余风险集中在三类：(1) 复用网页会话的一致性只做了观测，没有切换行为；(2) 日志与落盘的细节（请求 ID、脱敏、明文快照）；(3) 写了没用上的指标与状态字段。
- 工作区有较多未提交改动（`token_speed`、`doc/tokenspeed.md`、`skills/`、`.github/`、`bridge_commands`、`dom_adapter` 等），应按主题拆分提交。

## 2. 已解决（经提交与代码核对）

- 3.1 工具说明判重只看 system/user 行首（`1fd4875`）。
- 3.2 绝对超时优先延长，硬上限才截断并标 `length`（`26c146d`）。
- 3.4 管理操作取桶锁；`reset` 保留 `linked_url`（`b75059a`）。
- 3.5 步骤 A：已同步历史指纹，只观测（`3908a3c`）。
- 4.1 驱动启动与页面池加锁；4.2 空闲回收与 `mark_busy` 接线（`3861424`、`ba8ecf1`）。
- 4.3 取消语义（`GenerationCancelledError`）、非流式与 Responses 断连检测、提交前处理页面仍在生成（`da78a10`、`cf5c86f`、`fbc9a7e`）。
- 4.4 Responses 真流式、`store_thread` 保留 `tool_calls` / `tool_call_id`（`d3e3a08`、`9cc58bf`）。
- 4.5 中的 `tool_choice` 生效（`c07205e`）；`STREAM_KEEPALIVE_S` 接线。
- 4.6 `InternalError`(500)、去掉叠加重试、额度归因收窄、失败类别指标（`da78a10`、`3442f24`、`f8758a7`、`3743656`）。
- 4.7 会话 URL 精确比较（`2275ddb`）。
- 4.8 路径以 `PROJECT_ROOT` 解析、列表只按 `||` 切分、`LINK_ALLOWED_HOSTS=*` 告警、启动校验与摘要日志（`50d672c`、`570a930`）。
- 4.10 中的 `on_delta` 只在变化时回调、`stream_rewritten_total`、`stream_options.include_usage`（`c07205e`）。
- 第 5 节中的死配置 `TASK_KEEP_MESSAGES` 已接线，若干死函数已接线或删除（`cbf5af5`、D6.3）。
- 第 6 节中的最小 CI 与 mypy。

## 3. 仍然成立的问题

### 3.1 P0

- **复用会话的历史指纹还没有切换行为（原 3.5 步骤 B，T1.4b）**
  现在对不上只打日志，换任务、客户端回滚、压缩历史时，增量仍会发进旧网页会话。先看日志确认没有误判，再切换为「对不上则换新对话并完整重播」。相同提示词连续两轮加虚拟化时纯文本指纹分不开，优先找稳定的 DOM 消息 ID，找不到就宁可重播。README 应说明多任务用 `X-Claude-Session` 区分桶。
- **提交之后失败的桶没有标记「状态不确定」（T2.6）**
  网页会话里已有那条提示词，客户端重试同一增量会重复。依赖上一条。
- **端点鉴权（原 3.3，不在本次范围）**
  `/diagnostics`、`/session/{bucket}`、`/metrics`、`/readiness` 仍未鉴权，`/readiness` 与 `/diagnostics` 匿名可拉起浏览器。仅记录现状，不排任务。

### 3.2 P1

- **提示词体量（4.5 / T4.5）**
  增量模式下每轮仍重新注入完整工具说明，未见「已发工具清单摘要」之类的状态；`SYSTEM_MAX_CHARS` 默认仍是 8000，超长系统提示会被静默截断；`SEED_ENV_NOTE` 默认文案仍写死编码场景。
- **绑定桶触顶（4.7 / T3.7 遗留）**
  绑定的会话已满后，下一轮仍会 goto 同一个会话重复失败；未绑定桶新建会话的 URL 不保存，重启后只能整体重播。
- **日志（4.9 / T5.1）**
  - `X-Request-Id` 未清洗就写进日志与响应头（`server.py:206`）。
  - `bridge_commands.py:304` 仍用 `exc_info=True`，且日志直接打印会话 URL 与桶键，没走 `redact`。
  - 流式请求里 `request_id` / `session_key` 是否在生成阶段丢失，仍是推断，未验证。
- **回复外壳剔除（4.11 / T5.2）**
  文本兜底的前缀与末行时间戳边界没有补用例。
- **工具调用解析（4.11 / T5.3）**
  `strip_tool_call_blocks` 对 DSML、json 围栏、`tool_uses` 形态的残留，以及 `required` / 类型校验，均未做；`toolcalls.py` 仍有 714 行未拆层。
- **状态与落盘（4.12 / T5.4）**
  每次保存仍整体读写并同步 IO；`TaskSnapshot` 仍把 `last_reply` 与 `goal` 明文落盘，没有「不落正文」开关。
- **Responses 事件集（4.4 / T4.3）**
  缺 `output_item.added|done`、`output_text.done` 等，没有用真实客户端（如 Codex CLI）探测过。
- **读取已见回复时节点短暂消失（4.10 遗留）**
  `read_new` 抛 `ReplyExtractionError` 会让整个请求失败，属推断，需先复现再改。

### 3.3 写了没用上（T6.1 / T6.4）

- 指标除 `metrics.py` 外无源码引用：`request_retry_total`、`browser_unavailable_total`、`browser_selector_miss_total`、`reply_extraction_failure_total`、`session_recovery_total`、`tool_parse_failure_total`、`request_latency`、`reply_extraction_latency`。要么接上，要么删掉并同步 `design.md`。
- `SessionState.cap_failures` 仍只写不读（`lifecycle.py:296,324` 写，没有读取方）。
- 结构性冗余：`session_store.py` 是纯委托门面；`main.py` 与 `claude_api_server.py` 并存且都启动服务；`/bridge modal` 注册为「改状态」。
- `BUCKET_LOCK_TIMEOUT_S`（120s）小于 `RESPONSE_TIMEOUT_S`（300s）这条冲突，启动时只告警，默认值没改，是否调整待评估。

### 3.4 P2 工程化（T7.x）

- `requirements.txt` 无版本约束且与 `pyproject.toml` 重复，没有锁文件；`requires-python >=3.13` 偏严。
- ruff 仍只选 `E4/E7/E9/F/I`。
- 根目录仍有含登录态的 `user_data_chrome.bak-*`、`pre-refresh-*`；`.gitignore` 里 `.freebuff/` 仍重复（第 2 行与第 34 行）。
- 测试补强：`generate` 端到端时序、复用会话回归、同桶串行 / 跨桶并行、真实 e2e 的两条用例。
- 指标扩充与 Prometheus 文本格式；文档拆分（结束判定与回复识别从 `design.md` 拆出）、README「现象 -> 日志关键字 -> 处理」对照表、写明不支持多 worker。

## 4. 提交与文档卫生

- 工作区 33 项未提交，涉及功能（`token_speed`、`bridge_commands` 新增、`dom_adapter`、`generator`）与文档混在一起，建议按主题拆提交，并对照 `doc/tokenspeed.md` 与 `doc/bridge_commands.md` 确认文档与代码一致。
- `doc/tasks_claude.md` 的勾选应随提交同步；`doc/updates_deepseek.md` 附录已记录若干踩坑，可作为回归测试设计的参考。
- 旧版评审里「本次会话里确实不再带这段说明」那类未验证的断言已不再保留。

## 5. 建议实施顺序

1. 先把工作区改动按主题提交，保证 HEAD 与测试基线一致。
2. T1.4b：看指纹日志，再切换行为；随后做 T2.6。
3. T4.5：工具说明按清单摘要重发、调大 `SYSTEM_MAX_CHARS`、`SEED_ENV_NOTE` 默认留空。
4. T5.1：请求 ID 清洗、日志脱敏；T3.7 遗留（绑定桶触顶、保存未绑定会话 URL）。
5. T5.2 / T5.3 / T5.4：解析边界、工具调用拆层、落盘不落正文。
6. T6.1 / T6.4：指标与结构性冗余，接上或删除。
7. T4.3：真实客户端探测，结果记入 `doc/tasks.md`。
8. 阶段 7：依赖锁定、ruff 规则、目录整洁、测试与文档补强。

## 6. 未覆盖或不确定

- 本次没有重新通读各模块，第 3 节结论依据 grep、提交记录与台账；个别「仍未做」的判断可能漏掉未提交工作区里的实现。
- 日志上下文在流式请求中丢失，是依据 contextvars 与 Starlette 执行顺序的推断，未在真实请求上验证。
- Responses 事件集是否满足 Codex CLI，需要真实客户端验证。
- `GENERATING_STATE_JS` 的实现细节与 T2.5 的页面判定依赖关系未重读。
- `tests/e2e` 与真实浏览器行为没有在本次运行。
