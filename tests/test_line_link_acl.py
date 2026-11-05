"""LINE 自助绑定 —— 权威绑定目录的最小权限 / 伪造防护测试 (P0-2)。

评估结论 (修复前): ``<data>/notify/line-link/`` 是 2770 共享组可写目录,
Web 进程 (``y8y``, 属组 ``y8y-notify``) 拥有**目录写权限**, 因此可以直接
``unlink`` / ``os.replace`` / 新建 ``binding-*.json`` —— 也就是绕过 LINE
Webhook 的签名验证, 凭空指定任意收件人。修复后:

* 请求只能写进 ``line-link-inbox/`` (2770);
* 权威绑定/墓碑只在 ``line-link/`` (2750, 通知侧属主, Web 只有 r-x);
* ``<notify>/`` 加 sticky (3770), Web 不能把通知侧拥有的目录整个改名;
* 通知侧启动自检 ``verify_trusted_layout()`` fail closed。

跨 UID 的**真实验证**在 CI 的 ``sudo bash tests/linux_notify_acl.sh``
(Ubuntu 22.04 job, root + 两个真实系统账号 + runuser)。本文件用权限位做等价的
确定性验证, 并用静态断言钉死那段 CI 检查确实存在 (不会被悄悄删掉)。
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import line_link                                                      # noqa: E402
import line_webhook                                                   # noqa: E402
from notifications import LineProvider, NotifyConfig                  # noqa: E402

LINE_A = 'U' + 'a' * 32
LINE_FORGED = 'U' + 'f' * 32


def _config(data_dir, **extra):
    env = {
        'Y8Y_DATA_DIR': str(data_dir),
        'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
        'Y8Y_NOTIFY_ENABLED': '1',
        'Y8Y_NOTIFY_LINE_TOKEN': 'fake-token-for-tests',
        'Y8Y_NOTIFY_LINE_CHANNEL_SECRET': 'fake-secret-for-tests',
    }
    env.update(extra)
    return NotifyConfig(env)


def _event(code, line_id, event_id='acl-event-1'):
    return {
        'type': 'message', 'webhookEventId': event_id,
        'source': {'type': 'user', 'userId': line_id},
        'message': {'type': 'text', 'id': 'm1', 'text': code},
    }


# ══════════════════════════════════════════════════════════════════════
# 1. 目录布局与权限位
# ══════════════════════════════════════════════════════════════════════
def test_directory_mode_constants_are_the_minimum_required():
    assert line_link.TRUSTED_DIR_MODE == 0o2750
    assert line_link.INBOX_DIR_MODE == 0o2770
    assert line_link.NOTIFY_DIR_MODE == 0o3770
    assert line_link.NOTIFY_DIR_MODE & stat.S_ISVTX          # sticky: 不能挪走别人的目录
    assert line_link.NOTIFY_DIR_MODE & stat.S_ISGID
    # 权威目录: group 只有 r-x, 没有 w —— 这才是「Web 不能建/删/换文件」的依据
    assert line_link.TRUSTED_DIR_MODE & 0o050 == 0o050
    assert line_link.TRUSTED_DIR_MODE & 0o020 == 0
    assert line_link.TRUSTED_DIR_MODE & 0o002 == 0
    # 请求队列: group rwx
    assert line_link.INBOX_DIR_MODE & 0o070 == 0o070


def test_prefix_routing_keeps_requests_out_of_the_trusted_dir():
    route = line_link.LinkStore._dir_for
    assert route('binding-x.json') == 'trusted'
    assert route('unlinked-x.json') == 'trusted'
    assert route('result-x.json') == 'trusted'
    assert route('event-x.json') == 'trusted'
    assert route('capabilities.json') == 'trusted'
    for name in ('code-x.json', 'unlink-x.json', 'test-x.req.json',
                 'test-x.claim.json', 'test-x.res.json', 'test-rate.json', 'lock'):
        assert route(name) == 'inbox', name


def test_web_side_ensure_never_creates_the_trusted_directory(tmp_path):
    """Web 只建 inbox; 权威目录必须由通知侧自己 (预先) 建立。"""
    store = line_link.LinkStore(str(tmp_path / 'data'))
    store.ensure()
    assert os.path.isdir(store.inbox_dir)
    assert not os.path.exists(store.state_dir), \
        'Web 不能成为权威绑定目录的创建者/属主'


def test_ensure_trusted_tightens_the_security_relevant_mode_bits(tmp_path):
    """故意先造宽松目录, 通知侧必须收紧。

    只看**安全相关**的位: macOS 的 chmod(2) 会清掉目录的 setgid 位
    (Linux 会保留), 但 sticky / 「group 不可写」这两条在任何平台都必须成立。
    """
    store = line_link.LinkStore(str(tmp_path / 'data'))
    os.makedirs(store.notify_dir, mode=0o755, exist_ok=True)
    os.makedirs(store.state_dir, mode=0o755, exist_ok=True)
    os.makedirs(store.inbox_dir, mode=0o755, exist_ok=True)
    store.ensure_trusted()
    n = stat.S_IMODE(os.stat(store.notify_dir).st_mode)
    t = stat.S_IMODE(os.stat(store.state_dir).st_mode)
    i = stat.S_IMODE(os.stat(store.inbox_dir).st_mode)
    assert n & stat.S_ISVTX, 'notify/ 必须有 sticky 位'
    assert n & 0o070 == 0o070 and n & 0o007 == 0
    assert t & 0o022 == 0, '权威目录 group/other 不可写'
    assert t & 0o050 == 0o050, '权威目录 group 至少可读+可穿越'
    assert i & 0o070 == 0o070, '请求队列 group 可写'


def test_verify_trusted_layout_accepts_the_documented_layout(tmp_path):
    store = line_link.LinkStore(str(tmp_path / 'data'))
    store.ensure_trusted()
    assert store.verify_trusted_layout() == ''


def test_verify_trusted_layout_fails_closed_on_wrong_modes_or_owner(tmp_path, monkeypatch):
    store = line_link.LinkStore(str(tmp_path / 'data'))
    store.ensure_trusted()

    os.chmod(store.state_dir, 0o2770)                # group 可写 = Web 可伪造绑定
    assert 'group/other writable' in store.verify_trusted_layout()
    os.chmod(store.state_dir, 0o2750)

    # 属主不是通知侧 (模拟 Web 抢先建了同名目录)
    monkeypatch.setattr(os, 'geteuid', lambda: os.getuid() + 1)
    assert 'different uid' in store.verify_trusted_layout()
    monkeypatch.undo()
    assert store.verify_trusted_layout() == ''


def test_verify_trusted_layout_rejects_symlinked_state_dir(tmp_path):
    store = line_link.LinkStore(str(tmp_path / 'data'))
    store.ensure()
    os.makedirs(str(tmp_path / 'elsewhere'), exist_ok=True)
    os.symlink(str(tmp_path / 'elsewhere'), store.state_dir)
    assert 'symlink' in store.verify_trusted_layout()


def test_webhook_service_refuses_to_start_on_an_unsafe_layout(tmp_path, monkeypatch):
    """权威目录不属于通知侧账号时, 启动自检必须 fail closed。

    属主不对是 Web 抢先创建同名目录(进而伪造绑定)的唯一途径; 权限位错了
    通知侧自己能 chmod 回来, 但属主抢不回来。
    """
    store = line_link.LinkStore(str(tmp_path / 'data'))
    store.ensure_trusted()
    app = line_webhook.WebhookApp(_config(tmp_path / 'data'), store=store)
    monkeypatch.setattr(os, 'geteuid', lambda: os.getuid() + 1)
    try:
        with pytest.raises(line_link.TrustedLayoutError) as exc:
            app.start()
        assert 'different uid' in str(exc.value)
        assert not app.store.capabilities_enabled()
    finally:
        app.stop.set()
        monkeypatch.undo()


# ══════════════════════════════════════════════════════════════════════
# 2. 文件权限: 伪造 / 替换 / 删除都被父目录权限挡住
# ══════════════════════════════════════════════════════════════════════
def test_permission_bits_block_forge_replace_and_delete(tmp_path):
    """把权威目录设成 0550 (任何人都不可写), 三种伪造手法都必须失败。

    与 UID 无关: 创建/改名/删除文件只需要**父目录写权限**, 没有就一律 EACCES。
    生产布局是 2750 —— 属主(通知侧)可写, Web 属组只有 r-x, 效果相同。
    """
    store = line_link.LinkStore(str(tmp_path / 'data'))
    store.ensure_trusted()
    binding = Path(store.state_dir) / f'binding-{line_link.digest("alice")}.json'
    binding.write_text(json.dumps({'version': 1, 'user': 'alice',
                                   'line_user_id': LINE_A}), encoding='utf-8')
    os.chmod(binding, 0o644)
    os.chmod(store.state_dir, 0o0550)
    try:
        with pytest.raises(OSError):
            (Path(store.state_dir) / 'binding-forged.json').write_text('{}')
        tmp_file = tmp_path / 'replacement.json'
        tmp_file.write_text('{}', encoding='utf-8')
        with pytest.raises(OSError):
            os.replace(str(tmp_file), str(binding))
        with pytest.raises(OSError):
            os.unlink(str(binding))
        # 读仍被允许 —— Web 需要查询自己的绑定状态
        assert json.loads(binding.read_text(encoding='utf-8'))['user'] == 'alice'
    finally:
        os.chmod(store.state_dir, 0o2750)


def test_forged_binding_in_the_inbox_is_never_authoritative(tmp_path):
    """Web 能在 inbox 里写任何文件 —— 但权威解析器只读 trusted 目录。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    forged = {'version': 1, 'user': 'alice', 'line_user_id': LINE_FORGED}
    (Path(store.inbox_dir) / f'binding-{line_link.digest("alice")}.json').write_text(
        json.dumps(forged), encoding='utf-8')
    (Path(store.inbox_dir) / f'unlinked-{line_link.digest("alice")}.json').write_text(
        json.dumps({'version': 1, 'user': 'alice'}), encoding='utf-8')
    (Path(store.inbox_dir) / 'result-forged.json').write_text(
        json.dumps({'ok': True, 'user': 'alice', 'at': 9e18}), encoding='utf-8')

    assert store.binding('alice') is None
    assert store.unlinked('alice') is None
    assert store.bindings() == {}
    assert line_link.bound_target(str(data), 'alice') == ''
    assert line_link.bound_users(str(data)) == []
    assert store.latest_result('alice') is None
    assert LINE_FORGED not in json.dumps(store.status('alice'))
    config = _config(data, Y8Y_NOTIFY_LINE_TARGETS='alice=' + LINE_A)
    assert LineProvider(config).target_for('alice') == LINE_A   # 只认真正的旧配置


def test_binding_uses_verified_event_line_id_not_a_forged_record(tmp_path):
    """即使 Web 在 code 记录里塞了 LINE ID, 生效的仍是验签事件的 source.userId。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    payload, err = store.create_code('alice')
    assert err == ''
    record = store._read(store._path(f'code-{payload["hash"]}.json'))
    record['line_user_id'] = LINE_FORGED          # Web 能写 inbox, 但这是无效字段
    store._write(store._path(f'code-{payload["hash"]}.json'), record)
    app = line_webhook.WebhookApp(_config(data), store=store)
    app.stop.set()
    assert app.handle_event(_event(payload['code'], LINE_A)) == 'bound'
    assert store.binding('alice')['line_user_id'] == LINE_A
    assert LINE_FORGED not in json.dumps(store.bindings())
    # 没有任何经过验签的事件时, 光有文件造不出绑定
    store2 = line_link.LinkStore(str(tmp_path / 'data2'))
    store2.ensure()
    store2.create_code('bob')
    assert store2.bindings() == {}


def test_web_side_api_never_calls_trusted_write_methods():
    """静态断言: Web 进程的代码路径里没有权威写入口。"""
    web = (SRC / 'serve.py').read_text(encoding='utf-8')
    for forbidden in ('set_binding(', 'apply_unlink(', 'set_unlinked(',
                      'set_result(', 'mark_event(', 'ensure_trusted(',
                      'publish_capabilities(', 'set_test_result('):
        assert forbidden not in web, f'serve.py 不应调用 {forbidden}'
    assert 'request_unlink(' in web
    assert 'create_code(' in web and 'queue_test(' in web
    shared = (SRC / 'line_link.py').read_text(encoding='utf-8')
    assert 'def request_unlink' in shared and 'def apply_unlink' in shared
    assert 'def verify_trusted_layout' in shared


def test_web_unlink_request_is_only_a_request(tmp_path):
    """Web 提出解绑请求后, 权威绑定仍在; 必须等通知侧消费。"""
    data = tmp_path / 'data'
    store = line_link.LinkStore(str(data))
    store.ensure()
    store.set_binding('alice', LINE_A, now=1.0)
    store.request_unlink('alice')
    assert store.binding('alice') is not None
    assert store.unlinked('alice') is None
    assert store.has_unlink_request('alice') is True
    assert store.status('alice')['state'] == 'unlinking'
    app = line_webhook.WebhookApp(_config(data), store=store)
    app.stop.set()
    app.consume_requests()
    assert store.binding('alice') is None
    assert store.unlinked('alice') is not None
    assert store.status('alice')['state'] == 'unlinked'


# ══════════════════════════════════════════════════════════════════════
# 3. 跨 UID: CI 的 root 演练脚本必须覆盖这几条 (静态钉死)
# ══════════════════════════════════════════════════════════════════════
def test_ci_cross_uid_fixture_covers_the_new_layout():
    script = (ROOT / 'tests' / 'linux_notify_acl.sh').read_text(encoding='utf-8')
    workflow = (ROOT / '.github' / 'workflows' / 'ubuntu-2204.yml').read_text(
        encoding='utf-8')
    assert 'sudo bash tests/linux_notify_acl.sh' in workflow
    for marker in ('line-link', 'line-link-inbox', 'chmod 3770',
                   'binding-forged', 'test -r', 'binding-x.json'):
        assert marker in script, f'CI 跨 UID 演练缺少 {marker}'


def test_deployment_docs_declare_the_split_layout():
    texts = '\n'.join([
        (ROOT / 'docs' / 'line-self-link.md').read_text(encoding='utf-8'),
        (ROOT / 'docs' / 'deployment.md').read_text(encoding='utf-8'),
        (ROOT / 'README.md').read_text(encoding='utf-8'),
    ])
    for marker in ('2750', '2770', '3770', 'line-link-inbox', 'sticky'):
        assert marker in texts, f'部署文档缺少 {marker}'
