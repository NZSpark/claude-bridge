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

# 探测页面是否仍在生成：停止按钮 / 流式标记。
GENERATING_JS = """
    (selectors) => {
      const words = ['stop', '\\u505c\\u6b62'];
      for (const selector of selectors) {
        let nodes;
        try { nodes = document.querySelectorAll(selector); } catch (e) { continue; }
        for (const el of nodes) {
          const label = [
            el.getAttribute('aria-label') || '',
            el.getAttribute('data-testid') || '',
            el.getAttribute('title') || '',
            (el.textContent || '').slice(0, 40),
          ].join(' ').toLowerCase();
          if (el.getAttribute('data-is-streaming') === 'true') return true;
          if (words.some((w) => label.includes(w))) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) return true;
          }
        }
      }
      return false;
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
      const bodyText = ((document.body && document.body.innerText) || '').toLowerCase();
      const words = (opts.challenge_text || []).map((w) => String(w).toLowerCase());
      return {
        url: location.href,
        ready: document.readyState,
        title: document.title || '',
        composer: any(opts.input),
        auth: any(opts.auth),
        challenge: any(opts.challenge),
        challenge_text: words.some((w) => bodyText.includes(w)),
        body_text_len: bodyText.length,
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
    "GENERATING_JS",
    "PAGE_PROBE_JS",
    "BODY_TEXT_JS",
    "GENERATING_FALLBACK",
]
