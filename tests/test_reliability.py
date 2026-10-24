"""Data reliability: same-title isolation, filename safety, the DATA_LOCK
concurrency guarantee, and restart integrity for the new filename scheme.
"""
import threading

# Default server fixture maps alice@example.com → alice.
ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}


def _create(client, title, **kw):
    body = {'title': title, 'due': '2026-10-20'}
    body.update(kw)
    return client.post('/todo/api/tasks', body=body, headers=ALICE)


# ── 同名任务隔离 ───────────────────────────────────────────────────────

def test_many_same_title_tasks_are_independent(server):
    serve, httpd, client, tmp = server
    ids = []
    for i in range(5):
        status, body = _create(client, '重复标题', content=f'第{i}个')
        assert status == 201
        ids.append(body['id'])
    assert len(set(ids)) == 5, 'same-title tasks must get distinct files'
    # all visible, independently identifiable by body
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    same = [t for t in body['tasks'] if t['title'] == '重复标题']
    assert len(same) == 5
    bodies = {t['body'] for t in same}
    assert bodies == {f'第{i}个' for i in range(5)}
    # deleting one leaves the others untouched
    status, _ = client.delete(f'/todo/api/tasks/{ids[0]}', headers=ALICE)
    assert status == 200
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    assert len([t for t in body['tasks'] if t['title'] == '重复标题']) == 4


def test_create_never_overwrites_existing_file(server):
    """Byte-level guarantee: creating a same-title task must not touch
    the existing task file (the P1 data-loss bug)."""
    serve, httpd, client, tmp = server
    _, body = _create(client, '重要任务', content='原始内容')
    first_id = body['id']
    before = (tmp / 'data' / 'todo' / first_id).read_bytes()
    _, body = _create(client, '重要任务', content='新内容')
    assert body['id'] != first_id
    after = (tmp / 'data' / 'todo' / first_id).read_bytes()
    assert before == after


def test_same_title_in_same_project(server):
    serve, httpd, client, tmp = server
    _, b1 = _create(client, '子任务', project='项目X', content='A')
    _, b2 = _create(client, '子任务', project='项目X', content='B')
    assert b1['id'] != b2['id']
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    same = [t for t in body['tasks'] if t['title'] == '子任务']
    assert len(same) == 2 and all(t['project'] == '项目X' for t in same)


# ── 文件名安全 ─────────────────────────────────────────────────────────

def test_unsafe_title_characters_produce_safe_filenames(server):
    serve, httpd, client, tmp = server
    nasty = 'a/b\\c:d*e?f"g<h>i|j'
    status, body = _create(client, nasty)
    assert status == 201
    fname = body['id']
    # no path separators survive into the filename
    assert '/' not in fname
    assert (tmp / 'data' / 'todo' / fname).is_file()
    # display title preserved exactly
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    assert any(t['title'] == nasty for t in body['tasks'])


def test_dot_only_title_gets_uid_filename(server):
    """A title like '.' or '..' must never become the filename."""
    serve, httpd, client, tmp = server
    for title in ['.', '..', '...', ' . ']:
        status, body = _create(client, title)
        assert status == 201, title
        fname = body['id']
        assert fname not in ('.md', '...md'), title
        assert not fname.startswith('.'), title
        assert (tmp / 'data' / 'todo' / fname).is_file()


def test_very_long_title_filename_stays_within_limits(server):
    serve, httpd, client, tmp = server
    title = '长' * 300
    status, body = _create(client, title)
    assert status == 201
    fname = body['id']
    # filesystem-safe length (255-byte limit; 60 chars * 3 bytes + suffix)
    assert len(fname.encode('utf-8')) < 250
    assert (tmp / 'data' / 'todo' / fname).is_file()
    # full title preserved in frontmatter
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    assert any(t['title'] == title for t in body['tasks'])


# ── 重命名与关联保持 ───────────────────────────────────────────────────

def test_rename_new_task_keeps_id_and_gcal_fields(server):
    """P5.5: Google 集成已移除, 但历史文件的 task_uid/gcal_event_id
    必须在重命名/编辑后原样保留 (数据保全优先于功能清理)。"""
    serve, httpd, client, tmp = server
    _, body = _create(client, '原名', content='c')
    task_id = body['id']
    # 直接在文件里写下历史关联字段 (模拟旧版同步过的任务)
    text = (tmp / 'data' / 'todo' / task_id).read_text(encoding='utf-8')
    text = text.replace('---\n', '---\ntask_uid: uid-1\ngcal_event_id: evt-1\n', 1)
    (tmp / 'data' / 'todo' / task_id).write_text(text, encoding='utf-8')
    # rename
    status, body = client.put(f'/todo/api/tasks/{task_id}',
                              body={'title': '新名字'}, headers=ALICE)
    assert status == 200
    assert body['id'] == task_id            # ID unchanged
    text = (tmp / 'data' / 'todo' / task_id).read_text(encoding='utf-8')
    assert '\ntitle: 新名字\n' in text
    assert 'gcal_event_id: evt-1' in text    # association survives rename
    assert 'task_uid: uid-1' in text


def test_title_survives_content_only_edit(server):
    serve, httpd, client, tmp = server
    _, body = _create(client, '保持标题', content='旧')
    task_id = body['id']
    client.put(f'/todo/api/tasks/{task_id}', body={'content': '新'}, headers=ALICE)
    _, body = client.get(f'/todo/api/tasks/{task_id}', headers=ALICE)
    assert body['title'] == '保持标题'
    assert body['body'] == '新'


# ── 并发写保护 ─────────────────────────────────────────────────────────

def test_data_lock_prevents_lost_updates(server):
    """Two concurrent read-modify-write loops on the same task file must
    not lose updates when each takes DATA_LOCK (white-box regression
    guard for the lock; the pre-P5.5 version had the Google sync worker
    as the second writer)."""
    serve, httpd, client, tmp = server
    _, body = _create(client, '并发任务', content='c')
    task_id = body['id']
    fpath = tmp / 'data' / 'todo' / task_id

    def writer_a(rounds):
        for i in range(rounds):
            with serve.DATA_LOCK:
                raw = fpath.read_text(encoding='utf-8')
                fm, b = serve.parse_frontmatter(raw)
                fm[f'field_a_{i}'] = 'x'
                serve.atomic_write(str(fpath),
                                   serve.build_frontmatter(fm) + '\n\n' + b + '\n')

    def writer_b(rounds):
        for i in range(rounds):
            with serve.DATA_LOCK:
                raw = fpath.read_text(encoding='utf-8')
                fm, b = serve.parse_frontmatter(raw)
                fm[f'field_b_{i}'] = 'y'
                serve.atomic_write(str(fpath),
                                   serve.build_frontmatter(fm) + '\n\n' + b + '\n')

    t1 = threading.Thread(target=writer_a, args=(25,))
    t2 = threading.Thread(target=writer_b, args=(25,))
    t1.start(); t2.start(); t1.join(); t2.join()

    text = fpath.read_text(encoding='utf-8')
    missing_a = [i for i in range(25) if f'field_a_{i}: x' not in text]
    missing_b = [i for i in range(25) if f'field_b_{i}: y' not in text]
    assert not missing_a, f'lost writer_a updates: {missing_a}'
    assert not missing_b, f'lost writer_b updates: {missing_b}'


def test_concurrent_creates_same_title_via_http(server):
    """Parallel creation requests must each produce a distinct task."""
    serve, httpd, client, tmp = server

    def create():
        for _ in range(5):
            client.post('/todo/api/tasks',
                        body={'title': '并发创建', 'due': '2026-10-20'},
                        headers=ALICE)

    threads = [threading.Thread(target=create) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    same = [t for t in body['tasks'] if t['title'] == '并发创建']
    assert len(same) == 20


# ── 重启完整性（新格式） ───────────────────────────────────────────────

def test_restart_integrity_new_format(server):
    serve, httpd, client, tmp = server
    _, body = _create(client, '重启任务', content='c', color='red')
    task_id = body['id']
    client.put(f'/todo/api/tasks/{task_id}', body={'title': '重启任务改名'}, headers=ALICE)
    from conftest import _make_server, _stop_server
    # stop the first instance cleanly (HTTP) before reload
    _stop_server(serve, httpd)
    serve2, httpd2, base = _make_server(tmp, {
        'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
        'Y8Y_TRUSTED_PROXIES': '127.0.0.1',
        'Y8Y_DATA_DIR': str(tmp / 'data'),
    })
    try:
        from conftest import Client
        client2 = Client(base)
        _, body = client2.get(f'/todo/api/tasks/{task_id}', headers=ALICE)
        assert body['title'] == '重启任务改名'
        assert body['color'] == 'red'
        assert body['body'] == 'c'
    finally:
        _stop_server(serve2, httpd2)


def test_stale_tmp_files_cleaned(tmp_path):
    """Crash-leftover .tmp files are removed by the startup cleanup
    (invoked from main(); unit-tested directly here so importing the
    module never mutates the data dir)."""
    from conftest import _make_server, _stop_server
    data_dir = tmp_path / 'data'
    serve, httpd, _ = _make_server(tmp_path, {
        'Y8Y_USERS': 'alice@example.com=alice',
        'Y8Y_DATA_DIR': str(data_dir),
    })
    _stop_server(serve, httpd)
    # plant stale tmp files as if writes had crashed
    (data_dir / 'todo').mkdir(parents=True, exist_ok=True)
    (data_dir / 'todo' / 'ghost.md.tmp').write_text('半截任务', encoding='utf-8')
    (data_dir / 'lists' / 'shopping').mkdir(parents=True, exist_ok=True)
    (data_dir / 'lists' / 'shopping' / 'meta.json.tmp').write_text('{}', encoding='utf-8')
    serve._cleanup_stale_tmp_files()
    leftovers = list(data_dir.rglob('*.tmp'))
    assert leftovers == [], f'stale tmp files survived: {leftovers}'
