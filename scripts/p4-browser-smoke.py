#!/usr/bin/env python3
"""P4 演练 — 升级后实例的真实浏览器冒烟 (playwright, 需要 Chromium)。

⚠️ SAFETY: 会在目标实例上通过 UI 创建一个任务 — 只用于隔离演练实例。

Usage: p4-browser-smoke.py <base-url> <identity-email>

与 scripts/e2e-browser.py 的区别: 本脚本面向生产模式实例, 通过
browser context 的 extra HTTP headers 注入代理身份头 (Cf-Access-
Authenticated-User-Email), 因此可以在带认证的升级后实例上驱动真实 UI。
"""
import json
import sys
import time

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://127.0.0.1:8086'
EMAIL = sys.argv[2] if len(sys.argv) > 2 else 'alice@example.com'

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  ({detail})' if detail and not ok else ''))


def main():
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={'width': 1280, 'height': 900},
            extra_http_headers={'Cf-Access-Authenticated-User-Email': EMAIL},
        )
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))

        print('== 升级后实例的浏览器冒烟 (生产模式 + 代理身份头) ==')
        page.goto(BASE + '/todo/', wait_until='networkidle')
        check('页面标题', page.title() == 'y8yTODO')
        check('日历网格渲染', page.locator('.cal-cell').count() >= 28)

        print('== UI 创建任务 (真实点击) ==')
        title = f'P4演练任务-{int(time.time()) % 100000}'
        page.locator('#fabAdd').click()
        page.locator('#inputTitle').fill(title)
        page.locator('#inputDue').fill(
            time.strftime('%Y-%m-%d', time.localtime(time.time() + 86400 * 2)))
        page.locator('#modalActions button.btn-primary').click()
        page.wait_for_timeout(800)
        body = page.evaluate("fetch('/todo/api/tasks').then(r => r.text())")
        titles = [t['title'] for t in json.loads(body)['tasks']]
        check('任务经 UI 创建并持久化', title in titles, f'got {titles[:3]}')

        print('== 视图与主题 ==')
        page.locator('#viewTabList').click()
        page.wait_for_timeout(300)
        check('列表视图可见', page.locator('#viewList').is_visible())
        page.locator('#settingsBtn').click()
        page.wait_for_timeout(200)
        page.locator('[data-y8y-theme="dark"]').first.click()
        page.wait_for_timeout(300)
        check('暗色主题生效',
              page.evaluate("document.documentElement.getAttribute('data-theme')") == 'dark')

        real_errors = [e for e in errors if 'favicon' not in e.lower()]
        check('无 JS 运行时错误', not real_errors, f'{real_errors[:3]}')
        browser.close()

    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
