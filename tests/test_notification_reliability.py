"""通知投递的**可靠性**回归测试 —— 崩溃 / 并发 / 状态损坏 / 首次初始化。

这三类问题与业务口径无关, 但决定了 at-most-once 语义是否真的成立:

1. **写前持久化** —— 投递尝试必须**先落盘**再调 LINE Provider。进程崩在
   "已经发出去了"与"结果回写"之间时, 状态文件里留下的是 ``sending``,
   下一轮看到它就不再重发 (宁可漏发, 绝不重发)。
2. **状态损坏 fail-closed** —— ``notification_state.json`` 读不出来时
   绝不能当成全新空状态 (那会把所有到期提醒**重发一遍**)。
3. **并发互斥** —— 两个调度进程不能同时认领同一条通知 (``flock``)。

全部数据都是虚构的, LINE 渠道是**模拟**的 (不发网络请求, 不碰真实凭据)。
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SRC = Path(__file__).resolve().parent.parent / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import notify_runner                                                  # noqa: E402
from notifications import (                                            # noqa: E402
    IN_FLIGHT_STATUS,
    LOCK_FILE,
    DeliveryLock,
    LineProvider,
    NotificationStateCorrupt,
    NotifyConfig,
    ProviderError,
    Scheduler,
    SettingsStore,
    StateStore,
    normalize_settings,
    notify_state_dir,
)
from taskstore import atomic_write, build_frontmatter                 # noqa: E402

TZ = ZoneInfo('Asia/Tokyo')
TODAY = '2026-10-27'


class SimulatedCrash(Exception):
    """模拟进程在"已经发出去了"与"结果回写"之间崩掉。"""


class FakeLineProvider(LineProvider):
    """模拟 LINE Messaging API —— 记录投递, 绝不发网络请求。"""

    name = 'line'
    label = 'LINE'

    def __init__(self, config=None, transport=None, fail_times=0, error=None,
                 on_send=None):
        super().__init__(config, transport)
        self.sent = []          # 真正投递成功的
        self.attempted = []     # provider 被调用到的 (可能随后崩掉)
        self.calls = 0
        self.fail_times = fail_times
        self.error = error or ProviderError('模拟的 LINE 接口失败', retryable=True)
        self.on_send = on_send  # 回调: 可以抛异常模拟崩溃

    def configured(self):
        return True

    def target_for(self, user):
        return 'U-fake-target'

    def send(self, user, message):
        self.calls += 1
        self.attempted.append((user, message))
        if self.on_send is not None:
            self.on_send(self, user, message)
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
        'Y8Y_USERS': 'kara@example.com=kara',
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
    """kara 的通知设置 (默认: 开通知 + LINE, 不发每日摘要)。"""
    settings = {'enabled': True, 'channels': ['line'], 'daily_digest': False}
    settings.update(kw)
    return settings


def _at(hh, mm, day=TODAY):
    return datetime(2026, 10, int(day[-2:]), hh, mm, tzinfo=TZ)


def _state_file(data_dir):
    return Path(notify_state_dir(data_dir)) / 'notification_state.json'


def _reminder_state(data_dir):
    """状态文件里的第一条提醒记录。"""
    raw = json.loads(_state_file(data_dir).read_text(encoding='utf-8'))
    entry = list(raw['reminders'].values())[0]
    return entry['channels']['line']


def _digest_state(data_dir):
    """状态文件里的第一条摘要记录。"""
    raw = json.loads(_state_file(data_dir).read_text(encoding='utf-8'))
    key = next(iter(raw['digests']))
    return raw['digests'][key]


# ══════════════════════════════════════════════════════════════════════
# 1. 写前持久化: 投递尝试先落盘, 再调 LINE Provider
# ══════════════════════════════════════════════════════════════════════

def test_the_reminder_attempt_is_persisted_before_the_provider_is_called(tmp_path):
    """provider 被调用的那一刻, 状态文件里必须**已经**有这次投递的记录。

    否则 "已经发出去了但结果没回写" 的崩溃窗口就会变成一次重复投递。
    """
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    seen = {}

    def peek(provider, user, message):
        seen.update(_reminder_state(data))

    provider = FakeLineProvider(on_send=peek)
    _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()

    # provider 被调用时看到的是中间态 "sending" —— 已经是终态语义
    assert seen['status'] == IN_FLIGHT_STATUS
    assert seen['attempts'] == 1
    # 发送成功后落成终态
    assert _reminder_state(data)['status'] == 'sent'


def test_the_digest_attempt_is_persisted_before_the_provider_is_called(tmp_path):
    """摘要同理 —— 每天只有一条, 崩在中间绝不能第二天再补一条。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='买牛奶', due=TODAY, owner='shared')
    seen = {}

    def peek(provider, user, message):
        seen.update(_digest_state(data))

    provider = FakeLineProvider(on_send=peek)
    _scheduler(data, provider, _at(6, 0),
               settings={'kara': _kara(daily_digest=True)}).run()

    assert seen['status'] == IN_FLIGHT_STATUS
    assert seen['attempts'] == 1
    assert _digest_state(data)['status'] == 'sent'


def test_a_crash_after_the_reminder_send_does_not_lead_to_a_resend(tmp_path):
    """崩溃回归: 进程在 send 之后、结果回写之前挂掉 → 下一轮不重发。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')

    def crash(provider, user, message):
        raise SimulatedCrash('模拟进程在 LINE 投递之后崩溃')

    provider = FakeLineProvider(on_send=crash)
    sched = _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()})
    with pytest.raises(SimulatedCrash):
        sched.run()

    # 崩溃后留在盘上的是中间态 (而不是"没发过")
    assert _reminder_state(data)['status'] == IN_FLIGHT_STATUS

    # 进程重启后的下一轮: **不再**调一次 LINE 接口
    provider2 = FakeLineProvider()
    report = _scheduler(data, provider2, _at(8, 31),
                        settings={'kara': _kara()}).run()
    assert provider2.calls == 0
    assert report['sent'] == []


def test_a_crash_after_the_digest_send_does_not_lead_to_a_resend(tmp_path):
    """摘要的崩溃回归 —— 同一天不再发第二次。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='买牛奶', due=TODAY, owner='shared')

    def crash(provider, user, message):
        raise SimulatedCrash('模拟进程在摘要投递之后崩溃')

    provider = FakeLineProvider(on_send=crash)
    sched = _scheduler(data, provider, _at(6, 0),
                       settings={'kara': _kara(daily_digest=True)})
    with pytest.raises(SimulatedCrash):
        sched.run()

    assert _digest_state(data)['status'] == IN_FLIGHT_STATUS

    provider2 = FakeLineProvider()
    report = _scheduler(data, provider2, _at(6, 1),
                        settings={'kara': _kara(daily_digest=True)}).run()
    assert provider2.calls == 0
    assert report['sent'] == []


def test_the_intermediate_state_blocks_a_resend_even_inside_the_window(tmp_path):
    """中间态也属于"不再投" —— 窗口还没过也不能重发。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    state = _state_file(data)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({
        'version': 1,
        'reminders': {
            'kara|shared|todo/a.md|2026-10-01|2026-10-27T09:00': {
                'channels': {'line': {'status': IN_FLIGHT_STATUS,
                                      'at': '2026-10-27T00:00:00+00:00',
                                      'attempts': 1}},
                'updated_at': '2026-10-27T00:00:00+00:00',
            },
        },
        'digests': {},
    }), encoding='utf-8')

    provider = FakeLineProvider()
    _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()
    assert provider.calls == 0


# ══════════════════════════════════════════════════════════════════════
# 2. 状态损坏 → fail closed
# ══════════════════════════════════════════════════════════════════════

def test_a_corrupt_state_file_stops_every_delivery(tmp_path):
    """状态文件坏了 → 一条都不发 (绝不当成"全新空状态")。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    state = _state_file(data)
    state.parent.mkdir(parents=True, exist_ok=True)
    broken = '{ 这不是合法 JSON'
    state.write_text(broken, encoding='utf-8')

    provider = FakeLineProvider()
    sched = _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()})
    with pytest.raises(NotificationStateCorrupt):
        sched.run()

    assert provider.calls == 0
    # 损坏的文件不能被"空状态"悄悄覆盖掉 —— 人工还得能看现场
    assert state.read_text(encoding='utf-8') == broken


def test_a_state_file_that_is_not_an_object_is_treated_as_corrupt(tmp_path):
    """JSON 合法但结构不对 → 同样是损坏。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    state = _state_file(data)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('["不是", "一个", "对象"]', encoding='utf-8')

    provider = FakeLineProvider()
    with pytest.raises(NotificationStateCorrupt):
        _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()
    assert provider.calls == 0


def test_a_state_file_with_broken_sections_is_treated_as_corrupt(tmp_path):
    """reminders / digests 结构不对 → 判重依据已经不可信 → 停发。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    state = _state_file(data)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({'version': 1, 'reminders': '坏了'}),
                     encoding='utf-8')

    provider = FakeLineProvider()
    with pytest.raises(NotificationStateCorrupt):
        _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()
    assert provider.calls == 0


def test_an_unreadable_state_file_stops_every_delivery(tmp_path):
    """状态文件读不出来 (不是目录就是文件) → 同样 fail closed。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    state = _state_file(data)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.mkdir()                     # 状态文件变成一个目录 → open() 读不了

    provider = FakeLineProvider()
    with pytest.raises(NotificationStateCorrupt):
        _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()
    assert provider.calls == 0


def test_the_runner_exits_nonzero_on_a_corrupt_state_file(tmp_path, monkeypatch):
    """运维视角: 状态坏了 → 退出码非 0, 明确的报错, 零投递。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    state_dir = notify_state_dir(data)
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, 'notification_state.json'), 'w',
              encoding='utf-8') as f:
        f.write('{ 坏掉了')
    settings = SettingsStore(os.path.join(state_dir, 'notification_settings.json'))
    normalized, err = normalize_settings(_kara())
    assert err is None
    settings.set('kara', normalized)

    monkeypatch.setenv('Y8Y_USERS', 'kara@example.com=kara')
    monkeypatch.setenv('Y8Y_DATA_DIR', str(data))
    monkeypatch.setenv('Y8Y_NOTIFY_ENABLED', '1')
    monkeypatch.setenv('Y8Y_TIMEZONE', 'Asia/Tokyo')

    assert notify_runner.main([]) == 1


# ══════════════════════════════════════════════════════════════════════
# 3. 并发: 两个调度进程不能同时认领同一条通知
# ══════════════════════════════════════════════════════════════════════

def test_a_scheduler_skips_the_round_while_another_one_holds_the_lock(tmp_path):
    """锁被别人拿着 → 这一轮整轮跳过, 一条都不发。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    sched = _scheduler(data, FakeLineProvider(), _at(8, 30),
                       settings={'kara': _kara()})

    lock = DeliveryLock(os.path.join(notify_state_dir(data), LOCK_FILE))
    assert lock.acquire() is True
    try:
        provider = FakeLineProvider()
        report = _scheduler(data, provider, _at(8, 30),
                            settings={'kara': _kara()}).run()
        assert provider.calls == 0
        assert report['sent'] == []
        assert any('delivery lock' in item.get('reason', '')
                   for item in report['skipped'])
    finally:
        lock.release()


def test_two_schedulers_cannot_claim_the_same_notification(tmp_path):
    """两个 tick 撞在一起: 内层调度器拿不到锁 → 不发。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    inner = {'provider': FakeLineProvider(), 'report': None}

    def reentrant(provider, user, message):
        # 外层调度器正在投递时, 第二个调度器被触发
        inner['report'] = _scheduler(data, inner['provider'], _at(8, 30),
                                     settings={'kara': _kara()}).run()

    provider = FakeLineProvider(on_send=reentrant)
    _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()

    assert len(provider.sent) == 1
    assert inner['provider'].calls == 0
    assert inner['report']['sent'] == []
    assert any('delivery lock' in item.get('reason', '')
               for item in inner['report']['skipped'])


def test_the_lock_is_released_after_a_run(tmp_path):
    """跑完就释放 —— 下一轮不会被上一轮的残留锁挡住。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    provider = FakeLineProvider()
    _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()}).run()
    assert len(provider.sent) == 1

    lock = DeliveryLock(os.path.join(notify_state_dir(data), LOCK_FILE))
    assert lock.acquire() is True
    lock.release()

    # 第二个进程 (状态从磁盘重读) 不会重发
    provider2 = FakeLineProvider()
    report = _scheduler(data, provider2, _at(8, 31),
                        settings={'kara': _kara()}).run()
    assert provider2.calls == 0
    assert report['sent'] == []


def test_the_lock_is_released_even_when_the_run_crashes(tmp_path):
    """崩了也不能把锁攥在手里 —— 否则之后所有轮次都发不出东西。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')

    def crash(provider, user, message):
        raise SimulatedCrash('模拟进程在 LINE 投递之后崩溃')

    provider = FakeLineProvider(on_send=crash)
    sched = _scheduler(data, provider, _at(8, 30), settings={'kara': _kara()})
    with pytest.raises(SimulatedCrash):
        sched.run()

    lock = DeliveryLock(os.path.join(notify_state_dir(data), LOCK_FILE))
    assert lock.acquire() is True
    lock.release()


def test_the_lock_file_lives_in_the_notification_state_dir(tmp_path):
    """锁文件也在专用状态目录里 —— 不落在数据目录根部。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    _scheduler(data, FakeLineProvider(), _at(8, 30),
               settings={'kara': _kara()}).run()

    assert (Path(notify_state_dir(data)) / LOCK_FILE).exists()
    assert not (Path(data) / LOCK_FILE).exists()


# ══════════════════════════════════════════════════════════════════════
# 4. 首次初始化: 没有状态文件时一切照常
# ══════════════════════════════════════════════════════════════════════

def test_the_first_run_without_a_state_file_initializes_normally(tmp_path):
    """首次运行: 状态文件还不存在 → 正常初始化, 正常投递。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    assert not _state_file(data).exists()

    provider = FakeLineProvider()
    report = _scheduler(data, provider, _at(8, 30),
                        settings={'kara': _kara()}).run()

    assert len(provider.sent) == 1
    assert report['sent'][0]['kind'] == 'reminder'
    assert _state_file(data).exists()
    assert _reminder_state(data)['status'] == 'sent'


def test_the_first_run_creates_a_well_formed_state_file(tmp_path):
    """初始化出来的状态文件结构完整 (后续轮次靠它判重)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')
    _scheduler(data, FakeLineProvider(), _at(8, 30),
               settings={'kara': _kara()}).run()

    raw = json.loads(_state_file(data).read_text(encoding='utf-8'))
    assert raw['version'] == 1
    assert isinstance(raw['reminders'], dict) and raw['reminders']
    assert isinstance(raw['digests'], dict)


def test_a_dry_run_neither_writes_the_state_nor_takes_the_lock(tmp_path):
    """dry-run 只算不发 —— 不落状态, 也不占锁 (免得挡住真正的投递)。"""
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due=TODAY,
                due_time='09:00', owner='shared')

    provider = FakeLineProvider()
    report = _scheduler(data, provider, _at(8, 30),
                        settings={'kara': _kara()}).run(dry_run=True)

    assert provider.calls == 0
    assert report['planned']
    assert not _state_file(data).exists()
    assert not (Path(notify_state_dir(data)) / LOCK_FILE).exists()
