#!/usr/bin/env python3
"""Browser-level verification of the notification settings panel (V1).

⚠️ SAFETY: this script only reads and writes **notification preferences**
   (a side-car file in the data dir) — it never creates or mutates tasks.
   Still: point it at an isolated dev instance, never at a deployment
   holding real data.

Usage: browser_notify.py [base-url] [--headed]

Environment:
  Y8Y_NOTIFY_CHROME_PATH   explicit Chromium binary (optional; same
                           escape hatch as tests/browser_font_verify.py)

Checks:
  1. 设置面板里有通知分组
  2. 开关/渠道/提前提醒/摘要时间都能操作
  3. 保存后服务端真的存下来了 (页面内 fetch 校验)
  4. 重新打开面板能看到保存过的值 (跨会话持久化)
  5. 没有 JS 运行时错误

Requires: pip install playwright && playwright install chromium
"""
import json
import os
import sys
import time

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 and not sys.argv[1].startswith('--') \
    else 'http://127.0.0.1:8086'
HEADED = '--headed' in sys.argv
CHROME_PATH = os.environ.get('Y8Y_NOTIFY_CHROME_PATH', '').strip()

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  ({detail})' if detail and not ok else ''))


def main():
    from playwright.sync_api import sync_playwright
    errors = []
    with sync_playwright() as p:
        launch_kwargs = {'headless': not HEADED}
        if CHROME_PATH:
            launch_kwargs['executable_path'] = CHROME_PATH
        browser = p.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda e: errors.append(str(e)))

        print('== 加载 ==')
        page.goto(BASE + '/todo/', wait_until='networkidle')
        check('页面标题', page.title() == 'y8yTODO')

        # 幂等: 先把设置重置成一个已知基线, 免得上一轮跑过的残留状态影响断言。
        page.evaluate("""() => fetch('/todo/api/notification-settings', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: false, channels: [], lead_minutes: 30,
                                  daily_digest: false, digest_time: '08:00'})
        }).then(r => r.text())""")
        page.reload(wait_until='networkidle')
        page.wait_for_timeout(400)

        print('== 设置面板: 通知分组 ==')
        page.locator('#settingsBtn').click()
        page.wait_for_timeout(500)
        check('通知分组可见', page.locator('#notifyEnabledBtn').is_visible())
        check('渠道容器存在', page.locator('#notifyChannelsRow').count() == 1)
        check('提前提醒输入存在', page.locator('#notifyLeadMinutes').count() == 1)
        check('摘要时间输入存在', page.locator('#notifyDigestTime').count() == 1)

        print('== 保存设置 ==')
        # 默认是关闭的 → 点开
        before = page.locator('#notifyEnabledBtn').get_attribute('aria-pressed')
        check('默认未启用通知', before == 'false', f'got {before}')
        page.locator('#notifyEnabledBtn').click()
        check('可切换启用状态',
              page.locator('#notifyEnabledBtn').get_attribute('aria-pressed') == 'true')
        page.locator('#notifyDigestBtn').click()
        page.locator('#notifyLeadMinutes').fill('45')
        page.locator('#notifyDigestTime').fill('07:30')
        page.locator('#notifySaveBtn').click()
        page.wait_for_timeout(800)

        saved = json.loads(page.evaluate(
            "fetch('/todo/api/notification-settings').then(r => r.text())"))
        settings = saved.get('settings', {})
        check('服务端已保存 enabled', settings.get('enabled') is True, str(settings))
        check('服务端已保存 lead_minutes', settings.get('lead_minutes') == 45, str(settings))
        check('服务端已保存 daily_digest', settings.get('daily_digest') is True, str(settings))
        check('服务端已保存 digest_time', settings.get('digest_time') == '07:30', str(settings))

        print('== 重新打开面板 ==')
        # 面板还开着 → 先关掉, 再重新打开, 验证跨会话持久化
        page.keyboard.press('Escape')
        page.wait_for_timeout(400)
        check('面板已关闭', 'open' not in (page.locator('#settingsOverlay').get_attribute('class') or ''))
        page.locator('#settingsBtn').click()
        page.wait_for_timeout(600)
        check('重开后仍是启用状态',
              page.locator('#notifyEnabledBtn').get_attribute('aria-pressed') == 'true')
        check('重开后提前提醒仍是 45',
              page.locator('#notifyLeadMinutes').input_value() == '45',
              page.locator('#notifyLeadMinutes').input_value())
        check('重开后摘要时间仍是 07:30',
              page.locator('#notifyDigestTime').input_value() == '07:30',
              page.locator('#notifyDigestTime').input_value())

        print('== 还原 ==')
        page.locator('#notifyEnabledBtn').click()
        page.locator('#notifySaveBtn').click()
        page.wait_for_timeout(600)
        restored = json.loads(page.evaluate(
            "fetch('/todo/api/notification-settings').then(r => r.text())"))
        check('已还原为未启用', restored.get('settings', {}).get('enabled') is False)

        real_errors = [e for e in errors if 'favicon' not in e.lower()]
        check('无 JS 运行时错误', not real_errors, f'{real_errors[:3]}')
        browser.close()

    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
