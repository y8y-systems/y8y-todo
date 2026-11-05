#!/usr/bin/env python3
"""LINE 自助绑定 —— **无凭据**的共享状态层 (V1).

被两个进程导入:

* ``serve.py``         (Web, 系统用户 ``y8y``): 生成一次性绑定码、查询状态、
  解绑、把「发送测试通知」排进队列。
* ``line_webhook.py``  (Webhook, 系统用户 ``y8y-notify``): 校验 LINE 签名后
  兑换绑定码、写入绑定关系、消费测试推送队列。

本模块**不读取任何长期凭据**: 不碰 Channel Access Token, 也不碰 Channel
Secret。它连 ``Y8Y_NOTIFY_*`` 变量名都不出现在代码里 —— 这是刻意的,
``tests/test_line_link.py`` 会钉死这一点。

两个进程通过 ``<data_dir>/notify/line-link/`` 下的**单写者小文件**交换
状态, 没有跨账号的 read-modify-write:

===========================  =========  ===============================
文件名                       写者       内容 / 用途
===========================  =========  ===============================
``code-<hash>.json``         Web        一次性绑定码 (只存 sha256, 不存原文)
``result-<hash>.json``       Webhook    绑定结果 (成功 / 固定错误码)
``binding-<hash>.json``      Webhook    Todo 用户 ↔ LINE User ID
``test-<id>.req.json``       Web        测试推送请求
``test-<id>.res.json``       Webhook    测试推送结果
``event-<hash>.json``        Webhook    已处理的 webhookEventId (重放去重)
``test-rate.json``           Web        测试推送的每用户频率计数
===========================  =========  ===============================

文件名里的 ``<hash>`` 一律是 ``sha256`` 十六进制串, 用户输入永远不会
直接进文件名 (路径穿越 / 覆盖都不可能)。需要「读 + 判断 + 写」原子性的
操作统一在 ``LinkStore.lock()`` (同目录 flock 文件) 里完成 —— flock 对
只读 fd 也生效, 所以 0644 的锁文件两个账号都能打开。

所有文件都在数据目录里, **不在源码静态目录**, 因此不会被 Web 的静态
路由发布。备份只要备份 ``<data_dir>`` (见 README 备份章节) 就一并带上。
"""
import contextlib
import hashlib
import json
import os
import re
import secrets
import threading
import time

try:
    import fcntl                     # POSIX 文件锁 —— 跨进程互斥
except ImportError:                  # pragma: no cover - 非 POSIX 平台
    fcntl = None

from taskstore import atomic_write

# 与 notifications.NOTIFY_DIR_NAME 保持一致。本模块刻意不 import
# notifications (那边会 import 本模块), 由测试钉死两个常量相等。
NOTIFY_DIR_NAME = 'notify'
STATE_DIR_NAME = 'line-link'
CAPABILITIES_FILE = 'line-link-capabilities.json'
CAPABILITIES_VERSION = 1

#: 绑定码: 24 字节安全随机数 → 32 个 URL-safe 字符 (192 bit), 10 分钟有效。
CODE_TTL_SECONDS = 600
#: 每个用户最多同时挂 3 个待用码; 全实例最多 200 个 (防刷盘)。
MAX_PENDING_PER_USER = 3
MAX_PENDING_TOTAL = 200
#: 测试通知: 同用户 60 秒内最多 1 条, 1 小时最多 5 条。
TEST_MIN_INTERVAL_SECONDS = 60
TEST_MAX_PER_HOUR = 5
TEST_REQUEST_TTL_SECONDS = 300
#: Webhook 事件去重记录保留 24 小时, 最多 2000 条。
EVENT_TTL_SECONDS = 24 * 3600
MAX_EVENT_FILES = 2000
#: 能力表由常驻 Webhook 服务每 60 秒重写一次; 超过 5 分钟没更新视为离线。
CAPABILITY_REFRESH_SECONDS = 60
CAPABILITY_MAX_AGE_SECONDS = 300
#: Web 端新 API 的请求体上限。
WEB_BODY_LIMIT = 4096
#: 各类状态文件的 GC 期限 (秒)。
STATE_FILE_TTL_SECONDS = 24 * 3600

USER_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
LINE_ID_RE = re.compile(r'^U[0-9a-f]{32}$')
CODE_RE = re.compile(r'^[A-Za-z0-9_-]{20,64}$')
REQUEST_ID_RE = re.compile(r'^[0-9a-f]{32}$')
BOT_ID_RE = re.compile(r'^@?[A-Za-z0-9._-]{1,64}$')

#: 固定错误文案 —— 绝不包含用户可控内容或 LINE ID。
ERROR_MESSAGES = {
    'invalid_code': '绑定码无效，请重新生成后再发送。',
    'expired': '绑定码已过期，请重新生成。',
    'already_used': '绑定码已经被使用，请重新生成。',
    'already_bound': '这个 Todo 账号已经绑定了 LINE，请先解除绑定。',
    'line_account_in_use': '这个 LINE 账号已经绑定了其他 Todo 用户，请先让对方解除绑定。',
    'not_one_to_one': '绑定只能在 LINE 一对一聊天中完成，群聊里的消息不会被处理。',
    'too_many_codes': '绑定码创建太频繁，请稍后再试。',
    'not_bound': '请先绑定 LINE 账号。',
    'rate_limited': '测试通知发送太频繁，请稍后再试。',
    'send_failed': '测试通知发送失败，请检查 LINE 凭据与网络。',
    'request_expired': '请求已超时，请重试。',
    'forbidden': '无权查看这次测试。',
    'unknown_request': '请求不存在或已过期。',
    'not_configured': '服务器尚未启用 LINE 自助绑定，请联系管理员。',
}

_LOCAL_LOCK = threading.RLock()


# ── 小工具 ────────────────────────────────────────────────────────────
def digest(value):
    """sha256 十六进制 —— 用于文件名与绑定码索引 (原文不落盘)。"""
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def new_code():
    return secrets.token_urlsafe(24)


def new_request_id():
    return secrets.token_hex(16)


def valid_user(value):
    return isinstance(value, str) and bool(USER_RE.fullmatch(value))


def valid_line_id(value):
    return isinstance(value, str) and bool(LINE_ID_RE.fullmatch(value))


def mask_line_id(value):
    """给 UI 看的脱敏 LINE User ID —— 不暴露完整收件人标识。"""
    if not isinstance(value, str) or len(value) < 10:
        return ''
    return f'{value[:5]}…{value[-4:]}'


def deep_link(bot_id):
    """LINE 官方 Bot 的加好友 / 打开聊天 Deep Link (纯公开信息)。

    ``bot_id`` 是 Bot Basic ID, **不是凭据**。空值或含非法字符时返回空串,
    UI 退化为「复制绑定码 + 手动打开 LINE」。
    """
    raw = (bot_id or '').strip()
    if not BOT_ID_RE.fullmatch(raw):
        return ''
    return 'https://line.me/R/ti/p/' + (raw if raw.startswith('@') else '@' + raw)


# ── 状态存储 ──────────────────────────────────────────────────────────
class LinkStore:
    """``<data_dir>/notify/line-link/`` 的读写封装。

    构造**不创建任何目录**: 通知调度器 (只读绑定关系) 与 Web 的能力表
    查询都不该顺手造目录。要写的时候显式调 ``ensure()``。
    """

    def __init__(self, data_dir):
        self.data_dir = str(data_dir or '')
        self.notify_dir = os.path.join(self.data_dir, NOTIFY_DIR_NAME)
        self.state_dir = os.path.join(self.notify_dir, STATE_DIR_NAME)
        self.caps_path = os.path.join(self.notify_dir, CAPABILITIES_FILE)

    # -- 目录 / 锁 ------------------------------------------------------
    def ensure(self):
        """确保共享目录存在且 setgid + 组可写 (两个服务账号都要能建/删文件)。"""
        os.makedirs(self.state_dir, mode=0o2770, exist_ok=True)
        try:
            mode = os.stat(self.state_dir).st_mode
            needed = 0o2070                       # setgid + group rwx
            if (mode & needed) != needed:
                os.chmod(self.state_dir, (mode & 0o7777) | needed)
        except OSError:
            # 目录归另一个服务账号所有时改不了权限 —— 部署脚本已经建好它了,
            # 这里只是开发/单机环境的兜底, 失败不影响读写。
            pass
        return self.state_dir

    @contextlib.contextmanager
    def lock(self):
        """跨进程互斥。flock 对只读 fd 生效, 0644 锁文件两个账号都能打开。"""
        self.ensure()
        path = os.path.join(self.state_dir, 'lock')
        fd = os.open(path, os.O_CREAT | os.O_RDONLY, 0o666)
        try:
            with _LOCAL_LOCK:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # -- 原子文件操作 ---------------------------------------------------
    def _path(self, name):
        return os.path.join(self.state_dir, name)

    @staticmethod
    def _read(path):
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _write(path, obj):
        atomic_write(path, json.dumps(obj, ensure_ascii=False, sort_keys=True))

    @staticmethod
    def _write_once(path, obj):
        """原子「仅创建一次」——先 O_EXCL 写临时文件, 再 link 成最终名字。

        与 ``atomic_write`` 的 rename 不同, 这里**绝不覆盖**已存在的路径:
        一次性绑定码不能被第二个进程悄悄换掉。link 不可用时退化为
        O_EXCL 直写 (仍是 create-only, 只是不再整体原子)。
        """
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        tmp = f'{path}.{os.getpid()}.{secrets.token_hex(6)}.part'
        with open(tmp, 'x', encoding='utf-8') as f:
            f.write(text)
        try:
            try:
                os.link(tmp, path)
            except OSError:
                with open(path, 'x', encoding='utf-8') as f:
                    f.write(text)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    @staticmethod
    def _unlink(path):
        try:
            os.unlink(path)
        except OSError:
            pass

    def _entries(self, prefix, suffix='.json'):
        try:
            names = os.listdir(self.state_dir)
        except OSError:
            return []
        out = []
        for name in names:
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            data = self._read(self._path(name))
            if data is not None:
                out.append((name, data))
        return out

    # -- 能力表 (Webhook 服务在线标记, 不含任何凭据) -------------------
    def publish_capabilities(self, now=None):
        now = time.time() if now is None else now
        self.ensure()
        self._write(self.caps_path, {
            'version': CAPABILITIES_VERSION,
            'enabled': True,
            'updated_at': now,
        })

    def capabilities_enabled(self, now=None, max_age=CAPABILITY_MAX_AGE_SECONDS):
        """服务是否在线。文件超时 = 离线 (fail closed, 不把陈旧文件当在线)。"""
        data = self._read(self.caps_path)
        if not data or not data.get('enabled'):
            return False
        try:
            updated = float(data.get('updated_at', 0))
        except (TypeError, ValueError):
            return False
        now = time.time() if now is None else now
        return 0 <= (now - updated) <= max_age

    # -- 绑定码 ---------------------------------------------------------
    def _all_codes(self):
        out = []
        for name, data in self._entries('code-'):
            try:
                expires = float(data.get('expires_at', 0))
            except (TypeError, ValueError):
                expires = 0
            out.append((name, data, expires))
        out.sort(key=lambda item: item[2], reverse=True)
        return out

    def pending_codes(self, user=None, now=None, include_expired=False):
        now = time.time() if now is None else now
        out = []
        for name, data, expires in self._all_codes():
            if user is not None and data.get('user') != user:
                continue
            if expires <= now and not include_expired:
                continue
            out.append((name, data, expires))
        return out

    def create_code(self, user, now=None, replace=False, bot_id=''):
        """Web 侧: 为已认证用户创建一个一次性绑定码。

        返回 ``(payload, error_code)``。原始绑定码**只返回给浏览器**,
        服务端只保存它的 sha256 (文件名), 因此磁盘泄露也换不来绑定。
        """
        if not valid_user(user):
            return None, 'invalid_code'
        now = time.time() if now is None else now
        self.ensure()
        with self.lock():
            mine = self.pending_codes(user=user, now=now)
            if len(mine) >= MAX_PENDING_PER_USER:
                return None, 'too_many_codes'
            if len(self.pending_codes(now=now)) >= MAX_PENDING_TOTAL:
                return None, 'too_many_codes'
            code = new_code()
            h = digest(code)
            expires_at = now + CODE_TTL_SECONDS
            self._write_once(self._path(f'code-{h}.json'), {
                'version': 1,
                'user': user,
                'created_at': now,
                'expires_at': expires_at,
                'replace': bool(replace),
            })
            return ({
                'state': 'pending',
                'code': code,
                'hash': h,
                'expires_at': expires_at,
                'bot_id': (bot_id or '').strip(),
                'deep_link': deep_link(bot_id),
            }, '')

    def read_code(self, code):
        """按原文查绑定码记录 (调用方应持有 lock)。"""
        if not isinstance(code, str) or not CODE_RE.fullmatch(code):
            return None
        h = digest(code)
        data = self._read(self._path(f'code-{h}.json'))
        if data is None:
            return None
        return {'hash': h, 'record': data}

    def delete_code(self, code_hash):
        self._unlink(self._path(f'code-{code_hash}.json'))

    def cancel_codes(self, user):
        """解绑 / 绑定成功时, 作废该用户所有待用码 (调用方持有 lock)。"""
        for name, data in self._entries('code-'):
            if data.get('user') == user:
                self._unlink(self._path(name))

    # -- 绑定结果 -------------------------------------------------------
    def set_result(self, code_hash, ok, error='', user='', now=None):
        now = time.time() if now is None else now
        self._write(self._path(f'result-{code_hash}.json'), {
            'version': 1, 'ok': bool(ok), 'error': error or '',
            'user': user or '', 'at': now,
        })

    def latest_result(self, user, now=None, max_age=STATE_FILE_TTL_SECONDS):
        now = time.time() if now is None else now
        found = None
        for _name, data in self._entries('result-'):
            if data.get('user') != user:
                continue
            try:
                at = float(data.get('at', 0))
            except (TypeError, ValueError):
                at = 0
            if now - at > max_age:
                continue
            if found is None or at > found[0]:
                found = (at, data)
        return found[1] if found else None

    # -- 绑定关系 -------------------------------------------------------
    def binding(self, user):
        """只返回当前用户的绑定; 格式不合法一律视为未绑定。"""
        if not valid_user(user):
            return None
        data = self._read(self._path(f'binding-{digest(user)}.json'))
        if not data or data.get('user') != user:
            return None
        if not valid_line_id(data.get('line_user_id')):
            return None
        return data

    def bindings(self):
        out = {}
        for _name, data in self._entries('binding-'):
            user = data.get('user')
            if valid_user(user) and valid_line_id(data.get('line_user_id')):
                out[user] = data
        return out

    def set_binding(self, user, line_user_id, now=None):
        if not valid_user(user) or not valid_line_id(line_user_id):
            raise ValueError('invalid binding')
        now = time.time() if now is None else now
        self._write(self._path(f'binding-{digest(user)}.json'), {
            'version': 1,
            'user': user,
            'line_user_id': line_user_id,
            'bound_at': now,
            'source': 'self',
        })

    def unlink(self, user):
        """Web 侧解绑: 删绑定 + 作废所有待用码, 一次 lock 内完成。"""
        self.ensure()
        with self.lock():
            self._unlink(self._path(f'binding-{digest(user)}.json'))
            self.cancel_codes(user)

    # -- 测试通知队列 ---------------------------------------------------
    def queue_test(self, user, now=None):
        """Web 侧: 校验频率后把一次测试推送排进队列。"""
        if not valid_user(user):
            return None, 'forbidden'
        now = time.time() if now is None else now
        self.ensure()
        with self.lock():
            if self.binding(user) is None:
                return None, 'not_bound'
            state = self._read(self._path('test-rate.json')) or {}
            history = state.get('history') if isinstance(state.get('history'), dict) else {}
            stamps = [t for t in history.get(user, [])
                      if isinstance(t, (int, float)) and 0 <= now - t < 3600]
            if (stamps and now - stamps[-1] < TEST_MIN_INTERVAL_SECONDS) \
                    or len(stamps) >= TEST_MAX_PER_HOUR:
                return None, 'rate_limited'
            stamps.append(now)
            history[user] = stamps[-TEST_MAX_PER_HOUR:]
            self._write(self._path('test-rate.json'), {'version': 1, 'history': history})
            rid = new_request_id()
            self._write_once(self._path(f'test-{rid}.req.json'), {
                'version': 1, 'id': rid, 'user': user, 'requested_at': now,
            })
            return {'request_id': rid, 'state': 'pending'}, ''

    def pending_test_requests(self):
        return [(name[len('test-'):-len('.req.json')], data)
                for name, data in self._entries('test-', '.req.json')]

    def set_test_result(self, rid, ok, error='', now=None):
        now = time.time() if now is None else now
        self._write(self._path(f'test-{rid}.res.json'), {
            'version': 1, 'ok': bool(ok), 'error': error or '', 'at': now,
        })
        self._unlink(self._path(f'test-{rid}.req.json'))

    def drop_test_request(self, rid):
        self._unlink(self._path(f'test-{rid}.req.json'))

    def test_status(self, rid, user):
        if not isinstance(rid, str) or not REQUEST_ID_RE.fullmatch(rid):
            return {'state': 'error', 'error': ERROR_MESSAGES['unknown_request']}
        req = self._read(self._path(f'test-{rid}.req.json'))
        if req is not None and req.get('user') != user:
            return {'state': 'error', 'error': ERROR_MESSAGES['forbidden']}
        res = self._read(self._path(f'test-{rid}.res.json'))
        if res is not None:
            if res.get('ok'):
                return {'state': 'ok', 'completed_at': res.get('at')}
            err = str(res.get('error') or '')
            if err.startswith('send_failed:'):
                message = ERROR_MESSAGES['send_failed'] + '（' + err.split(':', 1)[1][:100] + '）'
            else:
                message = ERROR_MESSAGES.get(err, ERROR_MESSAGES['send_failed'])
            return {'state': 'error', 'error': message, 'completed_at': res.get('at')}
        if req is not None:
            return {'state': 'pending'}
        return {'state': 'error', 'error': ERROR_MESSAGES['unknown_request']}

    # -- Webhook 事件去重 -----------------------------------------------
    def seen_event(self, event_id):
        return self._read(self._path(f'event-{digest(event_id)}.json')) is not None

    def mark_event(self, event_id, now=None):
        now = time.time() if now is None else now
        try:
            self._write_once(self._path(f'event-{digest(event_id)}.json'),
                             {'version': 1, 'at': now})
        except OSError:
            pass

    # -- 状态查询 (Web 用) ---------------------------------------------
    def status(self, user, now=None, bot_id='', legacy=False,
               conflict=False, enabled=True):
        """当前用户的 LINE 绑定视图。**不返回原始绑定码, 不返回完整 LINE ID。**"""
        now = time.time() if now is None else now
        payload = {
            'available': bool(enabled),
            'state': 'unbound',
            'bot_id': (bot_id or '').strip(),
            'deep_link': deep_link(bot_id),
            'legacy': bool(legacy),
            'legacy_conflict': bool(conflict),
        }
        binding = self.binding(user)
        if binding:
            payload['state'] = 'bound'
            payload['line_user_masked'] = mask_line_id(binding.get('line_user_id'))
            payload['bound_at'] = binding.get('bound_at')
            return payload
        pending = self.pending_codes(user=user, now=now)
        if pending:
            name, _data, expires = pending[0]
            payload['state'] = 'pending'
            payload['hash'] = name[len('code-'):-len('.json')]
            payload['expires_at'] = expires
            return payload
        recent = self.pending_codes(user=user, now=now, include_expired=True)
        if recent:
            payload['state'] = 'expired'
            payload['error'] = ERROR_MESSAGES['expired']
            return payload
        result = self.latest_result(user, now=now)
        if result and not result.get('ok'):
            reason = result.get('error')
            payload['state'] = 'expired' if reason == 'expired' else 'error'
            payload['error'] = ERROR_MESSAGES.get(reason,
                                                 ERROR_MESSAGES['send_failed'])
            return payload
        return payload

    # -- GC (Webhook 服务周期性调用) ------------------------------------
    def gc(self, now=None):
        now = time.time() if now is None else now
        try:
            names = os.listdir(self.state_dir)
        except OSError:
            return
        events = []
        for name in names:
            path = self._path(name)
            if name.startswith('event-'):
                data = self._read(path)
                try:
                    at = float(data.get('at', 0)) if data else 0
                except (TypeError, ValueError):
                    at = 0
                if at and now - at > EVENT_TTL_SECONDS:
                    self._unlink(path)
                else:
                    events.append((at, name))
                continue
            if name.startswith(('code-', 'result-', 'test-')) and name.endswith('.json'):
                try:
                    age = now - os.path.getmtime(path)
                except OSError:
                    continue
                if age > STATE_FILE_TTL_SECONDS:
                    self._unlink(path)
        if len(events) > MAX_EVENT_FILES:
            events.sort()
            for _at, name in events[:len(events) - MAX_EVENT_FILES]:
                self._unlink(self._path(name))


# ── 给通知引擎用的轻量查询 (只读, 不建目录) -----------------------------
def bound_target(data_dir, user):
    """该用户自助绑定的 LINE User ID; 没有则空串。"""
    record = LinkStore(data_dir).binding(user)
    return record.get('line_user_id', '') if record else ''


def bound_users(data_dir):
    """所有已自助绑定的 Todo 用户 id (给能力表用, 不含 LINE ID)。"""
    return sorted(LinkStore(data_dir).bindings())
