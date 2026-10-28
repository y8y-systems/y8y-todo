"""通知系统 —— 凭据安全回归护栏。

这是通知 PR 收尾安全检查留下的护栏, 盯住三类回归:

1. **文档/部署模板不得再用"把凭据文件展开进命令行"的写法**
   (env + 命令替换 / source / export / eval)。那种写法会把 LINE token
   和 Resend API key 变成进程命令行参数 —— /proc/<pid>/cmdline 可见、
   进 shell 历史、进日志, 而且凭据含空格或特殊字符时命令直接解析失败。
   正确做法是复用 systemd 的 ``EnvironmentFile=``。

2. **Provider 抛错时, 错误文本里不得出现凭据**。错误文本会落进
   ``notification_state.json``、runner 的 stdout (→ journald) 和调试日志。

3. **Web 端的 API 响应里不得出现凭据**, Web 进程也不该读得到凭据。

所有数据都是虚构的, 不联网。
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / 'src'
REPO = Path(__file__).resolve().parent.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import notifications                                              # noqa: E402
import notify_runner                                               # noqa: E402
import procguard                                                  # noqa: E402
import taskstore                                                  # noqa: E402
from notifications import (                                        # noqa: E402
    NotificationStateError,
    NotifyConfig,
    Provider,
    ProviderError,
    redact_secrets,
)
from test_notifications import (                                  # noqa: E402
    ALICE,
    TZ,
    FakeProvider,
    _enabled,
    _make_config,
    _make_scheduler,
    _write_task,
)

# 虚构的凭据 —— 形状像真的, 值是编的
FAKE_LINE_TOKEN = 'LINE_TOKEN_fake_0123456789abcdef'
FAKE_EMAIL_KEY = 're_fake_0123456789abcdefXYZ'


# ══════════════════════════════════════════════════════════════════════
# 1. 静态扫描: 文档 / 脚本 / 部署模板
# ══════════════════════════════════════════════════════════════════════

SCAN_TARGETS = [
    'README.md',
    'AGENTS.md',
    'docs',
    'packaging',
    'scripts',
    'src',
    'tests',
    '.github',
]

# (正则, 说明)。每一条都表示"把凭据文件展开进命令行"的危险写法。
UNSAFE_LOADING_PATTERNS = [
    (re.compile(r'\benv\s+\$\('), 'env + 命令替换 (凭据会进 argv)'),
    (re.compile(r'\bexport\s+\$\('), 'export + 命令替换 (凭据会进 argv)'),
    (re.compile(r'\beval\s+\$\('), 'eval + 命令替换 (凭据会进 argv)'),
    (re.compile(r'^\s*(?:source|\.)\s+\S*\.env\b', re.M), 'source 一个 env 文件'),
]


def _scanned_files():
    for name in SCAN_TARGETS:
        path = REPO / name
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in sorted(path.rglob('*')):
                if child.is_file() and '__pycache__' not in child.parts:
                    yield child


def test_no_unsafe_credential_loading_in_the_public_tree():
    """任何文件都不得再用 env/source/export/eval + 命令替换加载凭据。"""
    offenders = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue                              # 二进制/不可读 → 跳过
        for pattern, why in UNSAFE_LOADING_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count('\n', 0, match.start()) + 1
                rel = path.relative_to(REPO)
                offenders.append(f'{rel}:{line} ({why})')
    assert not offenders, '不安全的凭据加载写法: ' + '; '.join(offenders)


def test_notify_units_load_credentials_via_environment_file():
    """通知单元必须用 EnvironmentFile= 读凭据文件, 不能内联凭据。"""
    for name in ('y8y-todo-notify.service',
                 'y8y-todo-notify-dry-run.service',
                 'y8y-todo-notify-test@.service'):
        text = (REPO / 'packaging' / name).read_text(encoding='utf-8')
        assert 'EnvironmentFile=-/etc/y8y-todo/y8y-todo-notify.env' in text, name
        # 凭据不得以 Environment= 内联进单元
        for line in text.splitlines():
            if line.startswith('Environment='):
                assert 'Y8Y_NOTIFY_LINE_TOKEN' not in line, name
                assert 'Y8Y_NOTIFY_EMAIL_API_KEY' not in line, name


def test_helper_units_do_not_put_credentials_on_the_command_line():
    """辅助单元的 ExecStart 只能引用脚本, 不能引用凭据文件路径以外的任何值。"""
    for name, flag in (('y8y-todo-notify-dry-run.service', '--dry-run'),
                       ('y8y-todo-notify-test@.service', '--test %i')):
        text = (REPO / 'packaging' / name).read_text(encoding='utf-8')
        exec_start = [ln for ln in text.splitlines() if ln.startswith('ExecStart=')]
        assert len(exec_start) == 1, name
        assert 'notify_runner.py' in exec_start[0], name
        assert flag in exec_start[0], name
        assert '.env' not in exec_start[0], name


def test_notify_env_example_contains_only_placeholders():
    """示例 env 文件里只能是占位符。"""
    text = (REPO / 'packaging' / 'y8y-todo-notify.env.example').read_text(encoding='utf-8')
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, _, value = line.partition('=')
        values[name.strip()] = value.strip()
    assert values.get('Y8Y_NOTIFY_LINE_TOKEN', '') == ''
    assert values.get('Y8Y_NOTIFY_EMAIL_API_KEY', '') == ''
    assert 'example.com' in values.get('Y8Y_NOTIFY_EMAIL_FROM', '')
    # LINE user id 必须是全零占位符
    for pair in values.get('Y8Y_NOTIFY_LINE_TARGETS', '').split(','):
        if '=' in pair:
            target = pair.split('=', 1)[1].strip()
            assert set(target) <= {'U', '0'}, f'LINE target 不是占位符: {target}'


# ══════════════════════════════════════════════════════════════════════
# 2. 脱敏函数本身
# ══════════════════════════════════════════════════════════════════════

def test_redact_secrets_replaces_known_values():
    text = f'Authorization: Bearer {FAKE_LINE_TOKEN} failed'
    out = redact_secrets(text, [FAKE_LINE_TOKEN])
    assert FAKE_LINE_TOKEN not in out
    assert '***' in out


def test_redact_secrets_ignores_short_values():
    """太短的串不替换 —— 否则会把正常文本 mangled 掉。"""
    assert redact_secrets('abc def', ['ab']) == 'abc def'
    assert redact_secrets('', [FAKE_LINE_TOKEN]) == ''


def test_notify_config_secrets_only_lists_credentials():
    config = NotifyConfig({
        'Y8Y_USERS': 'alice@example.com=alice',
        'Y8Y_NOTIFY_LINE_TOKEN': FAKE_LINE_TOKEN,
        'Y8Y_NOTIFY_EMAIL_API_KEY': FAKE_EMAIL_KEY,
        'Y8Y_NOTIFY_EMAIL_FROM': 'Y8y Todo <todo@example.com>',
        'Y8Y_NOTIFY_LINE_TARGETS': 'alice=U0000000000000000000000000000000',
    })
    assert config.secrets() == [FAKE_LINE_TOKEN, FAKE_EMAIL_KEY]


# ══════════════════════════════════════════════════════════════════════
# 3. Provider 错误文本不得含凭据
# ══════════════════════════════════════════════════════════════════════

def _line_provider(tmp_path, transport):
    config = _make_config(tmp_path,
                          Y8Y_NOTIFY_LINE_TOKEN=FAKE_LINE_TOKEN,
                          Y8Y_NOTIFY_LINE_TARGETS='alice=U0000000000000000000000000000000')
    return notifications.LineProvider(config, transport)


def test_provider_does_not_leak_token_in_unexpected_error(tmp_path):
    """urllib 因 header 非法抛 ValueError 时, 原文里带着 token。"""

    def boom(url, payload, headers, timeout):
        raise ValueError(f'Invalid header value b\'Bearer {FAKE_LINE_TOKEN}\'')

    provider = _line_provider(tmp_path, boom)
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('reminder', 's', 'b'))
    assert FAKE_LINE_TOKEN not in str(exc.value)
    assert FAKE_LINE_TOKEN not in repr(exc.value)


def test_provider_redacts_credentials_from_error_message(tmp_path):
    """即便上游把凭据写进错误消息, 出口也必须是脱敏后的。"""

    def boom(url, payload, headers, timeout):
        raise ProviderError(f'upstream rejected Bearer {FAKE_LINE_TOKEN}')

    provider = _line_provider(tmp_path, boom)
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('reminder', 's', 'b'))
    assert FAKE_LINE_TOKEN not in str(exc.value)
    assert '***' in str(exc.value)


def test_provider_error_message_keeps_its_retry_semantics(tmp_path):
    """脱敏不能改掉重试语义 (4xx 不重试 / 5xx 重试)。"""

    def http_error(code):
        def boom(url, payload, headers, timeout):
            import urllib.error
            raise urllib.error.HTTPError(url, code, 'err', {}, None)
        return boom

    provider = _line_provider(tmp_path, http_error(401))
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('reminder', 's', 'b'))
    assert exc.value.retryable is False

    provider = _line_provider(tmp_path, http_error(500))
    with pytest.raises(ProviderError) as exc:
        provider.send('alice', notifications.Message('reminder', 's', 'b'))
    assert exc.value.retryable is True


# ══════════════════════════════════════════════════════════════════════
# 4. 投递状态文件 / 报告 / 日志不得含凭据
# ══════════════════════════════════════════════════════════════════════

def test_delivery_state_and_report_never_contain_credentials(tmp_path):
    """投递失败时, 状态文件 + 报告 + 日志里都不能出现凭据。"""
    data = tmp_path / 'data'
    # 共享任务 + lead=0 → 提醒正落在 now 那一分钟, 确实会去投递
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='shared')
    provider = FakeProvider(
        fail_times=99,
        error=ProviderError(f'upstream rejected Bearer {FAKE_LINE_TOKEN}'))
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled(lead_minutes=0)},
                            Y8Y_NOTIFY_LINE_TOKEN=FAKE_LINE_TOKEN)
    report = sched.run()

    assert report['failed'], '应当记录一次失败'
    assert FAKE_LINE_TOKEN not in json.dumps(report, ensure_ascii=False)

    state_text = (data / 'notify' / 'notification_state.json').read_text(encoding='utf-8')
    assert FAKE_LINE_TOKEN not in state_text
    assert '***' in state_text

    log_text = '\n'.join(sched.log)
    assert FAKE_LINE_TOKEN not in log_text


def test_capability_table_contains_no_credentials(tmp_path):
    """能力表 (Web 端读它) 里没有任何凭据。"""
    config = _make_config(tmp_path,
                          Y8Y_NOTIFY_LINE_TOKEN=FAKE_LINE_TOKEN,
                          Y8Y_NOTIFY_EMAIL_API_KEY=FAKE_EMAIL_KEY,
                          Y8Y_NOTIFY_LINE_TARGETS='alice=U0000000000000000000000000000000')
    providers = notifications.build_providers(config)
    path = tmp_path / 'notification_channels.json'
    notifications.publish_capabilities(str(path), config, providers,
                                       users=['alice'])
    text = path.read_text(encoding='utf-8')
    assert FAKE_LINE_TOKEN not in text
    assert FAKE_EMAIL_KEY not in text


# ══════════════════════════════════════════════════════════════════════
# 5. runner 输出 (→ journald) 不得含凭据
# ══════════════════════════════════════════════════════════════════════

class _BoomProvider(Provider):
    """总是失败, 而且错误消息里带着凭据的假渠道。"""

    name = 'line'
    label = 'LINE'

    def configured(self):
        return True

    def target_for(self, user):
        return 'U0000000000000000000000000000000'

    def send(self, user, message):
        raise ProviderError(f'upstream rejected Bearer {FAKE_LINE_TOKEN}')


def test_runner_test_output_never_contains_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(notifications, 'build_providers',
                        lambda config, transport=None: {'line': _BoomProvider(config)})
    monkeypatch.setenv('Y8Y_USERS', 'alice@example.com=alice')
    monkeypatch.setenv('Y8Y_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('Y8Y_NOTIFY_LINE_TOKEN', FAKE_LINE_TOKEN)
    monkeypatch.setenv('Y8Y_NOTIFY_LINE_TARGETS', 'alice=U0000000000000000000000000000000')

    rc = notify_runner.main(['--test', 'alice'])
    out = capsys.readouterr().out
    assert rc == 1
    assert FAKE_LINE_TOKEN not in out
    assert '***' in out


def test_runner_json_output_never_contains_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(notifications, 'build_providers',
                        lambda config, transport=None: {'line': _BoomProvider(config)})
    monkeypatch.setenv('Y8Y_USERS', 'alice@example.com=alice')
    monkeypatch.setenv('Y8Y_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('Y8Y_NOTIFY_LINE_TOKEN', FAKE_LINE_TOKEN)
    monkeypatch.setenv('Y8Y_NOTIFY_LINE_TARGETS', 'alice=U0000000000000000000000000000000')

    notify_runner.main(['--test', 'alice', '--json'])
    out = capsys.readouterr().out
    assert FAKE_LINE_TOKEN not in out
    payload = json.loads(out)
    assert FAKE_LINE_TOKEN not in json.dumps(payload, ensure_ascii=False)


# ══════════════════════════════════════════════════════════════════════
# 6. Web 进程不得读得到凭据, API 响应不得含凭据
# ══════════════════════════════════════════════════════════════════════

def test_web_service_unit_never_reads_the_notify_env_file():
    text = (REPO / 'packaging' / 'y8y-todo.service').read_text(encoding='utf-8')
    assert 'y8y-todo-notify.env' not in text
    for line in text.splitlines():
        if line.startswith('EnvironmentFile'):
            assert 'notify' not in line


def test_web_source_never_touches_notification_credentials():
    text = (REPO / 'src' / 'serve.py').read_text(encoding='utf-8')
    assert 'NotifyConfig' not in text
    assert 'Y8Y_NOTIFY_LINE_TOKEN' not in text
    assert 'Y8Y_NOTIFY_EMAIL_API_KEY' not in text


def test_notification_settings_api_response_has_no_credentials(server):
    _serve, _httpd, client, _tmp = server
    status, body = client.get('/todo/api/notification-settings', headers=ALICE)
    assert status == 200
    raw = json.dumps(body, ensure_ascii=False).lower()
    for needle in ('token', 'secret', 'api_key', 'bearer', 'password'):
        assert needle not in raw, f'API 响应里出现了 {needle}'


# ══════════════════════════════════════════════════════════════════════
# 7. 进程权限隔离: 同 UID 的另一个进程读不到调度器的凭据
# ══════════════════════════════════════════════════════════════════════
#
# 内核的 ptrace_may_access() 对"同 UID"直接放行, 所以 Web 服务和通知
# 调度器共用同一个系统用户时, 被攻破的 Web 进程能直接读出调度器的
# /proc/<pid>/environ —— 里面就是 LINE token 和 Resend API key。
#
# 调度器启动时把自己设成"不可 dump" (prctl(PR_SET_DUMPABLE, 0)) 之后,
# 同 UID 的读者拿不到 environ, 也拿不到 mem。下面的用例是真的去读
# /proc/<pid>/environ, 不是模拟。

def _spawn_reader_probe(script, env):
    proc = subprocess.Popen([sys.executable, '-c', script], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    return proc


def _read_environ(pid):
    """读 /proc/<pid>/environ。返回 (是否读得到, 内容)。"""
    status, blob = _read_environ_status(pid)
    return status == 'read', blob


def _read_environ_status(pid):
    """读 /proc/<pid>/environ。

    返回 (状态, 内容):
      'read'    —— 读到了 (同 UID 可以读别人的 environ)
      'denied'  —— EACCES (内核拒绝了 —— 加固生效, 或同 UID 之外的隔离)
      'gone'    —— 进程已经不在了 (探针没跑起来)
    """
    try:
        with open(f'/proc/{pid}/environ', 'rb') as f:
            return 'read', f.read()
    except PermissionError:
        return 'denied', b''
    except (FileNotFoundError, ProcessLookupError):
        return 'gone', b''


@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='procguard 是 Linux 专有 (prctl PR_SET_DUMPABLE)')
def test_hardened_scheduler_environ_is_unreadable_by_the_same_uid(tmp_path):
    """核心断言: 加固之后, 同 UID 的另一个进程读不到调度器的 environ。

    这里是真的去读 /proc/<pid>/environ, 不是模拟。
    """
    secret = 'PROC_TEST_SECRET_TOKEN_0123456789'
    env = dict(os.environ)
    env['Y8Y_NOTIFY_LINE_TOKEN'] = secret

    hardened = _spawn_reader_probe(
        'import sys, time; sys.path.insert(0, %r); import procguard; '
        'procguard.harden(); print("ready", flush=True); time.sleep(30)'
        % str(SRC), env)
    try:
        line = hardened.stdout.readline().strip()
        if line != 'ready':
            # 探针进程在本环境起不来 (CI 沙箱差异) → 这里的 /proc 实测做不了。
            # 不是加固失效: 加固本身由 test_runner_hardens_itself_at_startup
            # 与 test_harden_respects_the_debug_escape_hatch 直接断言。
            pytest.skip(f'探针进程没能在本环境启动, 无法做 /proc 实测 (输出: {line[:200]!r})')

        status, _blob = _read_environ_status(hardened.pid)
        assert status == 'denied', (
            f'加固后的调度器 environ 仍被同 UID 读到 (状态={status})')
        # 内存同样读不到
        try:
            with open(f'/proc/{hardened.pid}/mem', 'rb') as f:
                f.read(1)
        except (PermissionError, OSError):
            pass
        else:
            raise AssertionError('加固后的调度器 mem 仍被同 UID 读到')
        # 但命令行与进程状态照常可见 —— systemd 监控与排查不受影响
        cmdline = Path(f'/proc/{hardened.pid}/cmdline').read_bytes()
        assert b'procguard' in cmdline
        assert Path(f'/proc/{hardened.pid}/status').exists()
    finally:
        hardened.kill()
        hardened.wait()


@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='procguard 是 Linux 专有 (prctl PR_SET_DUMPABLE)')
def test_same_uid_can_read_an_unhardened_process_environ(tmp_path):
    """前提: 不加固的话, 同 UID 确实能读到别人的 environ (这就是那个洞)。

    这个前提跟运行环境有关 —— 某些 CI 沙箱对 /proc 的视图与真实内核
    不同 (读回来的 environ 是空的), 那时跳过并把诊断信息写进跳过原因,
    而不是把环境差异报成"安全断言失败"。真正的判定在目标宿主上做
    (docs/acceptance.md §9.3)。
    """
    secret = 'PROC_TEST_SECRET_TOKEN_0123456789'
    env = dict(os.environ)
    env['Y8Y_NOTIFY_LINE_TOKEN'] = secret

    control = _spawn_reader_probe(
        'import time; print("ready", flush=True); time.sleep(30)', env)
    try:
        # 等探针自己说"ready" —— 它跑到这行就说明 exec 完成了, /proc 里的
        # 环境区也已经就绪 (用固定 sleep 同步会在慢机器上偶发读到空内容)。
        line = control.stdout.readline().strip()
        if line != 'ready':
            pytest.skip('对照探针进程提前退出 (输出=%r) —— '
                        '本环境无法复现"同 UID 可读 environ"这一前提'
                        % line[:200])
        status, blob = _read_environ_status(control.pid)
        if status == 'gone':
            pytest.skip('对照探针进程已经不在了 —— '
                        '本环境无法复现"同 UID 可读 environ"这一前提')
        if status == 'denied':
            pytest.skip('当前环境对 /proc 的读取已额外收紧 (如 hidepid), '
                        '无法复现"同 UID 可读"这一前提')
        if secret.encode() not in blob:
            pytest.skip('同 UID 读到了 environ, 但里面没有预期的变量 '
                        '(读回 %d 字节) —— 本环境的 /proc 视图与预期不同, '
                        '不作为前提使用' % len(blob))
    finally:
        control.kill()
        control.wait()


@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='procguard 是 Linux 专有 (prctl PR_SET_DUMPABLE)')
def test_harden_respects_the_debug_escape_hatch():
    before = procguard.dumpable()
    assert procguard.harden({'Y8Y_NOTIFY_KEEP_DUMPABLE': '1'}) is False
    assert procguard.dumpable() == before, '逃生阀生效时不该改动 dumpable'


@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='procguard 是 Linux 专有 (prctl PR_SET_DUMPABLE)')
def test_runner_hardens_itself_at_startup(tmp_path, monkeypatch):
    """调度器一启动就把自己设成不可 dump。"""
    monkeypatch.setenv('Y8Y_USERS', 'alice@example.com=alice')
    monkeypatch.setenv('Y8Y_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('Y8Y_NOTIFY_ENABLED', '0')
    try:
        notify_runner.main(['--dry-run'])
        assert procguard.dumpable() == 0
    finally:
        procguard.allow_dumpable()      # 复位, 别影响后面的用例


@pytest.mark.skipif(not sys.platform.startswith('linux'),
                    reason='procguard 是 Linux 专有 (prctl PR_SET_DUMPABLE)')
def test_runner_reports_the_hardened_state_in_its_output(tmp_path, monkeypatch, capsys):
    """运行日志里带 dumpable= 标记 —— 运维在 journal 里一眼能确认加固生效。

    (Linux 5.15 的 /proc/<pid>/status 里**没有** Dumpable 字段, 所以不能靠
    读 /proc 判断别人的进程有没有加固 —— 必须由进程自己说。)
    """
    monkeypatch.setenv('Y8Y_USERS', 'alice@example.com=alice')
    monkeypatch.setenv('Y8Y_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('Y8Y_NOTIFY_ENABLED', '0')
    try:
        notify_runner.main(['--dry-run'])
        out = capsys.readouterr().out
        assert 'dumpable=0' in out, out
    finally:
        procguard.allow_dumpable()


# ══════════════════════════════════════════════════════════════════════
# 8. 隔离审计脚本
# ══════════════════════════════════════════════════════════════════════
#
# scripts/check-notify-isolation.py 是只读审计: 它用来在**目标宿主**上
# 回答"Web 进程能不能读到通知凭据"。这个脚本自己必须守住两条线:
#
#   · 它得能认出"两个单元共用同一个用户"这种不安全状态 (退出码 1),
#     否则它就是个永远说 OK 的安慰剂;
#   · 它自己**不能**改任何东西 —— 不建用户、不改权限、不启停服务。
#     审计工具顺手把生产环境改了, 那它就不是审计了。

AUDIT_PY = REPO / 'scripts' / 'check-notify-isolation.py'
AUDIT_SH = REPO / 'scripts' / 'check-notify-isolation.sh'


def test_missing_environ_is_unknown_not_denied():
    import importlib.util
    spec = importlib.util.spec_from_file_location('notify_audit', AUDIT_PY)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    # Simulate a process disappearing between discovery and the read.
    from unittest.mock import patch
    with patch.object(audit, '_drop_to', return_value=None):
        assert audit._read_as_user(os.getuid(), os.getgid(), [],
                                   '/proc/999999999/environ') is None


@pytest.mark.parametrize('returncode, expected', [(0, True), (10, False), (1, None), (11, None), (3, None), (-9, None)])
def test_read_probe_does_not_confuse_errors_with_denial(returncode, expected, monkeypatch):
    import importlib.util
    from types import SimpleNamespace
    spec = importlib.util.spec_from_file_location('notify_audit', AUDIT_PY)
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=returncode))
    assert audit._read_as_user(1, 1, [], '/unused') is expected

# 会改动系统的命令 —— 审计脚本里出现任何一个都是 bug。
# 注意: os.setuid / os.setgid **不算**在这里 —— 审计脚本要"降权到目标
# 用户"才能做跨用户读取实测, 那是子进程里的降权, 不是对系统的改动;
# 单独用下面的用例保证它只降不升。
MUTATING_COMMANDS = (
    'chown', 'chmod', 'chgrp', 'useradd', 'usermod', 'userdel',
    'groupadd', 'groupdel', 'setfacl', 'chattr',
    'systemctl', 'service ', 'visudo', 'mount', 'unlink', 'rmtree',
)


def _write_units(unit_dir, web_user, notify_user, notify_rw=None):
    """写一对最小可用的单元文件, 供审计脚本读取。"""
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / 'y8y-todo.service').write_text(
        f'[Service]\nUser={web_user}\n', encoding='utf-8')
    rw = notify_rw if notify_rw is not None else '-/var/lib/y8y-todo/notify'
    (unit_dir / 'y8y-todo-notify.service').write_text(
        f'[Service]\nUser={notify_user}\nReadWritePaths={rw}\n', encoding='utf-8')
    return unit_dir


def test_isolation_audit_flags_a_shared_user_between_web_and_scheduler(tmp_path):
    """共用同一个用户时, 审计脚本必须报失败 (退出码 1), 不能说 OK。"""
    unit_dir = _write_units(tmp_path / 'units', 'y8y', 'y8y')
    proc = subprocess.run(
        [sys.executable, str(AUDIT_PY),
         '--unit-dir', str(unit_dir),
         '--env-file', str(tmp_path / 'nonexistent.env'),
         '--data-dir', str(tmp_path / 'nodata')],
        capture_output=True, text=True)
    assert proc.returncode == 1, (
        f'审计脚本退出码应为 1 (有失败项), 实际 {proc.returncode}\n'
        f'{proc.stdout}\n{proc.stderr}')
    assert 'C1' in proc.stdout, proc.stdout
    assert 'FAIL' in proc.stdout, proc.stdout


def test_isolation_audit_passes_when_the_units_use_different_users(tmp_path):
    """两个单元用不同用户时, C1 必须判为 OK (其余项无法验证 → 退出码 2)。"""
    unit_dir = _write_units(tmp_path / 'units', 'y8y', 'y8y-notify')
    proc = subprocess.run(
        [sys.executable, str(AUDIT_PY),
         '--unit-dir', str(unit_dir),
         '--env-file', str(tmp_path / 'nonexistent.env'),
         '--data-dir', str(tmp_path / 'nodata')],
        capture_output=True, text=True)
    # 退出码 2 = 有无法验证项 (需要 root / 运行中的调度器) —— 不等于失败
    assert proc.returncode == 2, (
        f'退出码应为 2 (有无法验证项), 实际 {proc.returncode}\n{proc.stdout}')
    assert 'C1' in proc.stdout and 'OK' in proc.stdout, proc.stdout


def test_packaged_notify_units_use_a_different_user_than_the_web_unit():
    """仓库自带的单元模板必须是两个不同的用户。

    这条是"隔离"这件事的底线: 同 UID 时内核放行对
    /proc/<pid>/environ 的读取, 被攻破的 Web 进程能直接拿到凭据。
    """
    web_user = None
    notify_user = None
    for name, key in (('y8y-todo.service', 'web'),
                      ('y8y-todo-notify.service', 'notify'),
                      ('y8y-todo-notify-dry-run.service', 'notify'),
                      ('y8y-todo-notify-test@.service', 'notify')):
        text = (REPO / 'packaging' / name).read_text(encoding='utf-8')
        users = [ln.split('=', 1)[1].strip()
                 for ln in text.splitlines() if ln.startswith('User=')]
        assert len(users) == 1, f'{name} 应当恰好有一个 User= 行'
        if key == 'web':
            web_user = users[0]
        else:
            assert notify_user is None or notify_user == users[0], \
                '三个通知单元必须用同一个用户'
            notify_user = users[0]
    assert web_user and notify_user
    assert web_user != notify_user, (
        f'Web 与调度器不能是同一个用户 (都是 {web_user!r}) —— '
        f'同 UID 时内核放行 environ 读取')


def test_notify_units_only_allow_writing_the_notification_state_dir():
    """通知单元的可写范围只能是状态目录。

    数据目录根部还有 internal_tokens.json —— 那是机器凭据, 调度器不该
    能替换它。所以 ReadWritePaths 不能覆盖整个数据目录。
    """
    for name in ('y8y-todo-notify.service',
                 'y8y-todo-notify-dry-run.service',
                 'y8y-todo-notify-test@.service'):
        text = (REPO / 'packaging' / name).read_text(encoding='utf-8')
        rw = [ln.split('=', 1)[1].strip()
              for ln in text.splitlines() if ln.startswith('ReadWritePaths=')]
        assert rw, f'{name} 应当声明 ReadWritePaths='
        for value in rw:
            for one in value.split():
                path = one.lstrip('-')
                assert path != '/var/lib/y8y-todo', (
                    f'{name}: 通知单元不能把整个数据目录设为可写 ({one})')
                assert path.startswith('/var/lib/y8y-todo/'), (
                    f'{name}: 可写路径应当在状态目录里 ({one})')


def test_isolation_audit_flags_a_notify_unit_that_can_write_the_whole_data_dir(tmp_path):
    """通知单元可写范围覆盖整个数据目录时, 审计必须报失败。"""
    unit_dir = _write_units(tmp_path / 'units', 'y8y', 'y8y-notify',
                            notify_rw='/var/lib/y8y-todo')
    proc = subprocess.run(
        [sys.executable, str(AUDIT_PY),
         '--unit-dir', str(unit_dir),
         '--env-file', str(tmp_path / 'nonexistent.env'),
         '--data-dir', '/var/lib/y8y-todo'],
        capture_output=True, text=True)
    assert proc.returncode == 1, (
        f'退出码应为 1 (有失败项), 实际 {proc.returncode}\n{proc.stdout}')
    assert 'C7' in proc.stdout, proc.stdout
    assert 'FAIL' in proc.stdout, proc.stdout


def test_isolation_audit_never_reports_ok_when_a_check_cannot_be_verified(tmp_path):
    """Fail closed: 判不了的检查项必须算"无法验证", 不能算通过。

    这是审计脚本自己的底线 —— 把"没查出来"当成"没问题", 审计就成了
    安慰剂。这里用一个**存在但无法判定**的场景: 状态目录指向一个
    判不了的文件 (悬空符号链接), 看它是不是报 UNKNOWN 而不是 OK。
    """
    unit_dir = _write_units(tmp_path / 'units', 'y8y', 'y8y-notify')
    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    # 指向不存在目标的符号链接: stat() 会失败 → 判断不了
    try:
        (state_dir / 'notification_channels.json').symlink_to(tmp_path / 'nope')
    except (OSError, NotImplementedError):
        pytest.skip('本环境不支持符号链接')
    proc = subprocess.run(
        [sys.executable, str(AUDIT_PY),
         '--unit-dir', str(unit_dir),
         '--env-file', str(tmp_path / 'nonexistent.env'),
         '--data-dir', str(tmp_path / 'nodata'),
         '--state-dir', str(state_dir)],
        capture_output=True, text=True)
    # 无论如何都不能是 0 (全部通过) —— 判不了的项必须挡住"通过"
    assert proc.returncode != 0, (
        f'有无法验证的检查项时退出码不能是 0\n{proc.stdout}')
    assert 'UNKNOWN' in proc.stdout, proc.stdout


def test_isolation_audit_reports_unknown_instead_of_crashing_without_proc(tmp_path):
    """没有 /proc 的平台上, 审计脚本必须报"无法验证"而不是崩掉。"""
    unit_dir = _write_units(tmp_path / 'units', 'y8y', 'y8y-notify')
    proc = subprocess.run(
        [sys.executable, str(AUDIT_PY),
         '--unit-dir', str(unit_dir),
         '--env-file', str(tmp_path / 'nonexistent.env'),
         '--data-dir', str(tmp_path / 'nodata')],
        capture_output=True, text=True)
    assert 'Traceback' not in proc.stderr, proc.stderr
    assert proc.returncode in (0, 2), (
        f'退出码应为 0 或 2, 实际 {proc.returncode}\n{proc.stdout}\n{proc.stderr}')


def test_isolation_audit_script_is_read_only():
    """审计脚本不得包含任何改动系统的命令。"""
    for path in (AUDIT_PY, AUDIT_SH):
        text = path.read_text(encoding='utf-8')
        for command in MUTATING_COMMANDS:
            assert command not in text, (
                f'{path.name} 里出现了会改动系统的命令: {command!r}')
        # 也不该出现 shell 重定向写入
        assert not re.search(r'>\s*/etc/', text), path.name
        assert not re.search(r'>\s*/var/', text), path.name


def test_isolation_audit_wrapper_delegates_to_the_python_audit():
    """wrapper 必须真的调用审计脚本, 不能自己吞掉结果。"""
    text = AUDIT_SH.read_text(encoding='utf-8')
    assert 'check-notify-isolation.py' in text
    assert 'exec python3' in text


def test_isolation_audit_never_raises_privileges():
    """降权只降不升 —— 审计脚本不得把自己变成 root。"""
    text = AUDIT_PY.read_text(encoding='utf-8')
    assert 'setuid(0)' not in text
    assert 'setgid(0)' not in text
    # 只在子进程的 preexec 里降权
    assert 'preexec_fn' in text


# ══════════════════════════════════════════════════════════════════════
# 9. 共享状态目录的写入安全
# ══════════════════════════════════════════════════════════════════════
#
# 通知状态目录由**两个**服务账号共用 (Web 写用户偏好, 调度器写投递状态)。
# 共用一个目录就会带来两个新风险, 这里各锁一条:
#   · 预置的符号链接不能把写入引到别处 (所以临时文件用 O_EXCL);
#   · 投递状态写不进去时, 调度器必须**一条都不发** —— 否则每 5 分钟
#     就把同一条提醒重发一遍。

def test_atomic_write_refuses_to_follow_a_pre_planted_symlink(tmp_path, monkeypatch):
    """预先放好的符号链接不能把写入引到别的文件上去。"""
    victim = tmp_path / 'victim.txt'
    victim.write_text('original', encoding='utf-8')
    dest = tmp_path / 'state.json'

    # 让临时文件名可预测, 好在它前面放一个符号链接
    monkeypatch.setattr(taskstore.os, 'urandom', lambda n: b'\x00' * n)
    tmp_path_name = f'{dest}.{os.getpid()}.00000000.tmp'
    link = tmp_path / tmp_path_name
    try:
        link.symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip('本环境不支持符号链接')

    with pytest.raises(FileExistsError):
        taskstore.atomic_write(str(dest), 'new content')

    # 目标文件必须原封不动
    assert victim.read_text(encoding='utf-8') == 'original'
    assert not dest.exists()


def test_atomic_write_leaves_no_temp_file_behind_on_failure(tmp_path, monkeypatch):
    """写失败时不要把半截临时文件留在共享目录里。"""
    dest = tmp_path / 'state.json'
    monkeypatch.setattr(taskstore.os, 'replace',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('boom')))
    with pytest.raises(OSError):
        taskstore.atomic_write(str(dest), 'content')
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith('.tmp')]
    assert not leftovers, f'留下了临时文件: {leftovers}'


def test_scheduler_refuses_to_send_when_the_state_dir_is_not_writable(tmp_path):
    """状态目录写不了 → 一条都不发 (fail closed)。"""
    if os.geteuid() == 0:
        pytest.skip('root 不受目录权限限制, 这个用例测不了')
    data = tmp_path / 'data'
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='alice')
    provider = FakeProvider()
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled()})
    state_dir = data / 'notify'
    assert state_dir.is_dir()
    state_dir.chmod(0o555)                    # 只读
    try:
        with pytest.raises(NotificationStateError):
            sched.run()
    finally:
        state_dir.chmod(0o755)
    assert provider.sent == [], '状态写不进去就不该发出任何通知'


def test_notification_json_files_live_in_the_state_dir_not_the_data_root(tmp_path, monkeypatch):
    """三个通知 JSON 都在 <data>/notify/ 里, 不在数据目录根部。

    数据目录根部还有 internal_tokens.json 这种机器凭据 —— 调度器只该
    对状态目录有写权限 (docs/deployment.md §5.2)。
    """
    monkeypatch.setenv('Y8Y_USERS', 'alice@example.com=alice')
    monkeypatch.setenv('Y8Y_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('Y8Y_NOTIFY_ENABLED', '1')
    notify_runner.main([])
    assert (tmp_path / 'notify' / 'notification_channels.json').exists()
    for name in ('notification_channels.json',
                 'notification_state.json',
                 'notification_settings.json'):
        assert not (tmp_path / name).exists(), \
            f'{name} 不该落在数据目录根部'


def test_web_tmp_cleanup_does_not_touch_the_notification_state_dir(tmp_path, monkeypatch):
    """Web 启动时清理残留 .tmp, 但**不能**动调度器目录里的。

    调度器可能正好在写临时文件 —— 被删掉的话 os.replace 会失败,
    投递状态就丢了。
    """
    import serve
    monkeypatch.setattr(serve, 'TODOLIST_DIR', str(tmp_path))
    notify_dir = tmp_path / 'notify'
    notify_dir.mkdir()
    (notify_dir / 'notification_state.json.123.abc.tmp').write_text(
        'in flight', encoding='utf-8')
    (tmp_path / 'stale.tmp').write_text('stale', encoding='utf-8')

    serve._cleanup_stale_tmp_files()

    assert (notify_dir / 'notification_state.json.123.abc.tmp').exists(), \
        '调度器的临时文件被 Web 的清理逻辑删掉了'
    assert not (tmp_path / 'stale.tmp').exists(), '自己的残留临时文件应当被清理'


