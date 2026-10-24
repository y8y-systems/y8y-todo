"""P5.5: the fictional legacy fixture mirrors the REAL production data
directory shape found by the P5 read-only survey (alice/bob/shared tasks,
legacy-agent soft-hidden history, project subdirs, done archives, lists,
shopping, a stray `_fix_config.py`, a historical backup subdirectory,
Google Calendar config leftovers, and unknown auxiliary files).

These tests verify the new version:
- shows the right tasks to the right users (private/shared/soft-hidden)
- never deletes or rewrites unrelated historical files
- performs no unauthorized data migration (tree shape is stable)
- survives normal writes without collateral damage
- and that the P4 backup tool covers exactly the files that must be kept.

All data is fictional; nothing here comes from any real deployment.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from legacy_fixture import ALICE, BOB, MACHINE, build_legacy_data

SRC = Path(__file__).resolve().parent.parent
SCRIPT = SRC / 'scripts' / 'p4-rehearsal.py'


def _tree_hashes(root):
    """path → sha256 for every file under root (relative paths)."""
    out = {}
    for p in sorted(root.rglob('*')):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_fixture_covers_production_shapes(tmp_path):
    """P5 调查到的每一项真实形态都必须在 fixture 里存在。"""
    data = tmp_path / 'data'
    build_legacy_data(data)
    for rel in (
        'todo/买菜.md',                        # no owner → defaults to alice
        'todo/bob的预约.md',                    # bob private
        'todo/共享大扫除.md',                   # shared
        'todo/legacy-agent遗留.md',                # soft-hidden history
        'todo/装修/买油漆.md',                  # project subdir
        'todo/gcal同步任务.md',                 # task_uid + gcal_event_id
        'recurring/月度复盘.md',
        'done/2026-09-01/已完成的事.md',
        'lists/shopping/meta.json',
        'lists/旅行清单/22222222-2222-2222-2222-222222222222.md',
        'internal_tokens.json', 'persistent_projects.json',
        'google_calendar/client_secret.json',  # historical OAuth leftovers
        'google_calendar/tokens.json',
        'google_calendar/settings.json',
        'google_calendar/oauth_states.json',
        '_fix_config.py',                      # stray legacy ops script
        'backup/todolist-20260801/todo/旧备份里的任务.md',   # backup subdir
        'holidays_cache.json', 'misc_notes.txt',            # unknown aux files
    ):
        assert (data / rel).is_file(), f'missing production shape: {rel}'


def test_valid_users_see_the_right_tasks(legacy_server):
    serve, httpd, client, tmp = legacy_server
    status, alice_body = client.get('/todo/api/tasks', headers=ALICE)
    assert status == 200
    alice_titles = [t['title'] for t in alice_body['tasks']]
    assert '买菜' in alice_titles              # own + default-owner tasks
    assert '共享大扫除' in alice_titles         # shared visible
    assert 'bob的预约' not in alice_titles       # bob private hidden
    assert 'legacy-agent遗留' not in alice_titles    # soft-hidden

    status, bob_body = client.get('/todo/api/tasks', headers=BOB)
    assert status == 200
    bob_titles = [t['title'] for t in bob_body['tasks']]
    assert 'bob的预约' in bob_titles
    assert '共享大扫除' in bob_titles
    assert '买菜' not in bob_titles


def test_shared_task_writable_by_both_users(legacy_server):
    serve, httpd, client, tmp = legacy_server
    # alice 改 shared 任务 → 200
    status, _ = client.put('/todo/api/tasks/共享大扫除.md',
                           body={'title': '共享大扫除(已改)'}, headers=ALICE)
    assert status == 200
    # bob 也能改 (shared 语义)
    status, _ = client.put('/todo/api/tasks/共享大扫除.md',
                           body={'title': '共享大扫除(bob也改了)'}, headers=BOB)
    assert status == 200
    # bob 改 alice 的私人任务 → 403
    status, _ = client.put('/todo/api/tasks/买菜.md',
                           body={'title': '越权'}, headers=BOB)
    assert status == 403

def test_legacy_agent_files_stay_on_disk_but_hidden(legacy_server):
    """软隐藏 = 不可见但不删除: 文件必须原地保留。"""
    serve, httpd, client, tmp = legacy_server
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    titles = [t['title'] for t in body['tasks']]
    assert 'legacy-agent遗留' not in titles
    assert (tmp / 'data' / 'todo' / 'legacy-agent遗留.md').is_file(), \
        'soft-hidden file was deleted!'


def test_startup_and_lifecycle_do_not_touch_unrelated_files(legacy_server):
    """启动 + 常规读写后: 除被操作任务外, 整棵树逐字节不变。"""
    serve, httpd, client, tmp = legacy_server
    data = tmp / 'data'
    before = _tree_hashes(data)

    # 常规生命周期 (只碰一个全新任务)
    status, body = client.post('/todo/api/tasks',
                               body={'title': '新任务', 'due': '2026-12-01'},
                               headers=ALICE)
    assert status == 201
    task_id = body['id']
    assert client.put(f'/todo/api/tasks/{task_id}', body={'title': '改名'},
                      headers=ALICE)[0] == 200
    assert client.delete(f'/todo/api/tasks/{task_id}', headers=ALICE)[0] == 200

    after = _tree_hashes(data)
    # 新增 + 删除互相抵消后, 不得有任何既有文件被改动
    changed = {rel for rel in before if rel in after and before[rel] != after[rel]}
    assert not changed, f'unrelated historical files modified: {sorted(changed)}'
    # 历史杂物一个都不能少
    for rel in ('_fix_config.py', 'holidays_cache.json', 'misc_notes.txt',
                'google_calendar/tokens.json', 'google_calendar/client_secret.json',
                'backup/todolist-20260801/todo/旧备份里的任务.md'):
        assert rel in after, f'historical file disappeared: {rel}'


def test_stale_tmp_cleanup_is_confined_to_tmp(legacy_server):
    """启动清理只删 .tmp 崩溃残留; 其他文件一律不碰。"""
    serve, httpd, client, tmp = legacy_server
    data = tmp / 'data'
    (data / 'todo' / 'ghost.md.tmp').write_text('半截', encoding='utf-8')
    (data / 'lists' / 'shopping' / 'meta.json.tmp').write_text('{}', encoding='utf-8')
    before = _tree_hashes(data)
    before = {k: v for k, v in before.items() if not k.endswith('.tmp')}
    # 重启一次 (触发 _cleanup_stale_tmp_files)
    from conftest import _make_server, _stop_server
    from legacy_fixture import LEGACY_ENV
    _stop_server(serve, httpd)
    serve2, httpd2, base2 = _make_server(tmp, dict(
        LEGACY_ENV, Y8Y_DATA_DIR=str(data)))
    try:
        after = _tree_hashes(data)
        after = {k: v for k, v in after.items() if not k.endswith('.tmp')}
        assert after == before, 'startup cleanup touched more than .tmp files'
        assert not any(k.endswith('.tmp') for k in after)
    finally:
        _stop_server(serve2, httpd2)


def test_backup_covers_files_that_must_be_kept(tmp_path):
    """P4 备份工具必须覆盖 google_calendar 凭据/杂物 (数据保全)。"""
    data = tmp_path / 'data'
    build_legacy_data(data)
    tar = tmp_path / 'b.tar.gz'
    r = subprocess.run(
        [sys.executable, str(SCRIPT), 'backup', str(data), str(tar)],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    manifest = json.loads((tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'))
    for rel in ('google_calendar/tokens.json',
                'google_calendar/client_secret.json',
                '_fix_config.py',
                'backup/todolist-20260801/todo/旧备份里的任务.md',
                'holidays_cache.json', 'misc_notes.txt'):
        assert rel in manifest, f'backup manifest missing {rel}'


def test_machine_token_channel_still_works(legacy_server):
    """y8ysystems 内部 API 契约 (X-Internal-Token) 不受影响。"""
    serve, httpd, client, tmp = legacy_server
    status, body = client.get('/todo/api/tasks', headers=MACHINE)
    assert status == 200
    assert {'current_user', 'tasks', 'completed_tasks', 'sections'} <= set(body)
