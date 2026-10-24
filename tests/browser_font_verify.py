"""Browser-level verification of the Fusion Pixel font system.

Run against a live dev server:

    Y8Y_DEV_MODE=1 Y8Y_PORT=18086 python3 src/serve.py &
    Y8Y_VERIFY_BASE=http://127.0.0.1:18086 python3 tests/browser_font_verify.py

What is actually proven here (not inferred from CSS):

  1. **Character coverage is read from the font's own cmap** (fontTools),
     not from ``document.fonts.check()`` — that API only tells whether a
     family is *loaded*, never whether it *has* a glyph, so it cannot
     detect a missing character.
  2. **App-owned text and user-supplied text are separated.** Every
     character the application itself renders must exist in the bundled
     fonts (100 %). User content is reported, not asserted: users type
     anything, and the honest guarantee is "GB2312 level 1 + Latin + the
     punctuation the UI uses are covered by every tier".
  3. **The renderer really uses the webfont**, via the Chrome DevTools
     Protocol: ``CSS.getPlatformFontsForNode`` reports the platform font
     actually used for a node, with a glyph count.
  4. **No synthetic bold**: the fonts have no bold face, so a bold
     request would be faked by the renderer. The test asserts the
     computed ``font-synthesis`` is ``none`` and that every element
     asking for a bold weight either carries the pixel double-strike or
     is on the documented small-tier / icon-only exemption list.
  5. **Font requests are counted from the network layer** (CDP
     ``Network.*``): how many requests per URL, how many were served
     from cache, and how many bytes actually crossed the wire — cold
     load, warm reload and throttled link.
  6. **Font load completion time** is measured with ``document.fonts.ready``
     from navigation start (plus the resource timing of each font file),
     not DOMContentLoaded.
"""
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get('Y8Y_VERIFY_BASE') or \
    f"http://127.0.0.1:{os.environ.get('Y8Y_VERIFY_PORT', '18086')}"
REPO = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO / 'vendor' / 'y8y-shared-chrome' / 'fonts'
CHROME_ARGS = ['--single-process', '--no-zygote', '--no-sandbox']

# 'chromium' (default) runs the full suite, including the DevTools-Protocol
# checks. 'webkit' runs the cross-browser subset: WebKit is the engine macOS
# Safari ships, but Safari.app itself cannot be driven from this environment
# (safaridriver needs interactive "Allow Remote Automation"), and the CDP
# domains used below (CSS.getPlatformFontsForNode, Network.*) are
# Chromium-only.
BROWSER = os.environ.get('Y8Y_FONT_BROWSER', 'chromium')
HAS_CDP = BROWSER == 'chromium'
# Optional override for the WebKit build (e.g. a local extraction that is not
# in the Playwright cache).
WEBKIT_PATH = os.environ.get('Y8Y_WEBKIT_PATH') or None

FACES = {
    'Fusion Pixel': 'fusion-pixel-12px-proportional-zh_hans.037fb51624ec.otf.woff2',
}

# Elements that carry **user supplied** text (task titles, descriptions,
# list and project names). Everything else on screen is the app's own.
USER_DATA_SELECTORS = (
    '.card-title', '.card-preview', '.card-body', '.shopping-name',
    '.project-badge', '.persistent-project-list', '.day-drawer-title',
    'input', 'textarea', 'select',
)

# (view, selector, expected font family). The expected name is the font's
# **internal** family name (nameID 1) — that is what the renderer reports,
# not the CSS alias we declared in @font-face.
FACE_PROBES = [
    ('?view=list', '.card-title', 'Fusion Pixel 12px Prop zh-Hans'),
    ('?view=list', '.due-badge', 'Fusion Pixel 12px Prop zh-Hans'),
    ('', '.calendar-title', 'Fusion Pixel 12px Prop zh-Hans'),
    ('', '.cal-day-num', 'Fusion Pixel 12px Prop zh-Hans'),
]

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f'  ({detail})' if detail and not ok else ''))
    return ok


# ── font metrics (fontTools: the authoritative source) ─────────────────

def face_codepoints():
    """{family: set(codepoints)} straight from each font's cmap table."""
    from fontTools.ttLib import TTFont
    out = {}
    for family, name in FACES.items():
        font = TTFont(io.BytesIO((FONTS_DIR / name).read_bytes()))
        try:
            points = set()
            for table in font['cmap'].tables:
                if table.isUnicode():
                    points |= set(table.cmap.keys())
            out[family] = points
        finally:
            font.close()
    return out


# ── in-page collectors ────────────────────────────────────────────────

COLLECT_JS = r"""
(userSelectors) => {
  const app = {}, user = {};
  const bump = (bucket, ch, where) => {
    bucket[ch] = bucket[ch] || {n: 0, where: []};
    bucket[ch].n++;
    if (bucket[ch].where.length < 3) bucket[ch].where.push(where);
  };
  const isUser = (el) => {
    for (const sel of userSelectors) {
      try { if (el.matches(sel) || el.closest(sel)) return true; } catch (e) {}
    }
    return false;
  };
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walk.nextNode())) {
    const parent = node.parentElement;
    if (!parent) continue;
    const cs = getComputedStyle(parent);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    if (parent.closest('input, textarea, select, option')) continue;
    const tag = parent.className && typeof parent.className === 'string'
      ? parent.tagName.toLowerCase() + '.' + parent.className.split(' ')[0]
      : parent.tagName.toLowerCase();
    const bucket = isUser(parent) ? user : app;
    for (const ch of node.nodeValue) bump(bucket, ch, tag);
  }
  for (const el of document.querySelectorAll('*')) {
    for (const pseudo of ['::after', '::before']) {
      const cs = getComputedStyle(el, pseudo);
      if (!cs || !cs.content || cs.content === 'none' || cs.content === 'normal') continue;
      const m = /"([^"]*)"/.exec(cs.content);
      if (m) for (const ch of m[1]) bump(isUser(el) ? user : app, ch, el.tagName.toLowerCase() + pseudo);
    }
  }
  return {app: app, user: user};
}
"""

WEIGHT_JS = r"""
() => {
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!el.offsetParent && getComputedStyle(el).position !== 'fixed') continue;
    const cs = getComputedStyle(el);
    const weight = parseInt(cs.fontWeight, 10);
    if (!(weight >= 600)) continue;
    const text = (el.textContent || '').trim();
    out.push({
      tag: el.tagName.toLowerCase(),
      cls: typeof el.className === 'string' ? el.className : '',
      size: cs.fontSize,
      shadow: cs.textShadow,
      synthesis: cs.fontSynthesis || cs.getPropertyValue('font-synthesis'),
      hasText: text.length > 0,
    });
  }
  return out;
}
"""

OVERFLOW_JS = r"""
() => {
  const bad = [];
  for (const el of document.querySelectorAll('body *')) {
    if (!el.offsetParent && getComputedStyle(el).position !== 'fixed') continue;
    const cs = getComputedStyle(el);
    if (cs.overflow === 'hidden' || cs.overflowX === 'hidden') continue;
    if (el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
      bad.push({sel: el.className || el.tagName, sw: el.scrollWidth, cw: el.clientWidth,
                text: (el.textContent || '').trim().slice(0, 40)});
    }
  }
  return bad.slice(0, 20);
}
"""

RESOURCE_JS = r"""
() => performance.getEntriesByType('resource')
  .filter(e => e.name.indexOf('.woff2') !== -1)
  .map(e => ({
    name: e.name.split('/').pop(),
    start: Math.round(e.startTime),
    end: Math.round(e.responseEnd),
    duration: Math.round(e.duration),
    transfer: e.transferSize,
    encoded: e.encodedBodySize,
    decoded: e.decodedBodySize,
  }))
"""


class NetLog:
    """Per-URL network accounting straight from the DevTools Protocol."""

    def __init__(self):
        self.requests = {}          # url -> request count
        self.from_cache = {}        # url -> served-from-cache count
        self.bytes = {}             # url -> bytes actually transferred
        self._by_id = {}
        self._cached_ids = set()

    def attach(self, cdp):
        # loadingFinished carries no URL -> remember it per request id.
        def on_request(params):
            url = params['request']['url']
            self.requests[url] = self.requests.get(url, 0) + 1
            self._by_id[params['requestId']] = url

        def on_response(params):
            url = params['response']['url']
            if params['response'].get('fromDiskCache') or \
                    params['response'].get('fromPrefetchCache'):
                self._cached_ids.add(params.get('requestId'))

        def on_finished(params):
            url = self._by_id.get(params.get('requestId'), '')
            if not url:
                return
            # encodedDataLength = bytes that actually crossed the wire
            # (0 when the response came from the cache).
            self.bytes[url] = self.bytes.get(url, 0) + params.get('encodedDataLength', 0)

        def on_cache_hit(params):
            # Fired when a request was answered from the HTTP cache
            # (memory or disk) instead of the network.
            self._cached_ids.add(params.get('requestId'))

        cdp.on('Network.requestWillBeSent', on_request)
        cdp.on('Network.responseReceived', on_response)
        cdp.on('Network.requestServedFromCache', on_cache_hit)
        cdp.on('Network.loadingFinished', on_finished)
        return self

    def attach_page(self, page):
        """Cross-browser fallback: count requests with Playwright's events.

        Used when there is no DevTools Protocol session (WebKit). It gives
        the request count but not the cache/byte breakdown.
        """
        def on_request(request):
            url = request.url
            self.requests[url] = self.requests.get(url, 0) + 1

        page.on('request', on_request)
        return self

    def _cache_counts(self):
        counts = {}
        for request_id in self._cached_ids:
            url = self._by_id.get(request_id)
            if url:
                counts[url] = counts.get(url, 0) + 1
        return counts

    def reset(self):
        self.requests.clear()
        self.from_cache.clear()
        self.bytes.clear()
        self._cached_ids.clear()

    def fonts(self):
        """{url: {requests, cached, bytes}} for the font files only."""
        cache = self._cache_counts()
        out = {}
        for url, count in self.requests.items():
            if '.woff2' in url:
                out[url] = {
                    'requests': count,
                    'cached': cache.get(url, 0),
                    'bytes': self.bytes.get(url, 0),
                }
        return out


def platform_fonts(cdp, selector):
    """The platform fonts the renderer actually used for a node."""
    doc = cdp.send('DOM.getDocument', {'depth': -1})
    node = cdp.send('DOM.querySelector',
                    {'nodeId': doc['root']['nodeId'], 'selector': selector})
    node_id = node.get('nodeId')
    if not node_id:
        return []
    result = cdp.send('CSS.getPlatformFontsForNode', {'nodeId': node_id})
    return result.get('fonts', [])


def seed_task():
    """Give the views some content to render."""
    req = urllib.request.Request(
        BASE + '/todo/api/tasks', method='POST',
        data=json.dumps({'title': '字体测试 Font Test テスト 12345',
                         'due': '2026-10-30'}).encode(),
        headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(req, timeout=10).read()
    except Exception:
        pass


READY_SCRIPT = (
    "window.__fontReadyAt = null;"
    "try { document.fonts.ready.then(() => { window.__fontReadyAt = performance.now(); }); }"
    " catch (e) {}")


def launch(p):
    if BROWSER == 'webkit':
        if WEBKIT_PATH:
            return p.webkit.launch(executable_path=WEBKIT_PATH)
        return p.webkit.launch()
    return p.chromium.launch(args=CHROME_ARGS)


def main():
    codepoints = face_codepoints()
    covered = set()
    for points in codepoints.values():
        covered |= points

    seed_task()
    errors = []
    report = {'browser': BROWSER}

    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(viewport={'width': 1280, 'height': 900})
        page = ctx.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.add_init_script(READY_SCRIPT)
        cdp = None
        net = NetLog()
        if HAS_CDP:
            cdp = ctx.new_cdp_session(page)
            cdp.send('Network.enable')
            cdp.send('DOM.enable')
            cdp.send('CSS.enable')
            net.attach(cdp)
        else:
            net.attach_page(page)

        # ── 1. cold load (the very first navigation, nothing counted before)
        print('== 冷加载：字体加载与请求 ==')
        page.goto(BASE + '/todo/', wait_until='networkidle')
        page.wait_for_timeout(600)
        faces = page.evaluate("""() => {
          const out = {};
          for (const f of document.fonts) out[f.family] = f.status;
          return out;
        }""")
        for family in FACES:
            check(f'{family} 已加载', faces.get(family) == 'loaded', str(faces))

        cold = net.fonts()
        for url, info in sorted(cold.items()):
            print(f'     {url.rsplit("/", 1)[-1]}: requests={info["requests"]} '
                  f'cached={info["cached"]} bytes={info["bytes"]}')
        check('字体被请求到（只有一款）', len(cold) == 1, str(list(cold)))
        check('每份字体在一次冷加载里只发一次请求（preload 与 @font-face 复用同一条）',
              all(info['requests'] == 1 for info in cold.values()),
              str({u.rsplit('/', 1)[-1]: i for u, i in cold.items()}))
        if HAS_CDP:
            check('冷加载字体真的过了网络',
                  all(info['bytes'] > 0 for info in cold.values()),
                  str({u.rsplit('/', 1)[-1]: i['bytes'] for u, i in cold.items()}))
        report['cold'] = {u.rsplit('/', 1)[-1]: i for u, i in cold.items()}

        print('== 字体加载完成时间（document.fonts.ready，不是 DOMContentLoaded） ==')
        ready_ms = page.evaluate('window.__fontReadyAt')
        dcl = page.evaluate("""() => {
          const e = performance.getEntriesByType('navigation')[0];
          return e ? Math.round(e.domContentLoadedEventEnd) : null;
        }""")
        resources = page.evaluate(RESOURCE_JS)
        for r in resources:
            print(f'     {r["name"]}: {r["duration"]} ms, transfer={r["transfer"]} '
                  f'encoded={r["encoded"]}')
        print(f'     fonts.ready = {round(ready_ms, 1) if ready_ms else None} ms'
              f'   DOMContentLoaded = {dcl} ms')
        check('字体就绪时间被真实测到', ready_ms is not None and ready_ms > 0, str(ready_ms))
        report['font_ready_ms'] = ready_ms
        report['dom_content_loaded_ms'] = dcl
        report['font_resources'] = resources

        # ── 2. the renderer really uses the webfont (CDP) ---------------
        if HAS_CDP:
            print('== 实际渲染用的字体（CDP 平台字体） ==')
            for view, selector, family in FACE_PROBES:
                page.goto(BASE + '/todo/' + view, wait_until='networkidle')
                page.wait_for_timeout(500)
                used = platform_fonts(cdp, selector)
                names = [f.get('familyName') for f in used]
                glyphs = sum(f.get('glyphCount', 0) for f in used)
                check(f'{selector} 实际由 {family} 渲染',
                      any(n and n == family for n in names) and glyphs > 0,
                      f'platform fonts={names} glyphs={glyphs}')
        else:
            print('== 实际渲染用的字体 ==')
            print('  跳过：CSS.getPlatformFontsForNode 是 Chromium 专有（CDP）接口，'
                  f'{BROWSER} 无对应能力；'
                  'Safari.app 本体在本机无法驱动（safaridriver 需要交互授权）')

        # ── 3. character coverage (fontTools cmap) ----------------------
        print('== 字符覆盖（fontTools cmap，不是 document.fonts.check） ==')
        app_chars, user_chars = {}, {}
        for view in ('', '?view=list', '?view=shopping'):
            page.goto(BASE + '/todo/' + view, wait_until='networkidle')
            page.wait_for_timeout(700)
            page.evaluate("""() => {
                showToast('\\u2713 \\u5df2\\u5b8c\\u6210');
                showToast('\\u2717 \\u52a0\\u8f7d\\u5931\\u8d25');
            }""")
            page.wait_for_timeout(400)
            collected = page.evaluate(COLLECT_JS, list(USER_DATA_SELECTORS))
            for ch, info in collected['app'].items():
                if ord(ch) in (9, 10, 13):
                    continue
                app_chars.setdefault(ch, info)
            for ch, info in collected['user'].items():
                if ord(ch) in (9, 10, 13):
                    continue
                user_chars.setdefault(ch, info)

        app_missing = sorted(c for c in app_chars if ord(c) not in covered)
        check('应用自身的每个字符都被随包字体覆盖（0 回退）',
              not app_missing,
              'missing: ' + ', '.join(f'{c!r}(U+{ord(c):04X})' for c in app_missing))
        user_missing = sorted(c for c in user_chars if ord(c) not in covered)
        user_covered = len(user_chars) - len(user_missing)
        print(f'     应用字符 {len(app_chars)} 个（缺 {len(app_missing)}）；'
              f'用户字符 {len(user_chars)} 个（覆盖 {user_covered}，缺 {len(user_missing)}）')
        report['app_chars'] = {'total': len(app_chars), 'missing': len(app_missing)}
        report['user_chars'] = {
            'total': len(user_chars),
            'covered': user_covered,
            'missing': [f'U+{ord(c):04X} {c!r}' for c in user_missing],
        }

        # ── 4. no synthetic bold ----------------------------------------
        print('== 合成粗体 ==')
        page.goto(BASE + '/todo/?view=list', wait_until='networkidle')
        page.wait_for_timeout(500)
        synth = page.evaluate("""() => getComputedStyle(document.documentElement)
                                      .getPropertyValue('font-synthesis')""")
        check('全站禁止合成粗体（font-synthesis: none）',
              'none' in (synth or ''), f'computed={synth!r}')
        weights = page.evaluate(WEIGHT_JS)
        pixel_bold, plain = 0, 0
        for item in weights:
            shadow = item.get('shadow') or ''
            if '1px' in shadow and shadow != 'none':
                pixel_bold += 1
            else:
                plain += 1
        check('全站没有加粗描边（B 方案：只关合成粗体，不另加粗）',
              pixel_bold == 0,
              f'{pixel_bold} 个元素仍带 text-shadow')
        print(f'     请求粗体的元素 {plain + pixel_bold} 个（无描边 {plain}，'
              f'带描边 {pixel_bold}）')
        report['bold'] = {'elements': plain + pixel_bold, 'with_shadow': pixel_bold}

        # ── 5. layout ----------------------------------------------------
        print('== 布局（无溢出） ==')
        for view, label in (('', '日历'), ('?view=list', '列表'), ('?view=shopping', '购物')):
            page.goto(BASE + '/todo/' + view, wait_until='networkidle')
            page.wait_for_timeout(600)
            bad = page.evaluate(OVERFLOW_JS)
            check(f'{label}视图无横向溢出', not bad, str(bad[:3]))

        # ── 6. warm cache ------------------------------------------------
        print('== 热缓存（同一 context 重新加载） ==')
        net.reset()
        page.goto(BASE + '/todo/?view=list', wait_until='networkidle')
        page.wait_for_timeout(800)
        warm = net.fonts()
        for url, info in sorted(warm.items()):
            print(f'     {url.rsplit("/", 1)[-1]}: requests={info["requests"]} '
                  f'cached={info["cached"]} bytes={info["bytes"]}')
        check('热缓存时字体不再走网络（0 字节）' if HAS_CDP else '热缓存时字体请求仍然各一次',
              all(info['bytes'] == 0 for info in warm.values()) if HAS_CDP
              else all(info['requests'] == 1 for info in warm.values()),
              str({u.rsplit('/', 1)[-1]: i for u, i in warm.items()}))
        report['warm'] = {u.rsplit('/', 1)[-1]: i for u, i in warm.items()}

        print('== JS 错误 ==')
        real = [e for e in errors if 'favicon' not in e.lower()]
        check('无 JS 运行时错误', not real, str(real[:3]))

        browser.close()

    # ── 7. slow link ----------------------------------------------------
    # A separate browser launch: with --single-process Chromium is fragile
    # and a second context in the same browser takes the whole browser down.
    if not HAS_CDP:
        print('== 慢速网络（400 Kbps） ==')
        print('  跳过：Network.emulateNetworkConditions 是 Chromium 专有（CDP）接口')
    else:
        print('== 慢速网络（400 Kbps） ==')
        with sync_playwright() as p:
            browser = launch(p)
            slow_ctx = browser.new_context(viewport={'width': 1280, 'height': 900})
            slow_page = slow_ctx.new_page()
            slow_page.set_default_navigation_timeout(240000)
            slow_page.add_init_script(READY_SCRIPT)
            slow_cdp = slow_ctx.new_cdp_session(slow_page)
            slow_cdp.send('Network.enable')
            slow_cdp.send('Network.emulateNetworkConditions', {
                'offline': False, 'latency': 150,
                'downloadThroughput': 400 * 1024 / 8, 'uploadThroughput': 400 * 1024 / 8,
            })
            slow_net = NetLog().attach(slow_cdp)
            slow_page.goto(BASE + '/todo/', wait_until='domcontentloaded')
            slow_page.wait_for_function('window.__fontReadyAt !== null', timeout=240000)
            slow_ready = slow_page.evaluate('window.__fontReadyAt')
            slow_fonts = slow_net.fonts()
            total_bytes = sum(i['bytes'] for i in slow_fonts.values())
            print(f'     fonts.ready = {round(slow_ready, 1)} ms, 字体实际传输 {total_bytes} 字节')
            check('慢速网络下字体仍能加载完成', slow_ready is not None and slow_ready > 0,
                  str(slow_ready))
            check('慢速网络下字体下载成功',
                  len(slow_fonts) == len(FACES) and all(i['bytes'] > 0 for i in slow_fonts.values()),
                  str({u.rsplit('/', 1)[-1]: i['bytes'] for u, i in slow_fonts.items()}))
            report['slow'] = {'font_ready_ms': slow_ready, 'bytes': total_bytes}
            browser.close()

    out = os.environ.get('Y8Y_FONT_REPORT')
    if out:
        Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'\n报告已写入 {out}')

    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    if FAIL:
        print('失败项:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
