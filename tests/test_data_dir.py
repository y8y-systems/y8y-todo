"""Data directory isolation and on-disk format compatibility.

Guarantees:
- data lives outside the source tree in production mode
- fresh data directories work (missing dirs created on demand)
- updates to source code never touch persistent data
- the on-disk markdown/frontmatter format stays byte-compatible
"""
import json

ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}


def test_data_written_outside_source_tree(server):
    serve, httpd, client, tmp = server
    src_root = tmp.parent / 'y8y-todo'
    status, body = client.post('/todo/api/tasks',
                               body={'title': '隔离验证', 'due': '2026-10-20'}, headers=ALICE)
    assert status == 201
    # The task file exists in the data dir (slug+uid filename)...
    task_file = tmp / 'data' / 'todo' / body['id']
    assert task_file.is_file()
    # ...and nowhere near the source tree.
    assert not (src_root / 'todo').exists()
    assert not (src_root / 'data').exists()


def test_fresh_data_dir_creates_structure(server):
    """A brand-new deployment (empty data dir) must work end to end."""
    serve, httpd, client, tmp = server
    data = tmp / 'data'
    assert not data.exists() or not any(data.iterdir())
    # Task creation on an empty data dir (regression: used to 500).
    status, body = client.post('/todo/api/tasks',
                               body={'title': '全新', 'due': '2026-10-20'}, headers=ALICE)
    assert status == 201
    assert (data / 'todo' / body['id']).is_file()
    # Shopping list bootstrap.
    status, _ = client.get('/todo/api/shopping', headers=ALICE)
    assert status == 200
    assert (data / 'lists' / 'shopping' / 'meta.json').is_file()
    # Persistent projects file.
    status, _ = client.put('/todo/api/persistent-projects',
                           body={'projects': ['x']}, headers=ALICE)
    assert status == 200
    assert (data / 'persistent_projects.json').is_file()


def test_on_disk_format_compatible(server):
    """The written markdown must keep the legacy frontmatter shape that
    existing production data uses (zero-migration compatibility)."""
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/tasks', body={
        'title': '格式验证', 'due': '2026-10-20', 'due_end': '2026-10-22',
        'color': 'orange', 'project': '格式', 'content': '正文内容',
    }, headers=ALICE)
    assert status == 201
    task_file = tmp / 'data' / 'todo' / '格式' / body['id'].split('/', 1)[1]
    text = task_file.read_text(encoding='utf-8')
    assert text.startswith('---\n')
    assert '\ntitle: 格式验证\n' in text       # 显示标题进 frontmatter (2026-10-17)
    assert '\nstatus: todo\n' in text
    assert '\nowner: alice\n' in text
    assert '\ndue: 2026-10-20\n' in text
    assert '\nproject: 格式\n' in text
    assert text.endswith('正文内容\n')
    # Recurring list syntax
    rec = tmp / 'data' / 'recurring'
    rec.mkdir(parents=True)
    (rec / '循环.md').write_text(
        '---\nstatus: todo\ndue: 2026-01-05\ncreated: 2026-01-01\nowner: alice\n'
        'recurring:\n  - 5\n  - 20\n---\n\n循环任务\n', encoding='utf-8')
    client.post('/todo/api/tasks/循环.md/done', headers=ALICE)
    text = (rec / '循环.md').read_text(encoding='utf-8')
    assert '\ndue: 2026-01-20\n' in text
    assert '\nrecurring:\n  - 5\n  - 20\n' in text


def test_done_archive_layout(server):
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/tasks',
                               body={'title': '归档', 'due': '2026-10-20'}, headers=ALICE)
    client.post(f'/todo/api/tasks/{body["id"]}/done', headers=ALICE)
    done_dirs = list((tmp / 'data' / 'done').iterdir())
    assert len(done_dirs) == 1
    assert len(done_dirs[0].name) == 10       # YYYY-MM-DD
    assert (done_dirs[0] / body['id']).is_file()


def test_no_temp_files_left_behind(server):
    """atomic_write must clean up its .tmp files."""
    serve, httpd, client, tmp = server
    client.post('/todo/api/tasks',
                body={'title': '原子', 'due': '2026-10-20'}, headers=ALICE)
    client.put('/todo/api/persistent-projects',
               body={'projects': ['p']}, headers=ALICE)
    leftovers = list(tmp.rglob('*.tmp'))
    assert leftovers == [], f'stale temp files: {leftovers}'


def test_legacy_owner_default(server):
    """Tasks without an owner field (legacy data) fall back to the first
    configured user, keeping old data visible after migration."""
    import os
    serve, httpd, client, tmp = server
    legacy = tmp / 'data' / 'todo'
    legacy.mkdir(parents=True)
    (legacy / '老数据.md').write_text(
        '---\nstatus: todo\ndue: 2026-10-20\ncreated: 2026-01-01\n---\n\n没有 owner 的老任务\n',
        encoding='utf-8')
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert status == 200
    titles = [t['title'] for t in body['tasks']]
    assert '老数据' in titles
    task = [t for t in body['tasks'] if t['title'] == '老数据'][0]
    assert task['owner'] == 'alice'           # default_owner = first configured user
