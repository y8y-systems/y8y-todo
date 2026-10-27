"""Notification engine for y8y Todo — V1.

Standard library only, no build step.  The engine is deliberately split
from the web server:

- ``serve.py``         — the HTTP app (never sends anything by itself)
- ``notifications.py`` — *this* module: settings, state, providers, scheduler
- ``notify_runner.py`` — the one-shot CLI a systemd timer calls

Nothing here mutates task files.  The scheduler only **reads** tasks and
writes two side-car files inside the data directory:

``<data_dir>/notify/notification_settings.json``
    per-user preferences (which channels, lead time, daily digest).
    No secrets — secrets live in the server's environment only.

``<data_dir>/notify/notification_state.json``
    delivery bookkeeping (what was already sent, retry counters).

``<data_dir>/notify/notification_channels.json``
    the credential-free capability table the web app renders.

All three live in a **dedicated state directory** (``<data_dir>/notify``)
rather than in the data directory root: the scheduler runs under its own
system account and needs write access to its own state, but the data
directory root also holds ``internal_tokens.json`` — a machine
credential the scheduler must never be able to replace. One small
directory both accounts may write; nothing else (see docs/deployment.md
§5.2).

Both files are **additive**: the on-disk task format is untouched, so an
upgrade never rewrites existing data (see AGENTS.md §3).

Safety rules baked into this module:

1. **Off by default.** A user must enable notifications themselves *and*
   the operator must enable the subsystem (``Y8Y_NOTIFY_ENABLED``).
2. **A missing/未配置 channel is never an error.** Providers that have no
   credentials or no target for a user are simply skipped.
3. **Private tasks never leak.** A notification is only ever built from
   tasks the recipient can already see in the UI (own tasks + shared
   tasks), using exactly the same rule as the web app
   (``taskstore.can_access``).
4. **No duplicate sends.** Every (user, task, occurrence, channel) tuple
   is recorded in the state file; failures are retried with backoff and a
   bounded number of attempts.
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone as _timezone

from config import Config
from taskstore import (
    atomic_write,
    can_access,
    display_title,
    parse_frontmatter,
    probe_writable,
    scan_md_files,
    task_owner,
)

__all__ = [
    'DEFAULT_SETTINGS',
    'Capabilities',
    'EmailProvider',
    'LineProvider',
    'Message',
    'NotificationStateError',
    'NotifyConfig',
    'Provider',
    'ProviderError',
    'Scheduler',
    'SettingsStore',
    'StateStore',
    'TaskView',
    'build_providers',
    'default_settings',
    'normalize_settings',
    'notify_state_dir',
    'publish_capabilities',
    'register_provider',
    'registered_channels',
    'scan_tasks',
]

# ── 常量 ──────────────────────────────────────────────────────────────
SETTINGS_FILE = 'notification_settings.json'
STATE_FILE = 'notification_state.json'
CHANNELS_FILE = 'notification_channels.json'
STATE_VERSION = 1
CAPABILITIES_VERSION = 1

#: 通知状态目录名 (相对数据目录)。
#:
#: 三个通知 JSON 全部放在这个**专用目录**里, 而不是数据目录根部 ——
#: 原因见 docs/deployment.md §5.2: 通知调度器以独立的系统用户运行,
#: 它需要写自己的投递状态, 但**不能**对数据目录有写权限 (那里还有
#: internal_tokens.json 这种机器凭据)。专用目录是"两边都能写、
#: 别的都碰不到"的那一小块。
NOTIFY_DIR_NAME = 'notify'

#: LINE push 文本上限 5000 字符; 留出余量并按条切分 (LINE 单次 push 最多 5 条)
LINE_CHUNK_SIZE = 4000
LINE_MAX_CHUNKS = 5

#: 每日摘要默认发送时间
DEFAULT_DIGEST_TIME = '08:00'

#: 提前提醒时间的合法范围 (分钟)
MAX_LEAD_MINUTES = 1440

#: 重试退避 (分钟): base * 2^(attempts-1), 上限 cap
RETRY_BASE_MINUTES = 5
RETRY_CAP_MINUTES = 60
MAX_ATTEMPTS = 5

_TIME_RE = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')


def _flag(value):
    return (value or '').strip().lower() in ('1', 'true', 'yes', 'on')


# ── 状态目录 ──────────────────────────────────────────────────────────
def notify_state_dir(data_dir):
    """通知状态目录的绝对路径 (``<data_dir>/notify``)。

    三个通知 JSON 都住在这里。两个服务账号 (Web ``y8y`` / 调度器
    ``y8y-notify``) 共享这一个目录, 数据目录根部则保持对调度器只读。
    """
    return os.path.join(str(data_dir), NOTIFY_DIR_NAME)


class NotificationStateError(Exception):
    """通知状态目录不可写 —— 宁可一条都不发, 也不要重复发送。

    投递状态是"不重复发送"的唯一依据: 记录不下来, 每一次 tick 都会把
    同一条提醒重发一遍。所以这个错误是 **fail closed** 的 —— 调度器在
    发出任何通知之前先探一次目录可写性, 探不过就整体停下来。
    """

    def __init__(self, directory, detail=''):
        self.directory = directory
        self.detail = detail
        super().__init__(self._message())

    def _message(self):
        lines = [
            f'通知状态目录不可写: {self.directory}',
            '调度器无法记录投递状态 —— 继续发送会导致同一条提醒被反复发送, '
            '因此本次不发送任何通知。',
            '修复 (以 root 执行):',
            f'  sudo install -d -o y8y-notify -g y8y-notify -m 2770 {self.directory}',
            '详见 docs/deployment.md §5.2。',
        ]
        if self.detail:
            lines.insert(1, f'  ({self.detail})')
        return '\n'.join(lines)


def _parse_targets(raw):
    """'alice=U123,bob=U456' → {'alice': 'U123', 'bob': 'U456'}"""
    out = {}
    for pair in (raw or '').split(','):
        user, sep, target = pair.strip().partition('=')
        user, target = user.strip(), target.strip()
        if sep and user and target:
            out[user] = target
    return out


# ── 脱敏 ──────────────────────────────────────────────────────────────
def redact_secrets(text, secrets):
    """把凭据值从任意文本里抹掉。

    错误消息最终会落进三个地方: 投递状态文件 (notification_state.json)、
    runner 的 stdout (→ journald)、以及能力表/调试日志。任何一处出现
    真实 Token / API Key 都是凭据泄露, 所以统一在这里做替换。

    规则:
      * 只抹"看起来像凭据"的长串 (≥ 4 字符), 短串替换会误伤正常文本;
      * 替换后统一写成 ``***``, 不保留长度/前缀等可用于还原的信息;
      * ``text`` 为空时原样返回, 调用方不必判空。
    """
    if not text:
        return ''
    out = str(text)
    for secret in (secrets or ()):
        value = str(secret or '').strip()
        if len(value) < 4:
            continue
        out = out.replace(value, '***')
    return out


# ── 配置 ──────────────────────────────────────────────────────────────
class NotifyConfig(Config):
    """Config + notification-specific environment variables.

    Secrets are read from the environment only — they never reach the
    repository, the data directory, or an API response.
    """

    def __init__(self, env=None):
        super().__init__(env)
        env = os.environ if env is None else env

        # 总开关: 默认关闭。即使服务器配好了凭据, 也要每个用户单独开启。
        self.notify_enabled = _flag(env.get('Y8Y_NOTIFY_ENABLED', '0'))

        # LINE Messaging API
        self.line_token = env.get('Y8Y_NOTIFY_LINE_TOKEN', '').strip()
        self.line_targets = _parse_targets(env.get('Y8Y_NOTIFY_LINE_TARGETS', ''))

        # Email (Resend)
        self.email_api_key = env.get('Y8Y_NOTIFY_EMAIL_API_KEY', '').strip()
        self.email_from = env.get('Y8Y_NOTIFY_EMAIL_FROM', '').strip()
        self.email_targets = _parse_targets(env.get('Y8Y_NOTIFY_EMAIL_TARGETS', ''))

        # 补发窗口: 任务截止后多久之内仍可补发 (调度器停机后的兜底)
        self.catchup_hours = max(0, _int(env.get('Y8Y_NOTIFY_CATCHUP_HOURS', '24'), 24))
        # HTTP 超时 (秒)
        self.timeout = max(1.0, _float(env.get('Y8Y_NOTIFY_TIMEOUT', '15'), 15.0))
        # 状态文件保留天数 (超过则清理历史投递记录)
        self.retention_days = max(1, _int(env.get('Y8Y_NOTIFY_RETENTION_DAYS', '30'), 30))

        # 身份 → 邮箱 (邮件渠道的默认收件地址)
        self.user_emails = {uid: email for email, uid in self.users.items()}

    def notification_summary(self):
        channels = []
        if self.line_token:
            channels.append('line')
        if self.email_api_key and self.email_from:
            channels.append('email')
        return (f'notify={"on" if self.notify_enabled else "off"} '
                f'channels={channels} catchup={self.catchup_hours}h')

    def secrets(self):
        """本配置里的所有敏感值 —— 供日志/状态文件脱敏使用。

        只含真正的凭据 (LINE token / Resend API key)。收件地址、用户 id
        之类不是凭据, 保持原样便于排查问题。
        """
        return [self.line_token, self.email_api_key]


def _int(raw, default):
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _float(raw, default):
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ── 用户设置 ──────────────────────────────────────────────────────────
def default_settings():
    """通知默认关闭 —— 没有任何配置时 Todo 的行为与今天完全一致。"""
    return {
        'enabled': False,
        'channels': [],
        'lead_minutes': 0,
        'daily_digest': False,
        'digest_time': DEFAULT_DIGEST_TIME,
    }


def normalize_settings(raw, known_channels=()):
    """校验并归一化一份设置 → (settings, error)。

    未知渠道 → 报错 (而不是静默忽略): 用户以为配好了, 实际没生效是最糟的
    失败模式。空的 known_channels 表示「不校验」(离线使用)。
    """
    if not isinstance(raw, dict):
        return None, 'settings must be a JSON object'
    out = default_settings()

    if 'enabled' in raw:
        out['enabled'] = bool(raw['enabled'])

    if 'channels' in raw:
        channels = raw['channels']
        if channels is None:
            channels = []
        if not isinstance(channels, list):
            return None, 'channels must be a list'
        clean = []
        for item in channels:
            if not isinstance(item, str):
                return None, 'channel names must be strings'
            name = item.strip()
            if not name:
                continue
            if known_channels and name not in known_channels:
                return None, f'unknown notification channel: {name}'
            if name not in clean:
                clean.append(name)
        out['channels'] = clean

    if 'lead_minutes' in raw:
        value = raw['lead_minutes']
        if isinstance(value, bool):
            return None, 'lead_minutes must be an integer number of minutes'
        if isinstance(value, str):
            value = _int(value, -1)
        if not isinstance(value, int) or value < 0 or value > MAX_LEAD_MINUTES:
            return None, (f'lead_minutes must be an integer between 0 and {MAX_LEAD_MINUTES}')
        out['lead_minutes'] = value

    if 'daily_digest' in raw:
        out['daily_digest'] = bool(raw['daily_digest'])

    if 'digest_time' in raw:
        value = str(raw['digest_time'] or '').strip()
        if not _TIME_RE.match(value):
            return None, 'digest_time must be HH:MM (24h)'
        out['digest_time'] = value

    return out, None


class SettingsStore:
    """每用户的通知偏好 (存在数据目录里, 不含任何凭据)。"""

    def __init__(self, path):
        self.path = path

    def _read(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def all(self):
        """原始数据 (user → settings dict)。"""
        return self._read()

    def get(self, user):
        """某用户的设置 (缺失时返回默认值, 不写盘)。"""
        raw = self._read().get(user)
        if not isinstance(raw, dict):
            return default_settings()
        settings, err = normalize_settings(raw)
        return settings if not err else default_settings()

    def enabled_users(self):
        """开启了通知的用户 id 列表。"""
        out = []
        for user, raw in self._read().items():
            if not isinstance(raw, dict):
                continue
            if raw.get('enabled'):
                out.append(user)
        return sorted(out)

    def set(self, user, settings):
        """写入某用户的设置 (整体覆盖该用户的条目)。"""
        data = self._read()
        data[user] = settings
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        return settings


# ── 投递状态 ──────────────────────────────────────────────────────────
class StateStore:
    """投递状态: 防止重复发送 + 支撑安全重试。

    ``reminders`` 的键是 *occurrence key*:

        user | owner | folder/basename | created | dueTdue_time

    键里带 due/due_time 是刻意的 —— 任务改了截止时间就等于换了一次
    "提醒事件", 旧事件的状态自然失效, 新事件重新计一次, 既不会漏提醒
    也不会因为改了时间而被旧的 "已发送" 记录吞掉。

    ``digests`` 的键是: user | date | channel (每天每渠道最多一次)。
    """

    def __init__(self, path):
        self.path = path
        self.data = self._read()

    @property
    def directory(self):
        return os.path.dirname(str(self.path))

    def ensure_writable(self):
        """确认状态文件所在目录可写 —— 写不了就**不能**发通知。

        投递状态是"不重复发送"的唯一依据: 记录不下来的话, 每一轮
        都会把同样的提醒再发一遍。所以这个检查放在**发出任何通知之前**
        —— 探不过就整体停下来 (fail closed)。

        抛 NotificationStateError = 不可写; 返回 True = 可写。
        """
        directory = self.directory
        if probe_writable(directory):
            return True
        detail = ''
        if not os.path.isdir(directory):
            detail = '目录不存在'
        else:
            try:
                info = os.stat(directory)
                detail = f'属主 uid={info.st_uid}, 权限 {info.st_mode & 0o777:04o}'
            except OSError as exc:
                detail = exc.strerror or str(exc)
        raise NotificationStateError(directory, detail)

    def _read(self):
        empty = {'version': STATE_VERSION, 'reminders': {}, 'digests': {}}
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return empty
        if not isinstance(data, dict):
            return empty
        data.setdefault('version', STATE_VERSION)
        if not isinstance(data.get('reminders'), dict):
            data['reminders'] = {}
        if not isinstance(data.get('digests'), dict):
            data['digests'] = {}
        return data

    def save(self):
        self.data['version'] = STATE_VERSION
        self.data['updated_at'] = datetime.now(_timezone.utc).isoformat()
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True))

    # ── reminders ──
    def reminder(self, key):
        entry = self.data['reminders'].get(key)
        return entry if isinstance(entry, dict) else None

    def channel_state(self, key, channel):
        entry = self.reminder(key)
        if not entry:
            return None
        state = entry.get('channels')
        if not isinstance(state, dict):
            return None
        ch = state.get(channel)
        return ch if isinstance(ch, dict) else None

    def set_channel_state(self, key, channel, payload, meta=None):
        entry = self.data['reminders'].setdefault(key, {'channels': {}})
        if meta:
            entry.update(meta)
        entry.setdefault('channels', {})[channel] = payload
        entry['updated_at'] = datetime.now(_timezone.utc).isoformat()
        return entry

    # ── digests ──
    def digest_state(self, key):
        state = self.data['digests'].get(key)
        return state if isinstance(state, dict) else None

    def set_digest_state(self, key, payload):
        self.data['digests'][key] = payload
        return payload

    # ── 清理 ──
    def prune(self, live_keys, retention_days, now=None):
        """丢弃过期记录。

        ``live_keys`` 是当前仍然存在的提醒事件键 —— 它们永远保留, 即使用户
        临时把提醒改到未来。其它记录保留 ``retention_days`` 天后清理
        (任务被删/被改期后, 旧记录不会无限堆积, 也不会让任务"复活"时
        重复提醒)。
        """
        now = now or datetime.now(_timezone.utc)
        cutoff = now - timedelta(days=retention_days)
        removed = 0

        for key in list(self.data['reminders']):
            if key in live_keys:
                continue
            entry = self.data['reminders'].get(key)
            stamp = _parse_iso(entry.get('updated_at')) if isinstance(entry, dict) else None
            if stamp is None or stamp < cutoff:
                del self.data['reminders'][key]
                removed += 1

        for key in list(self.data['digests']):
            entry = self.data['digests'].get(key)
            stamp = _parse_iso(entry.get('at')) if isinstance(entry, dict) else None
            if stamp is None or stamp < cutoff:
                del self.data['digests'][key]
                removed += 1

        return removed


def _parse_iso(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_timezone.utc)
    return parsed


# ── 渠道 (Provider) ───────────────────────────────────────────────────
class ProviderError(Exception):
    """投递失败。

    ``retryable=False`` 表示重试也没用 (凭据无效、请求格式错), 调度器会
    直接放弃而不是反复打接口。
    """

    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


class Message:
    """一条待投递的通知 (纯文本, 不含 HTML)。"""

    def __init__(self, kind, subject, body, link='', task_id=''):
        self.kind = kind          # 'reminder' | 'digest' | 'test'
        self.subject = subject
        self.body = body
        self.link = link
        self.task_id = task_id


def post_json(url, payload, headers, timeout):
    """标准库 HTTP POST (JSON)。失败一律抛 ProviderError。

    错误消息里**绝不**出现 Authorization header 的内容: 通用异常只记
    异常类型名, 不记原始文本 —— 例如 header 值里含换行或非 Latin-1
    字符时 urllib 会抛 ``ValueError: Invalid header value b'...'``,
    直接把 str(e) 拼进消息就等于把 token 写进了日志。
    """
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    try:
        req = urllib.request.Request(url, data=data, method='POST')
        req.add_header('Content-Type', 'application/json; charset=utf-8')
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
            return resp.status
    except urllib.error.HTTPError as e:
        try:
            e.read()                              # 读完即弃: 响应体不进日志
        except Exception:                                    # noqa: BLE001
            pass
        retryable = e.code in (408, 425, 429) or 500 <= e.code < 600
        raise ProviderError(f'HTTP {e.code} from {url}', retryable=retryable) from None
    except urllib.error.URLError as e:
        raise ProviderError(f'network error: {e.reason}', retryable=True) from None
    except Exception as e:                                   # noqa: BLE001
        raise ProviderError(f'unexpected error: {type(e).__name__}',
                            retryable=True) from None


class Provider:
    """通知渠道基类。

    新增渠道只要继承本类并注册 (register_provider), 调度器无需改动。
    """

    name = ''
    label = ''

    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport or post_json

    def configured(self):
        """服务端凭据是否齐备。"""
        return False

    def target_for(self, user):
        """该用户的投递目标 (地址 / LINE user id)。"""
        return ''

    def available_for(self, user):
        """该用户此刻能不能收 (凭据齐备 + 有目标)。"""
        return bool(self.configured() and self.target_for(user))

    def _safe(self, text):
        """错误文本脱敏 —— 凭据值一律换成 ``***``。"""
        getter = getattr(self.config, 'secrets', None)
        values = getter() if callable(getter) else ()
        return redact_secrets(text, values)

    def post(self, url, payload, headers):
        """调用 HTTP 接口, 把任何异常翻译成 ProviderError。

        重试策略在这里统一决定: 4xx (除 408/425/429) 是"重试也没用",
        5xx 与网络错误要重试。

        所有出口的错误消息都过一遍脱敏 —— 凭据值不会进入状态文件、
        runner 输出或 journald。
        """
        try:
            return self.transport(url, payload, headers, self.config.timeout)
        except ProviderError as e:
            raise ProviderError(self._safe(str(e)),
                                retryable=bool(getattr(e, 'retryable', True))) from None
        except urllib.error.HTTPError as e:
            retryable = e.code in (408, 425, 429) or 500 <= e.code < 600
            raise ProviderError(self._safe(f'HTTP {e.code} from {url}'),
                                retryable=retryable) from None
        except urllib.error.URLError as e:
            raise ProviderError(self._safe(f'network error: {e.reason}'),
                                retryable=True) from None
        except Exception as e:                                   # noqa: BLE001
            raise ProviderError(f'unexpected error: {type(e).__name__}',
                                retryable=True) from None

    def send(self, user, message):
        """投递一条消息。失败抛 ProviderError。"""
        raise NotImplementedError


class LineProvider(Provider):
    """LINE Messaging API (push message)。"""

    name = 'line'
    label = 'LINE'
    API = 'https://api.line.me/v2/bot/message/push'

    def configured(self):
        return bool(self.config.line_token)

    def target_for(self, user):
        return self.config.line_targets.get(user, '')

    def send(self, user, message):
        target = self.target_for(user)
        if not target:
            raise ProviderError(f'no LINE target configured for user {user!r}', retryable=False)
        chunks = chunk_text(message.body, LINE_CHUNK_SIZE, LINE_MAX_CHUNKS)
        payload = {
            'to': target,
            'messages': [{'type': 'text', 'text': chunk} for chunk in chunks],
        }
        self.post(self.API, payload,
                  {'Authorization': f'Bearer {self.config.line_token}'})


class EmailProvider(Provider):
    """Email (Resend API)。

    收件地址默认取自 ``Y8Y_USERS`` 的邮箱映射, 也可用
    ``Y8Y_NOTIFY_EMAIL_TARGETS`` 单独指定。
    """

    name = 'email'
    label = '邮件'
    API = 'https://api.resend.com/emails'

    def configured(self):
        return bool(self.config.email_api_key and self.config.email_from)

    def target_for(self, user):
        return (self.config.email_targets.get(user)
                or self.config.user_emails.get(user, ''))

    def send(self, user, message):
        target = self.target_for(user)
        if not target:
            raise ProviderError(f'no email target configured for user {user!r}', retryable=False)
        payload = {
            'from': self.config.email_from,
            'to': [target],
            'subject': message.subject,
            'text': message.body,
        }
        self.post(self.API, payload,
                  {'Authorization': f'Bearer {self.config.email_api_key}'})


_PROVIDER_FACTORIES = {}


def register_provider(name, factory):
    """注册一个渠道。factory(config) → Provider 实例。

    扩展点: 第三方渠道 (Slack / Discord / ntfy …) 只需实现 Provider 并
    注册, 调度器与设置 API 无需改动。
    """
    _PROVIDER_FACTORIES[name] = factory


register_provider(LineProvider.name, LineProvider)
register_provider(EmailProvider.name, EmailProvider)


def build_providers(config, transport=None):
    """实例化所有已注册渠道 (未配置的渠道也在列表里, 只是不可用)。"""
    providers = {}
    for name, factory in _PROVIDER_FACTORIES.items():
        try:
            providers[name] = factory(config, transport)
        except TypeError:
            providers[name] = factory(config)
    return providers


def registered_channels():
    """已注册的渠道名 (排序)。"""
    return sorted(_PROVIDER_FACTORIES)


# ── 渠道能力表 ────────────────────────────────────────────────────────
class Capabilities:
    """「谁能用哪个渠道」的能力表。

    **凭据永远不进 Web 进程**: LINE token / Resend key 只存在于调度器
    (notify_runner) 的环境里。调度器每次运行把一份**不含任何凭据**的
    能力表写进数据目录, Web 端只读它来渲染设置界面。

    这样即使 Web 应用出漏洞, 攻击者也拿不到第三方渠道的密钥。
    """

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def enabled(self):
        """子系统总开关 (调度器发布; 文件不存在 = 未开启)。"""
        return bool(self.load().get('enabled'))

    def channels(self):
        raw = self.load().get('channels')
        out = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, dict) and item.get('name'):
                out.append(item)
        return out

    def channels_for(self, user):
        """某用户能看到的渠道列表 (available = 该用户有投递目标)。"""
        out = []
        for item in self.channels():
            users = item.get('users')
            users = users if isinstance(users, list) else []
            out.append({
                'name': item.get('name'),
                'label': item.get('label') or item.get('name'),
                'available': user in users,
            })
        return out


def publish_capabilities(path, config, providers, users=()):
    """发布能力表 (由调度器调用)。**只写渠道名与可投递的用户, 不含凭据。**"""
    known = set(users) | set(config.users.values()) | {config.default_owner}
    channels = []
    for name, provider in sorted(providers.items()):
        targets = sorted(u for u in known if provider.target_for(u))
        if targets:
            channels.append({'name': name, 'label': provider.label, 'users': targets})
    payload = {
        'version': CAPABILITIES_VERSION,
        'enabled': bool(config.notify_enabled),
        'updated_at': _now_iso(),
        'channels': channels,
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def chunk_text(text, size, max_chunks):
    """把长文本切成最多 max_chunks 块, 每块不超过 size 字符。"""
    text = text or ''
    if len(text) <= size:
        return [text]
    chunks = []
    remaining = text
    while remaining and len(chunks) < max_chunks:
        if len(remaining) <= size:
            chunks.append(remaining)
            remaining = ''
            break
        cut = remaining.rfind('\n', 0, size)
        if cut <= 0:
            cut = size
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip('\n')
    if remaining and len(chunks) == max_chunks:
        chunks[-1] = chunks[-1][:size - 1] + '…'
    return chunks


# ── 任务视图 ──────────────────────────────────────────────────────────
class TaskView:
    """调度器看到的任务 (只读快照)。"""

    __slots__ = ('id', 'folder', 'basename', 'title', 'owner', 'due', 'due_date',
                 'due_time', 'created', 'project', 'recurring')

    def __init__(self, id, folder, basename, title, owner, due, due_date,
                 due_time, created, project, recurring):
        self.id = id
        self.folder = folder
        self.basename = basename
        self.title = title
        self.owner = owner
        self.due = due
        self.due_date = due_date
        self.due_time = due_time
        self.created = created
        self.project = project
        self.recurring = recurring

    @property
    def has_time(self):
        return bool(self.due_time)

    def occurrence_key(self, user):
        """提醒事件键 —— 见 StateStore 的说明。"""
        return '|'.join([
            user,
            self.owner or '',
            f'{self.folder}/{self.basename}',
            self.created or '',
            f'{self.due}T{self.due_time}',
        ])


def scan_tasks(data_dir, default_owner):
    """扫描 todo/ 与 recurring/ 下的活动任务 (只读)。"""
    tasks = []
    for folder in ('todo', 'recurring'):
        folder_path = os.path.join(str(data_dir), folder)
        for rel_path, fname in scan_md_files(folder_path):
            fpath = os.path.join(folder_path, rel_path)
            try:
                with open(fpath, encoding='utf-8') as f:
                    raw = f.read()
            except OSError:
                continue
            try:
                fm, _body = parse_frontmatter(raw)
            except Exception:                                # noqa: BLE001
                continue
            if fm.get('status', 'todo') != 'todo':
                continue
            due = str(fm.get('due', '') or '').strip()
            due_date = None
            if due:
                try:
                    due_date = datetime.strptime(due[:10], '%Y-%m-%d').date()
                except ValueError:
                    due_date = None
            due_time = str(fm.get('due_time', '') or '').strip()
            if not _TIME_RE.match(due_time):
                due_time = ''
            recurring = fm.get('recurring', '')
            tasks.append(TaskView(
                id=f'{folder}/{rel_path}',
                folder=folder,
                basename=fname,
                title=display_title(fm, fname),
                owner=task_owner(fm, default_owner),
                due=due,
                due_date=due_date,
                due_time=due_time,
                created=str(fm.get('created', '') or '').strip(),
                project=str(fm.get('project', '') or '').strip(),
                recurring=bool(recurring),
            ))
    return tasks


# ── 调度器 ────────────────────────────────────────────────────────────
class Scheduler:
    """计算并投递到期通知。

    一次 run() 做三件事:

    1. 扫描任务 → 找出每个用户"此刻该收到什么";
    2. 按渠道投递 (成功/失败都写状态, 失败按退避重试);
    3. 清理过期状态。

    ``dry_run=True`` 时只计算不投递、不写盘 —— 部署前先看清楚会发什么。
    """

    def __init__(self, data_dir, config, providers=None, settings_store=None,
                 state_store=None, now=None, tz=None):
        self.data_dir = str(data_dir)
        self.config = config
        self.providers = providers if providers is not None else build_providers(config)
        # 三个通知 JSON 都在专用状态目录里 —— 见 notify_state_dir()。
        state_dir = notify_state_dir(self.data_dir)
        self.state_dir = state_dir
        self.settings_store = settings_store or SettingsStore(
            os.path.join(state_dir, SETTINGS_FILE))
        self.state_store = state_store or StateStore(
            os.path.join(state_dir, STATE_FILE))
        self.tz = tz or _resolve_tz(config.timezone)
        self.now = now
        self.log = []

    # ── 主流程 ──
    def run(self, dry_run=False):
        now = self.now or datetime.now(self.tz)
        report = {
            'now': now.isoformat(),
            'dry_run': bool(dry_run),
            'sent': [],
            'failed': [],
            'skipped': [],
            'planned': [],
        }

        if not self.config.notify_enabled:
            report['skipped'].append({'reason': 'notifications disabled (Y8Y_NOTIFY_ENABLED=0)'})
            self.log.append('notifications disabled globally — nothing to do')
            return report

        users = self.settings_store.enabled_users()
        if not users:
            report['skipped'].append({'reason': 'no user has enabled notifications'})
            return report

        if not dry_run:
            # 投递状态是"不重复发送"的唯一依据: 写不进去 = 每轮都会重发。
            # 所以先探一次状态目录可写性, 探不过就**一条都不发**
            # (fail closed) —— 而不是发完才发现记不下来。
            self.state_store.ensure_writable()

        tasks = scan_tasks(self.data_dir, self.config.default_owner)
        live_keys = set()

        for user in users:
            settings = self.settings_store.get(user)
            channels = [c for c in settings['channels'] if c in self.providers]
            if not channels:
                report['skipped'].append({'user': user, 'reason': 'no channel selected'})
                continue
            usable = [c for c in channels if self.providers[c].available_for(user)]
            if not usable:
                report['skipped'].append({'user': user, 'reason': 'no channel configured on the server'})
                continue

            reminders, digest_today, digest_overdue = self._collect(user, settings, tasks)
            live_keys.update(key for key, _task, _msg in reminders)

            for key, task, message in reminders:
                self._deliver(user, key, task, message, usable, report, dry_run)

            self._maybe_digest(user, settings, digest_today, digest_overdue, usable,
                               report, dry_run)

        if not dry_run:
            self.state_store.prune(live_keys, self.config.retention_days)
            self.state_store.save()

        return report

    # ── 收集待发内容 ──
    def _collect(self, user, settings, tasks):
        """→ (reminders, digest_today, digest_overdue)"""
        now = self.now or datetime.now(self.tz)
        today = now.date()
        lead = timedelta(minutes=int(settings.get('lead_minutes', 0)))
        catchup = timedelta(hours=self.config.catchup_hours)

        reminders = []
        digest_today = []
        digest_overdue = []

        for task in tasks:
            # 可见性: 与 Web 端完全一致的规则 —— 自己的 + 共享的。
            # 别人的私人任务永远不会出现在任何人的通知里。
            if not can_access(task.owner, user):
                continue
            if task.due_date is None:
                continue

            if task.has_time:
                due_at = datetime.combine(
                    task.due_date,
                    datetime.strptime(task.due_time, '%H:%M').time(),
                    tzinfo=self.tz)
                remind_at = due_at - lead
                if now < remind_at:
                    continue                      # 还没到提醒时间
                if now > due_at + catchup:
                    continue                      # 过期太久了, 不补发
                message = self._reminder_message(user, task, due_at, lead)
                reminders.append((task.occurrence_key(user), task, message))
            else:
                # 全天任务 → 走每日摘要
                if task.due_date == today:
                    digest_today.append(task)
                elif task.due_date < today:
                    digest_overdue.append(task)

        return reminders, digest_today, digest_overdue

    def _reminder_message(self, user, task, due_at, lead):
        lines = [f'【y8y Todo 提醒】{task.title}']
        minutes = int(lead.total_seconds() // 60)
        when = f'{task.due} {task.due_time}' if task.has_time else task.due
        if minutes > 0:
            lines.append(f'截止：{when}（提前 {minutes} 分钟提醒）')
        else:
            lines.append(f'截止：{when}')
        if task.project:
            lines.append(f'项目：{task.project}')
        lines.append(self._link_line())
        return Message('reminder', f'提醒：{task.title}', '\n'.join(lines),
                       link=self._link(), task_id=task.id)

    def _digest_message(self, user, today_items, overdue_items):
        today = (self.now or datetime.now(self.tz)).date().isoformat()
        lines = [f'【y8y Todo 每日摘要】{today}']
        if today_items:
            lines.append(f'今日全天任务（{len(today_items)}）')
            for task in today_items:
                lines.append(f'· {task.title}' + (f'（{task.project}）' if task.project else ''))
        if overdue_items:
            lines.append(f'逾期任务（{len(overdue_items)}）')
            for task in overdue_items:
                when = f'{task.due} {task.due_time}' if task.has_time else task.due
                lines.append(f'· {task.title}（截止 {when}）')
        lines.append(self._link_line())
        subject = f'{today} 每日摘要（{len(today_items)} 全天 / {len(overdue_items)} 逾期）'
        return Message('digest', subject, '\n'.join(lines), link=self._link())

    def _link(self):
        return self.config.public_url or ''

    def _link_line(self):
        link = self._link()
        return f'打开：{link}/todo/' if link else '打开 Todo 应用查看详情'

    def _safe_error(self, exc, limit=200):
        """错误文本落盘/打印前的最后一道脱敏。

        ProviderError 的消息已经过 Provider._safe 脱敏, 这里再兜一次底:
        万一某个自定义 provider 直接把原始异常拼进消息, 也不会把凭据
        写进 notification_state.json 或 journald。
        """
        getter = getattr(self.config, 'secrets', None)
        values = getter() if callable(getter) else ()
        return redact_secrets(str(exc), values)[:limit]

    # ── 投递 ──
    def _deliver(self, user, key, task, message, channels, report, dry_run):
        meta = {
            'user': user,
            'task_id': task.id,
            'title': task.title,
            'due': task.due,
            'due_time': task.due_time,
            'owner': task.owner,
        }
        for channel in channels:
            state = self.state_store.channel_state(key, channel)
            if state and state.get('status') in ('sent', 'exhausted'):
                continue
            if state and state.get('next_attempt_at'):
                nxt = _parse_iso(state.get('next_attempt_at'))
                if nxt and (self.now or datetime.now(self.tz)) < nxt:
                    continue                       # 退避中, 下一轮再试
            if dry_run:
                report['planned'].append({'user': user, 'channel': channel,
                                          'task': task.title, 'kind': message.kind})
                continue
            try:
                self.providers[channel].send(user, message)
            except ProviderError as e:
                attempts = int((state or {}).get('attempts', 0)) + 1
                retryable = bool(getattr(e, 'retryable', True))
                if retryable and attempts < MAX_ATTEMPTS:
                    status = 'failed'
                    delay = min(RETRY_BASE_MINUTES * (2 ** (attempts - 1)), RETRY_CAP_MINUTES)
                    next_at = (self.now or datetime.now(self.tz)) + timedelta(minutes=delay)
                    payload = {'status': status, 'at': _now_iso(self.now),
                               'attempts': attempts, 'error': self._safe_error(e, 200),
                               'next_attempt_at': next_at.isoformat()}
                else:
                    status = 'exhausted'
                    payload = {'status': status, 'at': _now_iso(self.now),
                               'attempts': attempts, 'error': self._safe_error(e, 200)}
                self.state_store.set_channel_state(key, channel, payload, meta)
                report['failed'].append({'user': user, 'channel': channel,
                                         'task': task.title, 'error': self._safe_error(e, 200),
                                         'status': status})
                self.log.append(f'send failed user={user} channel={channel} '
                                f'task={task.title!r} status={status} '
                                f'error={self._safe_error(e, 120)}')
            else:
                attempts = int((state or {}).get('attempts', 0)) + 1
                payload = {'status': 'sent', 'at': _now_iso(self.now), 'attempts': attempts}
                self.state_store.set_channel_state(key, channel, payload, meta)
                report['sent'].append({'user': user, 'channel': channel,
                                       'task': task.title, 'kind': message.kind})
                self.log.append(f'sent user={user} channel={channel} kind={message.kind}')

    def _maybe_digest(self, user, settings, today_items, overdue_items, channels,
                      report, dry_run):
        if not settings.get('daily_digest'):
            return
        if not today_items and not overdue_items:
            return                              # 没有内容就不打扰
        now = self.now or datetime.now(self.tz)
        hh, mm = settings.get('digest_time', DEFAULT_DIGEST_TIME).split(':')
        digest_at = datetime.combine(now.date(), datetime.strptime(f'{hh}:{mm}', '%H:%M').time(),
                                      tzinfo=self.tz)
        if now < digest_at:
            return                              # 还没到摘要时间
        message = self._digest_message(user, today_items, overdue_items)
        day = now.date().isoformat()
        for channel in channels:
            key = f'{user}|{day}|{channel}'
            state = self.state_store.digest_state(key)
            if state and state.get('status') in ('sent', 'exhausted'):
                continue
            if dry_run:
                report['planned'].append({'user': user, 'channel': channel,
                                          'kind': 'digest', 'day': day})
                continue
            try:
                self.providers[channel].send(user, message)
            except ProviderError as e:
                attempts = int((state or {}).get('attempts', 0)) + 1
                retryable = bool(getattr(e, 'retryable', True))
                if retryable and attempts < MAX_ATTEMPTS:
                    status = 'failed'
                    delay = min(RETRY_BASE_MINUTES * (2 ** (attempts - 1)), RETRY_CAP_MINUTES)
                    next_at = now + timedelta(minutes=delay)
                    payload = {'status': status, 'at': _now_iso(self.now), 'attempts': attempts,
                               'error': self._safe_error(e, 200), 'next_attempt_at': next_at.isoformat()}
                else:
                    status = 'exhausted'
                    payload = {'status': status, 'at': _now_iso(self.now),
                               'attempts': attempts, 'error': self._safe_error(e, 200)}
                self.state_store.set_digest_state(key, payload)
                report['failed'].append({'user': user, 'channel': channel, 'kind': 'digest',
                                         'error': self._safe_error(e, 200), 'status': status})
            else:
                self.state_store.set_digest_state(key, {'status': 'sent',
                                                        'at': _now_iso(self.now),
                                                        'attempts': 1})
                report['sent'].append({'user': user, 'channel': channel, 'kind': 'digest'})
                self.log.append(f'sent digest user={user} channel={channel} day={day}')


def _now_iso(now=None):
    if now is None:
        return datetime.now(_timezone.utc).isoformat()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_timezone.utc)
    return now.astimezone(_timezone.utc).isoformat()


def _resolve_tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                                        # noqa: BLE001
        return _timezone.utc
