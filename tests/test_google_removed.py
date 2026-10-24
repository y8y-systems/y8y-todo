"""P5.5: the Google Calendar integration is REMOVED (user decision 2026-10).

These tests pin the post-removal contract:
1. The server runs with NO Google SDK and no google_calendar module;
   no background sync worker is ever spawned; no outbound network.
2. Every /todo/api/google-calendar/* path answers 410 Gone (before
   auth), never 200 — the feature cannot be triggered from UI or script.
3. The frontend (index.html) renders no Google Calendar settings entry
   and issues no google-calendar requests.
4. Historical data is preserved untouched: the google_calendar/
   directory files and task_uid / gcal_event_id frontmatter fields
   survive startup, edits, completion and other tasks' deletion.
5. Ordinary task operations (create/edit/complete/restore/delete,
   lists, recurring) are unaffected.

All data is fictional; no real Google account or token is involved.
"""
import hashlib
import json
import socket
import sys
import threading
from pathlib import Path

import pytest

from conftest import Client, _make_server, _stop_server
from legacy_fixture import ALICE

ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}

SRC_ROOT = Path(__file__).resolve().parent.parent / 'src'

GOOGLE_PATHS = (
    ('GET', '/todo/api/google-calendar/status'),
    ('GET', '/todo/api/google-calendar/oauth/callback'),
    ('GET', '/todo/api/google-calendar/initial-preview'),
    ('GET', '/todo/api/google-calendar/oauth/start'),
    ('POST', '/todo/api/google-calendar/oauth/start'),
    ('POST', '/todo/api/google-calendar/initial-apply'),
)


# ── 1. 模块与运行时: 无 SDK / 无 worker ─────────────────────────────────

def test_server_starts_without_google_module_or_sdk(server):
    serve, httpd, client, tmp = server
    # serve.py 不再 import google_calendar
    assert not hasattr(serve, 'GOOGLE_AUTH')
    assert not hasattr(serve, 'GOOGLE_CALLBACK_URI')
    # 没有任何 _gcal_* 残留机制
    gcal_names = [n for n in dir(serve)
                  if n.startswith('_gcal') or 'GCAL' in n.upper()]
    assert not gcal_names, f'gcal machinery left behind: {gcal_names}'
    # 没有同步 worker 线程
    workers = [t.name for t in threading.enumerate() if 'gcal' in t.name.lower()]
    assert not workers, f'google sync worker running: {workers}'
    # google_calendar 模块确实不在 sys.modules (文件已删除)
    assert 'google_calendar' not in sys.modules


def test_no_google_sdk_installed_in_test_env():
    """结构性保证: 测试环境没有 Google SDK — 任何真实调用都会 ImportError。"""
    for mod in ('google', 'googleapiclient', 'google_auth_oauthlib'):
        try:
            __import__(mod)
        except ImportError:
            continue
        raise AssertionError(f'{mod} present; the no-network guarantee is weakened')


# ── 2. API: 所有遗留端点 410 Gone (先于鉴权) ────────────────────────────

@pytest.mark.parametrize('method,path', GOOGLE_PATHS)
def test_google_endpoints_return_410_without_auth(server, method, path):
    """无身份头也必须 410 — 拦截先于鉴权, 且永远不是 200。"""
    serve, httpd, client, tmp = server
    if method == 'GET':
        status, body = client.get(path)
    else:
        status, body = client.post(path, body={'confirm': True})
    assert status == 410, f'{method} {path} → {status}'
    assert 'removed' in body.get('error', '')


@pytest.mark.parametrize('method,path', GOOGLE_PATHS)
def test_google_endpoints_return_410_with_auth(server, method, path):
    serve, httpd, client, tmp = server
    if method == 'GET':
        status, body = client.get(path, headers=ALICE)
    else:
        status, body = client.post(path, body={'confirm': True}, headers=ALICE)
    assert status == 410, f'{method} {path} (auth) → {status}'


def test_oauth_callback_with_code_is_inert(server):
    """真实 Google 跳回的回调 (带 code/state) 同样 410, 不交换 token。"""
    serve, httpd, client, tmp = server
    status, body = client.get(
        '/todo/api/google-calendar/oauth/callback?code=fake-code&state=fake-state',
        headers=ALICE)
    assert status == 410
    assert 'ya29' not in json.dumps(body, ensure_ascii=False)


# ── 3. UI: 无 Google 设置入口 ───────────────────────────────────────────

def test_index_html_has_no_google_ui():
    html = (SRC_ROOT / 'index.html').read_text(encoding='utf-8')
    assert 'googleCalendarConnectBtn' not in html
    assert 'googleCalendarStatus' not in html
    assert 'loadGoogleCalendarStatus' not in html
    assert 'startGoogleCalendarOAuth' not in html
    assert '/todo/api/google-calendar/' not in html
    assert '连接 Google' not in html


# ── 4. 历史数据保全 ─────────────────────────────────────────────────────

def _tree_hashes(root):
    out = {}
    for p in sorted(root.rglob('*')):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_historical_google_files_untouched_by_lifecycle(legacy_server):
    """旧版遗留的 google_calendar/ 目录: 启动 + 全套读写后逐字节不变。"""
    serve, httpd, client, tmp = legacy_server
    gdir = tmp / 'data' / 'google_calendar'
    assert gdir.is_dir(), 'legacy fixture must ship the historical directory'
    before = _tree_hashes(gdir)
    assert before, 'fixture must contain historical google files'

    # 普通任务全生命周期 (他人任务的增删改不得波及历史目录)
    status, body = client.post('/todo/api/tasks',
                               body={'title': '普通任务', 'due': '2026-11-01'},
                               headers=ALICE)
    assert status == 201
    task_id = body['id']
    client.put(f'/todo/api/tasks/{task_id}', body={'title': '改名'}, headers=ALICE)
    client.delete(f'/todo/api/tasks/{task_id}', headers=ALICE)

    after = _tree_hashes(gdir)
    assert after == before, 'historical google_calendar/ files were modified'


def test_completion_keeps_gcal_fields_in_file(legacy_server):
    """完成任务不再弹出 gcal_event_id (同步已移除, 历史字段必须留下)。"""
    serve, httpd, client, tmp = legacy_server
    status, _ = client.post('/todo/api/tasks/gcal同步任务.md/done', headers=ALICE)
    assert status == 200
    done_files = list((tmp / 'data' / 'done').rglob('*.md'))
    archived = [p for p in done_files
                if 'gcal_event_id: fakeeventid123' in p.read_text(encoding='utf-8')]
    assert archived, 'completed task lost its historical gcal fields'


def test_recurring_roll_keeps_gcal_fields(legacy_server):
    """重复任务推进到下一期: task_uid/gcal_event_id 原样保留 (旧行为会 pop)。"""
    serve, httpd, client, tmp = legacy_server
    # 给重复任务写下历史字段, 再完成它 → 推进下一期
    rec = tmp / 'data' / 'recurring' / '月度复盘.md'
    text = rec.read_text(encoding='utf-8')
    text = text.replace('---\n', '---\ntask_uid: uid-rec-1\ngcal_event_id: evt-rec-1\n', 1)
    rec.write_text(text, encoding='utf-8')
    status, _ = client.post('/todo/api/tasks/月度复盘.md/done', headers=ALICE)
    assert status == 200
    text = rec.read_text(encoding='utf-8')
    assert 'task_uid: uid-rec-1' in text
    assert 'gcal_event_id: evt-rec-1' in text


# ── 5. 网络隔离: 全套生命周期内没有任何出网请求 ─────────────────────────

def test_full_lifecycle_makes_no_outbound_connections(server, monkeypatch):
    """把非回环 connect 全部变成异常; 任何 Google 调用都会让测试失败。"""
    serve, httpd, client, tmp = server
    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError(f'outbound connection attempted: {address}')
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)

    status, body = client.post('/todo/api/tasks',
                               body={'title': '隔离任务', 'due': '2026-11-02'},
                               headers=ALICE)
    assert status == 201
    task_id = body['id']
    assert client.put(f'/todo/api/tasks/{task_id}', body={'title': '改'},
                      headers=ALICE)[0] == 200
    assert client.put(f'/todo/api/tasks/{task_id}', body={'completed': True},
                      headers=ALICE)[0] == 200
    assert client.delete(f'/todo/api/tasks/{task_id}', headers=ALICE)[0] == 200
    assert client.get('/todo/api/lists', headers=ALICE)[0] == 200


# ── 6. 普通功能不受影响 ─────────────────────────────────────────────────

def test_normal_task_operations_unaffected(legacy_server):
    """历史数据集上的常规读写: 权限/共享/软隐藏语义不回退。"""
    serve, httpd, client, tmp = legacy_server
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert status == 200
    titles = [t['title'] for t in body['tasks']]
    assert 'gcal同步任务' in titles          # 历史同步任务仍可见
    assert '共享大扫除' in titles             # shared 正常
    assert 'legacy-agent遗留' not in titles       # 软隐藏保持
