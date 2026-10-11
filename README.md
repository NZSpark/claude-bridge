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
| 多轮历史 | 每次任务「新对话 + 完整历史播种」，请求可重放、状态可恢复 |
| 会话隔离 | 按请求头 / `user` / UA 分桶，同桶串行、跨桶隔页面 |
| 工具调用模拟 | 用提示词约定 + 结构化围栏解析出合法 `tool_calls` |
| `GET /healthz` `GET /readiness` | 区分「进程活着」与「浏览器+登录+输入区就绪」 |
| `GET /metrics` `GET /diagnostics` | 计数、耗时、错误类别、选择器命中；**不返回正文** |
| `POST /session/reset` `/session/link` `/session/unlink` | 会话管理 |
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

如果希望**默认会话桶**始终在同一条 Claude 会话里继续，而不是每次新开对话：

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

生效范围为**默认会话桶**：其他桶（带 `X-Claude-Session` / `user` 的请求）仍各自
新开对话，避免多个桶共用同一条网页会话而互相串线。要让某个非默认桶也固定在一条
会话上，用 `POST /session/link` 单独绑定。非法值（非 HTTPS / 非允许主机 / 非会话
路径）会被忽略并回落到新开对话。

### 客户端接入

```bash
curl http://127.0.0.1:8003/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-web","messages":[{"role":"user","content":"用一句话介绍你自己"}]}'
```

OpenAI SDK / Pi / Codex 只需把 `base_url` 指向 `http://127.0.0.1:8003/v1`，
`api_key` 随便填（未启用 `BRIDGE_TOKEN` 时不校验）。

## 配置

所有可调参数集中在 `.env`（模板见 [.env.example](.env.example)）。常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `8003` | 监听地址；非回环会在启动时告警 |
| `CLAUDE_WEBSITE` | `https://claude.ai/new` | 目标入口，页面改版时可切换 |
| `CLAUDE_DEFAULT_LINK_URL` | 空 | 缺省初始会话链接（**仅默认桶**生效，见下） |
| `USER_DATA_DIR` | `./user_data` | Chromium 登录 profile（**勿提交到 Git**） |
| `HEADLESS` | `false` | 建议保持有头 |
| `CLAUDE_TIMEOUT` / `POLL_INTERVAL_S` | `300` / `1.5` | 单轮绝对超时 / 轮询间隔 |
| `STABLE_POLLS` / `QUIET_POLLS` / `STALL_POLLS` | `4` / `4` / `20` | 结束与停滞判定 |
| `SESSION_KEY_HEADER` | `X-Claude-Session` | 会话分桶请求头 |
| `MAX_SESSION_BUCKETS` / `BUCKET_LOCK_TIMEOUT_S` | `4` / `120` | 桶上限 / 同桶等待超时 |
| `CLAUDE_MAX_CONTEXT_CHARS` | `100000` | 桥接层独立的字符预算 |
| `BRIDGE_TOKEN` / `RESET_TOKEN` | 空 | 可选的 Bearer / 重置令牌 |
| `ENABLE_RESPONSES_API` | `false` | 是否开放 `/v1/responses` |

`.env.example`、`claude_web/config.py` 与本文档构成配置契约，
`tests/test_config_drift.py` 会阻止三者漂移。

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
