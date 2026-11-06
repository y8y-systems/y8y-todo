"""LINE 自助绑定 V1 —— 正常流程 + 安全回归测试。

所有凭据 / LINE User ID 都是**虚构**的, 所有 LINE API 调用都走注入的
transport (记录 payload, 不发网络请求)。测试期间不会向任何真实用户发消息。

覆盖:

  正常  A/B 各自绑定 → 各自收到测试消息 / 共享任务分别推送 → 关闭通知
        → 解绑停发 → 重新绑定恢复 → 旧环境变量兼容与迁移。
  安全  未登录、跨用户、过期码、重放、签名、伪造 LINE ID、账号冲突、
        频率限制、凭据隔离、私人任务、Webhook 越权、请求体大小、群聊、
        静态资源、消息内容不落日志。
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import line_link                                                      # noqa: E402
import line_webhook                                                   # noqa: E402
import notifications                                                  # noqa: E402
from notifications import (                                           # noqa: E402
    Capabilities,
    LineProvider,
    NotifyConfig,
    ProviderError,
    Scheduler,
    SettingsStore,
    StateStore,
    notify_state_dir,
    publish_capabilities,
)
from taskstore import atomic_write                                     # noqa: E402
from test_notifications import TZ, _enabled, _write_task              # noqa: E402

ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
BOB = {'Cf-Access-Authenticated-User-Email': 'bob@example.com'}
LINE_A = 'U' + 'a' * 32
LINE_B = 'U' + 'b' * 32
LINE_C = 'U' + 'c' * 32
FAKE_TOKEN = 'LINE_TOKEN_fake_0123456789abcdef'
FAKE_SECRET = 'CHANNEL_SECRET_fake_0123456789abcdef'
TEST_PUSH_URL = 'https://api.line.me/v2/bot/message/push'


# ── helpers ────────────────────────────────────────────────────────────
def _config(data_dir, **extra):
    env = {
        'Y8Y_DATA_DIR': str(data_dir),
        'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
        'Y8Y_TIMEZONE': 'Asia/Tokyo',
        'Y8Y_NOTIFY_ENABLED': '1',
        'Y8Y_NOTIFY_LINE_TOKEN': FAKE_TOKEN,
        'Y8Y_NOTIFY_LINE_CHANNEL_SECRET': FAKE_SECRET,
    }
    env.update(extra)
    return NotifyConfig(env)


def _transport(sink):
    def send(url, payload, headers, timeout):
        sink.append((url, payload, headers, timeout))
        return {}
    return send


def _app(data_dir, sink=None):
    sink = sink if sink is not None else []
    config = _config(data_dir)
    store = line_link.LinkStore(str(data_dir))
    store.ensure()
    store.publish_capabilities()
    app = line_webhook.WebhookApp(config, transport=_transport(sink), store=store)
    return app, sink, config


def _event(code, line_id=LINE_A, event_id=None, source_type='user',
           text_prefix='', msg_type='text'):
    return {
        'type': 'message',
        'webhookEventId': event_id or ('evt-' + hashlib.sha256(
            (code + line_id + str(time.time_ns())).encode()).hexdigest()[:16]),
        'timestamp': int(time.time() * 1000),
        'source': {'type': source_type, 'userId': line_id},
        'message': {'type': msg_type, 'id': 'm-1', 'text': text_prefix + code},
        'deliveryContext': {'isRedelivery': False},
    }


def _bind(store, code, line_id, event_id=None):
    """模拟一次经过签名验证的 Webhook 事件, 返回固定状态串。"""
    app, _sink, _config_obj = _app(store.data_dir)
    return app.handle_event(_event(code, line_id=line_id, event_id=event_id))


def _apply_unlink(store):
    """模拟通知侧后台线程消费解绑请求 (Web 只负责排队)。"""
    app, sink, config = _app(store.data_dir)
    app.consume_requests()
    return app, sink, config


def _create_code(client, headers=ALICE, replace=False):
    status, body = client.post('/todo/api/line-link', {'replace': replace},
                               headers=headers)
    assert status == 200, body
    assert body['state'] == 'pending'
    return body['code'], body['hash']


def _store_code(store, user='alice'):
    """直接从共享状态层造一个码 (不经 HTTP), 便于喂给 Webhook 测试。"""
    payload, err = store.create_code(user, bot_id='@bot')
    assert err == '' and payload
    return payload['code'], payload['hash']


def _sign(body, secret=FAKE_SECRET):
    return base64.b64encode(
        hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()
    ).decode('ascii')


def _raw_post(base, path, body, headers=None):
    req = urllib.request.Request(base + path, data=body, method='POST')
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@pytest.fixture()
def webhook_server(tmp_path):
    app, sink, config = _app(tmp_path / 'data')
    httpd = line_webhook.make_server(app, '127.0.0.1', 0)
    thread = threading.Thread(target=httpd.serve_forever,
                              kwargs={'poll_interval': 0.05}, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}', app, sink
    httpd.shutdown()
    httpd.server_close()
    app.stop.set()


@pytest.fixture()
def store(tmp_path):
    return line_link.LinkStore(str(tmp_path / 'data'))


# ══════════════════════════════════════════════════════════════════════
# 0. 基础不变量
# ══════════════════════════════════════════════════════════════════════
def test_state_dir_matches_notification_engine_constant():
    assert line_link.NOTIFY_DIR_NAME == notifications.NOTIFY_DIR_NAME


def test_deep_link_rejects_junk_and_is_public_info_only():
    assert line_link.deep_link('@abc123') == 'https://line.me/R/ti/p/@abc123'
    assert line_link.deep_link('abc123') == 'https://line.me/R/ti/p/@abc123'
    assert line_link.deep_link('') == ''
    assert line_link.deep_link('@bad/../../evil') == ''


def test_masked_line_id_never_exposes_the_full_id():
    masked = line_link.mask_line_id(LINE_A)
    assert LINE_A not in masked
    assert masked.startswith('Uaaaa') and masked.endswith('aaaa')


def test_capability_marker_expires_fail_closed(store):
    store.ensure()
    assert store.capabilities_enabled() is False
    store.publish_capabilities(now=1000.0)
    assert store.capabilities_enabled(now=1100.0) is True
    assert store.capabilities_enabled(now=1000.0 + 301) is False


# ══════════════════════════════════════════════════════════════════════
# 1. 正常流程
# ══════════════════════════════════════════════════════════════════════
def test_user_a_binds_line_via_web_then_webhook(server):
    _serve, _httpd, client, tmp = server
    code, code_hash = _create_code(client)
    assert line_link.CODE_RE.fullmatch(code)
    assert len(code_hash) == 64
    store = line_link.LinkStore(str(tmp / 'data'))
    assert store.status('alice')['state'] == 'pending'
    assert _bind(store, code, LINE_A) == 'bound'
    status = store.status('alice')
    assert status['state'] == 'bound'
    assert status['line_user_masked'] == line_link.mask_line_id(LINE_A)
    assert LINE_A not in json.dumps(status)
    assert store.binding('alice')['line_user_id'] == LINE_A
    assert store.read_code(code) is None                     # 只能用一次


def test_raw_binding_code_is_never_written_to_disk(server):
    _serve, _httpd, client, tmp = server
    code, _hash = _create_code(client)
    store = line_link.LinkStore(str(tmp / 'data'))
    for directory in (store.state_dir, store.inbox_dir):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            text = (Path(directory) / name).read_text(encoding='utf-8', errors='replace')
            assert code not in text, f'{name} 落盘了原始绑定码'


def test_user_b_binds_a_different_line(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code_a, _ = _create_code(client, ALICE)
    code_b, _ = _create_code(client, BOB)
    assert _bind(store, code_a, LINE_A) == 'bound'
    assert _bind(store, code_b, LINE_B) == 'bound'
    assert store.binding('alice')['line_user_id'] == LINE_A
    assert store.binding('bob')['line_user_id'] == LINE_B
    assert set(store.bindings()) == {'alice', 'bob'}


def test_test_notification_goes_only_to_each_users_own_line(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code_a, _ = _create_code(client, ALICE)
    code_b, _ = _create_code(client, BOB)
    _bind(store, code_a, LINE_A)
    _bind(store, code_b, LINE_B)
    app, sink, _cfg = _app(tmp / 'data')

    status_a, req_a = client.post('/todo/api/line-test', {}, headers=ALICE)
    status_b, req_b = client.post('/todo/api/line-test', {}, headers=BOB)
    assert status_a == status_b == 200
    app.consume_tests()
    assert len(sink) == 2
    targets = {item[1]['to'] for item in sink}
    assert targets == {LINE_A, LINE_B}
    for url, payload, headers, _timeout in sink:
        assert url == TEST_PUSH_URL                    # 复用既有 LINE API 客户端
        assert headers['Authorization'] == 'Bearer ' + FAKE_TOKEN
        assert payload['messages'][0]['text'] == line_webhook.TEST_BODY
    # 结果可被各自的发起者查询
    assert client.get(f"/todo/api/line-test?id={req_a['request_id']}",
                      headers=ALICE)[1]['state'] == 'ok'
    assert client.get(f"/todo/api/line-test?id={req_b['request_id']}",
                      headers=BOB)[1]['state'] == 'ok'
    # 不改变正常投递去重状态
    assert not (Path(tmp / 'data') / 'notify' / 'notification_state.json').exists()


def test_shared_task_is_delivered_separately_to_both_bound_users(tmp_path):
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_A, now=time.time())
    store.set_binding('bob', LINE_B, now=time.time())
    _write_task(data, 'todo', 'shared.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    sink = []
    config = _config(data, Y8Y_NOTIFY_LINE_TOKEN=FAKE_TOKEN)
    provider = LineProvider(config, _transport(sink))
    settings_store = SettingsStore(os.path.join(notify_state_dir(data),
                                                'notification_settings.json'))
    enabled_line = notifications.normalize_settings(
        _enabled(channels=('line',), lead_minutes=30))[0]
    settings_store.set('alice', enabled_line)
    settings_store.set('bob', enabled_line)
    scheduler = Scheduler(str(data), config, providers={'line': provider},
                          settings_store=settings_store,
                          state_store=StateStore(os.path.join(
                              notify_state_dir(data), 'notification_state.json')),
                          now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ), tz=TZ)
    report = scheduler.run()
    assert len(report['sent']) == 2
    assert {item[1]['to'] for item in sink} == {LINE_A, LINE_B}
    assert {item['user'] for item in report['sent']} == {'alice', 'bob'}


def test_disabled_user_receives_no_normal_reminder(tmp_path):
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_A, now=time.time())
    store.set_binding('bob', LINE_B, now=time.time())
    _write_task(data, 'todo', 'shared.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    sink = []
    config = _config(data)
    settings_store = SettingsStore(os.path.join(notify_state_dir(data),
                                                'notification_settings.json'))
    settings_store.set('alice', notifications.normalize_settings(
        _enabled(channels=('line',), lead_minutes=30))[0])
    settings_store.set('bob', notifications.normalize_settings(
        {'enabled': False, 'channels': ['line']})[0])
    scheduler = Scheduler(str(data), config,
                          providers={'line': LineProvider(config, _transport(sink))},
                          settings_store=settings_store,
                          state_store=StateStore(os.path.join(
                              notify_state_dir(data), 'notification_state.json')),
                          now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ), tz=TZ)
    scheduler.run()
    assert [item[1]['to'] for item in sink] == [LINE_A]


def test_unlink_stops_sending_to_the_old_line(server):
    _serve, _httpd, client, tmp = server
    data = tmp / 'data'
    store = line_link.LinkStore(str(data))
    code, _ = _create_code(client)
    _bind(store, code, LINE_A)
    config = _config(data)
    assert LineProvider(config).target_for('alice') == LINE_A

    status, body = client.delete('/todo/api/line-link', headers=ALICE)
    assert status == 200 and body['state'] == 'unlinking'
    # 解绑请求只是排队: 绑定记录仍然由通知侧独占, Web 碰不到。
    assert store.binding('alice') is not None
    assert store.has_unlink_request('alice') is True

    _apply_unlink(store)
    assert store.binding('alice') is None
    assert store.unlinked('alice') is not None
    assert client.get('/todo/api/line-link', headers=ALICE)[1]['state'] == 'unlinked'
    assert LineProvider(config).target_for('alice') == ''
    assert client.post('/todo/api/line-test', {}, headers=ALICE)[0] == 409
    caps_path = Path(tmp / 'data') / 'notify' / 'notification_channels.json'
    publish_capabilities(str(caps_path), config, {'line': LineProvider(config)})
    payload = json.loads(caps_path.read_text(encoding='utf-8'))
    assert not payload['channels']                     # 没有 LINE 目标 → 不发布渠道


def test_rebind_to_another_line_restores_notifications(server):
    _serve, _httpd, client, tmp = server
    data = tmp / 'data'
    store = line_link.LinkStore(str(data))
    code, _ = _create_code(client)
    _bind(store, code, LINE_A)
    client.delete('/todo/api/line-link', headers=ALICE)
    _apply_unlink(store)
    code2, _ = _create_code(client)
    assert _bind(store, code2, LINE_B) == 'bound'
    assert store.binding('alice')['line_user_id'] == LINE_B
    config = _config(data)
    sink = []
    LineProvider(config, _transport(sink)).send(
        'alice', notifications.Message('test', 's', 'hello'))
    assert sink[0][1]['to'] == LINE_B
    assert LINE_A not in json.dumps(sink)


def test_replace_flow_switches_line_without_a_separate_unlink(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    _bind(store, code, LINE_A)
    code2, _ = _create_code(client, replace=True)
    assert _bind(store, code2, LINE_C) == 'bound'
    assert store.binding('alice')['line_user_id'] == LINE_C
    assert client.get('/todo/api/line-link', headers=ALICE)[1]['state'] == 'bound'


# ══════════════════════════════════════════════════════════════════════
# 2. 旧环境变量兼容 / 迁移
# ══════════════════════════════════════════════════════════════════════
def test_legacy_env_target_still_works_before_self_binding(tmp_path):
    data = tmp_path / 'data'
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    assert LineProvider(config).target_for('alice') == LINE_A
    assert LineProvider(config).available_for('alice') is True


def test_self_binding_takes_precedence_and_conflict_is_visible(tmp_path):
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_B, now=time.time())
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    provider = LineProvider(config)
    assert provider.conflict_for('alice') is True
    assert provider.target_for('alice') == LINE_B        # 用户自己绑定的为准
    caps_path = Path(notify_state_dir(data)) / 'notification_channels.json'
    publish_capabilities(str(caps_path), config, {'line': provider})
    caps = Capabilities(str(caps_path))
    assert caps.line_legacy_users() == ['alice']
    assert caps.line_conflicts() == ['alice']
    text = caps_path.read_text(encoding='utf-8')
    assert LINE_A not in text and LINE_B not in text     # 能力表不含 LINE ID
    assert FAKE_TOKEN not in text and FAKE_SECRET not in text


def test_strict_conflict_policy_fails_closed(tmp_path):
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_B, now=time.time())
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A,
                     Y8Y_NOTIFY_LINE_CONFLICT_POLICY='strict')
    provider = LineProvider(config)
    assert provider.target_for('alice') == ''
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('test', 's', 'body'))
    assert '冲突' in str(exc.value)


# ── P0-1: 明确解绑 ≠ 从未绑定 ────────────────────────────────────────
def test_explicit_unlink_never_falls_back_to_legacy_target(server):
    """回归: 解绑后旧 Y8Y_NOTIFY_LINE_TARGETS 绝不能被自动恢复。"""
    _serve, _httpd, client, tmp = server
    data = tmp / 'data'
    store = line_link.LinkStore(str(data))
    code, _ = _create_code(client)
    assert _bind(store, code, LINE_B) == 'bound'
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    assert LineProvider(config).target_for('alice') == LINE_B

    client.delete('/todo/api/line-link', headers=ALICE)
    _apply_unlink(store)

    assert store.binding('alice') is None
    assert store.unlinked('alice') is not None
    assert LineProvider(config).target_for('alice') == ''
    assert LineProvider(config).available_for('alice') is False
    assert client.post('/todo/api/line-test', {}, headers=ALICE)[0] == 409
    status = client.get('/todo/api/line-link', headers=ALICE)[1]
    assert status['state'] == 'unlinked' and status['legacy_disabled'] is True
    assert '不会再' in status['hint']


def test_unlink_blocks_legacy_fallback_under_both_conflict_policies(tmp_path):
    for policy in ('binding', 'strict'):
        data = tmp_path / ('data-' + policy)
        store = line_link.LinkStore(str(data))
        store.ensure()
        store.set_binding('alice', LINE_B, now=time.time())
        store.apply_unlink('alice')
        config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A,
                         Y8Y_NOTIFY_LINE_CONFLICT_POLICY=policy)
        assert LineProvider(config).target_for('alice') == '', policy


def test_never_bound_user_keeps_legacy_target_after_upgrade(tmp_path):
    """升级兼容: 从未自助绑定过的老用户仍靠旧环境变量收通知。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    assert store.unlinked('alice') is None
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    assert LineProvider(config).target_for('alice') == LINE_A
    assert LineProvider(config).available_for('alice') is True
    # 另一个用户明确解绑, 不影响这位老用户
    store.apply_unlink('bob')
    assert LineProvider(config).target_for('alice') == LINE_A


def test_unlink_tombstone_is_persistent_and_rebind_clears_it(server):
    """墓碑跨进程可见; 只有重新绑定成功才清除。"""
    _serve, _httpd, client, tmp = server
    data = tmp / 'data'
    store = line_link.LinkStore(str(data))
    code, _ = _create_code(client)
    _bind(store, code, LINE_A)
    client.delete('/todo/api/line-link', headers=ALICE)
    _apply_unlink(store)

    # 全新的 LinkStore 实例 (模拟调度器另一个进程) 仍能看到墓碑
    other_process = line_link.LinkStore(str(data))
    assert other_process.unlinked('alice') is not None
    assert line_link.explicitly_unlinked(str(data), 'alice') is True
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    assert LineProvider(config).target_for('alice') == ''
    # 网页状态与 HTTP 服务重启无关
    assert client.get('/todo/api/line-link', headers=ALICE)[1]['state'] == 'unlinked'

    code2, _ = _create_code(client)
    assert _bind(store, code2, LINE_C) == 'bound'
    assert other_process.unlinked('alice') is None
    assert LineProvider(config).target_for('alice') == LINE_C


def test_unlink_cancels_queued_test_notification(server):
    """竞态: 解绑时已排队但尚未发送的测试通知不能发往旧 LINE 地址。"""
    _serve, _httpd, client, tmp = server
    data = tmp / 'data'
    store = line_link.LinkStore(str(data))
    code, _ = _create_code(client)
    _bind(store, code, LINE_A)
    req = client.post('/todo/api/line-test', {}, headers=ALICE)
    assert req[0] == 200
    rid = req[1]['request_id']

    # 用户紧接着解绑; 通知侧在同一个锁里取消待发测试。
    client.delete('/todo/api/line-link', headers=ALICE)
    app, sink, _cfg = _apply_unlink(store)

    assert sink == []                                   # 一次 LINE API 都没调用
    assert app.consume_tests() == []                    # 队列已被清空
    result = client.get(f'/todo/api/line-test?id={rid}', headers=ALICE)[1]
    assert result['state'] == 'error'
    assert result['error'] == line_link.ERROR_MESSAGES['cancelled']
    assert LineProvider(_config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A))\
        .target_for('alice') == ''


def test_send_rechecks_binding_after_claim_so_unlink_wins(tmp_path):
    """认领后、调用 LINE API 前解绑 → send() 重新解析收件人并 fail closed。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_A, now=time.time())
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    sink = []
    provider = LineProvider(config, _transport(sink))
    store.apply_unlink('alice')                       # 墓碑先落地
    with pytest.raises(ProviderError):
        provider.send('alice', notifications.Message('test', 's', 'body'))
    assert sink == []
    assert LINE_A not in json.dumps(sink)


# ── P0-1 (第二轮): 待处理解绑请求本身就要立即停发 ─────────────────────
def _scheduled_round(data, config, sink, when):
    """跑一轮真实的定时调度 (共享任务 09:00, 提前 30 分钟 → 08:30 触发)。

    每轮清掉去重状态, 让同一条提醒可以再次进入投递循环 —— 这样才能证明
    「停发」是因为收件人被挡住, 而不是因为去重状态恰好把消息吞了。
    """
    state_path = Path(notify_state_dir(data)) / 'notification_state.json'
    if state_path.exists():
        state_path.unlink()
    settings_store = SettingsStore(str(Path(notify_state_dir(data))
                                       / 'notification_settings.json'))
    enabled_line = notifications.normalize_settings(
        _enabled(channels=('line',), lead_minutes=30))[0]
    for user in ('alice', 'bob'):
        settings_store.set(user, enabled_line)
    return Scheduler(str(data), config,
                     providers={'line': LineProvider(config, _transport(sink))},
                     settings_store=settings_store,
                     state_store=StateStore(str(state_path)),
                     now=when, tz=TZ).run()


def test_pending_unlink_stops_scheduled_notifications_while_webhook_is_offline(server):
    """完整链路: Webhook 离线 → 解绑排队 → 定时通知触发 → 恢复写墓碑。

    回归的是修复前的漏洞: ``target_for()`` 只看绑定与墓碑, Webhook 离线时
    绑定还在、墓碑还没写, 于是旧 ``Y8Y_NOTIFY_LINE_TARGETS`` 又被启用。
    """
    _serve, _httpd, client, tmp = server
    data = tmp / 'data'
    store = line_link.LinkStore(str(data))
    code, _ = _create_code(client)
    assert _bind(store, code, LINE_B) == 'bound'
    store.set_binding('bob', LINE_C, now=time.time())
    _write_task(data, 'todo', 'shared.md', title='共享任务', due='2026-10-27',
                due_time='09:00', owner='shared')
    # 旧环境变量仍配着 alice 的**旧** LINE 账号, bob 的与绑定一致
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS=f'alice={LINE_A},bob={LINE_C}')
    sink = []
    provider = LineProvider(config, _transport(sink))
    assert provider.target_for('alice') == LINE_B

    # 基线: 解绑前定时通知确实发给两个人
    report = _scheduled_round(data, config, sink,
                              datetime(2026, 10, 27, 8, 30, tzinfo=TZ))
    assert {item[1]['to'] for item in sink} == {LINE_B, LINE_C}
    assert {item['user'] for item in report['sent']} == {'alice', 'bob'}
    sink.clear()

    # ① Webhook 离线: 只发 DELETE, 不启动也不调用任何消费线程
    status, body = client.delete('/todo/api/line-link', headers=ALICE)
    assert status == 200 and body['state'] == 'unlinking'
    assert store.has_unlink_request('alice') is True
    assert line_link.unlink_requested(str(data), 'alice') is True
    assert store.binding('alice') is not None          # 权威绑定还在
    assert store.unlinked('alice') is None              # 墓碑还没写
    assert provider.target_for('alice') == ''           # 但已经停发
    assert provider.available_for('alice') is False
    assert provider.target_for('bob') == LINE_C          # 不影响别人
    assert client.post('/todo/api/line-test', {}, headers=ALICE)[0] == 409
    assert client.post('/todo/api/line-link', {}, headers=ALICE)[0] == 409
    hint = client.get('/todo/api/line-link', headers=ALICE)[1]['hint']
    assert '已停止' in hint and '不会恢复' in hint

    # ② 定时通知连续两轮触发: alice 一条都不发, 旧地址 LINE_A 绝不复活
    for minute in (31, 35):
        r = _scheduled_round(data, config, sink,
                             datetime(2026, 10, 27, 8, minute, tzinfo=TZ))
        assert {item[1]['to'] for item in sink} == {LINE_C}, minute
        assert LINE_A not in json.dumps(sink) and LINE_B not in json.dumps(sink)
        assert any(x.get('user') == 'alice' and
                   x.get('reason') == 'no channel configured on the server'
                   for x in r['skipped']), r['skipped']
        sink.clear()

    # ③ 测试推送 / 直接调用 send() 也 fail closed
    with pytest.raises(ProviderError):
        provider.send('alice', notifications.Message('test', 's', 'body'))
    assert sink == []

    # ④ Webhook 恢复: 消费请求 → 删绑定 + 写墓碑 + 清空请求
    app, _sink, _cfg = _apply_unlink(store)
    assert store.binding('alice') is None
    assert store.unlinked('alice') is not None
    assert store.has_unlink_request('alice') is False
    assert app.consume_requests() == []
    assert provider.target_for('alice') == ''           # 墓碑继续挡住旧地址
    assert provider.available_for('alice') is False
    r = _scheduled_round(data, config, sink,
                         datetime(2026, 10, 27, 8, 40, tzinfo=TZ))
    assert {item[1]['to'] for item in sink} == {LINE_C}
    assert client.get('/todo/api/line-link', headers=ALICE)[1]['state'] == 'unlinked'


def test_pending_unlink_never_falls_back_to_legacy_under_any_policy(tmp_path):
    """两种冲突策略下, 排队中的解绑请求都压过绑定与旧环境变量。"""
    for policy in ('binding', 'strict'):
        data = tmp_path / ('data-' + policy)
        store = line_link.LinkStore(str(data))
        store.ensure()
        store.set_binding('alice', LINE_B, now=time.time())
        config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A,
                         Y8Y_NOTIFY_LINE_CONFLICT_POLICY=policy)
        provider = LineProvider(config)
        assert provider.target_for('alice') == ('' if policy == 'strict' else LINE_B)
        store.request_unlink('alice')
        assert provider.target_for('alice') == '', policy
        assert provider.available_for('alice') is False, policy
        with pytest.raises(ProviderError):
            provider.send('alice', notifications.Message('test', 's', 'body'))


def test_unlink_request_survives_gc_and_restarts(tmp_path):
    """请求没有 TTL: 长时间停机也不能让旧地址自动复活。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_B, now=time.time())
    store.request_unlink('alice')
    path = Path(store.inbox_dir) / f'unlink-{line_link.digest("alice")}.json'
    old = time.time() - 10 * line_link.STATE_FILE_TTL_SECONDS
    os.utime(path, (old, old))
    store.gc(now=time.time())
    assert path.exists(), 'GC 不能删掉尚未消费的解绑请求'
    assert store.has_unlink_request('alice') is True
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    assert LineProvider(config).target_for('alice') == ''
    # 新进程 (调度器) 也能看到同一条请求
    assert line_link.LinkStore(str(data)).has_unlink_request('alice') is True
    # 恢复后仍能正常收敛
    app = line_webhook.WebhookApp(config, store=store)
    app.stop.set()
    assert app.consume_requests() == []
    assert store.unlinked('alice') is not None
    assert not path.exists()


def test_only_well_formed_unlink_requests_are_effective(tmp_path):
    """inbox 是 Web 可写的: 内容不自洽的请求不能被当成有效意图。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_B, now=time.time())
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    provider = LineProvider(config)
    path = Path(store.inbox_dir) / f'unlink-{line_link.digest("alice")}.json'
    for bad in ({}, {'version': 1, 'user': 'alice'},
                {'version': 9, 'user': 'alice', 'requested_at': time.time()},
                {'version': 1, 'user': 'bob', 'requested_at': time.time()},
                {'version': 1, 'user': 'alice', 'requested_at': 'yesterday'}):
        path.write_text(json.dumps(bad), encoding='utf-8')
        assert store.has_unlink_request('alice') is False, bad
        assert provider.target_for('alice') == LINE_B, bad
    store.request_unlink('alice')
    assert provider.target_for('alice') == ''


def test_forged_unlink_filename_cannot_unlink_another_user(tmp_path):
    """文件名与内容不一致的请求必须被丢弃, 不能借通知侧之手解绑别人。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('bob', LINE_C, now=time.time())
    bogus = Path(store.inbox_dir) / 'unlink-bogus.json'
    bogus.write_text(json.dumps({'version': 1, 'user': 'bob',
                                 'requested_at': time.time()}), encoding='utf-8')
    assert store.has_unlink_request('bob') is False
    config = _config(data)
    assert LineProvider(config).target_for('bob') == LINE_C
    app = line_webhook.WebhookApp(config, store=store)
    app.stop.set()
    assert app.consume_requests() == []
    assert not bogus.exists()
    assert store.binding('bob') is not None
    assert store.unlinked('bob') is None


# ══════════════════════════════════════════════════════════════════════
# 3. 安全
# ══════════════════════════════════════════════════════════════════════
def test_unauthenticated_requests_cannot_create_or_read_or_delete(server):
    _serve, _httpd, client, _tmp = server
    assert client.get('/todo/api/line-link')[0] == 401
    assert client.post('/todo/api/line-link', {})[0] == 401
    assert client.delete('/todo/api/line-link')[0] == 401
    assert client.post('/todo/api/line-test', {})[0] == 401
    assert client.get('/todo/api/line-test?id=' + 'a' * 32)[0] == 401


def test_user_a_cannot_see_or_modify_b(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code_a, _ = _create_code(client, ALICE)
    _bind(store, code_a, LINE_A)
    status, body = client.get('/todo/api/line-link', headers=BOB)
    assert status == 200 and body['state'] == 'unbound'
    assert 'line_user_masked' not in body
    assert LINE_A not in json.dumps(body)
    # Bob 的解绑请求即使被通知侧消费, 也不能动 Alice 的绑定
    assert client.delete('/todo/api/line-link', headers=BOB)[0] == 200
    _apply_unlink(store)
    assert store.binding('alice')['line_user_id'] == LINE_A
    assert store.unlinked('alice') is None
    assert store.unlinked('bob') is not None
    # 测试推送结果也不能被别人查
    req = client.post('/todo/api/line-test', {}, headers=ALICE)[1]
    assert client.get(f"/todo/api/line-test?id={req['request_id']}",
                      headers=BOB)[1]['error'] == line_link.ERROR_MESSAGES['forbidden']


def test_expired_code_cannot_be_used(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, code_hash = _create_code(client)
    # 模拟 11 分钟过去: 直接把该文件的时间戳改到过去 (确定性, 不 sleep)
    path = Path(store._path(f'code-{code_hash}.json'))
    record = json.loads(path.read_text(encoding='utf-8'))
    record['expires_at'] = time.time() - 1
    atomic_write(str(path), json.dumps(record))
    assert _bind(store, code, LINE_A) == 'expired'
    assert store.binding('alice') is None
    status = client.get('/todo/api/line-link', headers=ALICE)[1]
    assert status['state'] == 'expired'


def test_used_code_cannot_be_replayed(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    assert _bind(store, code, LINE_A, event_id='evt-first') == 'bound'
    assert _bind(store, code, LINE_B, event_id='evt-second') == 'invalid_code'
    assert store.binding('alice')['line_user_id'] == LINE_A


def test_duplicate_webhook_event_is_idempotent(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    app, _sink, _cfg = _app(tmp / 'data')
    event = _event(code, LINE_A, event_id='evt-same')
    assert app.handle_event(event) == 'bound'
    assert app.handle_event(event) == 'duplicate'
    assert store.binding('alice')['line_user_id'] == LINE_A


def test_invalid_webhook_signature_is_rejected(webhook_server, store):
    base, app, _sink = webhook_server
    code, _hash = _store_code(store)
    body = json.dumps({'events': [_event(code, LINE_A)]}).encode('utf-8')
    # 缺签名 / 错误签名 / 伪造的 Channel Secret
    assert _raw_post(base, line_webhook.WEBHOOK_PATH, body)[0] == 401
    assert _raw_post(base, line_webhook.WEBHOOK_PATH, body,
                     {'X-Line-Signature': 'not-a-signature'})[0] == 401
    assert _raw_post(base, line_webhook.WEBHOOK_PATH, body,
                     {'X-Line-Signature': _sign(body, secret='wrong')})[0] == 401
    assert store.binding('alice') is None
    assert app.invalid_signatures == 3
    # 正确签名 → 200 且绑定成功
    assert _raw_post(base, line_webhook.WEBHOOK_PATH, body,
                     {'X-Line-Signature': _sign(body)})[0] == 200
    assert store.binding('alice')['line_user_id'] == LINE_A


def test_signature_is_over_the_raw_body_not_a_reserialized_copy(webhook_server, store):
    base, _app_obj, _sink = webhook_server
    code, _hash = _store_code(store)
    raw = ('{"events": [ ' + json.dumps(_event(code, LINE_A)) + ' ]}').encode('utf-8')
    sig = _sign(raw)
    assert _raw_post(base, line_webhook.WEBHOOK_PATH, raw,
                     {'X-Line-Signature': sig})[0] == 200
    # 改一个字节 (保持 JSON 语义) 后旧签名必须失效
    tampered = raw.replace(b'[ ', b'[  ', 1)
    assert _raw_post(base, line_webhook.WEBHOOK_PATH, tampered,
                     {'X-Line-Signature': sig})[0] == 401


def test_webhook_limits_body_size_and_path(webhook_server):
    base, app, _sink = webhook_server
    big = b'x' * (line_webhook.MAX_BODY + 1)
    assert _raw_post(base, line_webhook.WEBHOOK_PATH, big,
                     {'X-Line-Signature': _sign(big)})[0] == 413
    # 其它路径/方法一律 404, 即使签名正确
    body = b'{}'
    assert _raw_post(base, '/todo/api/line-link', body,
                     {'X-Line-Signature': _sign(body)})[0] == 404
    assert _raw_post(base, '/', body, {'X-Line-Signature': _sign(body)})[0] == 404


def test_webhook_get_is_not_an_entry_point(webhook_server):
    base, _app_obj, _sink = webhook_server
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(base + line_webhook.WEBHOOK_PATH, timeout=5)
    assert exc.value.code == 404


def test_forged_line_user_id_is_ignored(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    app, sink, _cfg = _app(tmp / 'data')
    for bogus in ('not-a-line-id', '../../etc/passwd', LINE_A.upper(), ''):
        event = _event(code, line_id=bogus)
        assert app.handle_event(event) == 'invalid_source'
    assert store.binding('alice') is None
    # 客户端尝试在 API 里塞 LINE ID 也无效: 只能发给服务端已绑定的目标
    assert _bind(store, code, LINE_A) == 'bound'
    sink.clear()
    status, body = client.post('/todo/api/line-test',
                               {'line_user_id': LINE_B, 'message': 'fake'},
                               headers=ALICE)
    assert status == 200
    app.consume_tests()
    assert [item[1]['to'] for item in sink] == [LINE_A]
    assert all('fake' not in json.dumps(item[1], ensure_ascii=False) for item in sink)


def test_same_line_account_cannot_bind_two_users(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code_a, _ = _create_code(client, ALICE)
    code_b, _ = _create_code(client, BOB)
    assert _bind(store, code_a, LINE_A) == 'bound'
    assert _bind(store, code_b, LINE_A) == 'line_account_in_use'
    assert store.binding('bob') is None
    status = client.get('/todo/api/line-link', headers=BOB)[1]
    assert status['state'] == 'error'
    assert '其他 Todo 用户' in status['error']


def test_group_and_room_chats_are_rejected(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    app, _sink, _cfg = _app(tmp / 'data')
    for kind in ('group', 'room'):
        assert app.handle_event(_event(code, LINE_A, source_type=kind)) \
            == 'not_one_to_one'
    assert store.binding('alice') is None
    # 群聊没消耗绑定码, 但也不会误绑定
    assert store.read_code(code) is not None


def test_high_frequency_test_notifications_are_rate_limited(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    _bind(store, code, LINE_A)
    assert client.post('/todo/api/line-test', {}, headers=ALICE)[0] == 200
    second = client.post('/todo/api/line-test', {}, headers=ALICE)
    assert second[0] == 429
    assert second[1]['code'] == 'rate_limited'


def test_binding_code_creation_is_rate_limited(server):
    _serve, _httpd, client, _tmp = server
    assert client.post('/todo/api/line-link', {}, headers=ALICE)[0] == 200
    assert client.post('/todo/api/line-link', {}, headers=ALICE)[0] == 200
    assert client.post('/todo/api/line-link', {}, headers=ALICE)[0] == 200
    assert client.post('/todo/api/line-link', {}, headers=ALICE)[0] == 429


def test_webhook_brute_force_is_throttled(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    app, _sink, _cfg = _app(tmp / 'data')
    for i in range(line_webhook.MAX_FAILED_ATTEMPTS + 3):
        app.handle_event(_event('X' * 32 + str(i), LINE_A,
                                event_id='bad-' + str(i)))
    assert app.handle_event(_event(code, LINE_A)) == 'rate_limited'
    assert store.binding('alice') is None


def test_webhook_never_logs_message_content(server, capsys):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    app, _sink, _cfg = _app(tmp / 'data')
    app.handle_event(_event(code, LINE_A))
    captured = capsys.readouterr()
    assert code not in captured.out + captured.err
    assert LINE_A not in captured.out + captured.err


def test_webhook_cannot_create_codes_or_escalate(server):
    """Webhook 侧没有生成绑定码 / 调用 Web 内部 API 的能力。"""
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    app, _sink, _cfg = _app(tmp / 'data')
    # 没有绑定码的事件不能凭空造出绑定
    assert app.handle_event(_event('nothing', LINE_A)) == 'ignored'
    assert store.bindings() == {}
    assert not hasattr(app, 'create_code')
    # 该进程只持有 Webhook 路径, 且没有 HTTP 客户端去调 Todo API
    source = (SRC / 'line_webhook.py').read_text(encoding='utf-8')
    assert '/todo/api/' not in source
    assert 'X-Internal-Token' not in source


def test_code_issued_for_a_always_binds_a_not_the_sender(server):
    """凭证与 Todo 用户绑定, 别的 LINE 用户拿到也改不了目标账号。"""
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client, ALICE)
    assert _bind(store, code, LINE_B) == 'bound'
    assert store.binding('alice')['line_user_id'] == LINE_B
    assert store.binding('bob') is None


def test_private_tasks_never_reach_other_users(tmp_path):
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_A, now=time.time())
    store.set_binding('bob', LINE_B, now=time.time())
    _write_task(data, 'todo', 'private.md', title='Bob 的私人任务',
                due='2026-10-27', due_time='09:00', owner='bob')
    sink = []
    config = _config(data)
    settings_store = SettingsStore(os.path.join(notify_state_dir(data),
                                                'notification_settings.json'))
    for user in ('alice', 'bob'):
        settings_store.set(user, notifications.normalize_settings(
            _enabled(channels=('line',), lead_minutes=30))[0])
    Scheduler(str(data), config,
              providers={'line': LineProvider(config, _transport(sink))},
              settings_store=settings_store,
              state_store=StateStore(os.path.join(notify_state_dir(data),
                                                 'notification_state.json')),
              now=datetime(2026, 10, 27, 8, 30, tzinfo=TZ), tz=TZ).run()
    assert sink == []


# ══════════════════════════════════════════════════════════════════════
# 4. 凭据隔离 / 部署资产
# ══════════════════════════════════════════════════════════════════════
@pytest.mark.server_env({
    'Y8Y_NOTIFY_LINE_TOKEN': FAKE_TOKEN,
    'Y8Y_NOTIFY_LINE_CHANNEL_SECRET': FAKE_SECRET,
    'Y8Y_LINE_BOT_ID': '@testbot',
})
def test_web_process_never_reads_or_exposes_credentials(server):
    serve, _httpd, client, _tmp = server
    assert not hasattr(serve.CONFIG, 'line_channel_secret')
    assert not hasattr(serve.CONFIG, 'line_token')
    for path in ('/todo/api/line-link', '/todo/api/notification-settings'):
        status, body = client.get(path, headers=ALICE)
        assert status == 200
        raw = json.dumps(body, ensure_ascii=False)
        assert FAKE_TOKEN not in raw and FAKE_SECRET not in raw
        lowered = raw.lower()
        for needle in ('token', 'secret', 'api_key', 'bearer', 'password'):
            assert needle not in lowered, f'{path} 响应里出现 {needle}'


def test_credential_names_do_not_appear_in_web_side_modules():
    for name in ('serve.py', 'line_link.py', 'config.py'):
        text = (SRC / name).read_text(encoding='utf-8')
        assert 'Y8Y_NOTIFY_LINE_TOKEN' not in text, name
        assert 'Y8Y_NOTIFY_LINE_CHANNEL_SECRET' not in text, name
        assert 'Y8Y_NOTIFY_EMAIL_API_KEY' not in text, name


def test_webhook_unit_follows_least_privilege():
    web = (ROOT / 'packaging' / 'y8y-todo-line-webhook.service').read_text(encoding='utf-8')
    main_unit = (ROOT / 'packaging' / 'y8y-todo.service').read_text(encoding='utf-8')
    assert 'User=y8y-notify' in web
    assert 'User=root' not in web
    assert 'y8y-todo-notify.env' not in main_unit        # Web 单元绝不读凭据文件
    assert 'EnvironmentFile=-/etc/y8y-todo/y8y-todo-notify.env' in web
    assert 'ReadWritePaths=-/var/lib/y8y-todo/notify' in web
    assert 'NoNewPrivileges=true' in web and 'ProtectSystem=strict' in web
    assert 'UMask=0022' in web
    for line in web.splitlines():
        if line.startswith('Environment='):
            assert 'TOKEN' not in line and 'SECRET' not in line
        if line.startswith('ExecStart='):
            assert '.env' not in line


def test_env_examples_contain_only_placeholders():
    notify_env = (ROOT / 'packaging' / 'y8y-todo-notify.env.example').read_text(
        encoding='utf-8')
    values = {}
    for line in notify_env.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
    assert values['Y8Y_NOTIFY_LINE_TOKEN'] == ''
    assert values['Y8Y_NOTIFY_LINE_CHANNEL_SECRET'] == ''
    assert values['Y8Y_NOTIFY_EMAIL_API_KEY'] == ''
    web_env = (ROOT / 'packaging' / 'y8y-todo.env.example').read_text(encoding='utf-8')
    assert 'CHANNEL_SECRET' not in web_env and 'LINE_TOKEN' not in web_env


def test_binding_data_is_not_a_public_static_resource(server):
    _serve, _httpd, client, tmp = server
    store = line_link.LinkStore(str(tmp / 'data'))
    code, _ = _create_code(client)
    _bind(store, code, LINE_A)
    # 未知路径会走 SPA fallback (返回 index.html, 200), 但**绝不能**把
    # 数据目录里的绑定文件/能力表内容带出来。
    for path in ('/line-link-capabilities.json', '/line-link/code-abc.json',
                 '/notify/line-link/capabilities.json',
                 '/notify/line-link/binding-x.json',
                 '/notify/line-link/unlinked-x.json',
                 '/notify/line-link-inbox/code-x.json', '/serve.py',
                 '/../data/notify/line-link/binding-x.json'):
        _status, body = client.get(path)
        raw = json.dumps(body, ensure_ascii=False)
        assert LINE_A not in raw, f'{path} 泄露了 LINE User ID'
        assert 'line_user_id' not in raw, f'{path} 泄露了绑定数据'
        assert '"enabled"' not in raw, f'{path} 泄露了能力表'
    # 绑定文件住在数据目录, 不在静态根
    assert not str(store.state_dir).startswith(str(SRC))


def test_api_rejects_oversized_and_malformed_bodies(server):
    _serve, _httpd, client, _tmp = server
    req = urllib.request.Request(
        client.base + '/todo/api/line-link',
        data=b'x' * (line_link.WEB_BODY_LIMIT + 10), method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with pytest.raises(urllib.error.HTTPError) as exc:
        opener.open(req, timeout=5)
    assert exc.value.code == 413


def test_unknown_test_request_id_is_rejected_without_leaking_paths(server):
    _serve, _httpd, client, _tmp = server
    for bad in ('../../etc/passwd', 'a' * 200, '', 'ABC'):
        status, body = client.get('/todo/api/line-test?id=' + bad, headers=ALICE)
        assert status == 200
        assert body['state'] == 'error'
        assert 'etc/passwd' not in json.dumps(body)


def test_capability_table_marks_bound_users_even_without_preferences(tmp_path):
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('carol', LINE_C, now=time.time())
    config = _config(data)
    caps_path = Path(notify_state_dir(data)) / 'notification_channels.json'
    payload = publish_capabilities(str(caps_path), config,
                                   {'line': LineProvider(config)})
    line_channels = [c for c in payload['channels'] if c['name'] == 'line']
    assert line_channels and 'carol' in line_channels[0]['users']
