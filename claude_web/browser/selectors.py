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

#: 选择器版本号：Claude 网页改版导致选择器失效时用于诊断。
SELECTOR_VERSION = "2026-10-11"

# ---- JS 片段 ----

# 读取回复节点完整文本：Claude 流式回复按 token 渲染动画，直接读 innerText 可能
# 拿到未完成的片段。这里在克隆节点上移除动画类与相关样式，挂到屏幕外再读，
# 既拿到完整文本，又保留块级换行。
COMPLETE_TEXT_JS = """
    (node) => {
      const clone = node.cloneNode(true);
      clone.querySelectorAll('[class*="animate"], [class*="pending"], [class*="streaming"]')
        .forEach((e) => e.classList.remove('animate', 'pending', 'streaming'));
      clone.querySelectorAll('[style]').forEach((e) => {
        e.style.animation = 'none';
        e.style.opacity = '1';
        e.style.visibility = 'visible';
      });
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

# 读取整页可见文本长度（用于上下文到顶的粗筛；正文匹配在 Python 侧做）。
BODY_TEXT_JS = "(selectors) => { let text = document.body ? (document.body.innerText || '') : '';" \
    " for (const s of selectors) { try { for (const n of document.querySelectorAll(s)) {" \
    " const t = n.innerText || ''; if (t) text = text.replace(t, ' '); } } catch (e) {} }" \
    " return text; }"

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
