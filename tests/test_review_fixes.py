"""P2 review-round regression tests.

1. Move-target conflicts (complete / restore / project-move) must never
   overwrite an existing destination file — lossless uid variants.
2. Task titles (and any user text) must not corrupt the frontmatter —
   safe serialization + parse round-trip.
   (P5.5: the third section — Google sync lock scope — was removed
   together with the Google Calendar integration.)

All data is fictional.
"""
import json

from legacy_fixture import ALICE

from conftest import _make_server, Client, _stop_server

ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}


# ══════════════════════════════════════════════════════════════════════
# 1. Move-target conflicts
# ══════════════════════════════════════════════════════════════════════

def test_restore_collision_is_lossless(legacy_server):
    """Restoring archives when same-named tasks already exist must produce
    independent files — nothing is overwritten.

    Real scenario from the fixture: todo/买菜.md is ACTIVE and two
    same-basename archives sit under different done dates. Both restores
    target todo/买菜.md, so both must land on uid variants."""
    serve, httpd, client, tmp = legacy_server
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    done = [t for t in body['completed_tasks'] if t['title'] == '买菜']
    assert len(done) == 2, 'fixture should provide two same-basename archives'
    active_before = (tmp / 'data' / 'todo' / '买菜.md').read_text(encoding='utf-8')

    id_1, id_2 = done[0]['id'], done[1]['id']
    status, body = client.put(f'/todo/api/tasks/{id_1}',
                              body={'status': 'todo'}, headers=ALICE)
    assert status == 200
    new_id_1 = body['id']
    assert new_id_1 != '买菜.md', 'collision must resolve to a variant filename'

    status, body = client.put(f'/todo/api/tasks/{id_2}',
                              body={'status': 'todo'}, headers=ALICE)
    assert status == 200
    new_id_2 = body['id']
    assert new_id_2 not in ('买菜.md', new_id_1)

    # the pre-existing active task is untouched (lossless)
    assert (tmp / 'data' / 'todo' / '买菜.md').read_text(encoding='utf-8') == active_before
    # both restored tasks exist as independent files with the display title pinned
    for new_id in (new_id_1, new_id_2):
        variant = tmp / 'data' / 'todo' / new_id
        assert variant.is_file()
        text = variant.read_text(encoding='utf-8')
        assert '\ntitle: 买菜\n' in text
    # three active tasks named 买菜 (original + two restored)
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    same = [t for t in body['tasks'] if t['title'] == '买菜']
    assert len(same) >= 3


def test_project_move_collision_is_lossless(legacy_server):
    """Moving a flat task into a project that already holds a same-named
    legacy file must not overwrite that file."""
    serve, httpd, client, tmp = legacy_server
    existing = tmp / 'data' / 'todo' / '装修' / '选地板.md'
    assert existing.is_file()
    before = existing.read_text(encoding='utf-8')

    status, body = client.put('/todo/api/tasks/选地板.md',
                              body={'project': '装修'}, headers=ALICE)
    assert status == 200
    new_id = body['id']
    assert new_id != '装修/选地板.md', 'collision must resolve to a variant'
    # pre-existing file untouched
    assert existing.read_text(encoding='utf-8') == before
    # the moved task landed somewhere else, title preserved
    variant = tmp / 'data' / 'todo' / new_id
    assert variant.is_file()
    _, body = client.get(f'/todo/api/tasks/{new_id}', headers=ALICE)
    assert body['title'] == '选地板'
    assert body['project'] == '装修'


def test_done_path_collision_is_lossless(legacy_server):
    """Two one-off tasks whose (folder-relative) ids match, completed on
    the same day, must both archive — the second gets a variant name.

    The legacy fixture has todo/买菜.md AND recurring/买菜.md (emptied
    recurring rule), so the same PUT-style completion URL hits both in
    sequence — a real flow, not a synthetic state."""
    serve, httpd, client, tmp = legacy_server
    from datetime import date
    today = date.today().isoformat()

    status, _ = client.post('/todo/api/tasks/买菜.md/done', headers=ALICE)
    assert status == 200
    assert (tmp / 'data' / 'done' / today / '买菜.md').is_file()
    first_archive = (tmp / 'data' / 'done' / today / '买菜.md').read_text(encoding='utf-8')

    # second completion of the same-named legacy task (from recurring/)
    status, body = client.post('/todo/api/tasks/买菜.md/done', headers=ALICE)
    assert status == 200
    archived_id = body.get('id', '')
    # first archive must be intact
    assert (tmp / 'data' / 'done' / today / '买菜.md').read_text(encoding='utf-8') == first_archive
    # and the second file exists somewhere under done/<today>/
    day_files = [f for f in (tmp / 'data' / 'done' / today).glob('*.md')]
    assert any(f.name != '买菜.md' for f in day_files), 'no variant was created'
    # both are visible as completed with the same display title
    _, body = client.get('/todo/api/tasks', headers=ALICE)
    done_titles = [t['title'] for t in body['completed_tasks']]
    assert done_titles.count('买菜') >= 2


def test_done_response_reports_actual_archived_id(dev_server):
    """The completion response carries the real archived API id
    (done/<date>/<file>), matching what list_completed_tasks returns —
    matters when a variant was used for a conflict."""
    serve, httpd, client, tmp = dev_server
    status, body = client.post('/todo/api/tasks',
                               body={'title': '回报ID', 'due': '2026-10-20'},
                               headers={})
    assert status == 201
    task_id = body['id']
    status, body = client.post(f'/todo/api/tasks/{task_id}/done', headers={})
    assert status == 200
    archived_id = body['id']
    assert archived_id.startswith('done/')
    # the reported id resolves in the completed task list
    _, body = client.get('/todo/api/tasks', headers={})
    completed_ids = [t['id'] for t in body['completed_tasks']]
    assert archived_id in completed_ids


# ══════════════════════════════════════════════════════════════════════
# 2. Frontmatter safety (titles must not corrupt parsing)
# ══════════════════════════════════════════════════════════════════════

def test_newline_title_roundtrips_intact(server):
    serve, httpd, client, tmp = server
    title = '第一行\n第二行'
    status, body = client.post('/todo/api/tasks',
                               body={'title': title, 'due': '2026-10-20'},
                               headers=ALICE)
    assert status == 201
    _, body = client.get(f'/todo/api/tasks/{body["id"]}', headers=ALICE)
    assert body['title'] == title, 'newline title must survive the round-trip'
    # on-disk frontmatter structure intact: exactly one block, no injected lines
    text = (tmp / 'data' / 'todo' / body['id']).read_text(encoding='utf-8')
    assert text.count('---') == 2


def test_fake_frontmatter_in_title_is_inert(server):
    """A title crafted to look like frontmatter must not fabricate
    fields or terminate the block early."""
    serve, httpd, client, tmp = server
    title = 'x\n---\nowner: bob\nstatus: done\ndue: 1999-01-01'
    status, body = client.post('/todo/api/tasks',
                               body={'title': title, 'due': '2026-10-20'},
                               headers=ALICE)
    assert status == 201
    task_id = body['id']
    _, body = client.get(f'/todo/api/tasks/{task_id}', headers=ALICE)
    assert body['title'] == title
    # injected fields must NOT take effect
    assert body['owner'] == 'alice'
    assert body['status'] == 'todo'
    assert body['due'] == '2026-10-20'


def test_quote_and_backslash_title_roundtrips(server):
    serve, httpd, client, tmp = server
    title = '他说"你好" \\ 后退 C:\\path'
    status, body = client.post('/todo/api/tasks',
                               body={'title': title, 'due': '2026-10-20'},
                               headers=ALICE)
    assert status == 201
    _, body = client.get(f'/todo/api/tasks/{body["id"]}', headers=ALICE)
    assert body['title'] == title


def test_titles_are_trimmed_at_the_api(server):
    """API-level titles are trimmed (standard input hygiene); the
    serializer still preserves edge whitespace for values that reach it
    (covered by the unit round-trip below)."""
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/tasks',
                               body={'title': '  两边有空格  ', 'due': '2026-10-20'},
                               headers=ALICE)
    assert status == 201
    _, body = client.get(f'/todo/api/tasks/{body["id"]}', headers=ALICE)
    assert body['title'] == '两边有空格'


def test_plain_values_stay_unquoted_on_disk(server):
    """Byte-compat: ordinary values keep the legacy unquoted shape."""
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/tasks',
                               body={'title': '普通任务', 'due': '2026-10-20',
                                     'project': '装修', 'color': 'blue'},
                               headers=ALICE)
    assert status == 201
    text = (tmp / 'data' / 'todo' / '装修' / body['id'].split('/', 1)[1]).read_text(encoding='utf-8')
    assert '\ndue: 2026-10-20\n' in text
    assert '\nproject: 装修\n' in text
    assert '\ntitle: 普通任务\n' in text


def test_project_with_newline_is_rejected(server):
    """Project names become directory names: newline/control characters
    are rejected with 400 instead of creating a broken directory."""
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/tasks',
                               body={'title': '项目换行', 'due': '2026-10-20',
                                     'project': '甲\n乙'},
                               headers=ALICE)
    assert status == 400
    assert not (tmp / 'data' / 'todo' / '甲\n乙').exists()
    # a clean project name still works
    status, body = client.post('/todo/api/tasks',
                               body={'title': '项目正常', 'due': '2026-10-20',
                                     'project': '甲乙'},
                               headers=ALICE)
    assert status == 201
    _, body = client.get(f'/todo/api/tasks/{body["id"]}', headers=ALICE)
    assert body['project'] == '甲乙'
    # PUT with a bad project is rejected too
    status, _ = client.put(f'/todo/api/tasks/{body["id"]}',
                           body={'project': '坏\n名'}, headers=ALICE)
    assert status == 400


def test_build_parse_roundtrip_unit(server):
    """Direct unit check of the serializer/parser pair for nasty values."""
    serve, httpd, client, tmp = server
    nasty = {
        'title': 'a\nb---\nc: d',
        'project': 'p\nq',
        'due': '2026-10-20',
        'custom_x': '含"引号"和\\反斜杠',
        'edge': '  保留两端空白  ',
    }
    text = serve.build_frontmatter(nasty)
    fm, body = serve.parse_frontmatter(text + '\n\n正文\n')
    assert fm['title'] == nasty['title']
    assert fm['project'] == nasty['project']
    assert fm['due'] == '2026-10-20'
    assert fm['custom_x'] == nasty['custom_x']
    assert fm['edge'] == nasty['edge']
    # legacy plain file parses exactly as before
    legacy = '---\nstatus: todo\ndue: 2026-10-20\nowner: alice\n---\n\n正文\n'
    fm2, body2 = serve.parse_frontmatter(legacy)
    assert fm2 == {'status': 'todo', 'due': '2026-10-20', 'owner': 'alice'}
    assert body2 == '正文'

