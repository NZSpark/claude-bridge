"""DOM 选择器与 JS 片段集中管理（doc/design.md 4.3）。

* 可被 ``.env`` 覆盖的**候选选择器**在 :mod:`claude_web.config`
  （``INPUT_SELECTORS`` / ``SEND_BUTTON_SELECTORS`` / ``RESPONSE_SELECTORS`` /
  ``GENERATING_SELECTORS`` / ``NEW_CHAT_SELECTORS`` / ``AUTH_INDICATORS`` /
  ``CHALLENGE_INDICATORS``），由 adapter 读取——这也是测试用
  ``patch.object(config, ...)`` 生效的契约；
* 纯代码内的 JS 片段放这里，作为唯一来源。

⚠️ 选择器仅为**候选**：doc/design.md 4.3 要求以真实页面探测结果为准。
``SELECTOR_VERSION`` 用于诊断——改版导致候选全部落空时，日志里能看出是哪一代选择器。
"""

#: 选择器 / DOM 提取逻辑的版本号：Claude 网页改版导致选择器失效时用于诊断。
SELECTOR_VERSION = "2026-10-12"

# ---- JS 片段 ----

# 读取回复节点完整文本：Claude 流式回复按 token 渲染动画，直接读 innerText 可能
# 拿到未完成的片段。这里在克隆节点上移除动画类与相关样式，挂到屏幕外再读，
# 既拿到完整文本，又保留块级换行。
#
# 同时剔除**页面外壳**（实测结构，2026-10-12，在真实页面上用 CDP 逐节点核对）：
# assistant 消息节点里除了模型正文还有：
#   * ``h2.sr-only[aria-hidden="true"]``：无障碍标题，内容形如
#     ``Claude responded: <整段正文>`` —— 不剔除就会让客户端看到
#     ``Claude responded: 收到。\n\n收到。``（「同一段正文出现两遍」的真实成因）；
#   * ``div[role="toolbar"][data-testid="message-actions"]``：Copy / Read aloud /
#     点赞 / 重试等按钮（图标无文本，但其内联文本与状态文本会漏进来）；
#   * ``<time>13 minutes ago</time>`` / ``just now``：时间戳；
#   * ``div[data-testid="growth-inline-line"]``：推广行
#     （``Claude works directly with your codebase`` + Install 按钮）。
#
# 剔除列表由 `REPLY_CHROME_SELECTORS` 提供（改版时只改配置）：根节点始终是整个克隆节点，
# 不用「正向选择正文容器」——那样在两轮之间根节点会切换，快照不再是前缀增长，流式会被
# 判定为「内容被重写」而中断。
COMPLETE_TEXT_JS = """
    (opts) => {
      const node = (opts && opts.node) || opts;
      const chrome = (opts && opts.chrome) || [];
      const clone = node.cloneNode(true);
      clone.querySelectorAll('[class*="animate"], [class*="pending"], [class*="streaming"]')
        .forEach((e) => e.classList.remove('animate', 'pending', 'streaming'));
      clone.querySelectorAll('[style]').forEach((e) => {
        e.style.animation = 'none';
        e.style.opacity = '1';
        e.style.visibility = 'visible';
      });
      for (const selector of chrome) {
        try { for (const el of clone.querySelectorAll(selector)) el.remove(); } catch (e) {}
      }
      const holder = document.createElement('div');
      holder.style.position = 'absolute';
      holder.style.left = '-99999px';
      holder.appendChild(clone);
      document.body.appendChild(holder);
      const text = clone.innerText || clone.textContent || '';
      holder.remove();
      return text;
    }
    """

# 探测页面是否仍在生成：**两个独立信号**（doc/design.md 4.2 要求多信号）。
#
# 1. 消息级（``reply``）：最后一条 assistant 回复节点自身的 ``data-is-streaming``。
#    实测该页面上每条回复节点都带这个属性（``true`` = 这条回复正在流式输出，
#    ``false`` = 已经写完），是「这条消息写完了吗」最直接的页面自述。
# 2. 控件级（``control``）：可见的停止按钮 / 全局流式标记 —— 站点改版可能让消息级
#    属性消失，两者取并集，避免只依赖单一选择器。
#
# 只回布尔与选择器名，不回显页面正文。
GENERATING_STATE_JS = """
    (opts) => {
      const replySelectors = (opts && opts.reply) || [];
      const controlSelectors = (opts && opts.control) || [];
      let streaming = null;
      let streamingSelector = null;
      for (const selector of replySelectors) {
        let nodes;
        try { nodes = document.querySelectorAll(selector); } catch (e) { continue; }
        if (!nodes.length) continue;
        // 与 Python 侧 read_reply_text 同一口径：只用**首个有命中的**选择器，
        // 且只看最后一条（嵌套容器会让计数翻倍）。
        const last = nodes[nodes.length - 1];
        const attr = last.getAttribute('data-is-streaming');
        if (attr !== null) {
          streaming = String(attr).toLowerCase() === 'true';
          streamingSelector = selector;
        }
        break;
      }
      const words = ['stop', '\\u505c\\u6b62'];
      let stopControl = false;
      for (const selector of controlSelectors) {
        let nodes;
        try { nodes = document.querySelectorAll(selector); } catch (e) { continue; }
        for (const el of nodes) {
          if (el.getAttribute('data-is-streaming') === 'true') { stopControl = true; break; }
          const label = [
            el.getAttribute('aria-label') || '',
            el.getAttribute('data-testid') || '',
            el.getAttribute('title') || '',
            (el.textContent || '').slice(0, 40),
          ].join(' ').toLowerCase();
          if (words.some((w) => label.includes(w))) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) { stopControl = true; break; }
          }
        }
        if (stopControl) break;
      }
      return {
        streaming: streaming,
        streaming_selector: streamingSelector,
        stop_control: stopControl,
      };
    }
    """

# 页面状态探测：**只回布尔与状态字符串，不回显页面正文**。
#
# 挑战页正文特征很重要：站点安全校验页（例如 Cloudflare / Turnstile 的
# "Performing security verification"）标题常常就是站点名，选择器也一个都不命中，
# 只看标题 / 选择器会把「被安全校验挡住」误诊成「未登录 / 改版」。这里在页面
# 内比对配置的文案特征，并把正文长度单独回传（诊断用，不回显正文）。
#
# 注意：本项目**只负责识别**校验页并如实上报 ``challenge_required``，
# 不自动勾选或绕过任何验证（doc/design.md 1.2、4.1）。
PAGE_PROBE_JS = """
    (opts) => {
      const count = (s) => { try { return document.querySelectorAll(s).length; } catch (e) { return -1; } };
      const any = (list) => list.some((s) => count(s) > 0);
      // 选择器命中必须是**可见**的：Cloudflare Turnstile 的 iframe 标题就含 "challenge"
      // （"Widget containing a Cloudflare security challenge"），但它常常是隐藏的、不代表
      // 页面被挡住。旧实现只数节点，于是把正常页面误判成挑战页。
      const visibleAny = (list) => list.some((s) => {
        try {
          for (const n of document.querySelectorAll(s)) {
            const rects = n.getClientRects ? n.getClientRects() : null;
            if (rects && rects.length) return true;
          }
        } catch (e) {}
        return false;
      });
      // 挑战文案只认「页面自身」的文本：会话正文（助手回复 / 用户消息 / 代码块）与输入区里的
      // 同名字符串属于历史内容，不能当作安全校验页的证据。实测一个讨论本项目的会话正文里
      // 整段引用了 "performing security verification"，旧实现在这种页面上每轮都误报
      // challenge_required，而页面其实完全可用（见 doc/cloudfare.md 3.4）。
      const pageText = () => {
        const clone = document.body ? document.body.cloneNode(true) : null;
        if (!clone) return '';
        const drop = ['script', 'style', 'noscript', 'template',
                      '[contenteditable="true"]', 'textarea'].concat(opts.exclude || []);
        for (const s of drop) {
          try { for (const n of clone.querySelectorAll(s)) n.remove(); } catch (e) {}
        }
        // 克隆节点不在渲染树里，innerText 退化为 textContent；上面的 script/style 已移除，
        // 剩下的就是页面自身可见文本（不包含会话正文与输入区）。
        return String(clone.innerText || clone.textContent || '').toLowerCase();
      };
      const text = pageText();
      const words = (opts.challenge_text || []).map((w) => String(w).toLowerCase()).filter((w) => w);
      const textPattern = words.find((w) => text.includes(w)) || '';
      return {
        url: location.href,
        ready: document.readyState,
        title: document.title || '',
        composer: any(opts.input),
        auth: any(opts.auth),
        challenge: visibleAny(opts.challenge),
        challenge_text: !!textPattern,
        // 命中的**配置文案**（不是页面正文），用于诊断与错误详情。
        challenge_text_pattern: textPattern,
        body_text_len: text.length,
        password: count('input[type="password"]') > 0,
      };
    }
    """

# 读取页面自身文本（用于上下文到顶的扫描；正文匹配在 Python 侧做）。
#
# 会话正文（助手 / 用户消息）、输入区与会话列表必须剔除：用户的提问里完全可能出现
# 「已达长度限制」这类字样，不剔除就会误报「会话到顶」。剔除清单由调用方（
# ``config.conversation_exclude_selectors()``）传入；script/style 与输入区是本函数自己
# 恒定的底子。
#
# 剔除用**克隆剪枝**（与 PAGE_PROBE_JS 同一手法），而不是「整页文本减子树 innerText」：
# body 的 innerText 会跨元素重新规范化空白，子树自己的 innerText 往往不是它的子串，
# 字符串替换会静默失败——实测一个引用了本仓库 .env.example 的会话里，那段文本出现两次
# （用户消息 + 助手回复），旧做法只剔掉一处，另一处继续让扫描命中，于是每个请求都被误报
# 成 context_limit_reached。
#
# 剪枝要做克隆，成本不低，而绝大多数轮次页面上根本没有这类提示：先做一次廉价的
# includes 预判，没命中直接返回空串（Python 侧据此判定为否）。
BODY_TEXT_JS = """
    (opts) => {
      const selectors = (opts && opts.selectors) || [];
      const patterns = (opts && opts.patterns) || [];
      const raw = document.body ? (document.body.innerText || '') : '';
      if (!raw) return '';
      const lowered = raw.toLowerCase();
      if (!patterns.some((p) => lowered.includes(p))) return '';
      const clone = document.body.cloneNode(true);
      const drop = ['script', 'style', 'noscript', 'template',
                    '[contenteditable="true"]', 'textarea'].concat(selectors);
      for (const s of drop) {
        try { for (const n of clone.querySelectorAll(s)) n.remove(); } catch (e) {}
      }
      // 克隆节点不在渲染树里，innerText 退化为 textContent；上面的非正文子树已移除。
      return String(clone.innerText || clone.textContent || '');
    }
    """

#: 「生成中」探测的硬编码兜底选择器（config 候选全落空时仍可用）。
GENERATING_FALLBACK = 'button[aria-label*="Stop"], [data-is-streaming="true"]'

__all__ = [
    "SELECTOR_VERSION",
    "COMPLETE_TEXT_JS",
    "GENERATING_STATE_JS",
    "PAGE_PROBE_JS",
    "BODY_TEXT_JS",
    "GENERATING_FALLBACK",
]
