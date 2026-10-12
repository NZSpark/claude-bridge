# ClaudeBridge

把 **Claude 网页版（claude.ai）** 桥接成本地 **OpenAI 兼容 API**，让 Pi Coding Agent、
Codex CLI、OpenAI SDK 等兼容客户端通过 `http://127.0.0.1:8003` 使用你已经登录的
Claude 网页会话，**无需 API Key**。

> ⚠️ 本项目不是官方 Anthropic API，也不绕过任何登录验证 / CAPTCHA / 站点安全挑战。
> 它通过 Playwright 驱动一个**你已手动登录**的持久化浏览器页面来提交与读取回复。
> 默认只监听本机回环地址。

设计文档见 [doc/design.md](doc/design.md)，实施记录见 [doc/tasks.md](doc/tasks.md)，
E2E 方案见 [doc/e2e_test_design.md](doc/e2e_test_design.md)。

## 它能做什么

| 能力 | 说明 |
| --- | --- |
| `GET /v1/models` | 返回**真实配置的**逻辑模型别名（默认只有 `claude-web`） |
| `POST /v1/chat/completions` | 非流式与 SSE 流式（合成流：由页面回复快照增量编码） |
| 多轮历史 | 同一网页会话里续写：首轮「新对话 + 完整历史播种」，之后**只发增量** |
| 会话隔离 | 按请求头 / `user` / UA 分桶，同桶串行、跨桶隔页面 |
| 工具调用模拟 | 用提示词约定 + 结构化围栏解析出合法 `tool_calls` |
| `GET /healthz` `GET /readiness` | 区分「进程活着」与「浏览器+登录+输入区就绪」 |
| `GET /metrics` `GET /diagnostics` | 计数、耗时、错误类别、选择器命中；**不返回正文** |
| `POST /session/reset` `/session/link` `/session/unlink` | 会话管理 |
| `/bridge ...` 聊天命令 | 在对话里查状态 / 绑定会话；由桥**本地应答**，不发给网页版（见 [doc/bridge_commands.md](doc/bridge_commands.md)） |
| `POST /v1/responses` | Responses API 兼容子集，默认关闭（`ENABLE_RESPONSES_API`） |

**明确不支持**（不伪装成已实现）：图片 / 音频 / 文件附件、Artifacts、扩展思考、
Anthropic 原生工具协议、精确 token 计费、多租户与公网生产部署。

## 快速开始

```bash
# 1) 依赖（需要 Python >= 3.13）
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 2) 安装 Chromium（持久化 profile 用它保存登录态）
.venv/bin/playwright install chromium

# 3) 首次登录：有头模式启动一次，在弹出的浏览器里手动登录 claude.ai
cp .env.example .env            # 按需修改
.venv/bin/python claude_api_server.py
#   登录完成后 Ctrl-C 退出；登录态已保存在 ./.user_data

# 4) 正常启动
.venv/bin/python claude_api_server.py
```

无显示服务器（Linux 服务器 / CI）请用 `xvfb-run` 包一层**有头**浏览器，而不是
设 `HEADLESS=true`（无头模式可能命中站点挑战页，属未验证能力）：

```bash
xvfb-run -a .venv/bin/python claude_api_server.py
```

### 复用已有 Chrome 登录态（可选，免去重复登录）

首次访问 claude.ai 时站点可能先给出安全校验页。**换浏览器并不能解决**——自带
Chromium 与本机真实 Chrome 实测同样返回 403，真正起作用的是「已登录且已获得站点
放行 cookie 的 profile」。因此不要手动代过校验，而是在某个 profile 里**人工完成
一次**校验/登录；之后所有请求都复用该 profile。

两种做法：

```bash
# 做法 A（推荐）：在桥接自己的 profile 里人工完成一次校验并登录
BROWSER_CHANNEL=chrome .venv/bin/python -m tests.e2e.probe \
  "https://claude.ai/chat/<会话 id>" --wait-human 300
# 在弹出的窗口里手动通过校验 / 登录；ok:true 后 profile 已可用。

# 做法 B：复制你日常 Chrome 的 profile 给桥接用（想直接沿用已有登录态时）
# 1) 先完全退出 Chrome（含后台常驻进程）
# 2) 复制用户目录到桥接专用目录（不要直接指向实时目录）
SRC="$HOME/Library/Application Support/Google/Chrome"
cp -R "$SRC" ./user_data_chrome
# 3) 在 .env 里指向副本
#    USER_DATA_DIR=./user_data_chrome
#    BROWSER_CHANNEL=chrome
#    BROWSER_PROFILE_DIRECTORY=Default      # 或 "Profile 1" 等
```

注意事项：

- profile 副本**等价于账户凭据**：已被 `.gitignore` 忽略，请勿外传或提交。
- 建议用副本而非直接指向日常 profile：Chrome 136+ 会拒绝对默认用户目录开启远程调试，
  且自动化不应直接动到你正在使用的那份登录态。
- 本项目**不做** UA 伪装 / 反检测定制：伪装 Safari 之类的字符串只会产生更可疑的
  指纹（站点依据 TLS/HTTP2 指纹与 JS 校验判定），且属于设计文档不允许的绕过手段。

### 固定到指定会话（缺省初始链接）

如果希望**默认会话桶**固定在一条**已存在**的 Claude 会话里（例如你手动开好、
带背景资料的会话）继续，而不是让桥自己新建：

```bash
# 1) 先只读探测链接是否真的可用（不提交任何提示词）
.venv/bin/python -m tests.e2e.probe "https://claude.ai/chat/<会话 id>"
#    ok:true 且 composer:true 才算可用。
#    challenged:true 表示命中了站点安全校验（如 "Performing security verification"）：
#    请在**有头窗口里人工完成验证 / 登录**，本项目不自动勾选、不绕过任何验证：
BROWSER_CHANNEL=chrome .venv/bin/python -m tests.e2e.probe "<会话 id 链接>" --wait-human 300

# 2) 探测通过后再写进 .env
CLAUDE_DEFAULT_LINK_URL=https://claude.ai/chat/<会话 id>
```

生效范围为**默认会话桶**：其他桶（带 `X-Claude-Session` / `user` 的请求）各自
在自己的网页会话里续写（首次新开对话并播种，之后只发增量），避免多个桶共用同一条
网页会话而互相串线。要让某个非默认桶也固定在一条
会话上，用 `POST /session/link` 单独绑定。非法值（非 HTTPS / 非允许主机 / 非会话
路径）会被忽略并回落到桥自建会话。

### 客户端接入

```bash
curl http://127.0.0.1:8003/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-web","messages":[{"role":"user","content":"用一句话介绍你自己"}]}'
```

OpenAI SDK / Pi / Codex 只需把 `base_url` 指向 `http://127.0.0.1:8003/v1`，
`api_key` 随便填（未启用 `BRIDGE_TOKEN` 时不校验）。

### 在对话里管理桥（`/bridge` 命令）

把命令**单独**作为一条 `user` 消息发过去，桥就自己回答，不会转发给 Claude 网页版
（所以页面失效、浏览器没起来时这些命令依然可用）：

```bash
curl http://127.0.0.1:8003/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"/bridge help"}]}'

# 把当前桶绑定到一条已有会话（也可以只发会话 ID）
curl http://127.0.0.1:8003/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'X-Claude-Session: pi-task-1' \
  -d '{"messages":[{"role":"user","content":"/bridge link https://claude.ai/chat/<会话 ID>"}]}'
```

可用命令：`help`、`status`、`models`、`session [status]`、`session reset`、
`session link <URL> [--seed]`、`session unlink`（`link` / `unlink` / `reset` 可省略
`session` 直接写）。只有「整条消息就是那一行」才算命令 —— 正文里提到 `/bridge` 照样
发给网页版。完整语义见 [doc/bridge_commands.md](doc/bridge_commands.md)。

## 配置

所有可调参数集中在 `.env`（模板见 [.env.example](.env.example)）。常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8003` | 监听地址；非回环会在启动时告警 |
| `CLAUDE_WEBSITE` | `https://claude.ai/new` | 目标入口，页面改版时可切换 |
| `CLAUDE_DEFAULT_LINK_URL` | 空 | 缺省初始会话链接（**仅默认桶**生效，见下） |
| `USER_DATA_DIR` | `./user_data` | Chromium 登录 profile（**勿提交到 Git**） |
| `HEADLESS` | `false` | 建议保持有头 |
| `CLAUDE_TIMEOUT` | `300` | 单轮绝对超时（秒） |
| `POLL_INTERVAL_S` / `POLL_INTERVAL_ACTIVE_S` | `1.5` / `0.4` | 页面静止 / 活跃时的轮询间隔（秒，只影响「多快看到变化」） |
| `SETTLE_S` / `SETTLE_NO_SIGNAL_S` | `2.5` / `6` | 页面自述已结束 / 探不到生成信号时，内容静默多久算完成（秒） |
| `SUBMIT_LOST_S` / `STALL_S` | `6` / `30` | 判定「提交未生效」/「停滞」所需的静止时长（秒） |
| `PRE_SUBMIT_WAIT_S` | `3` | 提交前若页面仍在生成，先等待的时长；仍未结束就点一次停止控件（0 = 不等） |
| `REPLY_CHROME_SELECTORS` | 见 `.env.example` | 回复节点里要剔除的页面外壳（无障碍标题 / 动作栏 / 时间戳 / 推广行） |
| `SESSION_KEY_HEADER` | `X-Claude-Session` | 会话分桶请求头 |
| `MAX_SESSION_BUCKETS` / `BUCKET_LOCK_TIMEOUT_S` | `4` / `120` | 桶上限 / 同桶等待超时 |
| `BUCKET_IDLE_TTL_S` / `REAP_INTERVAL_S` | `900` / `60` | 空闲页面回收时长 / 后台回收任务间隔（0 = 关闭） |
| `ADMIN_LOCK_TIMEOUT_S` | `10` | 管理操作（reset / link / unlink / modal）取桶锁的短超时；桶忙时快速返回忙碌错误，不打断在途任务 |
| `CLAUDE_MAX_CONTEXT_CHARS` | `100000` | 桥接层独立的字符预算 |
| `BRIDGE_TOKEN` / `RESET_TOKEN` | 空 | 可选的 Bearer / 重置令牌 |
| `ENABLE_RESPONSES_API` | `false` | 是否开放 `/v1/responses` |

`.env.example`、`claude_web/config.py` 与本文档构成配置契约，
`tests/test_config_drift.py` 会阻止三者漂移。

> 结束与停滞判定曾用「连续轮询次数」开关（`STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS`）。
> 这三项**已废弃并被忽略**，改用时长开关 `SETTLE_S` / `SETTLE_NO_SIGNAL_S` / `STALL_S`：
> 轮询次数阈值会把「等多久才算结束」和「多久轮询一次」绑在一起，调快轮询就同时缩短了
> 停滞保护。若你的 `.env` 里还有旧键，直接删掉即可（新键默认值与原行为等价，只有收尾
> 更快：`QUIET_POLLS=4 × 1.5s` → `SETTLE_S=2.5`）。

## 安全边界

- 默认只监听 `127.0.0.1`；改成 `0.0.0.0` 会让任何能访问该地址的人**复用你的
  Claude 登录态与额度**，启动会打醒目告警且不受支持。远程访问请用 SSH 端口转发 / VPN。
- 浏览器 profile、会话状态、任务快照、输出目录都在 `.gitignore` 中。
- `/diagnostics` 与 `/metrics` 只返回布尔 / 计数 / 类别，**不返回提示词与回复正文**。
- 会话绑定 URL 强制 HTTPS + 允许主机白名单 + 非会话路径拒绝，避免把持久化浏览器
  导航到任意站点。
- 工具调用只有通过参数校验且属于本次声明的工具集合才会返回；桥接层**不执行**模型
  生成的任何命令。

## 测试

```bash
.venv/bin/python -m pytest -q          # 默认套件：不联网、不起浏览器
.venv/bin/ruff check .                 # 静态检查
CLAUDE_E2E=1 .venv/bin/python -m pytest tests/e2e -q   # 真实浏览器 E2E（默认 skip）
.venv/bin/python tools/bench_pipeline.py   # 内容处理链路基准（离线，对比改动前后的等待秒数）
```

默认套件用 FakeDriver / FakePage 驱动真实的 DOM 适配器与生成状态机，覆盖协议转换、
会话分桶、SSE 编码、工具解析、错误映射与安全边界。真实网页测试必须显式开启，
且不会绕过站点安全机制。

## 已知限制

- Claude 网页 DOM 选择器是**候选**，需以真实页面探测结果为准；改版时用
  `/diagnostics` 的 `selector_version` 与选择器命中数定位。
- 流式是**合成流**（轮询页面回复快照做增量），不是上游 token 级流；若网页在流式中
  重写回复，桥接层无法撤回已发送内容，会停止继续发送而不是重复错误内容。
- 模型别名只有经真实探测确认映射的才会公开；默认只公布 `claude-web`。
- 额度用尽 / 升级弹窗的关闭依赖站点当前的弹窗结构（容器 `role="dialog"` + 关闭控件）；
  改版后若关不掉，日志里会有 `[弹窗] 额度/升级弹窗仍然在页面上`，把新控件加进
  `MODAL_CLOSE_SELECTORS` / `MODAL_CLOSE_TEXT_PATTERNS` 即可（无需改代码）。

## 故障排查

- **客户端一直等到超时**：先看日志里 `[生成]` 的收尾行（结束原因 + 轮询次数 + 正文
  字符数）。「提交未生效」会以 `prompt_submission_failed` 快速返回；页面空闲且正文无
  增长会以 `generation_stalled` 提前结束，都不会再空等到 `generation_timeout`。若仍出现
  绝对超时，说明页面确实一直报「生成中」（``data-is-streaming=true`` 或可见停止按钮）。
- **回复已经出来了，客户端却还多等几秒**：收尾等待由 `SETTLE_S`（默认 2.5s）决定——
  页面自述已结束、正文连续静默满这个时长才收尾。调小它见效最快，但太小时会在模型
  「分段输出之间的停顿」处提前收尾、把回复截断。判定阈值都是**时长**，因此调快
  `POLL_INTERVAL_ACTIVE_S` 不会削弱结束 / 停滞保护。
- **回复被截断 / 结果为空**：用 `GET /diagnostics` 看 `selector_version` 与选择器命中数，
  多数是站点改版导致回复节点选择器落空。
- **报 `reply_not_found`（「提交后未识别到本轮回复」）但网页上明明有回复**：日志里
  `[生成]` 的收尾行会带上现场（`assistant 条数/基线`、`提问 条数/基线`、`回复在提问之后`），
  错误详情里也有 `reply_count` / `reply_baseline` / `user_count` / `user_baseline` / `trailing_reply`。
  两把钥匙任一成立就认回复：**assistant 条数变多**，或**本轮提问已经落到页面上且其后
  已有 assistant 节点**（提问按文本指纹比较，与条数无关）。已知的两种真实误报：
  ① 页面在渲染本轮回复时回收 / 复用了旧行（条数停在基线上）；
  ② 长会话里页面只保留渲染窗口，提交后条数**比基线更少**（如 `assistant 6/基线 8，
  提问 5/基线 8`）。两种都由第二把钥匙捕获，日志里会多一行「靠结构信号认出来的」。
  若 `trailing_reply=false`（页面上压根没有排在本轮提问之后的回复节点），那确实是网页
  端没有渲染出回复：看 `GET /diagnostics` 的选择器命中数与 `selector_version`。
- **正文里混进页面文字**（同一段正文出现两遍、结尾多出 `just now` / `13 minutes ago`、
  或 `Claude responded:` / `Claude works directly with your codebase`）：回复节点里除了模型
  正文还有无障碍标题、动作栏、时间戳与推广行，默认由 `REPLY_CHROME_SELECTORS` 剔除；
  站点改版把新的外壳混进来时，把对应选择器加进这项配置即可（无需改代码）。
- **误报 `context_limit_reached`（「会话已达上下文长度上限」）而会话明明还能用**：判定
  是在整页文本里找 `CONTEXT_LIMIT_PATTERNS` 的特征文案，因此两件事必须同时成立才不会误报：
  扫描只在**即将判定失败**时做一次（不采用「每次请求扫一次」），且扫描前剔除会话正文
  （**包括用户消息**）与输入区。若你确实把这类文字（例如本仓库 `.env.example` 的那一行）
  发进了会话，旧版本会对每个请求误报——升级后正常；如果你想更严，把
  `CONTEXT_LIMIT_PATTERNS` 收窄成只属于站点提示的句子。
- **弹出了「Upgrade to keep chatting / You hit your 5-hour message limit」之类的额度弹窗**：
  这是网页账号的用量额度用尽。桥接会在每轮提交前自动把它关掉（只点关闭 / 先不用类控件，
  绝不点升级或付费按钮），所以它不会再把「提示词写得进、Enter 不生效」伪装成
  `prompt_submission_failed` / `generation_stalled`；现在会直接回 `usage_limit_reached`
  （HTTP 429）。想手动关一次：在对话里发 `/bridge modal`。真要用就等额度重置或升级。
- **`challenge_required`**：在普通浏览器里先完成一次人工验证；本项目**不**自动绕过验证。
  若页面其实可用（能打字），只会记为 `challenge_suspect` 诊断提示，不阻断请求。
