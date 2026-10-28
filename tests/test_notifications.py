"""Notification system V1 — unit + integration tests.

Covers the eight behaviours the V1 brief asks for:

1. 独立调度器 (systemd timer 友好的一次性运行)
2. 可扩展的 Provider 架构
3. LINE / Email (Resend) 两个渠道
4. 用户自选渠道 / 提前提醒时间 / 每日摘要
5. 指定时间提醒 + 全天任务早间汇总
6. 投递状态: 防重复发送 + 安全重试
7. 归属与共享任务权限: 私人任务绝不外泄
8. 完成 / 删除 / 改时间 / 循环推进后提醒状态正确更新

All data is fictional.  No network access: the providers are fakes.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SRC = Path(__file__).resolve().parent.parent / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import notifications                                              # noqa: E402
from notifications import (                                        # noqa: E402
    Capabilities,
    NotifyConfig,
    Provider,
    ProviderError,
    Scheduler,
    SettingsStore,
    StateStore,
    build_providers,
    chunk_text,
    default_settings,
    normalize_settings,
    notify_state_dir,
    publish_capabilities,
    registered_channels,
    scan_tasks,
)
from taskstore import atomic_write, build_frontmatter             # noqa: E402

TZ = ZoneInfo('Asia/Tokyo')
ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
BOB = {'Cf-Access-Authenticated-User-Email': 'bob@example.com'}


# ── 测试替身 ──────────────────────────────────────────────────────────
class FakeProvider(Provider):
    """记录投递的假渠道 (不发网络请求)。"""

    name = 'fake'
    label = '测试渠道'

    def __init__(self, config=None, transport=None, fail_times=0,
                 error=None, retryable=True, targets=None):
        super().__init__(config, transport)
        self.sent = []
        self.fail_times = fail_times
        self.error = error or ProviderError('模拟失败', retryable=retryable)
        self.targets = targets

    def configured(self):
        return True

    def target_for(self, user):
        if self.targets is not None:
            return self.targets.get(user, '')
        return 'fake-target'

    def send(self, user, message):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.error
        self.sent.append((user, message))


def _write_task(data_dir, folder, filename, **fields):
    """写一个虚构任务文件。"""
    fm = {
        'title': fields.pop('title', '虚构任务'),
        'status': fields.pop('status', 'todo'),
        'created': fields.pop('created', '2026-10-01'),
        'owner': fields.pop('owner', 'alice'),
    }
    fm.update(fields)
    path = os.path.join(str(data_dir), folder, filename)
    atomic_write(path, build_frontmatter(fm) + '\n\n正文\n')
    return path


def _make_config(data_dir, **extra):
    env = {
        'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
        'Y8Y_DATA_DIR': str(data_dir),
        'Y8Y_TIMEZONE': 'Asia/Tokyo',
        'Y8Y_NOTIFY_ENABLED': '1',
    }
    env.update(extra)
    return NotifyConfig(env)


def _make_scheduler(data_dir, providers, now, settings=None, **env_extra):
    # 通知 JSON 都在专用状态目录里 (docs/deployment.md §5.2) —— 与 Web
    # 服务读写的是同一个位置
    state_dir = notify_state_dir(data_dir)
    settings_store = SettingsStore(os.path.join(state_dir, 'notification_settings.json'))
    for user, raw in (settings or {}).items():
        normalized, err = normalize_settings(raw)
        assert err is None, err
        settings_store.set(user, normalized)
    state_store = StateStore(os.path.join(state_dir, 'notification_state.json'))
    config = _make_config(data_dir, **env_extra)
    return Scheduler(data_dir, config, providers=providers,
                     settings_store=settings_store, state_store=state_store,
                     now=now, tz=TZ)


def _enabled(channels=('fake',), **kw):
    settings = {'enabled': True, 'channels': list(channels)}
    settings.update(kw)
    return settings


# ══════════════════════════════════════════════════════════════════════
# 4. 用户设置: 渠道 / 提前提醒 / 每日摘要
# ══════════════════════════════════════════════════════════════════════

def test_default_settings_are_off():
    """默认关闭通知 —— 没有配置时 Todo 的行为与今天完全一致。"""
    s = default_settings()
    assert s['enabled'] is False
    assert s['channels'] == []
    assert s['daily_digest'] is False
    assert s['digest_time'] == '06:00'       # 业务口径: 每天 06:00 一条摘要
    assert s['lead_minutes'] == 30           # 业务口径: 事件提前 30 分钟提醒


def test_normalize_settings_accepts_valid_input():
    settings, err = normalize_settings(
        {'enabled': True, 'channels': ['line', 'email'], 'lead_minutes': 30,
         'daily_digest': True, 'digest_time': '07:30'},
        known_channels=['line', 'email'])
    assert err is None
    assert settings['enabled'] is True
    assert settings['channels'] == ['line', 'email']
    assert settings['lead_minutes'] == 30
    assert settings['digest_time'] == '07:30'


def test_normalize_settings_rejects_unknown_channel():
    """未知渠道必须报错 —— 静默忽略会让用户以为配好了。"""
    settings, err = normalize_settings({'channels': ['telegram']},
                                       known_channels=['line', 'email'])
    assert settings is None
    assert 'telegram' in err


@pytest.mark.parametrize('payload,needle', [
    ({'lead_minutes': -1}, 'lead_minutes'),
    ({'lead_minutes': 2000}, 'lead_minutes'),
    ({'lead_minutes': 'soon'}, 'lead_minutes'),
    ({'digest_time': '25:00'}, 'digest_time'),
    ({'digest_time': '8am'}, 'digest_time'),
    ({'channels': 'line'}, 'channels'),
])
def test_normalize_settings_rejects_bad_values(payload, needle):
    settings, err = normalize_settings(payload, known_channels=['line', 'email'])
    assert settings is None
    assert needle in err


def test_settings_store_roundtrip_and_isolation(tmp_path):
    store = SettingsStore(str(tmp_path / 'notification_settings.json'))
    assert store.get('alice')['enabled'] is False
    store.set('alice', _enabled(['line'], lead_minutes=15))
    assert store.get('alice')['lead_minutes'] == 15
    # 另一个用户不受影响
    assert store.get('bob')['enabled'] is False
    assert store.enabled_users() == ['alice']


def test_settings_store_ignores_corrupt_file(tmp_path):
    path = tmp_path / 'notification_settings.json'
    path.write_text('{not json', encoding='utf-8')
    store = SettingsStore(str(path))
    assert store.get('alice') == default_settings()


# ══════════════════════════════════════════════════════════════════════
# 2 + 3. Provider 架构 / LINE / Email
# ══════════════════════════════════════════════════════════════════════

def test_registered_channels_include_line_and_email():
    names = registered_channels()
    assert 'line' in names and 'email' in names


def test_provider_architecture_is_extensible(tmp_path):
    """新渠道只要继承 Provider 并注册, 调度器无需改动。"""
    class SlackProvider(Provider):
        name = 'slack'
        label = 'Slack'

        def configured(self):
            return True

        def target_for(self, user):
            return 'C123'

        def send(self, user, message):
            return None

    notifications.register_provider('slack', SlackProvider)
    try:
        assert 'slack' in registered_channels()
        providers = build_providers(_make_config(tmp_path))
        assert 'slack' in providers
        assert providers['slack'].available_for('alice')
    finally:
        notifications._PROVIDER_FACTORIES.pop('slack', None)


def test_line_provider_requires_token_and_target(tmp_path):
    config = _make_config(tmp_path)
    provider = notifications.LineProvider(config)
    assert not provider.configured()
    assert not provider.available_for('alice')

    config = _make_config(tmp_path, Y8Y_NOTIFY_LINE_TOKEN='secret-token',
                          Y8Y_NOTIFY_LINE_TARGETS='alice=U123')
    provider = notifications.LineProvider(config)
    assert provider.configured()
    assert provider.available_for('alice')
    assert not provider.available_for('bob')       # 没配目标 → 不可用


def test_email_provider_target_defaults_to_users_email(tmp_path):
    config = _make_config(tmp_path, Y8Y_NOTIFY_EMAIL_API_KEY='k',
                          Y8Y_NOTIFY_EMAIL_FROM='todo@example.com')
    provider = notifications.EmailProvider(config)
    assert provider.target_for('alice') == 'alice@example.com'
    assert provider.available_for('alice')

    config = _make_config(tmp_path, Y8Y_NOTIFY_EMAIL_API_KEY='k',
                          Y8Y_NOTIFY_EMAIL_FROM='todo@example.com',
                          Y8Y_NOTIFY_EMAIL_TARGETS='alice=other@example.com')
    assert notifications.EmailProvider(config).target_for('alice') == 'other@example.com'


def test_line_provider_sends_expected_payload(tmp_path):
    """LINE push 的请求体形状 (离线断言, 不发网络请求)。"""
    calls = []

    def transport(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return 200

    config = _make_config(tmp_path, Y8Y_NOTIFY_LINE_TOKEN='tok',
                          Y8Y_NOTIFY_LINE_TARGETS='alice=U123')
    provider = notifications.LineProvider(config, transport)
    provider.send('alice', notifications.Message('reminder', 's', '提醒正文'))

    url, payload, headers, _timeout = calls[0]
    assert url == 'https://api.line.me/v2/bot/message/push'
    assert payload['to'] == 'U123'
    assert payload['messages'][0]['text'] == '提醒正文'
    assert headers['Authorization'] == 'Bearer tok'


def test_email_provider_sends_expected_payload(tmp_path):
    calls = []

    def transport(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return 200

    config = _make_config(tmp_path, Y8Y_NOTIFY_EMAIL_API_KEY='key',
                          Y8Y_NOTIFY_EMAIL_FROM='todo@example.com')
    provider = notifications.EmailProvider(config, transport)
    provider.send('alice', notifications.Message('reminder', '主题', '正文'))

    url, payload, headers, _timeout = calls[0]
    assert url == 'https://api.resend.com/emails'
    assert payload['to'] == ['alice@example.com']
    assert payload['subject'] == '主题'
    assert payload['text'] == '正文'
    assert headers['Authorization'] == 'Bearer key'


def test_provider_http_error_is_classified(tmp_path):
    """4xx (除 408/429) 不重试; 5xx / 网络错误要重试。"""
    import urllib.error
    import urllib.request

    def make(code):
        def transport(url, payload, headers, timeout):
            raise urllib.error.HTTPError(url, code, 'err', {}, None)
        return transport

    config = _make_config(tmp_path, Y8Y_NOTIFY_LINE_TOKEN='tok',
                          Y8Y_NOTIFY_LINE_TARGETS='alice=U123')
    provider = notifications.LineProvider(config, make(401))
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('reminder', 's', 'b'))
    assert exc.value.retryable is False

    provider = notifications.LineProvider(config, make(500))
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('reminder', 's', 'b'))
    assert exc.value.retryable is True

    def net_error(url, payload, headers, timeout):
        raise urllib.error.URLError('unreachable')

    provider = notifications.LineProvider(config, net_error)
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('reminder', 's', 'b'))
    assert exc.value.retryable is True


def test_chunk_text_splits_long_bodies():
    chunks = chunk_text('a' * 9000, 4000, 5)
    assert len(chunks) == 3
    assert all(len(c) <= 4000 for c in chunks)
    assert chunk_text('短', 4000, 5) == ['短']


# ══════════════════════════════════════════════════════════════════════
# 5. 指定时间提醒 + 全天任务早间汇总
# ══════════════════════════════════════════════════════════════════════

def test_timed_task_reminder_is_sent_at_due_minus_lead(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()

    # 提前 30 分钟 → 08:30 起可发
    early = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 29, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=30)})
    assert early.run()['sent'] == []

    due_time = _make_scheduler(data, {'fake': provider},
                               now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                               settings={'alice': _enabled(lead_minutes=30)})
    report = due_time.run()
    assert len(report['sent']) == 1
    assert report['sent'][0]['user'] == 'alice'
    user, message = provider.sent[0]
    assert user == 'alice'
    assert '开会' in message.body
    assert '09:00' in message.body
    assert '提前 30 分钟' in message.body


def test_all_day_task_is_not_point_reminded(tmp_path):
    """没有时间的任务只进每日摘要, 不走单点提醒。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='买牛奶', due='2026-10-27', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 6, 0, tzinfo=TZ),
                            settings={'alice': _enabled(daily_digest=True,
                                                        digest_time='06:00')})
    report = sched.run()
    # 单点提醒没有发 (只有摘要)
    assert [item for item in report['sent'] if item['kind'] == 'reminder'] == []
    assert len(provider.sent) == 1
    assert provider.sent[0][1].kind == 'digest'
    assert '买牛奶' in provider.sent[0][1].body


def test_daily_digest_lists_today_tasks_only(tmp_path):
    """摘要只讲**今天**的事 —— 昨天的不补进来, 明天的也不提前讲。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='今天的任务', due='2026-10-27', owner='shared')
    _write_task(data, 'todo', 'b.md', title='昨天的任务', due='2026-10-26', owner='shared')
    _write_task(data, 'todo', 'c.md', title='明天的任务', due='2026-10-28', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 6, 0, tzinfo=TZ),
                            settings={'alice': _enabled(daily_digest=True,
                                                        digest_time='06:00')})
    sched.run()
    body = provider.sent[0][1].body
    assert '今天的任务' in body
    assert '昨天的任务' not in body
    assert '明天的任务' not in body


def test_digest_is_not_sent_before_digest_time(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='今天的任务', due='2026-10-27', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 5, 59, tzinfo=TZ),
                            settings={'alice': _enabled(daily_digest=True,
                                                        digest_time='06:00')})
    sched.run()
    assert provider.sent == []

    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 6, 0, tzinfo=TZ),
                            settings={'alice': _enabled(daily_digest=True,
                                                        digest_time='06:00')})
    sched.run()
    assert len(provider.sent) == 1


def test_digest_not_sent_when_nothing_to_say(tmp_path):
    data = tmp_path / 'data'
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 6, 0, tzinfo=TZ),
                            settings={'alice': _enabled(daily_digest=True,
                                                        digest_time='06:00')})
    sched.run()
    assert provider.sent == []


# ══════════════════════════════════════════════════════════════════════
# 6. 投递状态: 防重复发送 + 安全重试
# ══════════════════════════════════════════════════════════════════════

def test_same_occurrence_is_not_sent_twice(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()
    for _ in range(3):
        sched = _make_scheduler(data, {'fake': provider},
                                now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                                settings={'alice': _enabled(lead_minutes=0)})
        sched.run()
    assert len(provider.sent) == 1


def test_failed_send_is_not_retried(tmp_path):
    """at-most-once: 发送失败就是终态 —— 不重试, 不补发。

    代价是明确的: 偶发网络故障会丢掉这一条通知, 而不是延迟之后补上。
    """
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider(fail_times=1)
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    report = sched.run()
    assert report['sent'] == []
    assert report['failed'][0]['status'] == 'failed'

    # 下一轮 (还在同一个窗口里) 也**不会**再打一次接口
    provider.fail_times = 0
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 1, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    assert sched.run()['sent'] == []
    assert sched.run()['failed'] == []
    assert provider.sent == []


def test_non_retryable_failure_is_not_retried(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider(fail_times=5, error=ProviderError('凭据无效', retryable=False))
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    report = sched.run()
    assert report['failed'][0]['status'] == 'exhausted'

    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 5, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    assert sched.run()['failed'] == []      # 放弃了, 不再打接口


def test_failed_state_is_persisted_across_processes(tmp_path):
    """状态存在磁盘上 —— 调度器重启后不会把失败的通知当成"没发过"。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider(fail_times=1)
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    sched.run()

    state = json.loads(
        (data / 'notify' / 'notification_state.json').read_text(encoding='utf-8'))
    entry = list(state['reminders'].values())[0]
    assert entry['channels']['fake']['status'] == 'failed'
    assert entry['channels']['fake']['attempts'] == 1
    # 没有"下次重试时间" —— 这条通知不会再试了
    assert not entry['channels']['fake'].get('next_attempt_at')


def test_state_prunes_stale_entries(tmp_path):
    """过期记录会被清理 (默认保留 30 天 —— 任务"复活"时不重复提醒)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    sched.run()
    state_file = data / 'notify' / 'notification_state.json'
    assert json.loads(state_file.read_text(encoding='utf-8'))['reminders']

    # 任务被删掉 → 记录暂时保留 (保留期内"复活"不重复提醒)
    os.remove(data / 'todo' / 'a.md')
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 5, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    sched.run()
    assert json.loads(state_file.read_text(encoding='utf-8'))['reminders']

    # 记录过期 → 清理掉, 状态文件不会无限增长
    state = json.loads(state_file.read_text(encoding='utf-8'))
    old = (datetime.now(TZ) - timedelta(days=90)).isoformat()
    for entry in state['reminders'].values():
        entry['updated_at'] = old
    atomic_write(str(state_file), json.dumps(state, ensure_ascii=False))

    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 10, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)})
    sched.run()
    assert json.loads(state_file.read_text(encoding='utf-8'))['reminders'] == {}


# ══════════════════════════════════════════════════════════════════════
# 7. 归属与共享任务权限
# ══════════════════════════════════════════════════════════════════════

def test_private_task_is_never_notified(tmp_path):
    """通知范围 ≠ 可见范围: 私人任务谁都不发 (连本人自己也不发)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='alice的私人任务', due='2026-10-27',
                due_time='09:00', owner='alice')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled(), 'bob': _enabled()})
    sched.run()
    assert provider.sent == []
    for _user, message in provider.sent:
        assert 'alice的私人任务' not in message.body


def test_private_task_never_leaks_to_other_users(tmp_path):
    """别人的私人任务永远不会出现在任何人的通知里 (含 shared 范围)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='alice的私人任务', due='2026-10-27',
                due_time='09:00', owner='alice')
    _write_task(data, 'todo', 'b.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled(), 'bob': _enabled()})
    sched.run()
    recipients = sorted({user for user, _msg in provider.sent})
    assert recipients == ['alice', 'bob']
    for _user, message in provider.sent:
        assert 'alice的私人任务' not in message.body
        assert '共享任务' in message.body


def test_own_and_shared_scope_keeps_the_v1_behaviour(tmp_path):
    """显式退回 V1 范围时, 本人的任务也进通知 (默认不是这样)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    _write_task(data, 'todo', 'b.md', title='alice的私人任务', due='2026-10-27',
                due_time='09:00', owner='alice')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled(), 'bob': _enabled()},
                            Y8Y_NOTIFY_TASK_SCOPE='own_and_shared')
    sched.run()
    bodies = {user: message.body for user, message in provider.sent}
    assert 'alice的私人任务' in bodies['alice']
    assert 'alice的私人任务' not in bodies['bob']
    assert '共享任务' in bodies['bob']


def test_shared_task_is_visible_to_every_enabled_user(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled(), 'bob': _enabled()})
    sched.run()
    recipients = sorted({user for user, _msg in provider.sent})
    assert recipients == ['alice', 'bob']


def test_recipient_allowlist_limits_who_receives(tmp_path):
    """灰度: 收件人白名单之外的已启用用户收不到任何通知。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled(), 'bob': _enabled()},
                            Y8Y_NOTIFY_RECIPIENTS='alice')
    sched.run()
    recipients = sorted({user for user, _msg in provider.sent})
    assert recipients == ['alice']


def test_disabled_user_gets_nothing(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled()})
    sched.run()
    recipients = {user for user, _msg in provider.sent}
    assert recipients == {'alice'}


def test_legacy_task_without_owner_follows_default_owner(tmp_path):
    """旧数据没有 owner 字段 → 与 Web 端一致地归给第一个配置用户。

    (范围用 V1 的 own_and_shared, 因为默认范围只看共享任务。)
    """
    data = tmp_path / 'data'
    path = os.path.join(str(data), 'todo', 'legacy.md')
    atomic_write(path, '---\ntitle: 旧任务\ndue: 2026-10-27\ndue_time: 09:00\n---\n\n正文\n')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled(), 'bob': _enabled()},
                            Y8Y_NOTIFY_TASK_SCOPE='own_and_shared')
    sched.run()
    recipients = {user for user, _msg in provider.sent}
    assert recipients == {'alice'}      # default_owner = alice


# ══════════════════════════════════════════════════════════════════════
# 1. 调度器行为: 关闭 / 无渠道 / 补发窗口 / dry-run
# ══════════════════════════════════════════════════════════════════════

def test_notifications_are_off_by_default(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='alice')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled()},
                            Y8Y_NOTIFY_ENABLED='0')
    report = sched.run()
    assert report['sent'] == []
    assert provider.sent == []


def test_missing_channel_configuration_is_not_an_error(tmp_path):
    """渠道没配置 → 跳过, 不抛异常 (Todo 不能因为通知崩掉)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='alice')
    provider = FakeProvider(targets={})          # 谁都没有目标
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled()})
    report = sched.run()
    assert report['sent'] == []
    assert report['skipped']
    assert not report['failed']


def test_user_without_channels_is_skipped(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='alice')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': {'enabled': True, 'channels': []}})
    report = sched.run()
    assert report['sent'] == []
    assert report['skipped']


def test_window_bounds_late_reminders(tmp_path):
    """错过触发窗口 → 不补发 (只有窗口内的延迟可以接受)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()

    # 窗口外 (晚了一整天) → 不发
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 29, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=30)})
    assert sched.run()['sent'] == []

    # 窗口内 (目标时刻 + 1 分钟) → 发
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 31, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=30)})
    assert len(sched.run()['sent']) == 1


def test_dry_run_does_not_send_or_write(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=30)})
    report = sched.run(dry_run=True)
    assert provider.sent == []
    assert report['planned']
    assert not (data / 'notify' / 'notification_state.json').exists()


def test_scan_tasks_ignores_done_and_broken_files(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='活动任务', due='2026-10-27')
    _write_task(data, 'todo', 'b.md', title='已完成', status='done')
    atomic_write(os.path.join(str(data), 'todo', 'c.md'), 'not a task file')
    tasks = scan_tasks(str(data), 'alice')
    titles = [t.title for t in tasks]
    assert '活动任务' in titles
    assert '已完成' not in titles


def test_invalid_due_time_treated_as_all_day(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='坏时间', due='2026-10-27',
                due_time='25:99', owner='alice')
    tasks = scan_tasks(str(data), 'alice')
    assert tasks[0].has_time is False


# ══════════════════════════════════════════════════════════════════════
# 能力表 (Web 端看不到凭据, 只看"谁能用哪个渠道")
# ══════════════════════════════════════════════════════════════════════

def test_capabilities_table_contains_no_credentials(tmp_path):
    config = _make_config(tmp_path, Y8Y_NOTIFY_LINE_TOKEN='super-secret',
                          Y8Y_NOTIFY_LINE_TARGETS='alice=U123')
    providers = build_providers(config)
    path = str(tmp_path / 'notification_channels.json')
    payload = publish_capabilities(path, config, providers)

    text = (tmp_path / 'notification_channels.json').read_text(encoding='utf-8')
    assert 'super-secret' not in text
    assert 'U123' not in text
    by_name = {ch['name']: ch for ch in payload['channels']}
    assert by_name['line']['users'] == ['alice']
    # 邮件渠道来自 Y8Y_USERS 的邮箱映射 —— 也是能力表的一部分
    assert by_name['email']['users'] == ['alice', 'bob']

    caps = Capabilities(path)
    assert caps.enabled() is True
    line = [c for c in caps.channels_for('alice') if c['name'] == 'line'][0]
    assert line['available'] is True
    assert [c for c in caps.channels_for('bob') if c['name'] == 'line'][0]['available'] is False


def test_capabilities_missing_file_is_disabled(tmp_path):
    caps = Capabilities(str(tmp_path / 'nope.json'))
    assert caps.enabled() is False
    assert caps.channels_for('alice') == []
