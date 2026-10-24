"""Authentication: dev mode, trusted-proxy header, X-Internal-Token."""
import json
from pathlib import Path

import pytest

from conftest import Client, _make_server, _stop_server


def _run_isolated(tmp_path, env):
    """Start a server with a fully custom environment (used for tests
    that need different trusted-proxy / token setups than the default
    fixture). Returns (serve_module, httpd, client)."""
    env = dict(env)
    env['Y8Y_DATA_DIR'] = str(tmp_path / 'data')
    serve, httpd, base = _make_server(tmp_path, env)
    return serve, httpd, Client(base)


def test_dev_mode_single_user_no_credentials(dev_server):
    serve, httpd, client, tmp = dev_server
    status, body = client.get('/todo/api/tasks')
    assert status == 200
    assert body['current_user'] == 'dev'
    # Even a forged identity header is ignored in dev mode (loopback only).
    status, body = client.get('/todo/api/tasks',
                              headers={'Cf-Access-Authenticated-User-Email': 'someone@evil.example'})
    assert status == 200
    assert body['current_user'] == 'dev'


def test_unauthenticated_requests_rejected(server):
    serve, httpd, client, tmp = server
    for method, path in [
        ('get', '/todo/api/tasks'),
        ('post', '/todo/api/tasks'),
        ('get', '/todo/api/lists'),
        ('get', '/todo/api/persistent-projects'),
    ]:
        status, _ = getattr(client, method)(path)
        assert status == 401, f'{method.upper()} {path} must require auth'


def test_identity_header_trusted_from_trusted_proxy(server):
    serve, httpd, client, tmp = server
    status, body = client.get('/todo/api/tasks',
                              headers={'Cf-Access-Authenticated-User-Email': 'alice@example.com'})
    assert status == 200
    assert body['current_user'] == 'alice'


def test_identity_header_ignored_from_untrusted_source(tmp_path):
    """A public client connecting directly and forging the identity header
    must NOT be authenticated — the header is only honored from trusted
    proxy addresses."""
    serve, httpd, client = _run_isolated(tmp_path, {
        'Y8Y_USERS': 'alice@example.com=alice',
        'Y8Y_TRUSTED_PROXIES': '10.9.9.9',   # test client (127.0.0.1) NOT trusted
    })
    try:
        status, _ = client.get('/todo/api/tasks',
                               headers={'Cf-Access-Authenticated-User-Email': 'alice@example.com'})
        assert status == 401, 'forged header from untrusted source must be rejected'
    finally:
        _stop_server(serve, httpd)


def test_unknown_email_rejected(server):
    serve, httpd, client, tmp = server
    status, _ = client.get('/todo/api/tasks',
                           headers={'Cf-Access-Authenticated-User-Email': 'nobody@example.com'})
    assert status == 401


def test_internal_token_authentication(tmp_path):
    """y8ysystems-style machine auth: token from internal_tokens.json.
    The token is a credential, so it is honored from any source — even
    when the identity-header path is closed (untrusted client)."""
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'internal_tokens.json').write_text(json.dumps({'alice': 'secret-token-123'}))
    serve, httpd, client = _run_isolated(tmp_path, {
        'Y8Y_USERS': 'alice@example.com=alice',
        'Y8Y_TRUSTED_PROXIES': '10.9.9.9',   # header path closed
    })
    try:
        status, body = client.get('/todo/api/tasks',
                                  headers={'X-Internal-Token': 'secret-token-123'})
        assert status == 200
        assert body['current_user'] == 'alice'
        status, _ = client.get('/todo/api/tasks',
                               headers={'X-Internal-Token': 'wrong-token'})
        assert status == 401
    finally:
        _stop_server(serve, httpd)


def test_owner_isolation_between_users(server):
    serve, httpd, client, tmp = server
    alice = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
    bob = {'Cf-Access-Authenticated-User-Email': 'bob@example.com'}
    status, body = client.post('/todo/api/tasks',
                               body={'title': 'alice私有', 'due': '2026-10-20'},
                               headers=alice)
    assert status == 201
    status, body = client.post('/todo/api/tasks',
                               body={'title': '共享任务', 'due': '2026-10-20', 'owner': 'shared'},
                               headers=alice)
    assert status == 201
    # Bob sees only the shared task, not alice's private one.
    status, body = client.get('/todo/api/tasks', headers=bob)
    titles = [t['title'] for t in body['tasks']]
    assert '共享任务' in titles
    assert 'alice私有' not in titles
    # Alice sees both.
    status, body = client.get('/todo/api/tasks', headers=alice)
    titles = [t['title'] for t in body['tasks']]
    assert 'alice私有' in titles and '共享任务' in titles


def test_cannot_create_task_for_other_user(server):
    serve, httpd, client, tmp = server
    alice = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
    status, _ = client.post('/todo/api/tasks',
                            body={'title': 'x', 'owner': 'bob'}, headers=alice)
    assert status == 403


# ── 公开版部署安全加固测试 (开源审计 O2-B) ─────────────────────────────
#
# 信任判定的依据是 **TCP 对端地址** (self.client_address), 不是任何可伪造
# 的 header — 这些测试把这一点钉死, 供陌生部署者/贡献者参考。

def test_forwarded_for_spoofing_cannot_grant_trust(tmp_path):
    """X-Forwarded-For 等转发头不能把不受信的客户端伪装成受信代理。

    公开部署的安全边界: 身份头只在请求**真实来自** Y8Y_TRUSTED_PROXIES
    中的地址时才被采信; 客户端自称"经由受信代理转发"不构成信任。
    """
    serve, httpd, client = _run_isolated(tmp_path, {
        'Y8Y_USERS': 'alice@example.com=alice',
        'Y8Y_TRUSTED_PROXIES': '10.9.9.9',   # 测试客户端 (127.0.0.1) 不在列表
    })
    try:
        status, _ = client.get('/todo/api/tasks', headers={
            'Cf-Access-Authenticated-User-Email': 'alice@example.com',
            'X-Forwarded-For': '10.9.9.9',        # 伪造的转发链
            'X-Real-IP': '10.9.9.9',
            'Forwarded': 'for=10.9.9.9',
        })
        assert status == 401, 'spoofed forwarding headers must not grant trust'
    finally:
        _stop_server(serve, httpd)


def test_whitespace_only_internal_token_is_ignored(server):
    """空白/纯空格 X-Internal-Token 不得命中任何身份 (strip 后为空 → 拒绝)。"""
    serve, httpd, client, tmp = server
    for token in ('', '   ', '\t'):
        status, _ = client.get('/todo/api/tasks',
                               headers={'X-Internal-Token': token})
        assert status == 401, f'token {token!r} must not authenticate'


def test_custom_auth_header_name_is_honored(tmp_path):
    """Y8Y_AUTH_HEADER 可配置 — 自定义头名同样只在受信代理侧生效。"""
    serve, httpd, client = _run_isolated(tmp_path, {
        'Y8Y_USERS': 'alice@example.com=alice',
        'Y8Y_AUTH_HEADER': 'X-Auth-Email',
        'Y8Y_TRUSTED_PROXIES': '10.9.9.9',       # 关闭默认头路径
    })
    try:
        # 默认头名不再被采信
        status, _ = client.get('/todo/api/tasks',
                               headers={'Cf-Access-Authenticated-User-Email': 'alice@example.com'})
        assert status == 401
        # 自定义头名 + 受信来源 → 认证成功
        serve2, httpd2, client2 = _run_isolated(tmp_path / 'trusted', {
            'Y8Y_USERS': 'alice@example.com=alice',
            'Y8Y_AUTH_HEADER': 'X-Auth-Email',
        })
        try:
            status, body = client2.get('/todo/api/tasks',
                                       headers={'X-Auth-Email': 'alice@example.com'})
            assert status == 200
            assert body['current_user'] == 'alice'
        finally:
            _stop_server(serve2, httpd2)
    finally:
        _stop_server(serve, httpd)


def test_identity_header_value_normalization(server):
    """身份头大小写/空白不敏感: ' Alice@Example.com ' 仍映射到 alice。"""
    serve, httpd, client, tmp = server
    status, body = client.get('/todo/api/tasks', headers={
        'Cf-Access-Authenticated-User-Email': '  Alice@Example.COM  '})
    assert status == 200
    assert body['current_user'] == 'alice'


# ── 身份头重复值 (代理误用"追加"语义的放大效应, 开源审计加固) ──────────
#
# get() 只取第一个值: 若代理把客户端伪造值放在前面再追加真实值 (Caddy
# 的 header_up +X 误配置), 采信第一个值 = 采信伪造身份 (实测确认)。
# 应用层硬化: 同值重复宽容, 冲突值一律视为未认证。

def _raw_get_with_headers(port, header_lines):
    import socket
    s = socket.create_connection(('127.0.0.1', port), timeout=5)
    req = 'GET /todo/api/tasks HTTP/1.1\r\nHost: 127.0.0.1\r\n'
    req += ''.join(h + '\r\n' for h in header_lines)
    req += 'Connection: close\r\n\r\n'
    s.sendall(req.encode())
    data = b''
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, body = data.partition(b'\r\n\r\n')
    status = int(head.split(b'\r\n')[0].split()[1])
    try:
        return status, json.loads(body)
    except Exception:
        return status, {}


def test_conflicting_duplicate_identity_header_rejected(server):
    """身份头出现两次且值冲突 → 未认证 (防"追加"型代理误配置放大伪造)。"""
    serve, httpd, client, tmp = server
    from urllib.parse import urlparse
    port = urlparse(client.base).port
    H = 'Cf-Access-Authenticated-User-Email: '
    # 两种顺序都必须拒绝 (不能依赖值的排列顺序)
    st1, _ = _raw_get_with_headers(port, [H + 'bob@example.com', H + 'alice@example.com'])
    st2, _ = _raw_get_with_headers(port, [H + 'alice@example.com', H + 'bob@example.com'])
    assert st1 == 401 and st2 == 401


def test_identical_duplicate_identity_header_tolerated(server):
    """同值重复 (某些代理链会这样) → 仍然按该身份认证。"""
    serve, httpd, client, tmp = server
    from urllib.parse import urlparse
    port = urlparse(client.base).port
    H = 'Cf-Access-Authenticated-User-Email: '
    st, body = _raw_get_with_headers(port, [H + 'alice@example.com', H + 'alice@example.com'])
    assert st == 200
    assert body['current_user'] == 'alice'


def test_empty_identity_header_rejected(server):
    """受信代理发来空身份头 → 未认证 (空值不映射任何身份)。"""
    serve, httpd, client, tmp = server
    status, _ = client.get('/todo/api/tasks',
                           headers={'Cf-Access-Authenticated-User-Email': ''})
    assert status == 401


# ── O3-P1: 显示偏好与身份解耦 / 'self' owner 语义 ──────────────────────

def test_preferences_reported_per_identity(server):
    """GET /todo/api/tasks 带上每用户显示偏好 (中国节假日), 与身份解耦。"""
    serve, httpd, client, tmp = server
    alice = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
    bob = {'Cf-Access-Authenticated-User-Email': 'bob@example.com'}
    status, body = client.get('/todo/api/tasks', headers=alice)
    assert status == 200
    assert body['preferences'] == {'cn_holidays': True}
    status, body = client.get('/todo/api/tasks', headers=bob)
    assert status == 200
    assert body['preferences'] == {'cn_holidays': True}


@pytest.mark.server_env({'Y8Y_CN_HOLIDAY_USERS': 'alice'})
def test_preferences_honor_user_list(server):
    """Y8Y_CN_HOLIDAY_USERS 控制默认值: alice 开, bob 关。"""
    serve, httpd, client, tmp = server
    status, body = client.get('/todo/api/tasks',
                              headers={'Cf-Access-Authenticated-User-Email': 'alice@example.com'})
    assert body['preferences']['cn_holidays'] is True
    status, body = client.get('/todo/api/tasks',
                              headers={'Cf-Access-Authenticated-User-Email': 'bob@example.com'})
    assert body['preferences']['cn_holidays'] is False


def test_task_owner_accepts_self_alias(server):
    """'self' 在任务创建/编辑时解析为当前用户 (与列表系统一致, O3-P1)。"""
    serve, httpd, client, tmp = server
    alice = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
    status, body = client.post('/todo/api/tasks',
                               body={'title': 'self任务', 'owner': 'self'}, headers=alice)
    assert status == 201
    status, body = client.get('/todo/api/tasks', headers=alice)
    task = [t for t in body['tasks'] if t['title'] == 'self任务'][0]
    assert task['owner'] == 'alice'
    # 编辑时同样接受 'self'
    status, _ = client.put(f"/todo/api/tasks/{task['id']}",
                           body={'owner': 'self'}, headers=alice)
    assert status == 200
    status, body = client.get('/todo/api/tasks', headers=alice)
    task = [t for t in body['tasks'] if t['title'] == 'self任务'][0]
    assert task['owner'] == 'alice'


def test_task_owner_self_alias_still_rejects_other_users(server):
    """'self' 只是别名 — 指向他人的 owner 依旧 403。"""
    serve, httpd, client, tmp = server
    alice = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
    status, _ = client.post('/todo/api/tasks',
                            body={'title': 'x', 'owner': 'bob'}, headers=alice)
    assert status == 403


# ── O3-N1: 公开代理剥离机器 Token (文档化契约的测试锁定) ─────────────────
#
# 应用认证优先级: Internal Token → 代理身份头 (有意设计, 未改)。公开部署
# 的安全边界在代理侧: 公开入口必须剥离客户端 X-Internal-Token (docs/
# deployment.md §3), 否则浏览器用户挟带他人的有效 Token 即可冒充他人。
# 本测试用 stdlib 模拟"文档化的公开代理"锁定该契约; 真实 Caddy/nginx 的
# E2E 见 docs/acceptance.md §7 与 tests/browser_multiuser.py。

def _start_public_proxy(app_port, authenticated_email):
    """模拟文档化的公开认证代理: 注入身份头 (认证结果), 剥离机器 Token。"""
    import http.server
    import threading
    import urllib.request

    class ProxyHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            req = urllib.request.Request(
                f'http://127.0.0.1:{app_port}{self.path}', method='GET')
            # 文档行为 1: 身份来自认证结果, 不是客户端请求
            req.add_header('Cf-Access-Authenticated-User-Email', authenticated_email)
            # 文档行为 2: X-Internal-Token 不被转发 (公开入口剥离)
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    req, timeout=10) as r:
                body = r.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(('127.0.0.1', 0), ProxyHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def test_public_proxy_strips_machine_token(server):
    """Bob 登录后携带 Alice 的有效 Token 走公开代理 → 仍是 bob。

    同时证明该 Token 本身有效 (直连 app 是 alice) — 所以保护来自代理
    剥离, 不是 Token 失效。
    """
    import urllib.request
    from urllib.parse import urlparse
    serve, httpd, client, tmp = server
    app_port = urlparse(client.base).port
    # Alice 的有效机器 Token (写入服务器内存 token 表, 无需重启)
    alice_token = 'public-proxy-alice-token-fictional-001'
    serve.INTERNAL_TOKENS['alice'] = alice_token
    proxy, proxy_port = _start_public_proxy(app_port, 'bob@example.com')
    try:
        # 1) 直连 app + Token → alice (Token 有效; 机器客户端路径不变)
        req = urllib.request.Request(
            f'http://127.0.0.1:{app_port}/todo/api/tasks',
            headers={'X-Internal-Token': alice_token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                req, timeout=10) as r:
            assert json.loads(r.read())['current_user'] == 'alice'
        # 2) 经公开代理 + 同一 Token → bob (代理剥离了 Token)
        req = urllib.request.Request(
            f'http://127.0.0.1:{proxy_port}/todo/api/tasks',
            headers={'X-Internal-Token': alice_token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                req, timeout=10) as r:
            assert json.loads(r.read())['current_user'] == 'bob'
        # 3) 经公开代理 + 伪造身份头 → bob (认证结果覆盖)
        req = urllib.request.Request(
            f'http://127.0.0.1:{proxy_port}/todo/api/tasks',
            headers={'Cf-Access-Authenticated-User-Email': 'alice@example.com'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                req, timeout=10) as r:
            assert json.loads(r.read())['current_user'] == 'bob'
    finally:
        proxy.shutdown()
        proxy.server_close()
        serve.INTERNAL_TOKENS.pop('alice', None)


def _nginx_blocks(md_text):
    """Yield the contents of every ```nginx fenced block in md_text."""
    lines = md_text.splitlines()
    inside, buf = False, []
    for ln in lines:
        if ln.strip().startswith('```nginx'):
            inside, buf = True, []
            continue
        if inside and ln.strip() == '```':
            yield '\n'.join(buf)
            inside = False
            continue
        if inside:
            buf.append(ln)


def _brace_block(text, open_idx):
    """Given text and the index of an opening '{', return the block body."""
    depth, i = 0, open_idx
    while i < len(text):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:i]
        i += 1
    return text[open_idx + 1:]


def _find_all(text, needle):
    idx, out = 0, []
    while True:
        idx = text.find(needle, idx)
        if idx == -1:
            return out
        out.append(idx)
        idx += 1


def test_documented_nginx_config_strips_token_in_same_block():
    """Guard the O3-N1 nginx misconfiguration found by real E2E (nginx 1.26.2).

    nginx inherits server-level proxy_set_header directives ONLY when the
    location defines none of its own. A 'defaults at server level, identity
    override in location' split therefore silently drops the token
    stripping — measured: a browser user carrying someone else's valid
    token was identified as that user through the proxy. Every location
    that sets proxy_set_header must strip X-Internal-Token in the SAME
    block; a server-level stripping next to such a location is the trap.
    """
    md = (Path(__file__).resolve().parent.parent / 'docs' / 'deployment.md').read_text(encoding='utf-8')
    nginx_blocks = list(_nginx_blocks(md))
    assert nginx_blocks, 'no ```nginx blocks found in docs/deployment.md'
    checked = 0
    for block in nginx_blocks:
        if 'X-Internal-Token' not in block:
            continue
        checked += 1
        # server-level stripping coexisting with a location that sets its
        # own proxy_set_header is exactly the broken pattern
        server_open = block.find('server {')
        if server_open != -1:
            server_body = _brace_block(block, block.find('{', server_open))
            if 'proxy_set_header X-Internal-Token' in server_body:
                for loc_open in _find_all(server_body, 'location {'):
                    loc_body = _brace_block(server_body, loc_open)
                    if 'proxy_set_header' in loc_body:
                        raise AssertionError(
                            'docs/deployment.md: server-level token stripping '
                            'next to a location with its own proxy_set_header '
                            'is not inherited (nginx rule) — move the '
                            'stripping directive into the location block')
        # every location that sets proxy_set_header must strip the token
        # in the same block
        for loc_open in _find_all(block, 'location {'):
            loc_body = _brace_block(block, loc_open)
            if 'proxy_set_header' in loc_body:
                assert 'proxy_set_header X-Internal-Token "";' in loc_body, (
                    'docs/deployment.md: a location block sets proxy_set_header '
                    'but does not strip X-Internal-Token in the same block')
    assert checked >= 1, 'expected at least one nginx block mentioning X-Internal-Token'
