"""Multi-user browser regression for a public-style deployment (O3-P1).

Unlike tests/browser_verify.py (single dev user, direct access), this script
drives the real UI through a **stdlib authenticating reverse proxy** that
behaves exactly like the documented public-proxy config (see
docs/deployment.md §3):

  - HTTP Basic auth identifies alice / bob (fictional passwords)
  - the identity header is injected from the authentication result
  - a client-supplied X-Internal-Token is **stripped** (the documented
    public-proxy hardening, O3-N1) — the machine-token path is only for
    clients that reach the app directly (localhost / private network)

Verified in real Chromium:

  1. Alice and Bob are each identified as themselves (no identity bleed)
  2. per-user task isolation (alice's private task invisible to bob)
  3. shared tasks visible to both
  4. Chinese-holiday display is a **configurable feature**, not bound to a
     hardcoded username (the O3-P1 regression): alice sees CN holidays,
     can toggle them off locally, bob is unaffected
  5. list ownership toggle uses 'self' — un-sharing a list no longer
     submits another user's name / 403s (the O3-P1 functional bug)
  6. Bob carrying Alice's VALID internal token through the public proxy
     is still bob (O3-N1 contract), while the same token used directly
     against the app authenticates as alice (machine clients keep working)

Prereqs: playwright + chromium; fictional data only; the app runs on
loopback with an isolated data dir under /tmp.

Usage:
    python3 tests/browser_multiuser.py            # default ports 18087/18088
    Y8Y_BM_APP_PORT=19087 Y8Y_BM_PROXY_PORT=19088 python3 tests/browser_multiuser.py
"""
import base64
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

SRC = Path(__file__).resolve().parent.parent / 'src'
APP_PORT = int(os.environ.get('Y8Y_BM_APP_PORT', '18087'))
PROXY_PORT = int(os.environ.get('Y8Y_BM_PROXY_PORT', '18088'))
APP_BASE = f'http://127.0.0.1:{APP_PORT}'
PROXY_BASE = f'http://127.0.0.1:{PROXY_PORT}'

USERS = {
    'alice@example.com': 'alice-fictional-pw',
    'bob@example.com': 'bob-fictional-pw',
}
ALICE_TOKEN = 'multiuser-alice-token-fictional-001'

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f'  ({detail})' if detail and not ok else ''))


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


# ── stdlib authenticating proxy (documented public-proxy contract) ──────

class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Basic-auth in front of the app; injects the identity header from the
    authentication result and strips client-supplied X-Internal-Token."""

    def _basic_user(self):
        raw = self.headers.get('Authorization', '')
        if not raw.startswith('Basic '):
            return None
        try:
            decoded = base64.b64decode(raw[6:]).decode('utf-8')
        except Exception:
            return None
        email, sep, pw = decoded.partition(':')
        if sep and USERS.get(email) == pw:
            return email
        return None

    def _proxy(self):
        user = self._basic_user()
        if user is None:
            body = b'proxy auth required'
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="y8y-multiuser"')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        length = int(self.headers.get('Content-Length', 0) or 0)
        payload = self.rfile.read(length) if length else None
        req = urllib.request.Request(APP_BASE + self.path, data=payload, method=self.command)
        # Identity header from the AUTH RESULT — never from the client request.
        req.add_header('Cf-Access-Authenticated-User-Email', user)
        ctype = self.headers.get('Content-Type')
        if ctype:
            req.add_header('Content-Type', ctype)
        # X-Internal-Token is deliberately NOT forwarded: the public proxy
        # strips it (docs/deployment.md §3 hardening).
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=15) as resp:
                data, status = resp.read(), resp.status
                resp_ctype = resp.headers.get('Content-Type', 'application/octet-stream')
        except urllib.error.HTTPError as e:
            data, status = e.read(), e.code
            resp_ctype = e.headers.get('Content-Type', 'application/octet-stream')
        self.send_response(status)
        self.send_header('Content-Type', resp_ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _proxy

    def log_message(self, *args):
        pass


# ── app under test ──────────────────────────────────────────────────────

def start_app(data_dir):
    env = dict(os.environ)
    env.update({
        'Y8Y_DATA_DIR': str(data_dir),
        'Y8Y_BIND': '127.0.0.1',
        'Y8Y_PORT': str(APP_PORT),
        'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
        'Y8Y_TRUSTED_PROXIES': '127.0.0.1',
        'Y8Y_TIMEZONE': 'Asia/Tokyo',
    })
    proc = subprocess.Popen(
        [sys.executable, str(SRC / 'serve.py')],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    for _ in range(60):
        try:
            with socket.create_connection(('127.0.0.1', APP_PORT), timeout=0.5):
                return proc
        except OSError:
            if proc.poll() is not None:
                print(proc.stdout.read().decode('utf-8', 'replace'))
                raise SystemExit('app failed to start')
            time.sleep(0.25)
    raise SystemExit('app did not come up')


def goto_calendar_month(page, year_month):
    """Navigate the calendar to 'YYYY · MM' deterministically (from any date)."""
    for _ in range(48):
        title = page.locator('#calTitle').inner_text().strip()
        if title == year_month:
            return True
        cur_y, cur_m = (int(x) for x in title.replace(' ', '').split('·'))
        target_y, target_m = (int(x) for x in year_month.replace(' ', '').split('·'))
        if (target_y, target_m) < (cur_y, cur_m):
            page.locator('#calPrev').click()
        else:
            page.locator('#calNext').click()
        page.wait_for_timeout(120)
    return False


def api_current_user(page):
    return page.evaluate(
        "fetch('/todo/api/tasks').then(r => r.json()).then(d => d.current_user)")


def api_tasks(page):
    return page.evaluate(
        "fetch('/todo/api/tasks').then(r => r.json()).then(d => d.tasks.map(t => t.title))")


def wait_for_task(page, title, rounds=20):
    """轮询等待 title 出现在任务列表 (落库异步, 避免固定 sleep 抖动)。"""
    for _ in range(rounds):
        if title in api_tasks(page):
            return True
        page.wait_for_timeout(250)
    return False


def error_toast_visible(page):
    return page.evaluate(
        "document.getElementById('toast').classList.contains('show') && "
        "document.getElementById('toast').textContent.trim().startsWith('✗')")


def main():
    data_dir = Path(tempfile.mkdtemp(prefix='y8y-multiuser-'))
    # Alice's machine token (fictional) — proves the token itself works when
    # the client reaches the app directly, and is ignored through the proxy.
    (data_dir / 'internal_tokens.json').write_text(json.dumps({'alice': ALICE_TOKEN}))
    app = start_app(data_dir)
    proxy = http.server.ThreadingHTTPServer(('127.0.0.1', PROXY_PORT), _ProxyHandler)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    print(f'app={APP_BASE} proxy={PROXY_BASE} data={data_dir}')
    print('== 多用户浏览器回归 (Alice / Bob, 经认证代理) ==')

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            alice_ctx = browser.new_context(
                viewport={'width': 1280, 'height': 900},
                http_credentials={'username': 'alice@example.com',
                                  'password': USERS['alice@example.com']})
            bob_ctx = browser.new_context(
                viewport={'width': 1280, 'height': 900},
                http_credentials={'username': 'bob@example.com',
                                  'password': USERS['bob@example.com']})
            errors = []
            alice = alice_ctx.new_page()
            bob = bob_ctx.new_page()
            for pg in (alice, bob):
                pg.on('pageerror', lambda e: errors.append(str(e)))
                pg.on('console', lambda m: errors.append(m.text) if m.type == 'error' else None)

            # 1. 身份: 各自登录各自的身份 (O3-P1: 不再有 || 'alice' 兜底)
            alice.goto(PROXY_BASE + '/todo/', wait_until='networkidle')
            bob.goto(PROXY_BASE + '/todo/', wait_until='networkidle')
            check('Alice 身份为 alice (无 alice 兜底)', api_current_user(alice) == 'alice')
            check('Bob 身份为 bob', api_current_user(bob) == 'bob')

            # 2. 中国节假日 = 可配置功能 (O3-P1 核心回归, 不与用户名绑定)
            check('日历默认定位含国庆假的月份', goto_calendar_month(alice, '2026 · 10'))
            alice.locator('#settingsBtn').click()
            alice.wait_for_timeout(200)
            cn_btn = alice.locator('#cnHolidayBtn')
            check('中国节假日开关存在且默认开',
                  cn_btn.count() == 1 and cn_btn.get_attribute('aria-pressed') == 'true')
            alice.locator('#settingsOverlay .btn-cancel').click()
            alice.wait_for_timeout(200)
            cn_cells = alice.locator('.cal-cn-holiday-cell')
            check('Alice 日历显示中国节假日底纹 (国庆 10-01..10-07)',
                  cn_cells.count() >= 7, f'count={cn_cells.count()}')
            names = alice.evaluate(
                "Array.from(document.querySelectorAll('.cal-day-num .cal-holiday'))"
                ".map(e => e.textContent)")
            check('Alice 日历显示节日名 (国庆节)', '国庆节' in names, f'names={names[:6]}')
            workday_tags = alice.locator('.cal-workday')
            check('Alice 日历显示调休上班日「班」标签 (2026-10-10)',
                  workday_tags.count() >= 1, f'count={workday_tags.count()}')
            # 关掉 → 全部消失; 且不影响 Bob
            alice.locator('#settingsBtn').click()
            alice.wait_for_timeout(150)
            alice.locator('#cnHolidayBtn').click()
            alice.locator('#settingsOverlay .btn-cancel').click()
            alice.wait_for_timeout(250)
            check('关闭后节假日底纹消失', alice.locator('.cal-cn-holiday-cell').count() == 0)
            check('关闭后「班」标签消失', alice.locator('.cal-workday').count() == 0)
            check('Bob 的节假日显示不受 Alice 开关影响',
                  bob.locator('.cal-cn-holiday-cell').count() >= 7,
                  f'bob count={bob.locator(".cal-cn-holiday-cell").count()}')
            # 再打开 (本机覆盖持久化)
            alice.locator('#settingsBtn').click()
            alice.wait_for_timeout(150)
            alice.locator('#cnHolidayBtn').click()
            alice.locator('#settingsOverlay .btn-cancel').click()
            alice.wait_for_timeout(250)
            check('重新开启后恢复显示', alice.locator('.cal-cn-holiday-cell').count() >= 7)

            # 3. per-user 任务隔离 (UI 真实创建)
            alice.locator('#fabAdd').click()
            alice.locator('#inputTitle').fill('Alice私有任务')
            alice.locator('#inputDue').fill('2026-10-25')
            alice.locator('#modalActions button.btn-primary').click()
            alice.wait_for_timeout(800)
            check('Alice 私有任务已创建', wait_for_task(alice, 'Alice私有任务'))
            check('Bob 看不到 Alice 的私有任务', 'Alice私有任务' not in api_tasks(bob))
            # 轮询等待任务落库后再校验 owner (避免固定 sleep 的时序抖动)
            owner = None
            for _ in range(20):
                owner = alice.evaluate(
                    "fetch('/todo/api/tasks').then(r=>r.json()).then(d=>{"
                    "const t=d.tasks.find(t=>t.title==='Alice私有任务');"
                    "return t ? t.owner : null;})")
                if owner:
                    break
                alice.wait_for_timeout(250)
            check('任务 owner 为 alice (不是 alice)', owner == 'alice', f'owner={owner!r}')

            # 4. 共享任务双向可见
            alice.locator('#fabAdd').click()
            alice.locator('#inputTitle').fill('共享任务')
            alice.locator('#inputDue').fill('2026-10-26')
            alice.locator('#inputOwner').select_option('shared')
            alice.locator('#modalActions button.btn-primary').click()
            alice.wait_for_timeout(800)
            check('共享任务对 Alice 可见', wait_for_task(alice, '共享任务'))
            check('共享任务对 Bob 可见', wait_for_task(bob, '共享任务'))

            # 5. 列表归属切换走 'self' (O3-P1 功能缺陷回归)
            alice.locator('#viewTabShopping').click()
            alice.wait_for_timeout(300)
            alice.once('dialog', lambda d: d.accept('回归测试列表'))
            alice.locator('#listNewBtn').click()
            alice.wait_for_timeout(900)
            check('新列表已创建', '回归测试列表' in alice.evaluate(
                "Array.from(document.querySelectorAll('.project-menu-name')).map(e=>e.textContent)"))
            # 默认 shared → 切私有: 旧实现会提交 owner='alice' 被 403
            alice.locator('#listOwnerBtn').click()
            alice.wait_for_timeout(900)
            check('切换归属未出现错误提示 (无 403)', not error_toast_visible(alice))
            badge = alice.locator('#shoppingListOwner').inner_text()
            check('私有后徽标显示 alice (self 解析为当前用户)', 'alice' in badge, f'badge={badge!r}')
            check('Bob 看不到 Alice 的私有列表',
                  '回归测试列表' not in bob.evaluate(
                      "Array.from(document.querySelectorAll('.project-menu-name')).map(e=>e.textContent)"))
            # 再切回 shared (双向都通)
            alice.locator('#listOwnerBtn').click()
            alice.wait_for_timeout(900)
            check('切回共享无错误提示', not error_toast_visible(alice))
            check('切回共享后徽标为共享', '共享' in alice.locator('#shoppingListOwner').inner_text())
            bob.reload(wait_until='networkidle')   # Bob 拉取最新列表集合
            bob.wait_for_timeout(500)
            check('Bob 现在能看到共享列表',
                  '回归测试列表' in bob.evaluate(
                      "Array.from(document.querySelectorAll('.project-menu-name')).map(e=>e.textContent)"))

            # 6. 公开代理 + 机器 Token 边界 (O3-N1 契约, 浏览器侧)
            # Bob 携带 Alice 的有效 token 经公开代理 → 仍是 bob (代理已剥离)
            still_bob = bob.evaluate(
                "fetch('/todo/api/tasks', {headers: {'X-Internal-Token': %s}})"
                ".then(r => r.json()).then(d => d.current_user)" % json.dumps(ALICE_TOKEN))
            check('Bob 携带 Alice 有效 Token 过公开代理仍是 bob', still_bob == 'bob')
            # 伪造身份头同样无效 (代理用认证结果覆盖)
            forged = bob.evaluate(
                "fetch('/todo/api/tasks', {headers: "
                "{'Cf-Access-Authenticated-User-Email': 'alice@example.com'}})"
                ".then(r => r.json()).then(d => d.current_user)")
            check('Bob 伪造 Alice 身份头仍是 bob', forged == 'bob')
            # 同一 token 直连 app (机器客户端路径) → alice: 机器入口不受影响
            direct = urllib.request.Request(
                APP_BASE + '/todo/api/tasks',
                headers={'X-Internal-Token': ALICE_TOKEN})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(direct, timeout=10) as r:
                direct_user = json.loads(r.read())['current_user']
            check('同一 Token 直连 app 仍为 alice (机器客户端路径不变)', direct_user == 'alice')

            real_errors = [e for e in errors if 'favicon' not in e.lower()]
            check('两个会话均无 JS 运行时错误', not real_errors, f'{real_errors[:3]}')

            alice_ctx.close()
            bob_ctx.close()
            browser.close()
    finally:
        proxy.shutdown()
        app.terminate()
        try:
            app.wait(timeout=10)
        except Exception:
            app.kill()
        shutil.rmtree(data_dir, ignore_errors=True)

    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败')
    if FAIL:
        print('失败项:', FAIL)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
