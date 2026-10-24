/* ============================================================================
   darklight.js — y8y 共享主题决策引擎 (单一来源, 2026-09-22)
   物理源: /var/lib/todolist-legacy/shared/chrome/darklight.js  → 全站经 /assets/darklight.js 引用
   ----------------------------------------------------------------------------
   三态模式:
     auto    默认 —— 跟随系统偏好, 且**系统变化时实时响应**(旧实现完全缺失)
     light   手动浅色
     dark    手动深色
   存储: localStorage['y8y-theme-mode'] = 'auto' | 'light' | 'dark'
   迁移: 旧键 'theme' / 'y8yshop-theme' 一律清除并忽略 → 升级后首次进入回到 auto
   输出: <html data-theme="light|dark">               渲染用 (CSS 只认这个)
         <html data-y8y-theme-mode="auto|light|dark"> 当前模式 (UI 高亮用)

   API:
     darklight.prePaint()   <head> 内同步调用, 零闪白
     darklight.set(m)       设定模式并持久化
     darklight.cycle()      auto → light → dark → auto (单按钮用)
     darklight.mode()       当前模式 ('auto'|'light'|'dark')
     darklight.resolved()   实际生效 ('light'|'dark')
     darklight.onChange(fn) 订阅变化

   事件:
     document 'y8y:theme'          {mode, resolved}        ← 新名, 权威
     document 'y8y-theme-changed'  {mode, resolved, dark}  ← 旧名, 兼容各站图表重绘

   按钮协议 (各站 UI 无需写 JS):
     <button data-y8y-theme="auto|light|dark">  点击即设, 自动 active + aria-pressed
     <button data-y8y-theme-cycle>              点击轮转三态
   ========================================================================== */
(function (global) {
  var KEY = 'y8y-theme-mode';
  var LEGACY = ['theme', 'y8yshop-theme'];   // 各站历史私有键, 已废弃
  var MODES = ['auto', 'light', 'dark'];
  var mq = global.matchMedia ? global.matchMedia('(prefers-color-scheme: dark)') : null;
  var mode = 'auto';

  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function lsDel(k) { try { localStorage.removeItem(k); } catch (e) {} }

  /* 读取模式; 顺手做一次性迁移(清除旧键 → 回到 auto) */
  function load() {
    for (var i = 0; i < LEGACY.length; i++) {
      if (lsGet(LEGACY[i]) !== null) lsDel(LEGACY[i]);
    }
    var v = lsGet(KEY);
    return (v === 'light' || v === 'dark') ? v : 'auto';
  }

  function sys() { return (mq && mq.matches) ? 'dark' : 'light'; }
  function resolve(m) { return m === 'auto' ? sys() : m; }

  /* 把模式刷到 DOM (data-theme 渲染 + data-y8y-theme-mode 状态 + 按钮高亮) */
  function paint() {
    var root = document.documentElement;
    var r = resolve(mode);
    root.setAttribute('data-theme', r);
    root.setAttribute('data-y8y-theme-mode', mode);
    highlight();
    return r;
  }

  function highlight() {
    var btns = document.querySelectorAll('[data-y8y-theme]');
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute('data-y8y-theme') === mode;
      btns[i].classList.toggle('active', on);
      btns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  function emit() {
    var r = resolve(mode);
    try { document.dispatchEvent(new CustomEvent('y8y:theme', { detail: { mode: mode, resolved: r } })); } catch (e) {}
    try { document.dispatchEvent(new CustomEvent('y8y-theme-changed', { detail: { mode: r, resolved: r, dark: r === 'dark' } })); } catch (e) {}
  }

  function set(m) {
    if (MODES.indexOf(m) < 0) m = 'auto';
    mode = m;
    lsSet(KEY, m);
    paint();
    emit();
    return mode;
  }

  function cycle() { return set(MODES[(MODES.indexOf(mode) + 1) % MODES.length]); }

  /* 点击委托(捕获阶段, 不被各站 stopPropagation 干扰) */
  var bound = false;
  function bind() {
    if (bound) return;
    bound = true;
    document.addEventListener('click', function (ev) {
      var t = ev.target;
      while (t && t.nodeType === 1) {
        if (t.hasAttribute('data-y8y-theme')) {
          ev.preventDefault(); ev.stopPropagation(); set(t.getAttribute('data-y8y-theme')); return;
        }
        if (t.hasAttribute('data-y8y-theme-cycle')) {
          ev.preventDefault(); ev.stopPropagation(); cycle(); return;
        }
        t = t.parentNode;
      }
    }, true);
  }

  function onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  /* <head> 内同步调用: 定模式 + 上色 + 挂系统监听 + 绑按钮 */
  function prePaint() {
    mode = load();
    paint();
    if (mq) {
      var h = function () { if (mode === 'auto') { paint(); emit(); } };  // ← auto 态实时跟随系统
      if (mq.addEventListener) mq.addEventListener('change', h);
      else if (mq.addListener) mq.addListener(h);
    }
    onReady(highlight);
    bind();
    return mode;
  }

  global.darklight = {
    version: '1.0.0',
    MODES: MODES,
    KEY: KEY,
    prePaint: prePaint,
    set: set,
    cycle: cycle,
    mode: function () { return mode; },
    resolved: function () { return resolve(mode); },
    system: sys,
    onChange: function (fn) { document.addEventListener('y8y:theme', fn); },
    repaint: paint
  };

  /* 旧 API 兼容层: 老代码若调 window.y8y.theme.* 仍然工作 */
  global.y8y = global.y8y || {};
  global.y8y.theme = {
    prePaint: prePaint,
    apply: function (m) { return set(m); },
    current: function () { return resolve(mode); },
    mode: function () { return mode; },
    system: sys
  };
})(window);
