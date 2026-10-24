"""Historical data compatibility: the fictional legacy tree from
legacy_fixture.py must keep working for read / edit / complete /
restore, across restarts, and through the y8ysystems machine contract.

All data is invented for this test; no real production content is used.
"""
from legacy_fixture import (
    ALICE, BOB, MACHINE, ITEM_EGG, ITEM_PASSPORT,
)

from conftest import Client, _make_server, _stop_server


# ── 读取 ──────────────────────────────────────────────────────────────

def test_legacy_tasks_readable_with_expected_titles(legacy_server):
    serve, httpd, client, tmp = legacy_server
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert status == 200
    titles = {t['title'] for t in body['tasks']}
    # filename-derived titles for legacy files (no frontmatter title)
    assert '买菜' in titles
    assert '周报' in titles
    assert '共享大扫除' in titles
    assert '裸文件' in titles            # no frontmatter at all
    assert 'gcal同步任务' in titles
    assert '带自定义字段' in titles
    assert '月度复盘' in titles and '年度事项' in titles   # recurring
    # project tasks are included
    assert '买油漆' in titles and '选地板' in titles


def test_legacy_owner_semantics_preserved(legacy_server):
    serve, httpd, client, tmp = legacy_server
    # alice sees: own + shared (incl. no-owner defaulting to alice)
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    alice_titles = {t['title'] for t in body['tasks']}
    assert 'bob的预约' not in alice_titles          # bob's private
    assert 'legacy-agent遗留' not in alice_titles        # removed identity → soft-hidden
    assert '买菜' in alice_titles                    # no-owner → first user (alice)
    # bob sees: own + shared only
    _, body = client.get('/todo/api/tasks', headers=BOB)
    bob_titles = {t['title'] for t in body['tasks']}
    assert 'bob的预约' in bob_titles
    assert '共享大扫除' in bob_titles
    assert '周报' not in bob_titles                # alice's private
    assert 'legacy-agent遗留' not in bob_titles


def test_legacy_completed_tasks_readable(legacy_server):
    serve, httpd, client, tmp = legacy_server
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    done_titles = {t['title'] for t in body['completed_tasks']}
    assert '已完成的事' in done_titles
    assert '装修收尾' in done_titles                # project subdir inside done/


def test_legacy_lists_readable(legacy_server):
    serve, httpd, client, tmp = legacy_server
    _, body = client.get('/todo/api/lists', headers=ALICE)
    lists = {l['id']: l for l in body['lists']}
    assert 'shopping' in lists and lists['shopping']['owner'] == 'shared'
    assert '旅行清单' in lists and lists['旅行清单']['owner'] == 'alice'
    # shopping: uuid item shows body-derived title; legacy item shows filename
    _, body = client.get('/todo/api/lists/shopping/items', headers=ALICE)
    titles = [i['title'] for i in body['items']]
    assert '鸡蛋' in titles
    assert '牛奶' in titles
    # custom list
    _, body = client.get('/todo/api/lists/旅行清单/items', headers=ALICE)
    assert [i['title'] for i in body['items']] == ['护照']


def test_legacy_persistent_projects_readable(legacy_server):
    serve, httpd, client, tmp = legacy_server
    _, body = client.get('/todo/api/persistent-projects', headers=ALICE)
    assert body['projects'] == ['装修', '学习']


# ── 修改 ──────────────────────────────────────────────────────────────

def test_put_on_legacy_task_preserves_unknown_fields(legacy_server):
    serve, httpd, client, tmp = legacy_server
    status, _ = client.put('/todo/api/tasks/带自定义字段.md',
                           body={'color': 'red', 'due': '2026-10-26'}, headers=ALICE)
    assert status == 200
    text = (tmp / 'data' / 'todo' / '带自定义字段.md').read_text(encoding='utf-8')
    assert '\ncolor: red\n' in text
    assert '\ndue: 2026-10-26\n' in text
    assert '\ncustom_rating: 5\n' in text   # unknown field survives
    # filename unchanged → title still derives from filename
    _, body = client.get('/todo/api/tasks/带自定义字段.md', headers=ALICE)
    assert body['title'] == '带自定义字段'


def test_rename_legacy_task_keeps_filename(legacy_server):
    """Renaming a legacy (filename-titled) task writes the title into
    frontmatter and leaves the file — and its ID — untouched."""
    serve, httpd, client, tmp = legacy_server
    status, body = client.put('/todo/api/tasks/周报.md',
                              body={'title': '本周工作总结'}, headers=ALICE)
    assert status == 200
    text = (tmp / 'data' / 'todo' / '周报.md').read_text(encoding='utf-8')
    assert '\ntitle: 本周工作总结\n' in text
    # ID unchanged, display title updated
    _, body = client.get('/todo/api/tasks/周报.md', headers=ALICE)
    assert body['title'] == '本周工作总结'


def test_put_preserves_gcal_association(legacy_server):
    """Editing a Google-synced task must not lose its event linkage."""
    serve, httpd, client, tmp = legacy_server
    status, _ = client.put('/todo/api/tasks/gcal同步任务.md',
                           body={'color': 'purple'}, headers=ALICE)
    assert status == 200
    text = (tmp / 'data' / 'todo' / 'gcal同步任务.md').read_text(encoding='utf-8')
    assert 'gcal_event_id: fakeeventid123' in text
    assert 'task_uid: 9f8e7d6c-5b4a-3210-fedc-ba9876543210' in text


def test_create_same_title_as_legacy_task(legacy_server):
    """A new task with the same title as a legacy file must not touch it."""
    serve, httpd, client, tmp = legacy_server
    _, before_body = client.get('/todo/api/tasks', headers=ALICE)
    before_count = len([t for t in before_body['tasks'] if t['title'] == '买菜'])
    legacy_before = (tmp / 'data' / 'todo' / '买菜.md').read_text(encoding='utf-8')
    status, body = client.post('/todo/api/tasks',
                               body={'title': '买菜', 'due': '2026-10-19', 'content': '第二次'},
                               headers=ALICE)
    assert status == 201
    assert body['id'] != '买菜.md'                 # new unique filename
    # legacy file byte-identical
    assert (tmp / 'data' / 'todo' / '买菜.md').read_text(encoding='utf-8') == legacy_before
    # both tasks visible with the same display title (fixture has 2 legacy
    # files named 买菜: todo/ + recurring/ with an emptied rule)
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    same = [t for t in body['tasks'] if t['title'] == '买菜']
    assert len(same) == before_count + 1


# ── 完成 / 恢复 ────────────────────────────────────────────────────────

def test_complete_legacy_task(legacy_server):
    serve, httpd, client, tmp = legacy_server
    status, _ = client.post('/todo/api/tasks/周报.md/done', headers=ALICE)
    assert status == 200
    assert not (tmp / 'data' / 'todo' / '周报.md').exists()
    done_files = list((tmp / 'data' / 'done').glob('*/*.md'))
    # two pre-existing + the newly archived one
    assert any(f.name == '周报.md' for f in done_files)
    # archived file keeps the legacy filename (no title in frontmatter → OK)
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    assert '周报' in {t['title'] for t in body['completed_tasks']}


def test_restore_legacy_project_task(legacy_server):
    """A completed project task restores back into its project subdir."""
    serve, httpd, client, tmp = legacy_server
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    done_id = [t['id'] for t in body['completed_tasks'] if t['title'] == '装修收尾'][0]
    status, body = client.put(f'/todo/api/tasks/{done_id}',
                              body={'status': 'todo'}, headers=ALICE)
    assert status == 200
    assert (tmp / 'data' / 'todo' / '装修' / '装修收尾.md').is_file()
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    task = [t for t in body['tasks'] if t['title'] == '装修收尾'][0]
    assert task['project'] == '装修'
    assert task['status'] == 'todo'


def test_legacy_recurring_advances(legacy_server):
    serve, httpd, client, tmp = legacy_server
    # Multi-day-in-month: next listed day within the same month.
    status, body = client.post('/todo/api/tasks/月度复盘.md/done', headers=ALICE)
    assert status == 200
    assert body['next_due'] == '2026-01-20'
    # Exhausts the month → same day next year.
    status, body = client.post('/todo/api/tasks/年度事项.md/done', headers=ALICE)
    assert status == 200
    assert body['next_due'] == '2027-03-05'
    # files stay in place, recurring field intact
    text = (tmp / 'data' / 'recurring' / '月度复盘.md').read_text(encoding='utf-8')
    assert '\nrecurring:\n  - 5\n  - 20\n' in text
    assert '\ndue: 2026-01-20\n' in text


# ── 重启后完整性 ──────────────────────────────────────────────────────

def test_restart_preserves_data_integrity(legacy_server):
    """Reload the module (simulating a restart) against the same data
    dir — everything must still read back correctly."""
    serve, httpd, client, tmp = legacy_server
    # mutate a bit first
    client.put('/todo/api/tasks/买菜.md', body={'color': 'cyan'}, headers=ALICE)
    client.post('/todo/api/tasks/裸文件.md/done', headers=ALICE)
    # stop the first instance cleanly (HTTP) before reloading
    _stop_server(serve, httpd)
    from legacy_fixture import LEGACY_ENV
    env = dict(LEGACY_ENV)
    env['Y8Y_DATA_DIR'] = str(tmp / 'data')
    serve2, httpd2, base = _make_server(tmp, env)
    try:
        client2 = type(client)(base)
        _, body = client2.get('/todo/api/tasks', headers=ALICE)
        titles = {t['title'] for t in body['tasks']}
        assert '买菜' in titles                     # edit survived
        assert '裸文件' not in titles                # completion survived
        assert '裸文件' in {t['title'] for t in body['completed_tasks']}
        done = {t['title'] for t in body['completed_tasks']}
        assert '已完成的事' in done and '装修收尾' in done
    finally:
        _stop_server(serve2, httpd2)


# ── y8ysystems 机器契约 ───────────────────────────────────────────────

def test_machine_contract_on_legacy_data(legacy_server):
    serve, httpd, client, tmp = legacy_server
    status, body = client.get('/todo/api/tasks', headers=MACHINE)
    assert status == 200
    assert body['current_user'] == 'alice'
    for key in ('current_user', 'tasks', 'completed_tasks', 'sections'):
        assert key in body
    task = [t for t in body['tasks'] if t['title'] == '周报'][0]
    for key in ('id', 'title', 'due', 'due_end', 'due_time', 'created',
                'color', 'status', 'recurring', 'project', 'owner', 'body'):
        assert key in task, f'missing contract field: {key}'
    assert task['owner'] == 'alice'


def test_legacy_bridge_token_entry_is_a_live_machine_credential(tmp_path):
    """P6-A 实测: internal_tokens.json 的遗留 legacy-bridge 条目**仍然有效**。

    docs/legacy-data.md §5 曾声称 legacy-bridge 条目"被忽略/不授权" — 与代码
    不符: _current_user() 对 token 映射做反向查找, 不校验身份是否在
    Y8Y_USERS 中。实测 (P6-A 生产 + 本虚构复演): legacy-bridge token → 200,
    current_user='legacy-bridge'; shared 可读**可写**; 可创建 legacy-bridge 归属任务;
    任何用户的私人任务一律 403。本测试锁定该行为 — 将来若要改变 token
    语义, 必须是刻意的决定并同步本测试与文档。
    """
    import json
    from legacy_fixture import LEGACY_ENV, build_legacy_data
    data = tmp_path / 'data'
    data.mkdir(parents=True)
    build_legacy_data(data)
    # 生产 internal_tokens.json 的遗留形态: alice + legacy-bridge 两个条目
    (data / 'internal_tokens.json').write_text(json.dumps(
        {'alice': 'legacy-token-abc123', 'legacy-bridge': 'legacy-bridge-token-xyz'}),
        encoding='utf-8')
    serve, httpd, base = _make_server(
        tmp_path, dict(LEGACY_ENV, Y8Y_DATA_DIR=str(data)))
    client = Client(base)
    LEGACY_BRIDGE = {'X-Internal-Token': 'legacy-bridge-token-xyz'}
    try:
        # 1. 认证有效: 200 + current_user=legacy-bridge (P6-A 实测同款)
        status, body = client.get('/todo/api/tasks', headers=LEGACY_BRIDGE)
        assert status == 200
        assert body['current_user'] == 'legacy-bridge'
        titles = {t['title'] for t in body['tasks']}
        # 2. 权限范围: shared 可见; 用户私人与软隐藏的 legacy-agent 不可见
        assert '共享大扫除' in titles
        assert 'bob的预约' not in titles          # bob 私人
        assert '周报' not in titles                # alice 私人
        assert 'legacy-agent遗留' not in titles        # 移除身份 → 软隐藏
        # 3. shared **可写**: legacy-bridge 的编辑对真实用户可见
        shared = [t for t in body['tasks'] if t['title'] == '共享大扫除'][0]
        st_edit, _ = client.put(f'/todo/api/tasks/{shared["id"]}',
                                {'title': '共享大扫除-legacy-bridge改'}, headers=LEGACY_BRIDGE)
        assert st_edit == 200
        st_alice, alice_view = client.get('/todo/api/tasks', headers=MACHINE)
        assert st_alice == 200
        assert '共享大扫除-legacy-bridge改' in {
            t['title'] for t in alice_view['tasks']}
        # 4. 可创建任务 (归属 legacy-bridge)
        st_create, _ = client.post('/todo/api/tasks',
                                   {'title': 'legacy-bridge建的任务'}, headers=LEGACY_BRIDGE)
        assert st_create == 201
        st_legacy_bridge2, legacy_bridge_view = client.get('/todo/api/tasks', headers=LEGACY_BRIDGE)
        made = [t for t in legacy_bridge_view['tasks'] if t['title'] == 'legacy-bridge建的任务']
        assert made and made[0]['owner'] == 'legacy-bridge'
        # 5. 越权被拒: 看不到/改不了/删不了 alice 的私人任务
        priv = [t for t in alice_view['tasks'] if t['owner'] == 'alice'][0]
        assert client.get(f'/todo/api/tasks/{priv["id"]}', headers=LEGACY_BRIDGE)[0] == 403
        assert client.delete(f'/todo/api/tasks/{priv["id"]}', headers=LEGACY_BRIDGE)[0] == 403
    finally:
        _stop_server(serve, httpd)


# ── 遗留 owner 数据的可见性边界 ────────────────────────────────────────

def test_removed_owner_data_never_leaks(legacy_server):
    """legacy-agent-owned legacy files must be invisible to every user and
    to the machine token — and must not be deletable through the API."""
    serve, httpd, client, tmp = legacy_server
    for headers in (ALICE, BOB, MACHINE):
        _, body = client.get('/todo/api/tasks', headers=headers)
        assert 'legacy-agent遗留' not in {t['title'] for t in body['tasks']}
        assert 'legacy-agent遗留' not in {t['title'] for t in body['completed_tasks']}
    # direct id access also refused (owner check happens before serving)
    status, _ = client.get('/todo/api/tasks/legacy-agent遗留.md', headers=ALICE)
    assert status == 403
    # the file itself is untouched on disk
    assert (tmp / 'data' / 'todo' / 'legacy-agent遗留.md').is_file()
