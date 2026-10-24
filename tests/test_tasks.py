"""Task lifecycle: CRUD, project subdirectories, done/restore, recurring."""
import pytest

ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}


def _create(client, title, **kw):
    body = {'title': title, 'due': '2026-10-20'}
    body.update(kw)
    return client.post('/todo/api/tasks', body=body, headers=ALICE)


def test_task_crud_lifecycle(server):
    serve, httpd, client, tmp = server
    # Create
    status, body = _create(client, '买牛奶')
    assert status == 201
    task_id = body['id']
    # 新方案: 文件名 = slug + uid, 与标题分离 (ID 由 API 返回, 不假设格式)
    assert task_id.endswith('.md') and '买牛奶' in task_id
    # Read (list)
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert status == 200
    assert [t['title'] for t in body['tasks']] == ['买牛奶']
    # Read (single)
    status, body = client.get(f'/todo/api/tasks/{task_id}', headers=ALICE)
    assert status == 200
    assert body['title'] == '买牛奶'
    assert body['color'] == 'green'
    assert body['owner'] == 'alice'
    # Update
    status, body = client.put(f'/todo/api/tasks/{task_id}',
                              body={'color': 'red', 'content': '要冷的'}, headers=ALICE)
    assert status == 200
    status, body = client.get(f'/todo/api/tasks/{task_id}', headers=ALICE)
    assert body['color'] == 'red'
    assert body['body'] == '要冷的'
    # Delete
    status, _ = client.delete(f'/todo/api/tasks/{task_id}', headers=ALICE)
    assert status == 200
    status, _ = client.get(f'/todo/api/tasks/{task_id}', headers=ALICE)
    assert status == 404


def test_task_requires_title(server):
    serve, httpd, client, tmp = server
    status, _ = client.post('/todo/api/tasks', body={'due': '2026-10-20'}, headers=ALICE)
    assert status == 400


def test_project_subdirectory(server):
    serve, httpd, client, tmp = server
    status, body = _create(client, '大纲', project='装修')
    assert status == 201
    # 项目任务: id = <project>/<slug-uid>.md
    assert body['id'].startswith('装修/')
    # File lives under todo/<project>/
    task_file = tmp / 'data' / 'todo' / '装修' / body['id'].split('/', 1)[1]
    assert task_file.is_file()
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert body['tasks'][0]['project'] == '装修'


def test_done_and_restore(server):
    serve, httpd, client, tmp = server
    status, body = _create(client, '写周报')
    task_id = body['id']
    # Complete
    status, _ = client.post(f'/todo/api/tasks/{task_id}/done', headers=ALICE)
    assert status == 200
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert body['tasks'] == []
    assert [t['title'] for t in body['completed_tasks']] == ['写周报']
    # Archived under done/<date>/
    done_files = list((tmp / 'data' / 'done').glob('*/*.md'))
    assert len(done_files) == 1
    # Restore (id becomes done/<date>/<file>)
    done_id = body['completed_tasks'][0]['id']
    status, body = client.put(f'/todo/api/tasks/{done_id}',
                              body={'status': 'todo'}, headers=ALICE)
    assert status == 200
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert [t['title'] for t in body['tasks']] == ['写周报']


def test_recurring_task_advances_to_next_date(server):
    serve, httpd, client, tmp = server
    # Create a recurring task directly on disk (data-compatible format),
    # since the web UI intentionally does not expose recurring creation.
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    (rec_dir / '月度复盘.md').write_text(
        '---\nstatus: todo\ndue: 2026-01-05\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n  - 5\n  - 20\n---\n\n月底复盘\n',
        encoding='utf-8')
    # Completing should roll due to the next listed day in the same month.
    status, body = client.post('/todo/api/tasks/月度复盘.md/done', headers=ALICE)
    assert status == 200
    assert body['next_due'] == '2026-01-20'
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    task = [t for t in body['tasks'] if t['title'] == '月度复盘'][0]
    assert task['due'] == '2026-01-20'
    assert task['status'] == 'todo'      # still active (recurring), not archived


def test_recurring_year_rollover(server):
    serve, httpd, client, tmp = server
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    (rec_dir / '年报.md').write_text(
        '---\nstatus: todo\ndue: 2026-03-20\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n  - 5\n---\n\n年度事项\n',
        encoding='utf-8')
    # No remaining date this month → rolls to same day next year.
    status, body = client.post('/todo/api/tasks/年报.md/done', headers=ALICE)
    assert status == 200
    assert body['next_due'] == '2027-03-05'


# ── recurring_dates 格式验证 (O3-N2) ────────────────────────────────────
#
# 旧实现零验证: 非法规则被接受, 完成推进时 ValueError 被静默吞掉 → 任务卡在
# recurring/ 里 status=done, 界面不可见。现在写入前 400, 计算失败 400 且不
# 落盘 (任务保持未完成、始终可见)。

def _create_recurring(client, recurring_dates, recurring=True, **kw):
    body = {'title': '重复任务', 'due': '2026-10-20', 'recurring': recurring}
    if recurring_dates is not None:
        body['recurring_dates'] = recurring_dates
    body.update(kw)
    return client.post('/todo/api/tasks', body=body, headers=ALICE)


@pytest.mark.parametrize('bad', [
    [''], ['abc'], ['02-30'], ['13-01'], ['00-10'], ['1-2-3'], [0], [32], [-1],
    [None], [['5']], [{'d': 5}], [True], ['5', 'not-a-date'], [3.5],
])
def test_post_recurring_rejects_invalid_dates(server, bad):
    serve, httpd, client, tmp = server
    status, body = _create_recurring(client, bad)
    assert status == 400, f'{bad!r} must be rejected'
    assert 'recurring_dates' in body.get('error', '')
    # 非法输入不得落盘
    assert not list((tmp / 'data' / 'recurring').glob('*.md'))


def test_post_recurring_requires_nonempty_dates(server):
    serve, httpd, client, tmp = server
    # recurring=true 但 recurring_dates 缺失/为空 → 400 (旧实现会建一个
    # 空规则任务, 完成时静默卡死)
    assert _create_recurring(client, None)[0] == 400
    assert _create_recurring(client, [])[0] == 400
    assert not list((tmp / 'data' / 'recurring').glob('*.md'))


@pytest.mark.parametrize('good,expect_disk', [
    (['5', '20'], '  - 5\n  - 20\n'),          # 月内多日 (旧格式等价)
    ([5, 15], '  - 5\n  - 15\n'),              # int 输入 → 十进制等价
    (['03-01'], '  - 03-01\n'),                # 每年日期
    (['02-29'], '  - 02-29\n'),                # 闰日合法
    ('03-01', '  - 03-01\n'),                  # 单字符串标量 (旧磁盘格式兼容)
    (['5', '5'], '  - 5\n'),                   # 去重保序
])
def test_post_recurring_accepts_valid_formats(server, good, expect_disk):
    serve, httpd, client, tmp = server
    status, body = _create_recurring(client, good)
    assert status == 201, f'{good!r} must be accepted'
    rec_files = list((tmp / 'data' / 'recurring').glob('*.md'))
    assert len(rec_files) == 1
    assert f'\nrecurring:\n{expect_disk}' in rec_files[0].read_text(encoding='utf-8')


def test_completion_uncomputable_rule_fails_loud_and_keeps_task_visible(server):
    """O3-N2 核心回归: 规则与 due 所在月不兼容 (2 月里的 '31') → 完成必须
    400 且不落盘 — 任务保持 status=todo, 在活动列表里可见。"""
    serve, httpd, client, tmp = server
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    task_file = rec_dir / '月底缴费.md'
    task_file.write_text(
        '---\nstatus: todo\ndue: 2026-02-10\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n  - 31\n---\n\n每月 31 号缴费\n',
        encoding='utf-8')
    before = task_file.read_bytes()
    status, body = client.post('/todo/api/tasks/月底缴费.md/done', headers=ALICE)
    assert status == 400
    assert 'Recurring rule cannot advance' in body.get('error', '')
    # 关键: 不落盘 — 文件逐字节不变, 任务仍是 todo (不会卡成看不见的 done)
    assert task_file.read_bytes() == before
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    task = [t for t in body['tasks'] if t['title'] == '月底缴费']
    assert len(task) == 1 and task[0]['status'] == 'todo'


def test_completion_handcorrupted_legacy_rule_fails_loud(server):
    """旧数据里手写坏的规则 ('abc') → 400 响亮失败, 不再是静默卡死。"""
    serve, httpd, client, tmp = server
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    task_file = rec_dir / '坏规则.md'
    task_file.write_text(
        '---\nstatus: todo\ndue: 2026-10-20\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n  - abc\n---\n\n坏规则遗留\n',
        encoding='utf-8')
    before = task_file.read_bytes()
    status, _ = client.post('/todo/api/tasks/坏规则.md/done', headers=ALICE)
    assert status == 400
    assert task_file.read_bytes() == before
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert [t for t in body['tasks'] if t['title'] == '坏规则']


def test_completion_invalid_due_on_recurring_fails_loud(server):
    """due 本身不是 YYYY-MM-DD → 400, 不静默标记完成。"""
    serve, httpd, client, tmp = server
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    task_file = rec_dir / '坏日期.md'
    task_file.write_text(
        '---\nstatus: todo\ndue: 十月底\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n  - 5\n---\n\n坏日期\n',
        encoding='utf-8')
    before = task_file.read_bytes()
    status, body = client.post('/todo/api/tasks/坏日期.md/done', headers=ALICE)
    assert status == 400
    assert 'Invalid due date' in body.get('error', '')
    assert task_file.read_bytes() == before


def test_recurring_cross_year_boundary(server):
    """跨年边界: due 2026-12-31 + 规则 01-01 → 2027-01-01。"""
    serve, httpd, client, tmp = server
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    (rec_dir / '跨年.md').write_text(
        '---\nstatus: todo\ndue: 2026-12-31\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n  - 01-01\n---\n\n跨年重复\n',
        encoding='utf-8')
    status, body = client.post('/todo/api/tasks/跨年.md/done', headers=ALICE)
    assert status == 200
    assert body['next_due'] == '2027-01-01'


def test_recurring_month_end_31_in_31_day_month(server):
    """规则 31 在 31 天的月份正常工作 (年周期): 01-31 → 次年 01-31。"""
    serve, httpd, client, tmp = server
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    (rec_dir / '月费.md').write_text(
        '---\nstatus: todo\ndue: 2026-01-31\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n  - 31\n---\n\n月底费用\n',
        encoding='utf-8')
    status, body = client.post('/todo/api/tasks/月费.md/done', headers=ALICE)
    assert status == 200
    assert body['next_due'] == '2027-01-31'


def test_recurring_empty_list_still_one_off(server):
    """兼容: 空规则列表 (旧数据) 仍按一次性任务完成并归档。"""
    serve, httpd, client, tmp = server
    rec_dir = tmp / 'data' / 'recurring'
    rec_dir.mkdir(parents=True)
    (rec_dir / '空规则.md').write_text(
        '---\nstatus: todo\ndue: 2026-10-20\ncreated: 2026-01-01\n'
        'owner: alice\nrecurring:\n---\n\n规则已清空\n',
        encoding='utf-8')
    status, body = client.post('/todo/api/tasks/空规则.md/done', headers=ALICE)
    assert status == 200
    assert body['id'].startswith('done/')
    assert not (rec_dir / '空规则.md').exists()


def test_sections_classification(server):
    serve, httpd, client, tmp = server
    from datetime import date, timedelta
    today = date.today()
    _create(client, '过期任务', due=(today - timedelta(days=1)).isoformat())
    _create(client, '今天任务', due=today.isoformat())
    _create(client, '本周任务', due=(today + timedelta(days=3)).isoformat())
    _create(client, '远期任务', due=(today + timedelta(days=30)).isoformat())
    _create(client, '无期限任务', due='')
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert set(body['sections'].keys()) == {'overdue', 'today', 'this_week', 'future', 'nodue'}
    titles = {t['title'] for t in body['tasks']}
    assert '今天任务' in titles
    assert body['sections']['today']  # non-empty


def test_persistent_projects_roundtrip(server):
    serve, httpd, client, tmp = server
    status, body = client.get('/todo/api/persistent-projects', headers=ALICE)
    assert status == 200 and body['projects'] == []
    status, body = client.put('/todo/api/persistent-projects',
                              body={'projects': ['装修', '装修', '学习']}, headers=ALICE)
    assert status == 200
    assert body['projects'] == ['装修', '学习']    # deduped, order kept
    status, body = client.get('/todo/api/persistent-projects', headers=ALICE)
    assert body['projects'] == ['装修', '学习']
