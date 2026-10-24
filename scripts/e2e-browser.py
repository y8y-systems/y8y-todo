#!/usr/bin/env python3
"""Browser-level E2E verification for a running y8y-todo dev instance.

⚠️ SAFETY: this script MUTATES the target — it creates a task through
   the real UI. Only point it at an isolated dev/test instance with
   fictional data, never at a deployment holding real tasks.

Usage: e2e-browser.py <base-url> [--headed]

Runs the real UI in Chromium: page load, task creation through the
modal, rendering, view switching, theme switching. Requires
`pip install playwright` and a Chromium build.
"""
import sys
import time

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://127.0.0.1:8086'
HEADED = '--headed' in sys.argv

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  ({detail})' if detail and not ok else ''))


def main():
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not HEADED)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))

        print('== 加载 ==')
        page.goto(BASE + '/todo/', wait_until='networkidle')
        check('页面标题', page.title() == 'y8yTODO')
        check('日历网格渲染', page.locator('.cal-cell').count() >= 28)
        check('视图 tab ×3', page.locator('.view-tab').count() == 3)

        print('== UI 创建任务 ==')
        page.locator('#fabAdd').click()
        page.locator('#inputTitle').fill('Linux端到端任务')
        page.locator('#inputDue').fill(time.strftime('%Y-%m-%d', time.localtime(time.time() + 86400 * 2)))
        page.locator('#modalActions button.btn-primary').click()
        page.wait_for_timeout(800)
        body = page.evaluate("fetch('/todo/api/tasks').then(r => r.text())")
        import json
        titles = [t['title'] for t in json.loads(body)['tasks']]
        check('任务经 UI 创建并持久化', 'Linux端到端任务' in titles, f'got {titles[:3]}')

        print('== 视图与主题 ==')
        page.locator('#viewTabList').click()
        page.wait_for_timeout(300)
        check('列表视图可见', page.locator('#viewList').is_visible())
        page.locator('#viewTabShopping').click()
        page.wait_for_timeout(300)
        check('购物视图可见', page.locator('#viewShopping').is_visible())
        page.locator('#settingsBtn').click()
        page.wait_for_timeout(200)
        page.locator('[data-y8y-theme="dark"]').first.click()
        page.wait_for_timeout(300)
        check('暗色主题生效',
              page.evaluate("document.documentElement.getAttribute('data-theme')") == 'dark')
        page.locator('[data-y8y-theme="light"]').first.click()
        page.wait_for_timeout(300)
        check('亮色主题生效',
              page.evaluate("document.documentElement.getAttribute('data-theme')") == 'light')

        real_errors = [e for e in errors if 'favicon' not in e.lower()]
        check('无 JS 运行时错误', not real_errors, f'{real_errors[:3]}')
        browser.close()

    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
