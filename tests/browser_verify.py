"""Browser-level verification of the P1 build (run against a live dev server).

Checks: page loads without JS errors, three views render, icons resolve,
task creation works end-to-end through the UI, theme switching works,
and the XSS fix holds in the real DOM.

Port: the script talks to a **dev-mode** server, default
http://127.0.0.1:18086 (kept for historical reasons — not the app's
default 8086). Override with Y8Y_VERIFY_PORT / Y8Y_VERIFY_BASE:

    Y8Y_DEV_MODE=1 Y8Y_PORT=18086 python3 src/serve.py &
    python3 tests/browser_verify.py
"""
import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

# 默认 18086 (历史原因, 与应用默认端口 8086 不同); 可用环境变量覆盖。
BASE = os.environ.get('Y8Y_VERIFY_BASE') or \
    f"http://127.0.0.1:{os.environ.get('Y8Y_VERIFY_PORT', '18086')}"
PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f'  ({detail})' if detail and not ok else ''))


def main():
    # Seed an XSS-probe task through the API before loading the page.
    req = urllib.request.Request(
        BASE + '/todo/api/tasks', method='POST',
        data=json.dumps({'title': '<img src=x onerror="window.__xss=1">xss探针',
                         'due': '2026-10-21', 'content': '<script>window.__xss2=1</script>正文'}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req) as r:
        check('API 播种 XSS 探针任务', r.status == 201)

    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)

        print('== 加载与渲染 ==')
        page.goto(BASE + '/todo/', wait_until='networkidle')
        check('页面标题', page.title() == 'y8yTODO')
        check('header 渲染', page.locator('.header .brand-mark img').count() == 1)
        check('logo SVG 加载完成',
              page.evaluate("document.querySelector('.brand-mark img').complete"))
        check('dev 模式单用户身份为 dev', page.evaluate(
            "fetch('/todo/api/tasks').then(r => r.json()).then(d => d.current_user)") == 'dev')
        check('日历视图默认显示', page.locator('#viewCalendar').is_visible())
        check('日历网格渲染', page.locator('.cal-cell').count() >= 28)
        check('视图 tab ×3', page.locator('.view-tab').count() == 3)
        check('设置弹窗存在', page.locator('#settingsOverlay').count() == 1)

        print('== 图标资源 ==')
        icon_results = page.evaluate("""() => {
            const probe = new Image();
            const names = ['add','list','calendar','cart','reload','settings','bookmark',
                           'expand','pager','check','trash','edit','copy','close','sun','moon',
                           'recurring','menu'];
            return Promise.all(names.map(n => new Promise(res => {
                const img = new Image();
                img.onload = () => res([n, true]);
                img.onerror = () => res([n, false]);
                img.src = '/todo/assets/icons/ui/' + n + '.svg';
            })));
        }""")
        missing = [n for n, ok in icon_results if not ok]
        check('18 个图标 SVG 全部可加载', not missing, f'missing: {missing}')

        # Mask is actually applied (a visible icon has non-zero box and mask-image).
        mask_ok = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('.y8i'));
            const visible = els.find(el => el.getBoundingClientRect().width > 0);
            if (!visible) return false;
            const cs = getComputedStyle(visible);
            return cs.maskImage !== 'none' && cs.maskImage.includes('.svg');
        }""")
        check('图标 mask 方案生效', mask_ok)

        print('== XSS 防护（真实 DOM） ==')
        page.goto(BASE + '/todo/?view=list', wait_until='networkidle')
        xss_executed = page.evaluate('window.__xss === 1 || window.__xss2 === 1')
        check('注入脚本未执行', not xss_executed)
        title_html = page.evaluate(
            "document.querySelector('.card-title span') ? document.querySelector('.card-title span').innerHTML : ''")
        check('标题以转义文本呈现', '&lt;img' in title_html and '<img' not in title_html,
              f'got: {title_html[:60]}')

        print('== UI 端到端：新建任务 ==')
        page.goto(BASE + '/todo/', wait_until='networkidle')
        page.locator('#fabAdd').click()
        page.locator('#inputTitle').fill('浏览器验证任务')
        page.locator('#inputDue').fill('2026-10-25')
        page.locator('#modalActions button.btn-primary').click()
        page.wait_for_timeout(600)
        body = json.loads(page.evaluate("fetch('/todo/api/tasks').then(r => r.text())"))
        check('任务持久化到后端', any(t['title'] == '浏览器验证任务' for t in body['tasks']))

        print('== P2: UI 创建 XSS payload 任务 ==')
        page.locator('#fabAdd').click()
        xss_title = '<img src=x onerror="window.__uixss=1">UI注入'
        page.locator('#inputTitle').fill(xss_title)
        page.locator('#inputDue').fill('2026-10-26')
        page.locator('#modalActions button.btn-primary').click()
        page.wait_for_timeout(700)
        uixss_executed = page.evaluate('window.__uixss === 1')
        check('UI 创建的 XSS 任务未执行脚本', not uixss_executed)
        page.goto(BASE + '/todo/?view=list', wait_until='networkidle')
        uixss_executed = page.evaluate('window.__uixss === 1')
        check('刷新后仍未执行', not uixss_executed)
        card_titles = page.evaluate(
            "Array.from(document.querySelectorAll('.card-title span')).map(e => e.innerHTML)")
        check('XSS 标题以转义文本渲染',
              any('&lt;img' in h for h in card_titles),
              f'got: {card_titles[:2]}')

        print('== P2: UI 创建同名任务 ==')
        page.goto(BASE + '/todo/', wait_until='networkidle')
        page.locator('#fabAdd').click()
        page.locator('#inputTitle').fill('同名UI任务')
        page.locator('#inputDue').fill('2026-10-27')
        page.locator('#modalActions button.btn-primary').click()
        page.wait_for_timeout(700)
        page.locator('#fabAdd').click()
        page.locator('#inputTitle').fill('同名UI任务')
        page.locator('#inputContent').fill('第二个')
        page.locator('#inputDue').fill('2026-10-27')
        page.locator('#modalActions button.btn-primary').click()
        page.wait_for_timeout(700)
        body = json.loads(page.evaluate("fetch('/todo/api/tasks').then(r => r.text())"))
        same = [t for t in body['tasks'] if t['title'] == '同名UI任务']
        check('两个同名任务并存（无覆盖）', len(same) == 2, f'count={len(same)}')
        check('两个任务内容各自独立',
              {t['body'] for t in same} >= {'同名UI任务', '第二个'})

        print('== P2: UI 重命名任务 ==')
        page.goto(BASE + '/todo/?view=list', wait_until='networkidle')
        page.wait_for_timeout(400)
        # 精确定位「浏览器验证任务」卡片（列表按截止日排序, 不能盲点第一张）
        target = page.locator('.card', has_text='浏览器验证任务').first
        target.click()                          # 打开编辑弹窗
        page.wait_for_timeout(300)
        page.locator('#inputTitle').fill('改名后的任务')
        page.locator('#modalActions button.btn-primary').click()
        page.wait_for_timeout(700)
        body = json.loads(page.evaluate("fetch('/todo/api/tasks').then(r => r.text())"))
        titles = [t['title'] for t in body['tasks']]
        check('重命名生效且旧标题消失',
              '改名后的任务' in titles and '浏览器验证任务' not in titles,
              f'titles={titles[:4]}')

        print('== 视图切换 ==')
        page.locator('#viewTabShopping').click()
        page.wait_for_timeout(300)
        check('购物视图可见', page.locator('#viewShopping').is_visible())
        check('购物视图含输入框', page.locator('#shoppingInput').count() == 1)
        page.locator('#viewTabList').click()
        page.wait_for_timeout(300)
        check('列表视图可见', page.locator('#viewList').is_visible())

        print('== 主题切换 ==')
        page.locator('#settingsBtn').click()
        page.wait_for_timeout(200)
        page.locator('[data-y8y-theme="dark"]').first.click()
        page.wait_for_timeout(300)
        theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
        check('暗色主题生效', theme == 'dark', f'got: {theme}')
        page.locator('[data-y8y-theme="light"]').first.click()
        page.wait_for_timeout(300)
        theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
        check('亮色主题生效', theme == 'light')

        print('== 购物条目 UI ==')
        page.locator('#settingsOverlay .btn-cancel').click()
        page.locator('#viewTabShopping').click()
        page.wait_for_timeout(200)
        import time
        item_name = f'牛奶-{int(time.time()) % 100000}'
        page.locator('#shoppingInput').fill(item_name)
        page.locator('#shoppingAdd').click()
        page.wait_for_timeout(800)
        dom_names = page.evaluate(
            "Array.from(document.querySelectorAll('.shopping-name')).map(e => e.textContent)")
        check('购物条目添加成功',
              dom_names.count(item_name) == 1, f'DOM: {dom_names}')

        print('== JS 错误 ==')
        real_errors = [e for e in errors if 'favicon' not in e.lower()]
        check('无 JS 运行时错误', not real_errors, f'{real_errors[:3]}')

        page.screenshot(path='/tmp/y8y-verify-calendar.png', full_page=False)
        page.locator('#viewTabList').click()
        page.wait_for_timeout(300)
        page.screenshot(path='/tmp/y8y-verify-list.png', full_page=False)
        browser.close()

    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    if FAIL:
        print('失败项:', FAIL)
        sys.exit(1)


if __name__ == '__main__':
    main()
