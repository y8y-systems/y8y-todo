"""通知策略的行为测试 —— 把业务口径钉死。

目标语义 (本次调整的**唯一**目标):

1. 只通知**共享**任务 (``owner: shared``) —— 私人任务谁都不发;
2. 每天 06:00 (Asia/Tokyo) 一条摘要, 含当天带时间的与全天的任务, **不含逾期**;
3. 带时间的任务在截止前 30 分钟提醒一次;
4. at-most-once: 错过不补发、失败不重试、重复调度不重复发;
5. 摘要与单点提醒互相独立 (早上在摘要里出现过, 到点照样提醒)。

全部数据都是虚构的, LINE 渠道是**模拟**的 (不发网络请求)。
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

import notifications                                              # noqa: E402
from notifications import (                                        # noqa: E402
    LineProvider,
    NotifyConfig,
    ProviderError,
    Scheduler,
    SettingsStore,
    StateStore,
    default_settings,
    normalize_settings,
    notify_state_dir,
)
from taskstore import atomic_write, build_frontmatter             # noqa: E402

TZ = ZoneInfo('Asia/Tokyo')
TODAY = '2026-10-27'


# ── 模拟的 LINE 渠道 ──────────────────────────────────────────────────
class FakeLineProvider(LineProvider):
    """模拟 LINE Messaging API —— 记录投递, 绝不发网络请求。"""

    name = 'line'
    label = 'LINE'

    def __init__(self, config=None, transport=None, fail_times=0, error=None):
        super().__init__(config, transport)
        self.sent = []
        self.calls = 0
        self.fail_times = fail_times
        self.error = error or ProviderError('模拟的 LINE 接口失败', retryable=True)

    def configured(self):
        return True

    def target_for(self, user):
        return 'U-fake-target'

    def send(self, user, message):
        self.calls += 1
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
        'owner': fields.pop('owner', 'shared'),
    }
    fm.update(fields)
    path = os.path.join(str(data_dir), folder, filename)
    atomic_write(path, build_frontmatter(fm) + '\n\n正文\n')
    return path


def _make_config(data_dir, **extra):
    env = {
        'Y8Y_USERS': 'kara@example.com=kara,bridge@example.com=bridge',
        'Y8Y_DATA_DIR': str(data_dir),
        'Y8Y_TIMEZONE': 'Asia/Tokyo',
        'Y8Y_NOTIFY_ENABLED': '1',
    }
    env.update(extra)
    return NotifyConfig(env)


def _scheduler(data_dir, provider, now, settings=None, **env_extra):
    state_dir = notify_state_dir(data_dir)
    settings_store = SettingsStore(os.path.join(state_dir, 'notification_settings.json'))
    for user, raw in (settings or {}).items():
        normalized, err = normalize_settings(raw)
        assert err is None, err
        settings_store.set(user, normalized)
    state_store = StateStore(os.path.join(state_dir, 'notification_state.json'))
    config = _make_config(data_dir, **env_extra)
    return Scheduler(data_dir, config, providers={'line': provider},
                     settings_store=settings_store, state_store=state_store,
                     now=now, tz=TZ)


def _kara(**kw):
    """kara 的通知设置 (默认: 开通知 + LINE + 每日摘要)。"""
    settings = {'enabled': True, 'channels': ['line'], 'daily_digest': True}
    settings.update(kw)
    return settings


def _at(hh, mm, day=TODAY):
    return datetime(2026, 10, int(day[-2:]), hh, mm, tzinfo=TZ)


# ══════════════════════════════════════════════════════════════════════
# 1. 任务范围: 只发共享任务, 私人任务一律不发
# ══════════════════════════════════════════════════════════════════════

def test_shared_task_is_sent_and_private_task_is_not(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'shared.md', title='共享任务', due=TODAY,
                due_time='09:00', owner='shared')
    _write_task(data, 'todo', 'kara.md', title='kara的私人任务', due=TODAY,
                due_time='09:00', owner='kara')
    _write_task(data, 'todo', 'bridge.md', title='bridge的私人任务', due=TODAY,
                due_time='09:00', owner='bridge')

    provider = FakeLineProvider()
    _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()

    assert len(provider.sent) == 1
    user, message = provider.sent[0]
    assert user == 'kara'
    assert '共享任务' in message.body
    assert 'kara的私人任务' not in message.body
    assert 'bridge的私人任务' not in message.body


def test_other_users_tasks_never_reach_kara(tmp_path):
    """别人的私人任务 (owner=bridge) 也不会进 kara 的通知。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'bridge.md', title='bridge的私人任务', due=TODAY,
                due_time='09:00', owner='bridge')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()
    assert provider.sent == []


def test_recipient_allowlist_limits_the_delivery(tmp_path):
    """灰度: 白名单之外的用户 (即便开了通知、有目标) 也收不到。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'shared.md', title='共享任务', due=TODAY,
                due_time='09:00', owner='shared')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(8, 30),
               settings={'kara': _kara(), 'bridge': _kara()},
               Y8Y_NOTIFY_RECIPIENTS='kara').run()
    assert {user for user, _m in provider.sent} == {'kara'}


# ══════════════════════════════════════════════════════════════════════
# 2 + 3. 每日 06:00 摘要: 当天带时间的 + 全天的, 不含逾期
# ══════════════════════════════════════════════════════════════════════

def test_daily_digest_at_six_contains_timed_and_all_day_tasks(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    _write_task(data, 'todo', 'early.md', title='站会', due=TODAY,
                due_time='09:00', owner='shared')
    _write_task(data, 'todo', 'allday.md', title='买牛奶', due=TODAY, owner='shared')

    provider = FakeLineProvider()
    _scheduler(data, provider, _at(6, 0), settings={'kara': _kara()}).run()

    assert len(provider.sent) == 1
    body = provider.sent[0][1].body
    assert '客户会议' in body
    assert '站会' in body
    assert '买牛奶' in body
    # 带时间的按时间排序, 全天任务单独一节
    assert body.index('09:00 站会') < body.index('14:00 客户会议')
    assert '全天任务' in body


def test_digest_default_time_is_six_in_the_morning(tmp_path):
    """默认摘要时间就是 06:00 (不需要额外配置)。"""
    assert default_settings()['digest_time'] == '06:00'
    assert default_settings()['lead_minutes'] == 30

    data = tmp_path / 'data'
    _write_task(data, 'todo', 'allday.md', title='买牛奶', due=TODAY, owner='shared')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(5, 59), settings={'kara': _kara()}).run()
    assert provider.sent == []                     # 还没到 06:00

    provider2 = FakeLineProvider()
    _scheduler(data, provider2, _at(6, 0), settings={'kara': _kara()}).run()
    assert len(provider2.sent) == 1


def test_overdue_tasks_are_not_in_todays_digest(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'overdue.md', title='昨天的任务', due='2026-10-26',
                owner='shared')
    _write_task(data, 'todo', 'today.md', title='今天的任务', due=TODAY, owner='shared')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(6, 0), settings={'kara': _kara()}).run()
    body = provider.sent[0][1].body
    assert '今天的任务' in body
    assert '昨天的任务' not in body


def test_no_digest_when_nothing_is_due_today(tmp_path):
    data = tmp_path / 'data'
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(6, 0), settings={'kara': _kara()}).run()
    assert provider.sent == []                     # 不发空摘要


# ══════════════════════════════════════════════════════════════════════
# 4 + 5. 事件提前 30 分钟提醒; 摘要与提醒互不影响
# ══════════════════════════════════════════════════════════════════════

def test_event_reminder_fires_thirty_minutes_before_due(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    provider = FakeLineProvider()

    _scheduler(data, provider, _at(13, 29), settings={'kara': _kara()}).run()
    assert provider.sent == []                     # 还没到 13:30

    _scheduler(data, provider, _at(13, 30), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1
    body = provider.sent[0][1].body
    assert '客户会议' in body
    assert '14:00' in body
    assert '提前 30 分钟' in body


def test_digest_and_event_reminder_are_independent(tmp_path):
    """早上 06:00 出现在摘要里的事件, 到点照样提醒 (两条互不影响)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    provider = FakeLineProvider()

    _scheduler(data, provider, _at(6, 0), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1
    assert provider.sent[0][1].kind == 'digest'

    _scheduler(data, provider, _at(13, 30), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 2
    assert provider.sent[1][1].kind == 'reminder'


# ══════════════════════════════════════════════════════════════════════
# 6 + 7 + 9. 不重复、不补发、重启不补历史
# ══════════════════════════════════════════════════════════════════════

def test_repeated_scheduling_does_not_send_twice(tmp_path):
    """重复调度 (timer 连着触发) → 只发一次。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    provider = FakeLineProvider()
    for minute in (13, 30, 31, 32):
        _scheduler(data, provider, _at(13, minute), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1
    assert provider.calls == 1


def test_digest_is_sent_at_most_once_per_day(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'allday.md', title='买牛奶', due=TODAY, owner='shared')
    provider = FakeLineProvider()
    for minute in (0, 1, 2, 3, 4, 5):
        _scheduler(data, provider, _at(6, minute), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1


def test_missed_window_is_never_sent_later(tmp_path):
    """错过触发窗口 → 永远不补发 (摘要与单点提醒都是)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    _write_task(data, 'todo', 'allday.md', title='买牛奶', due=TODAY, owner='shared')
    provider = FakeLineProvider()

    # 调度器 06:00 时不可用 → 09:00 才恢复
    _scheduler(data, provider, _at(9, 0), settings={'kara': _kara()}).run()
    assert provider.sent == []                     # 摘要没补发

    # 事件提醒的窗口 (13:30) 还没到 → 也没发
    assert provider.sent == []

    # 错过了事件提醒的窗口 (13:30 + 10 分钟) → 依然不补发
    _scheduler(data, provider, _at(14, 30), settings={'kara': _kara()}).run()
    assert provider.sent == []
    assert provider.calls == 0


def test_missed_digest_is_recorded(tmp_path):
    """错过的摘要在投递状态里留痕 (运维看得到"今天没发出去")。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'allday.md', title='买牛奶', due=TODAY, owner='shared')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(9, 0), settings={'kara': _kara()}).run()

    state = json.loads(
        (data / 'notify' / 'notification_state.json').read_text(encoding='utf-8'))
    entry = state['digests'][f'kara|{TODAY}|line']
    assert entry['status'] == 'missed'


def test_restart_does_not_send_historical_reminders(tmp_path):
    """调度器重启 (新进程) 不会把历史提醒再发一遍。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(13, 30), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1

    # "重启": 新进程、新状态文件读取、同一个 provider 实例
    _scheduler(data, provider, _at(13, 32), settings={'kara': _kara()}).run()
    _scheduler(data, provider, _at(13, 35), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1
    assert provider.calls == 1


# ══════════════════════════════════════════════════════════════════════
# 8. Provider 失败不自动重试
# ══════════════════════════════════════════════════════════════════════

def test_provider_failure_is_not_retried(tmp_path):
    """LINE 接口失败 → 记成终态, 不自动重试 (at-most-once)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    provider = FakeLineProvider(fail_times=1)
    report = _scheduler(data, provider, _at(13, 30),
                        settings={'kara': _kara()}).run()
    assert report['sent'] == []
    assert report['failed'][0]['status'] == 'failed'
    assert provider.calls == 1

    # 下一轮还在窗口里 → 不再打接口 (失败的这一次就是那一次机会)
    report = _scheduler(data, provider, _at(13, 32),
                        settings={'kara': _kara()}).run()
    assert report['sent'] == []
    assert provider.calls == 1


def test_failed_digest_is_not_retried(tmp_path):
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'allday.md', title='买牛奶', due=TODAY, owner='shared')
    provider = FakeLineProvider(fail_times=1)
    report = _scheduler(data, provider, _at(6, 0), settings={'kara': _kara()}).run()
    assert report['failed'][0]['kind'] == 'digest'
    assert provider.calls == 1

    report = _scheduler(data, provider, _at(6, 2), settings={'kara': _kara()}).run()
    assert provider.calls == 1


# ══════════════════════════════════════════════════════════════════════
# 10. 循环任务推进 + 改时间 → 各自有新的提醒机会
# ══════════════════════════════════════════════════════════════════════

def test_recurring_advance_gets_its_own_reminder(tmp_path):
    """循环任务推进到下一期 → 新的 occurrence → 新的提醒机会。"""
    data = tmp_path / 'data'
    _write_task(data, 'recurring', 'weekly.md', title='周会', due=TODAY,
                due_time='09:00', owner='shared', recurring=['5', '20'])

    provider = FakeLineProvider()
    _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1

    # 推进到下一期 (due 变了) → 新的提醒时刻还没到
    _write_task(data, 'recurring', 'weekly.md', title='周会', due='2026-11-05',
                due_time='09:00', owner='shared', recurring=['5', '20'])
    _scheduler(data, provider, _at(8, 35), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1

    # 下一期的提醒时刻到了 → 必须发出来 (旧的 "已发送" 不能压住它)
    _scheduler(data, provider, datetime(2026, 11, 5, 8, 30, tzinfo=TZ),
               settings={'kara': _kara()}).run()
    assert len(provider.sent) == 2


def test_changing_the_time_recalculates_the_reminder(tmp_path):
    """改了截止时间 → 按新的时间重新算 (旧的提醒状态不作数)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='14:00', owner='shared')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(13, 30), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1

    # 改到 16:00 → 新的提醒时刻 (15:30) 还没到
    _write_task(data, 'todo', 'event.md', title='客户会议', due=TODAY,
                due_time='16:00', owner='shared')
    _scheduler(data, provider, _at(13, 35), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1

    # 新的提醒时刻到了 → 新的提醒必须发出来
    _scheduler(data, provider, _at(15, 30), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 2


# ══════════════════════════════════════════════════════════════════════
# 11. 原有 Todo 任务格式 / API / Web UI 不变
# ══════════════════════════════════════════════════════════════════════

def test_task_file_format_is_unchanged(tmp_path):
    """通知策略不碰任务文件格式 —— frontmatter 字段与写盘形状不变。"""
    data = tmp_path / 'data'
    path = _write_task(data, 'todo', 'a.md', title='虚构任务', due=TODAY,
                       due_time='09:00', owner='shared', project='装修')
    text = Path(path).read_text(encoding='utf-8')
    assert text.startswith('---\n')
    assert 'title: 虚构任务\n' in text
    assert f'due: {TODAY}\n' in text
    assert 'due_time: 09:00\n' in text
    assert 'owner: shared\n' in text
    assert 'status: todo\n' in text
    # 通知设置**不**写进任务文件 (旁挂文件, 见 AGENTS.md §3)
    assert 'notify' not in text
    assert 'lead_minutes' not in text
    assert 'digest_time' not in text


def test_notification_settings_contract_is_unchanged():
    """通知设置的字段集合不变 → Web 端与 API 契约不需要改。"""
    assert set(default_settings()) == {
        'enabled', 'channels', 'lead_minutes', 'daily_digest', 'digest_time'}
    settings, err = normalize_settings(
        {'enabled': True, 'channels': ['line'], 'lead_minutes': 30,
         'daily_digest': True, 'digest_time': '06:00'},
        known_channels=['line', 'email'])
    assert err is None
    assert set(settings) == set(default_settings())


def test_web_ui_panel_is_unchanged():
    """Web UI 不动: 通知面板还在, 用的还是同一个接口。"""
    html = (SRC / 'index.html').read_text(encoding='utf-8')
    for marker in ('notifyEnabledBtn', 'notifyDigestBtn', 'notifyChannelsRow',
                   'notifyLeadMinutes', 'notifyDigestTime', 'notifySaveBtn'):
        assert marker in html, marker
    assert '/todo/api/notification-settings' in html
