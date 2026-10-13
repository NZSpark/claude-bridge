# Bridge 聊天命令（`/bridge ...`）

在对话里直接查桥接状态、管理当前会话桶。命令由桥**本地应答**，不发给 Claude 网页版。

姊妹项目 `deepseek-bridge` 有同名机制，本文件只描述本项目**真正实现**的那部分。

## 命令

| 命令 | 作用 | 风险 |
| --- | --- | --- |
| `/bridge help [session]` | 命令列表 / 分主题帮助 | 只读 |
| `/bridge status` | 桥接、浏览器与当前桶的状态摘要 | 只读 |
| `/bridge token` | 最近一轮与本桶平均的 token 速度（prefill / decode） | 只读 |
| `/bridge models` | 本桥公布的模型名（与 `GET /v1/models` 同源） | 只读 |
| `/bridge modal` | 关闭挡路的额度 / 升级弹窗（只点关闭类控件） | 改页面 |
| `/bridge session [status]` | 当前桶的会话状态摘要 | 只读 |
| `/bridge session reset` | 下一轮开新网页会话并播种完整历史 | 改状态 |
| `/bridge session link <URL> [--seed]` | 把当前桶绑定到指定网页会话 | 改状态 |
| `/bridge session unlink` | 解除当前桶的会话绑定 | 改状态 |

- 裸 `/bridge` 等于 `/bridge help`；`/bridge popup` 是 `/bridge modal` 的别名；
  `/bridge speed` / `/bridge tokens` 是 `/bridge token` 的别名。
- `/bridge token` 的口径（token 数为估算、速度为「网页版真实链路下的有效速度」而非
  模型裸速度）见 [tokenspeed.md](tokenspeed.md)。
- `/bridge modal` 只点**关闭 / 先不用**类控件，不点升级或付费按钮，也不改会话状态；
  它回答「页面上到底有没有这个弹窗」，而生成链路每轮开始本来就会自己关一次。
- `link` / `unlink` / `reset` 可以省略 `session` 直接写（`/bridge link <URL>`）。
- `--seed` 表示「那条会话里没有此前记录」，下一轮把完整历史播种进去。
- 会话地址可只发 **会话 ID**（`/bridge link 5a39cd68-…`），桥会补成
  `https://<允许主机>/chat/<ID>` 再校验。

## 语义与边界

1. **精确匹配**：只有「整条消息就是那一行」才算命令。多行文本、正文里提到
   `/bridge`、以及其它 `/` 开头的文本，都照旧发给网页版。命名空间内语法匹配但参数
   不合法的写法（如 `/bridge frobnicate`）返回用法提示，不发给网页版。
2. **只看最后一条消息，且必须是 `user`**：工具循环的后续请求会带着同一批历史重发，
   若回溯历史里的 `user` 消息，旧命令会被反复执行。
3. **只作用于当前会话桶**：没有「指定别的桶」的参数；桶由 `X-Claude-Session` /
   `user` / UA 按普通请求的同一套规则决定（`SESSION_SCOPING=false` 时即默认桶）。
4. **本地应答**：命令不经过生成链路，因此浏览器未启动 / 页面失效 / 登录过期时
   依然可用（这正是排障最需要它们的时候）。`/bridge link` 例外：绑定要真的把页面
   导航过去，浏览器不可用时如实报「无法绑定」。
5. **不泄露**：回复只含状态摘要，不含配置值、Cookie、令牌、正文或其它桶的信息。
   错误只回一行（细节进日志）。
6. **两条协议一致**：`/v1/chat/completions` 与 `/v1/responses` 共用同一套解析与执行，
   流式请求会收到同样的应答（本地 SSE，不触发上游生成）。
7. **不硬造能力**：本项目在同一网页会话里续写（首轮播种完整历史、之后只发增量），
   因此没有单独的 `reseed` 命令（输入它会得到解释与替代做法）；也没有按桶设置项，
   因此没有 `settings` 命令。

## HTTP 等价入口

命令是对既有管理端点的**会话内快捷方式**，语义完全一致：

| 命令 | HTTP |
| --- | --- |
| `/bridge status` | `GET /diagnostics`（浏览器部分）/ `GET /readiness`（真的探测页面） |
| `/bridge models` | `GET /v1/models` |
| `/bridge session status` | `GET /session/{bucket}` |
| `/bridge session reset` | `POST /session/reset` |
| `/bridge session link` | `POST /session/link` |
| `/bridge session unlink` | `POST /session/unlink` |

## 与 `deepseek-bridge` 的差异

- 保留 **`/bridge link` / `/bridge unlink` 短写**（本项目的用户已经在用这两种写法）。
- 不提供 `settings save-files`：本项目没有代码块自动落盘能力。
- 不提供 `session reseed`：本项目首轮就完整播种、之后只发增量，上下文默认连贯；
  怀疑网页端上下文丢了就用 `session reset` 换一条新会话重新播种。
