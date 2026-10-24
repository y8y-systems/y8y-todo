"""Legacy data fixture — a completely FICTIONAL old-format data directory.

Simulates the on-disk layout of the pre-refactor app so the test suite
can verify that historical data keeps working end to end:

- tasks without an `owner` field (defaults to first configured user)
- tasks owned by alice / bob / shared
- project subdirectory tasks
- a task carrying gcal_event_id / task_uid (Google-synced; kept as a
  historical-data preservation case — the integration was removed in
  P5.5 but the fields must stay on disk)
- a task with an unknown custom frontmatter field (must survive edits)
- a task with NO frontmatter at all
- a task owned by the removed legacy-agent identity (soft-hidden)
- recurring tasks (month-multi-day + year-rollover cases)
- completed tasks under done/<date>/, incl. project subdirectories
- shopping list with a legacy title-named item + a uuid item
- a custom list with meta.json
- persistent_projects.json / internal_tokens.json / historical
  google_calendar settings (fictional; no real tokens anywhere)

All content is invented; nothing here comes from any real deployment.
"""
import json
import uuid

ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
BOB = {'Cf-Access-Authenticated-User-Email': 'bob@example.com'}
MACHINE = {'X-Internal-Token': 'legacy-token-abc123'}

# Deterministic uuid for list items (stable ids across runs).
ITEM_EGG = '11111111-1111-1111-1111-111111111111'
ITEM_PASSPORT = '22222222-2222-2222-2222-222222222222'

# Frontmatter snippets written verbatim, in the OLD format (no `title`).
FMT = """---
status: todo
created: 2026-01-01
{extra}---
{body}
"""

# Completed tasks carry status: done (as the app writes when archiving).
DONE_FMT = """---
status: done
created: 2026-01-01
{extra}---
{body}
"""


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def build_legacy_data(data_root):
    """Populate data_root with the fictional legacy tree. Returns it."""
    todo = data_root / 'todo'
    rec = data_root / 'recurring'

    # ── active tasks ────────────────────────────────────────────────
    # No owner field → defaults to first configured user (alice).
    _write(todo / '买菜.md', FMT.format(extra='due: 2026-10-18\n', body='青菜和豆腐\n'))
    # Owned by alice, with a time and an end date.
    _write(todo / '周报.md', FMT.format(
        extra='due: 2026-10-20\ndue_end: 2026-10-21\ndue_time: 09:30\ncolor: blue\n',
        body='本周工作汇总\n'))
    # Shared task (visible to both users).
    _write(todo / '共享大扫除.md', FMT.format(
        extra='due: 2026-10-25\nowner: shared\n', body='全家一起大扫除\n'))
    # Bob's private task.
    _write(todo / 'bob的预约.md', FMT.format(
        extra='due: 2026-10-22\nowner: bob\ncolor: pink\n', body='牙医复查\n'))
    # Project subdirectory tasks.
    _write(todo / '装修' / '买油漆.md', FMT.format(
        extra='due: 2026-10-19\nproject: 装修\nowner: alice\ncolor: orange\n', body='客厅墙面漆\n'))
    _write(todo / '装修' / '选地板.md', FMT.format(
        extra='due: 2026-10-28\nproject: 装修\nowner: alice\n', body='复合地板样品\n'))
    # Flat task whose basename collides with the project task above
    # (used by the move-conflict regression tests).
    _write(todo / '选地板.md', FMT.format(
        extra='due: 2026-10-29\nowner: alice\n', body='放在客厅的平铺任务\n'))
    # Google-synced task: carries event association fields.
    _write(todo / 'gcal同步任务.md', FMT.format(
        extra='due: 2026-10-21\nowner: alice\ncolor: green\n'
              'task_uid: 9f8e7d6c-5b4a-3210-fedc-ba9876543210\n'
              'gcal_event_id: fakeeventid123\n',
        body='已同步到 Google 日历的任务\n'))
    # Unknown custom frontmatter field — must survive edits untouched.
    _write(todo / '带自定义字段.md', FMT.format(
        extra='due: 2026-10-23\nowner: alice\ncustom_rating: 5\n',
        body='有旧版扩展字段的任务\n'))
    # Removed legacy-hardware-calendar identity: no configured user matches → soft-hidden.
    _write(todo / 'legacy-agent遗留.md', FMT.format(
        extra='due: 2026-10-24\nowner: legacy-agent\n', body='历史机器日历遗留任务\n'))
    # No frontmatter at all — bare markdown body.
    _write(todo / '裸文件.md', '只是正文, 没有任何 frontmatter\n')

    # ── recurring tasks ─────────────────────────────────────────────
    # Multi-day within month: completing rolls to the next listed day.
    _write(rec / '月度复盘.md', FMT.format(
        extra='due: 2026-01-05\nowner: alice\nrecurring:\n  - 5\n  - 20\n',
        body='每月 5 号和 20 号复盘\n'))
    # Exhausts the month → rolls to next year same day.
    _write(rec / '年度事项.md', FMT.format(
        extra='due: 2026-03-20\nowner: alice\nrecurring:\n  - 5\n',
        body='每年 3 月的事项\n'))
    # Empty recurring list: falsy → treated as one-off on completion
    # (moves to done/ like a normal task). Same basename as todo/买菜.md
    # → drives the done-path collision regression test.
    _write(rec / '买菜.md', FMT.format(
        extra='due: 2026-10-18\nowner: alice\nrecurring:\n',
        body='重复规则已清空的旧任务\n'))

    # ── completed tasks ─────────────────────────────────────────────
    _write(data_root / 'done' / '2026-09-01' / '已完成的事.md', DONE_FMT.format(
        extra='completed: 2026-09-01\nowner: alice\n', body='八月收尾\n'))
    # Completed task that lived in a project subdirectory.
    _write(data_root / 'done' / '2026-09-15' / '装修' / '装修收尾.md', DONE_FMT.format(
        extra='completed: 2026-09-15\nproject: 装修\nowner: alice\n', body='旧房验收\n'))
    # Two same-basename archives from different dates: restoring both
    # lands on the same todo/ path (drives the restore-collision test).
    _write(data_root / 'done' / '2026-09-20' / '买菜.md', DONE_FMT.format(
        extra='completed: 2026-09-20\nowner: alice\n', body='第一次买菜的归档\n'))
    _write(data_root / 'done' / '2026-09-25' / '买菜.md', DONE_FMT.format(
        extra='completed: 2026-09-25\nowner: alice\n', body='第二次买菜的归档\n'))

    # ── lists ───────────────────────────────────────────────────────
    shopping = data_root / 'lists' / 'shopping'
    _write(shopping / 'meta.json', json.dumps(
        {'name': '购物', 'owner': 'shared', 'created': '2026-08-01'}, ensure_ascii=False))
    # New-format item: uuid filename, title in body.
    _write(shopping / f'{ITEM_EGG}.md',
           '---\nstatus: todo\ncreated: 2026-10-10\nowner: shared\n---\n\n鸡蛋\n')
    # Legacy item: title IS the filename.
    _write(shopping / '牛奶.md',
           '---\nstatus: todo\ncreated: 2026-10-11\nowner: shared\n---\n\n纯牛奶两盒\n')

    travel = data_root / 'lists' / '旅行清单'
    _write(travel / 'meta.json', json.dumps(
        {'name': '旅行清单', 'owner': 'alice', 'created': '2026-08-20'}, ensure_ascii=False))
    _write(travel / f'{ITEM_PASSPORT}.md',
           '---\nstatus: todo\ncreated: 2026-08-20\nowner: alice\n---\n\n护照\n')

    # ── cross-device state ──────────────────────────────────────────
    _write(data_root / 'persistent_projects.json', json.dumps(
        {'alice': ['装修', '学习']}, ensure_ascii=False))
    _write(data_root / 'internal_tokens.json', json.dumps(
        {'alice': 'legacy-token-abc123'}))
    # Fictional Google settings — NO real tokens anywhere in the fixture.
    _write(data_root / 'google_calendar' / 'settings.json', json.dumps({
        'alice': {
            'calendar_id': 'fictional-calendar-id@group.calendar.example',
            'calendar_name': 'TODO',
            'last_sync_at': '2026-09-30T10:00:00+09:00',
            'scope': 'owner_and_shared',
        }
    }, ensure_ascii=False))
    # P5.5: 真实生产目录 additionally holds the historical OAuth client
    # credentials and token store (all fictional). The Google integration
    # is removed; these files must survive untouched.
    _write(data_root / 'google_calendar' / 'client_secret.json', json.dumps({
        'installed': {
            'client_id': 'fictional-client-id.apps.googleusercontent.com',
            'client_secret': 'GOCSPX-FICTIONAL-NOT-REAL',
        }
    }, ensure_ascii=False))
    _write(data_root / 'google_calendar' / 'tokens.json', json.dumps({
        'alice': {
            'token': 'ya29.FICTIONAL-NOT-REAL',
            'refresh_token': '1//FICTIONAL-NOT-REAL',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'client_id': 'fictional-client-id.apps.googleusercontent.com',
            'scopes': ['https://www.googleapis.com/auth/calendar'],
        }
    }, ensure_ascii=False))
    _write(data_root / 'google_calendar' / 'oauth_states.json', '{}')

    # ── P5.5: 真实生产目录的杂物 (P5 只读调查实录形态) ─────────────────
    # 旧版留下的运维脚本 — 普通 .py 文件, 绝不能被新版删除或改动。
    _write(data_root / '_fix_config.py',
           '# fictional legacy ops script\n'
           'import json\n'
           'print("fictional fix script — never executed by the app")\n')
    # 历史备份子目录 — 真实生产数据目录里有旧备份副本。
    _write(data_root / 'backup' / 'todolist-20260801' / 'todo' / '旧备份里的任务.md',
           FMT.format(extra='due: 2026-08-01\nowner: alice\n', body='八月备份副本\n'))
    (data_root / 'backup' / 'todolist-20260801' / 'todo'
           ).joinpath('index.json').write_text(
        '{"note": "fictional legacy backup index"}', encoding='utf-8')
    # 未知用途的普通 JSON / 辅助文件 — 新版不得删除或修改。
    _write(data_root / 'holidays_cache.json', json.dumps(
        {'2026-10-01': 'fictional-holiday'}, ensure_ascii=False))
    _write(data_root / 'misc_notes.txt', 'fictional scratch notes\n')

    return data_root


LEGACY_ENV = {
    'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
    'Y8Y_TRUSTED_PROXIES': '127.0.0.1',
    'Y8Y_TIMEZONE': 'Asia/Tokyo',
}
