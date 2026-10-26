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
import re
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
from notifications import (                                        # noqa: E402
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
    _write_task(data, 'todo', 'a.md', title='开会', due='2026-10-27',
                due_time='09:00', owner='alice')
    provider = FakeProvider(
        fail_times=99,
        error=ProviderError(f'upstream rejected Bearer {FAKE_LINE_TOKEN}'))
    sched = _make_scheduler(data, {'fake': provider},
                            now=datetime(2026, 10, 27, 9, 0, tzinfo=TZ),
                            settings={'alice': _enabled()},
                            Y8Y_NOTIFY_LINE_TOKEN=FAKE_LINE_TOKEN)
    report = sched.run()

    assert report['failed'], '应当记录一次失败'
    assert FAKE_LINE_TOKEN not in json.dumps(report, ensure_ascii=False)

    state_text = (data / 'notification_state.json').read_text(encoding='utf-8')
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
