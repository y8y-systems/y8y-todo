"""Notification system V1 — API / HTTP-integration tests.

Split out of tests/test_notifications.py so that the **engine** tests stay
independent of the web layer:

  * engine + providers + state  -> tests/test_notifications.py
  * HTTP API + task lifecycle   -> tests/test_notification_api.py  (this file)

The task-lifecycle cases here drive the real HTTP server (the `server`
fixture) and then run the scheduler against the same data directory, which
is what proves that "complete / delete / change the time / roll a
recurring task forward" correctly updates the reminder state.

All data is fictional.  No network access: the provider is a fake.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parent.parent / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from notifications import (                                        # noqa: E402
    Scheduler,
    SettingsStore,
    StateStore,
    normalize_settings,
    notify_state_dir,
)
from test_notifications import (                                  # noqa: E402
    ALICE,
    BOB,
    TZ,
    FakeProvider,
    _enabled,
    _make_config,
)

# ══════════════════════════════════════════════════════════════════════
# API: /todo/api/notification-settings
# ══════════════════════════════════════════════════════════════════════

def test_notification_settings_api_requires_auth(server):
    _serve, _httpd, client, _tmp = server
    status, _body = client.get('/todo/api/notification-settings')
    assert status == 401


def test_notification_settings_api_roundtrip(server):
    _serve, _httpd, client, tmp = server
    status, body = client.get('/todo/api/notification-settings', headers=ALICE)
    assert status == 200
    assert body['settings']['enabled'] is False
    assert body['settings']['channels'] == []

    status, body = client.put('/todo/api/notification-settings', headers=ALICE, body={
        'enabled': True, 'channels': ['line'], 'lead_minutes': 30,
        'daily_digest': True, 'digest_time': '07:30',
    })
    assert status == 200
    assert body['settings']['enabled'] is True
    assert body['settings']['lead_minutes'] == 30
    assert body['settings']['daily_digest'] is True
    assert body['settings']['digest_time'] == '07:30'

    # 落盘了 (调度器读同一个文件 —— 在专用状态目录里, 见 §5.2)
    stored = json.loads(
        (tmp / 'data' / 'notify' / 'notification_settings.json').read_text(encoding='utf-8'))
    assert stored['alice']['lead_minutes'] == 30


def test_notification_settings_api_rejects_unknown_channel(server):
    _serve, _httpd, client, _tmp = server
    status, body = client.put('/todo/api/notification-settings', headers=ALICE,
                               body={'channels': ['telegram']})
    assert status == 400
    assert 'telegram' in body['error']


def test_notification_settings_are_per_user(server):
    _serve, _httpd, client, _tmp = server
    client.put('/todo/api/notification-settings', headers=ALICE,
               body={'enabled': True, 'channels': ['line']})
    status, body = client.get('/todo/api/notification-settings', headers=BOB)
    assert status == 200
    assert body['settings']['enabled'] is False
    assert body['settings']['channels'] == []


def test_notification_settings_api_never_leaks_other_users(server):
    _serve, _httpd, client, _tmp = server
    client.put('/todo/api/notification-settings', headers=ALICE,
               body={'enabled': True, 'channels': ['line'], 'lead_minutes': 45})
    status, body = client.get('/todo/api/notification-settings', headers=BOB)
    # bob 没设置过 → 拿到的是默认口径 (提前 30 分钟), 不是 alice 的 45
    assert body['settings']['lead_minutes'] == 30


# ══════════════════════════════════════════════════════════════════════
# 8. 任务生命周期 → 提醒状态正确更新 (与真实 HTTP 服务联调)
# ══════════════════════════════════════════════════════════════════════

def _data_dir(server):
    return str(server[3] / 'data')


def _scheduler_for_server(server, now, provider):
    data = _data_dir(server)
    # 通知 JSON 都在专用状态目录里 —— 与 Web 服务读写的是同一个位置
    # (docs/deployment.md §5.2)
    state_dir = notify_state_dir(data)
    settings_store = SettingsStore(os.path.join(state_dir, 'notification_settings.json'))
    normalized, err = normalize_settings(_enabled())
    assert err is None
    settings_store.set('alice', normalized)
    config = _make_config(data)
    return Scheduler(data, config, providers={'fake': provider},
                     settings_store=settings_store,
                     state_store=StateStore(os.path.join(state_dir, 'notification_state.json')),
                     now=now, tz=TZ)


def _future_due(hours_ahead=2):
    """一个将来的截止时刻 —— 提醒目标 = 截止时刻 − 30 分钟。"""
    return (datetime.now(TZ) + timedelta(hours=hours_ahead)).replace(
        second=0, microsecond=0)


def _remind_at(due):
    """提醒时刻 (默认提前 30 分钟)。"""
    return due - timedelta(minutes=30)


def test_completed_task_is_not_reminded_again(server):
    _serve, _httpd, client, _tmp = server
    due = _future_due()
    status, body = client.post('/todo/api/tasks', headers=ALICE, body={
        'title': '要完成的任务', 'due': due.date().isoformat(),
        'due_time': due.strftime('%H:%M'), 'owner': 'shared'})
    assert status == 201
    task_id = body['id']

    provider = FakeProvider()
    report = _scheduler_for_server(server, _remind_at(due), provider).run()
    assert len(report['sent']) == 1

    # 完成 → 不再提醒
    status, _body = client.post(f'/todo/api/tasks/{task_id}/done', headers=ALICE)
    assert status == 200
    report = _scheduler_for_server(server, _remind_at(due), provider).run()
    assert report['sent'] == []
    assert len(provider.sent) == 1


def test_deleted_task_is_not_reminded_again(server):
    _serve, _httpd, client, _tmp = server
    due = _future_due()
    status, body = client.post('/todo/api/tasks', headers=ALICE, body={
        'title': '要删除的任务', 'due': due.date().isoformat(),
        'due_time': due.strftime('%H:%M'), 'owner': 'shared'})
    assert status == 201
    task_id = body['id']

    provider = FakeProvider()
    assert len(_scheduler_for_server(server, _remind_at(due), provider).run()['sent']) == 1

    status, _body = client.delete(f'/todo/api/tasks/{task_id}', headers=ALICE)
    assert status == 200
    report = _scheduler_for_server(server, _remind_at(due), provider).run()
    assert report['sent'] == []
    assert len(provider.sent) == 1


def test_changing_due_time_schedules_a_new_reminder(server):
    """改了时间 → 新的一次提醒 (旧状态不能把新提醒吞掉)。"""
    _serve, _httpd, client, _tmp = server
    due = _future_due()
    status, body = client.post('/todo/api/tasks', headers=ALICE, body={
        'title': '改时间的任务', 'due': due.date().isoformat(),
        'due_time': due.strftime('%H:%M'), 'owner': 'shared'})
    assert status == 201
    task_id = body['id']

    provider = FakeProvider()
    assert len(_scheduler_for_server(server, _remind_at(due), provider).run()['sent']) == 1

    # 改到 3 小时后 → 新的提醒时刻还没到, 不应该立刻再发
    moved = due + timedelta(hours=3)
    status, _body = client.put(f'/todo/api/tasks/{task_id}', headers=ALICE,
                               body={'due': moved.date().isoformat(),
                                     'due_time': moved.strftime('%H:%M')})
    assert status == 200
    report = _scheduler_for_server(server, _remind_at(due), provider).run()
    assert report['sent'] == []
    assert len(provider.sent) == 1

    # 新时刻到了 → 新提醒必须发出来 (不是被旧的 "已发送" 记录压住)
    report = _scheduler_for_server(server, _remind_at(moved), provider).run()
    assert len(report['sent']) == 1
    assert len(provider.sent) == 2


def test_recurring_advance_schedules_the_next_reminder(server):
    """循环任务完成推进后 → 下一期要有自己的提醒。"""
    _serve, _httpd, client, _tmp = server
    due = _future_due()
    status, body = client.post('/todo/api/tasks', headers=ALICE, body={
        'title': '循环任务', 'due': due.date().isoformat(),
        'due_time': due.strftime('%H:%M'), 'owner': 'shared',
        'recurring': True, 'recurring_dates': ['5', '20']})
    assert status == 201
    task_id = body['id']

    provider = FakeProvider()
    assert len(_scheduler_for_server(server, _remind_at(due), provider).run()['sent']) == 1

    # 完成 → due 推进到下一期 (规则 5/20 → 本月的 5 号或 20 号)
    status, body = client.post(f'/todo/api/tasks/{task_id}/done', headers=ALICE)
    assert status == 200
    next_due = body.get('next_due')
    assert next_due, 'recurring completion must report the next due date'

    # 推进后的那一期要用新的提醒 —— 旧的 "已发送" 记录不能压住它
    next_dt = datetime.strptime(next_due, '%Y-%m-%d').replace(
        hour=due.hour, minute=due.minute, tzinfo=TZ)
    report = _scheduler_for_server(server, _remind_at(next_dt), provider).run()
    assert len(report['sent']) == 1
    assert len(provider.sent) == 2


def test_todo_api_still_works_without_notification_configuration(server):
    """没配通知 → Todo 的一切照旧 (不报错)。"""
    _serve, _httpd, client, _tmp = server
    status, body = client.get('/todo/api/tasks', headers=ALICE)
    assert status == 200
    assert 'tasks' in body
    status, body = client.post('/todo/api/tasks', headers=ALICE, body={
        'title': '普通任务', 'due': '2026-10-27'})
    assert status == 201
    status, body = client.get('/todo/api/notification-settings', headers=ALICE)
    assert status == 200
    assert body['available'] is False      # 调度器没跑过 → 通知未启用
