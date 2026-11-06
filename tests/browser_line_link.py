#!/usr/bin/env python3
"""Browser-level verification of the LINE self-service binding UI (V1).

Self-contained: creates an isolated dev data dir, writes a **fresh**
credential-free capability marker (as the webhook service would), starts
``serve.py`` in dev mode on an ephemeral loopback port, drives the real UI
with Playwright, then stops everything. It never touches a real deployment,
never sends a real LINE message, and uses only fictional ids.

Usage:
    python3 tests/browser_line_link.py [--headed] [--shots DIR]

Requires: pip install playwright && playwright install chromium
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
import line_link                                                      # noqa: E402
import line_webhook                                                   # noqa: E402

HEADED = '--headed' in sys.argv
SHOTS = None
if '--shots' in sys.argv:
    SHOTS = Path(sys.argv[sys.argv.index('--shots') + 1])
    SHOTS.mkdir(parents=True, exist_ok=True)

LINE_ID = 'U' + 'a' * 32
PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f'  ({detail})' if detail and not ok else ''))


def shot(page, name):
    if SHOTS:
        # LINE 分组在设置弹窗末尾, 先把滚动容器滚到它, 截图才有内容。
        page.locator('#lineLinkGroup').scroll_into_view_if_needed()
        page.wait_for_timeout(150)
        page.screenshot(path=str(SHOTS / f'{name}.png'), full_page=False)


def main():
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix='y8y-line-link-browser-'))
    data = tmp / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.publish_capabilities(now=time.time())

    env = dict(os.environ)
    for key in [k for k in env if k.startswith('Y8Y_')]:
        env.pop(key)
    # serve.py 用固定端口, 先向 OS 借一个空闲端口。
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    env.update({
        'Y8Y_DEV_MODE': '1',
        'Y8Y_DEV_USER': 'dev',
        'Y8Y_DATA_DIR': str(data),
        'Y8Y_PORT': str(port),
        'Y8Y_LINE_BOT_ID': '@linebrowser',
    })
    base = f'http://127.0.0.1:{port}'

    proc = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'serve.py')],
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        import urllib.request
        for _ in range(60):
            try:
                urllib.request.urlopen(base + '/todo/', timeout=1).read(1)
                break
            except Exception:                                      # noqa: BLE001
                time.sleep(0.15)
        else:
            raise RuntimeError('dev server did not start')

        errors = []
        with sync_playwright() as p:
            launch = {'headless': not HEADED}
            chrome = os.environ.get('Y8Y_NOTIFY_CHROME_PATH', '').strip()
            if chrome:
                launch['executable_path'] = chrome
            browser = p.chromium.launch(**launch)
            context = browser.new_context(viewport={'width': 1280, 'height': 900},
                                          permissions=['clipboard-read', 'clipboard-write'])
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))

            print('== 加载 / 未绑定态 ==')
            page.goto(base + '/todo/', wait_until='networkidle')
            page.locator('#settingsBtn').click()
            page.wait_for_timeout(700)
            check('LINE 分组可见', page.locator('#lineLinkGroup').is_visible())
            check('状态显示未绑定', '未绑定' in page.locator('#lineLinkState').inner_text(),
                  page.locator('#lineLinkState').inner_text())
            check('绑定按钮可用', page.locator('#lineLinkStartBtn').is_enabled())
            shot(page, '01-unbound')

            print('== 绑定码 / 进度态 ==')
            page.locator('#lineLinkStartBtn').click()
            # 等异步 POST 真的回来 (不能靠固定 sleep, 会有竞态)
            page.locator('#lineLinkPending').wait_for(state='visible', timeout=8000)
            code = page.locator('#lineLinkCode').inner_text().strip()
            check('显示绑定码', bool(code) and len(code) >= 20, code[:12])
            check('显示进度', page.locator('#lineLinkProgress').is_visible())
            check('打开 LINE 深链',
                  (page.locator('#lineLinkOpenBtn').get_attribute('href') or '')
                  .startswith('https://line.me/R/ti/p/'))
            check('复制按钮可用', page.locator('#lineLinkCopyBtn').is_enabled())
            shot(page, '02-pending')

            print('== 模拟已验签 Webhook 写入绑定 → 自动变已连接 ==')
            store.set_binding('dev', LINE_ID, now=time.time())
            for name, _data in store._entries('code-'):
                store._unlink(store._path(name))
            page.locator('#lineLinkBound').wait_for(state='visible', timeout=8000)
            state_text = page.locator('#lineLinkState').inner_text()
            check('状态自动变已连接', '已连接' in state_text, state_text)
            check('显示脱敏 LINE ID', '…' in state_text and LINE_ID not in state_text,
                  state_text)
            check('测试通知按钮可见', page.locator('#lineLinkTestBtn').is_visible())
            check('解绑按钮可见', page.locator('#lineLinkUnlinkBtn').is_visible())
            check('更换按钮可见', page.locator('#lineLinkRebindBtn').is_visible())
            shot(page, '03-bound')

            print('== 解绑 (Web 只排队, 通知侧写墓碑) ==')
            page.on('dialog', lambda d: d.accept())
            page.locator('#lineLinkUnlinkBtn').click()
            page.wait_for_function(
                "document.getElementById('lineLinkState').textContent.includes('正在解除绑定')",
                timeout=8000)
            check('解绑进入过渡态',
                  '正在解除绑定' in page.locator('#lineLinkState').inner_text())
            check('过渡态明确告知通知已停发',
                  '已停止' in page.locator('#lineLinkState').inner_text(),
                  page.locator('#lineLinkState').inner_text())
            check('过渡态提示旧配置不会恢复',
                  '不会恢复' in page.locator('#lineLinkHint').inner_text(),
                  page.locator('#lineLinkHint').inner_text())
            check('Web 只写了请求, 权威绑定仍在', store.binding('dev') is not None)
            check('Web 没有写解绑墓碑', store.unlinked('dev') is None)

            # 模拟常驻 Webhook 服务的后台线程消费解绑请求
            from notifications import NotifyConfig
            cfg = NotifyConfig({
                'Y8Y_DATA_DIR': str(data),
                'Y8Y_NOTIFY_LINE_CHANNEL_SECRET': 'fake-secret-for-browser-test',
                'Y8Y_NOTIFY_LINE_TOKEN': 'fake-token-for-browser-test',
            })
            app = line_webhook.WebhookApp(cfg, store=store)
            app.stop.set()
            app.consume_requests()

            page.locator('#lineLinkUnboundActions').wait_for(state='visible', timeout=8000)
            page.wait_for_function(
                "document.getElementById('lineLinkState').textContent.includes('已解绑')",
                timeout=8000)
            state_text = page.locator('#lineLinkState').inner_text()
            check('解绑后显示为「已解绑」而非「从未绑定」', '已解绑' in state_text,
                  state_text)
            check('界面提示旧配置不会恢复',
                  '不会再' in page.locator('#lineLinkHint').inner_text(),
                  page.locator('#lineLinkHint').inner_text())
            check('服务端绑定已删除', store.binding('dev') is None)
            check('服务端写入持久解绑墓碑', store.unlinked('dev') is not None)
            check('手机/桌面端可重新绑定',
                  page.locator('#lineLinkStartBtn').is_enabled())

            print('== 手机视口 (390x844) ==')
            page.wait_for_timeout(2600)                # 等解绑 toast 消失
            page.set_viewport_size({'width': 390, 'height': 844})
            page.wait_for_timeout(400)
            check('手机端绑定按钮可见', page.locator('#lineLinkStartBtn').is_visible())
            # 绑定按钮必须真的落在可视区域内 (弹窗可滚动, 但要能找到它)
            page.locator('#lineLinkStartBtn').scroll_into_view_if_needed()
            check('手机端绑定按钮在视口内',
                  page.locator('#lineLinkStartBtn').bounding_box() is not None)
            overflow = page.evaluate(
                'document.documentElement.scrollWidth > document.documentElement.clientWidth + 1')
            check('无横向溢出', not overflow)
            shot(page, '04-mobile')

            real_errors = [e for e in errors if 'favicon' not in e.lower()]
            check('无 JS 运行时错误', not real_errors, str(real_errors[:3]))
            context.close()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    if SHOTS:
        print(f'截图目录: {SHOTS}')
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
