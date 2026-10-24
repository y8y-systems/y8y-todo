/* ============================================================================
   ui.js — y8y 共享 UI 行为（2026-09-22 建立）
   物理源: /var/lib/todolist-legacy/shared/chrome/ui.js   → 各站经 /assets/ui.js?v=N 引用

   目前只有一条：**刷新按钮协议**。
     各站注册：   window.y8yRefresh = async () => { await Promise.all([loadA(), loadB()]); };
     共享层负责： 绑定 .y8y-refresh-btn 的点击、busy 态（闪烁）、错误兜底。
   这样"按钮长什么样/放哪里"统一，"刷新哪些数据"由各站自己决定（数据源本来就不同）。
   ========================================================================== */
(function () {
  function busy(btn, on) {
    if (on) { btn.dataset.busy = '1'; btn.classList.add('busy'); btn.setAttribute('aria-busy', 'true'); }
    else { btn.dataset.busy = ''; btn.classList.remove('busy'); btn.setAttribute('aria-busy', 'false'); }
  }
  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('.y8y-refresh-btn') : null;
    if (!btn) return;
    e.preventDefault();
    if (btn.dataset.busy) return;                       // 防连点
    var fn = window.y8yRefresh;
    if (typeof fn !== 'function') {
      console.warn('[y8y] 这个页面没注册 window.y8yRefresh —— 刷新按钮无动作');
      return;
    }
    busy(btn, true);
    Promise.resolve()
      .then(function () { return fn(); })
      .catch(function (err) { console.error('[y8y] 刷新失败:', err); })
      .then(function () { busy(btn, false); });
  });
})();

/* ============================================================================
   ui.js — 第 2 条：手机端「一个 menu 收全部」（2026-09-23 定案，全站统一）

   行为：≤620px 时 header 只留【☰ 菜单】【刷新】【设置】；header 里其余控件（各站
   自己的中间槽 .header-right / .header-actions / .y8y-nav / .nav / .nav-group …）
   被**移动**进 `.y8y-menu-panel`（面板挂在 header 内部，所以各站既有的
   `.header .xxx` 选择器在面板里照样命中）。>620px 按记录原样移回。

   为什么由共享层做 DOM 迁移、而不是各站写两份 HTML：各站槽的类名/数量/顺序都不同，
   写两份必然漂移；移动 DOM 不动事件监听器（监听器跟着元素走）。

   逃生口：元素加 `data-y8y-header-keep` ⇒ 永不收进面板（给"进某模式必须一抬手点到"
   的组用，例如编辑器的保存组）。

   ⚠️ 跨站前提（改各站 header 时注意）：品牌 = .brand-img / .brand-title（及 .brand-btn /
   .brand-wrap）；「留在 header 的」= .y8y-refresh-btn / #refresh / .settings-btn / #settings。
   新加 header 控件若不在这些判据里、也不在某个容器内，它会被收进面板。
   ⚠️ 按钮的显隐完全交给 CSS（`.header .y8y-menu-btn{display:none}` + ≤620px 覆写），
   本脚本不碰 display —— 这样 CSS 与 JS 谁没加载都能优雅降级（没 JS = 维持平铺）。
   ========================================================================== */
(function () {
  var BREAKPOINT = '(max-width: 620px)';
  var KEEP = '.y8y-refresh-btn, #refresh, .settings-btn, #settings';
  var MQ = window.matchMedia ? window.matchMedia(BREAKPOINT) : null;
  var els = null;

  function isBrand(el) {
    return el.classList.contains('brand-img') || el.classList.contains('brand-title')
        || el.classList.contains('brand-btn') || el.classList.contains('brand-wrap');
  }
  function keepIt(el) {
    return el.matches(KEEP) || el.hasAttribute('data-y8y-header-keep');
  }

  function build(header) {
    var btn = header.querySelector('.y8y-menu-btn');
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'y8y-menu-btn';
      btn.setAttribute('aria-label', '菜单');
      btn.setAttribute('aria-expanded', 'false');
      btn.innerHTML = '<span class="y8i y8i-menu" aria-hidden="true"></span>';
      var anchor = header.querySelector('.brand-title') || header.querySelector('.brand-img');
      if (anchor && anchor.parentNode === header) anchor.insertAdjacentElement('afterend', btn);
      else header.insertBefore(btn, header.firstElementChild);
    }
    var panel = header.querySelector('.y8y-menu-panel');
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'y8y-menu-panel';
      panel.setAttribute('aria-label', '页面菜单');
      header.appendChild(panel);
    }
    return { btn: btn, panel: panel };
  }

  function remember(el) {
    if (!el.__y8yHome) el.__y8yHome = { parent: el.parentNode, next: el.nextElementSibling };
  }
  function putBack(el) {
    var home = el.__y8yHome;
    if (!home || !home.parent) return;
    var next = (home.next && home.next.parentNode === home.parent) ? home.next : null;
    home.parent.insertBefore(el, next);
    el.__y8yHome = null;
  }

  function collapseIntoPanel(header, ref) {
    // ① header 的直接子级（品牌 / 刷新 / 设置 / 按钮 / 面板 除外）→ 面板
    [].slice.call(header.children).forEach(function (el) {
      if (el === ref.btn || el === ref.panel || isBrand(el)) return;
      if (keepIt(el)) return;
      remember(el);
      ref.panel.appendChild(el);
    });
    // ② 被搬走的容器里若藏着刷新 / 设置（各站常把它们包在 .header-right 里）
    //    ⇒ 提到 header 上，拼成 定的顺序：【菜单】【刷新】【设置】
    var anchor = ref.btn;
    [].slice.call(ref.panel.querySelectorAll(KEEP)).forEach(function (el) {
      if (el.hasAttribute('data-y8y-header-keep')) return;
      remember(el);
      anchor.insertAdjacentElement('afterend', el);
      anchor = el;
    });
  }

  function restoreAll(header, ref) {
    // 顺序：先把"被提出来"的刷新/设置放回原容器，再把容器放回 header（容器带着子元素一起回）
    [].slice.call(header.children).forEach(function (el) {
      if (el === ref.btn || el === ref.panel || isBrand(el)) return;
      if (el.__y8yHome) putBack(el);
    });
    [].slice.call(ref.panel.children).forEach(function (el) {
      if (el.__y8yHome) putBack(el);
    });
  }

  function close(ref) {
    if (!ref) return;
    ref.panel.classList.remove('open');
    ref.btn.setAttribute('aria-expanded', 'false');
  }

  /* 手机端"靠右"由共享层统一承担（桌面端靠各站中间槽的 margin-left:auto，槽一进菜单就失效）。
     给"留下来的第一个可见元素"打标记，CSS 里 [data-y8y-menu-lead] 吃 margin-left:auto。
     ⚠️ 只能有一个元素吃 auto —— 两个会在 flex 里均分剩余空间、把按钮拆开（2026-09-22 踩过）。 */
  function markLead(header, ref) {
    clearLead(header);
    var lead = [].slice.call(header.children).filter(function (el) {
      if (el === ref.panel || isBrand(el)) return false;
      return getComputedStyle(el).display !== 'none';   // data-empty 的菜单按钮会被跳过
    })[0];
    if (lead) lead.setAttribute('data-y8y-menu-lead', '1');
  }
  function clearLead(header) {
    [].slice.call(header.querySelectorAll('[data-y8y-menu-lead]')).forEach(function (el) {
      el.removeAttribute('data-y8y-menu-lead');
    });
  }

  /* 面板态给图标按钮补文字（元素带 data-y8y-menu-label="列表"）；还原时移除。 */
  function addLabels(panel) {
    [].slice.call(panel.querySelectorAll('[data-y8y-menu-label]')).forEach(function (el) {
      if (el.querySelector(':scope > .y8y-menu-label')) return;
      var span = document.createElement('span');
      span.className = 'y8y-menu-label';
      span.textContent = el.getAttribute('data-y8y-menu-label');
      el.appendChild(span);
    });
  }
  function removeLabels(panel) {
    [].slice.call(panel.querySelectorAll('.y8y-menu-label')).forEach(function (el) { el.remove(); });
  }

  /* 中间槽的项在**桌面端**也要文字（2026-09-23 方案 B：图标 24 + 文字 13）。
     与面板共用同一个 data-y8y-menu-label / .y8y-menu-label：桌面给槽内的项补，
     进面板时面板那份逻辑先查 :scope > .y8y-menu-label ⇒ 不会重复补第二份。
     只作用于 .y8y-header-center 内部 —— 没采用中间槽的站完全不受影响。 */
  function addSlotLabels() {
    [].slice.call(document.querySelectorAll('.y8y-header-center [data-y8y-menu-label]')).forEach(function (el) {
      if (el.querySelector(':scope > .y8y-menu-label')) return;
      var span = document.createElement('span');
      span.className = 'y8y-menu-label';
      span.textContent = el.getAttribute('data-y8y-menu-label');
      el.appendChild(span);
    });
  }

  function sync() {
    var header = document.querySelector('.header');
    if (!header) return;
    els = build(header);
    if (MQ && MQ.matches) {
      collapseIntoPanel(header, els);
      // 该站 header 本来就没有"中间控件"（例如 review 只有刷新 + 设置）⇒ 菜单没东西可放。
      // 这时不要把菜单按钮摆出来 —— 点开一片空白比没有按钮更糟。
      els.btn.dataset.empty = els.panel.children.length ? '' : '1';
      addLabels(els.panel);
      markLead(header, els);
    } else {
      close(els);
      clearLead(header);
      removeLabels(els.panel);
      restoreAll(header, els);
      addSlotLabels();          // 桌面端：槽内的项显示文字（面板那份已被 removeLabels 清掉）
      delete els.btn.dataset.empty;
    }
  }

  function init() {
    var header = document.querySelector('.header');
    if (!header) return;
    els = build(header);
    sync();
    if (MQ) {
      if (MQ.addEventListener) MQ.addEventListener('change', sync);
      else if (MQ.addListener) MQ.addListener(sync);
    }
    els.btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = els.panel.classList.toggle('open');
      els.btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', function (e) {
      if (!els.panel.classList.contains('open')) return;
      if (els.panel.contains(e.target) || els.btn.contains(e.target)) return;
      close(els);
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(els); });
    // 面板里点了任何按钮/链接 ⇒ 收起面板（否则它一直挡着内容）
    els.panel.addEventListener('click', function (e) {
      var hit = e.target && e.target.closest ? e.target.closest('button, a, input, select, label') : null;
      if (hit) close(els);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
