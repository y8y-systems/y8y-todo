#!/usr/bin/env python3
"""LINE 自助绑定 —— **无凭据**的共享状态层 (V1).

被两个进程导入:

* ``serve.py``         (Web, 系统用户 ``y8y``): 生成一次性绑定码、查询状态、
  **提出**解绑请求、把「发送测试通知」排进队列。
* ``line_webhook.py``  (Webhook, 系统用户 ``y8y-notify``): 校验 LINE 签名后
  兑换绑定码、**独占写入**绑定关系、消费解绑请求与测试推送队列。

本模块**不读取任何长期凭据**: 不碰 Channel Access Token, 也不碰 Channel
Secret。代码里不出现任何凭据环境变量名 —— 这是刻意的,
``tests/test_line_link.py`` 会静态钉死这一点。(文档里提到旧配置名
``Y8Y_NOTIFY_LINE_TARGETS`` 只是说明优先级, 它不是凭据, 也没有被读取。)

## 两级目录 (P0-2: 权威绑定记录只能由通知侧写)

``<data>/notify/`` 是 3770 (setgid + **sticky**) 共享目录。sticky 位保证
Web 账号 (``y8y``) 只能删除/改名**自己拥有的**条目, 不能把通知侧拥有的
``line-link/`` 整个挪走再伪冒一个。

```
<data>/notify/
  line-link/                 2750  y8y-notify:y8y-notify   ← Web 只有 r-x
    binding-<hash>.json     webhook 写  最终生效的绑定
    unlinked-<hash>.json    webhook 写  解绑墓碑 (禁止回退旧地址)
    result-<hash>.json      webhook 写  绑定结果
    event-<hash>.json       webhook 写  事件去重
    capabilities.json       webhook 写  在线标记
  line-link-inbox/           2770  y8y-notify:y8y-notify   ← Web + webhook 都可写
    code-<hash>.json        Web 写   一次性绑定码 (只存 sha256)
    unlink-<hash>.json      Web 写   解绑**请求** (不是权威记录)
    test-<id>.req.json      Web 写   测试推送请求
    test-<id>.claim.json    webhook 写 已认领的请求 (防竞态)
    test-<id>.res.json      webhook 写 测试推送结果
    test-rate.json          Web 写   频率计数
    lock                    两者     flock 协调文件 (非权限边界)
```

目录权限是**唯一**保证「Web 不能删/换/伪造 binding 记录」的机制:
创建、改名、替换一个文件都需要**父目录的写权限**, 而 Web 对
``line-link/`` 没有写权限 (2750)。Web 仍能读 0644 的绑定文件来显示自己的
状态 —— 应用层的按用户隔离由 ``status(user)`` 保证。

此外通知侧启动时会调 ``verify_trusted_layout()`` 自检: **``notify/`` 本身
也必须归通知侧账号所有** —— Linux 的 sticky 位不限制「目录所有者」删除
别人的目录项, 若 ``notify/`` 归 Web 用户所有, 它照样能把 ``line-link/``
挪走再伪冒一个。自检还覆盖三级目录的权限位与符号链接, 不满足就
**fail closed** 拒绝启动, 防止运维漏配或有人预先抢占目录名。

## 解绑的**两段式**语义 (P0-1)

1. Web 点「解除绑定」→ 只在 inbox 写一个 ``unlink-*`` **请求**。从这一刻起
   ``LineProvider.target_for()`` 就直接返回空串 (fail closed): 哪怕 Webhook
   服务离线、权威绑定还在、旧 ``Y8Y_NOTIFY_LINE_TARGETS`` 还配着, 调度器
   也不会再向该用户发任何新的 LINE 通知。请求**没有 TTL**, Webhook 离线多久
   就挡多久。
2. Webhook 恢复 → 消费请求, 删绑定、写持久化 ``unlinked-*`` 墓碑、取消待发
   测试、删请求文件。墓碑保证此后**永不回退**旧地址; 只有「从未绑定」的
   用户才用旧环境变量 (升级兼容)。重新绑定成功时墓碑被清除。

也就是说「已提出解绑」「明确解绑」「从未绑定」是三种状态, 前两种都停发,
只有第三种能享受旧配置的升级兼容。GC 不删 ``unlink-*`` 请求, 避免离线
久了自动「复活」旧地址。

文件名里的 ``<hash>`` 一律是 ``sha256`` 十六进制串, 用户输入永远不会
直接进文件名 (路径穿越 / 覆盖都不可能)。需要「读 + 判断 + 写」原子性的
操作统一在 ``LinkStore.lock()`` (inbox 里的 flock 文件) 里完成 —— flock 对
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
import stat
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
#: 与 state_dir **平级**: Web 的 ensure() 不该顺手把权威目录建出来
#: (os.makedirs 建中间目录时用默认 0755, 那样 Web 就成了它的属主)。
INBOX_DIR_NAME = 'line-link-inbox'
CAPABILITIES_FILE = 'capabilities.json'
CAPABILITIES_VERSION = 1

#: 通知状态目录: setgid + sticky + group rwx (Web 不能挪走通知侧的目录)。
NOTIFY_DIR_MODE = 0o3770
#: 权威状态目录: setgid + owner rwx + group r-x (Web 只读, 不能建/删/换文件)。
TRUSTED_DIR_MODE = 0o2750
#: 请求队列目录: setgid + owner/group rwx (两个账号都能建/删自己的文件)。
INBOX_DIR_MODE = 0o2770

#: 前缀 → 权威目录。其余一律落在 inbox。
TRUSTED_PREFIXES = ('binding-', 'unlinked-', 'result-', 'event-', 'capabilities')

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
    'cancelled': '已解除 LINE 绑定，本次测试通知已取消。',
    'forbidden': '无权查看这次测试。',
    'unknown_request': '请求不存在或已过期。',
    'not_configured': '服务器尚未启用 LINE 自助绑定，请联系管理员。',
}

_LOCAL_LOCK = threading.RLock()


class TrustedLayoutError(RuntimeError):
    """权威状态目录不满足最小权限要求 —— 通知侧必须拒绝启动 (fail closed)。"""


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


def _touch_dir(path, mode):
    """创建目录并尽力把权限**精确**钉死 (跨账号时改不了就忽略)。

    不用「按位或补缺」: 那样一个 0755 的目录会变成 2755 (other 仍有 r-x),
    达不到 2750 的收紧效果。生产环境由 Operator 预先用 ``install -d`` 建好;
    这里是开发/单机以及通知侧自身启动时的兜底。
    """
    os.makedirs(path, mode=mode, exist_ok=True)
    try:
        current = os.stat(path).st_mode & 0o7777
        if current != mode:
            os.chmod(path, mode)
    except OSError:
        pass
    return path


# ── 状态存储 ──────────────────────────────────────────────────────────
class LinkStore:
    """``<data>/notify/line-link/`` (权威) + ``.../inbox/`` (请求队列)。

    构造**不创建任何目录**: 通知调度器与 Web 的状态查询都是只读的。
    """

    def __init__(self, data_dir):
        self.data_dir = str(data_dir or '')
        self.notify_dir = os.path.join(self.data_dir, NOTIFY_DIR_NAME)
        self.state_dir = os.path.join(self.notify_dir, STATE_DIR_NAME)
        self.inbox_dir = os.path.join(self.notify_dir, INBOX_DIR_NAME)
        self.caps_path = os.path.join(self.state_dir, CAPABILITIES_FILE)
        self.lock_path = os.path.join(self.inbox_dir, 'lock')

    # -- 目录 / 锁 ------------------------------------------------------
    @staticmethod
    def _dir_for(name):
        return 'trusted' if name.startswith(TRUSTED_PREFIXES) else 'inbox'

    def _path(self, name):
        base = self.state_dir if self._dir_for(name) == 'trusted' else self.inbox_dir
        return os.path.join(base, name)

    def ensure(self):
        """Web 侧可安全调用: 只保证 notify/ 与 inbox/ 存在, **绝不创建权威目录**。"""
        _touch_dir(self.notify_dir, NOTIFY_DIR_MODE)
        _touch_dir(self.inbox_dir, INBOX_DIR_MODE)
        return self.inbox_dir

    def ensure_trusted(self):
        """通知侧专用: 创建并加固权威目录 (2750), 顺便保证 inbox 存在。"""
        self.ensure()
        _touch_dir(self.state_dir, TRUSTED_DIR_MODE)
        return self.state_dir

    def verify_trusted_layout(self, uid=None):
        """通知侧启动自检 —— 返回空串表示 OK, 否则给出**脱敏**的失败原因。

        fail closed: ``notify/`` 与权威目录都必须由本进程自己拥有, 且
        group/other 不可写。Linux 的 sticky 位**不限制目录所有者** —— 若
        ``notify/`` 归 Web 用户所有, 它照样能 ``rmdir``/改名通知侧拥有的
        ``line-link/`` 再伪冒一个 (sticky 挡不住, 属主检查才能挡住)。
        三级目录都必须是真实目录, 不能是符号链接。
        """
        uid = os.geteuid() if uid is None else uid
        try:
            parent = os.lstat(self.notify_dir)
        except OSError:
            return 'notification state directory is missing'
        if stat.S_ISLNK(parent.st_mode):
            return 'notification state directory is a symlink'
        if not stat.S_ISDIR(parent.st_mode):
            return 'notification state directory is not a directory'
        # P0-2: sticky 只约束「非目录属主」, 目录属主可以删别人的条目。
        if parent.st_uid != uid:
            return 'notification state directory is owned by a different uid'
        if not (parent.st_mode & stat.S_ISVTX):
            return 'notification state directory lost its sticky bit'
        if parent.st_mode & 0o007:
            return 'notification state directory is accessible by others'
        if (parent.st_mode & 0o050) != 0o050:
            return 'notification state directory is not traversable by the shared group'
        if (parent.st_mode & 0o700) != 0o700:
            return 'notification state directory is not writable by its owner'
        try:
            info = os.lstat(self.state_dir)
        except OSError:
            return 'trusted state directory is missing'
        if stat.S_ISLNK(info.st_mode):
            return 'trusted state directory is a symlink'
        if not stat.S_ISDIR(info.st_mode):
            return 'trusted state directory is not a directory'
        if info.st_uid != uid:
            return 'trusted state directory is owned by a different uid'
        if info.st_mode & 0o022:
            return 'trusted state directory is group/other writable'
        if (info.st_mode & 0o050) != 0o050:
            return 'trusted state directory is not readable by the shared group'
        try:
            inbox = os.lstat(self.inbox_dir)
        except OSError:
            return 'request inbox is missing'
        if stat.S_ISLNK(inbox.st_mode):
            return 'request inbox is a symlink'
        if not stat.S_ISDIR(inbox.st_mode):
            return 'request inbox is not a directory'
        if inbox.st_uid != uid:
            return 'request inbox is owned by a different uid'
        if (inbox.st_mode & 0o070) != 0o070:
            return 'request inbox is not writable by the shared group'
        if inbox.st_mode & 0o002:
            return 'request inbox is writable by others'
        return ''

    @contextlib.contextmanager
    def lock(self):
        """跨进程互斥。flock 对只读 fd 生效, 锁文件放在两个账号都能建的 inbox。"""
        self.ensure()
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDONLY, 0o666)
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
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
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
        base = self.state_dir if self._dir_for(prefix) == 'trusted' else self.inbox_dir
        try:
            names = os.listdir(base)
        except OSError:
            return []
        out = []
        for name in names:
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            data = self._read(os.path.join(base, name))
            if data is not None:
                out.append((name, data))
        return out

    # -- 能力表 (Webhook 服务在线标记, 不含任何凭据) -------------------
    def publish_capabilities(self, now=None):
        now = time.time() if now is None else now
        self.ensure_trusted()
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

    # -- 绑定码 (inbox, Web 写) -----------------------------------------
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
        """作废该用户所有待用码 (调用方持有 lock)。"""
        for name, data in self._entries('code-'):
            if data.get('user') == user:
                self._unlink(self._path(name))

    # -- 绑定结果 (trusted, Webhook 写) ---------------------------------
    def set_result(self, code_hash, ok, error='', user='', now=None):
        now = time.time() if now is None else now
        self.ensure_trusted()
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

    # -- 绑定关系 (trusted, **只有通知侧可写**) -------------------------
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
        """通知侧专用: 写最终生效的绑定, 并清除该用户的解绑墓碑。"""
        if not valid_user(user) or not valid_line_id(line_user_id):
            raise ValueError('invalid binding')
        now = time.time() if now is None else now
        self.ensure_trusted()
        self._write(self._path(f'binding-{digest(user)}.json'), {
            'version': 1,
            'user': user,
            'line_user_id': line_user_id,
            'bound_at': now,
            'source': 'self',
        })
        self._unlink(self._path(f'unlinked-{digest(user)}.json'))

    # -- 解绑墓碑 (trusted, **只有通知侧可写**) -------------------------
    def unlinked(self, user):
        """该用户是否**明确解绑**过。墓碑存在 = 永不回退旧环境变量。"""
        if not valid_user(user):
            return None
        data = self._read(self._path(f'unlinked-{digest(user)}.json'))
        if not data or data.get('user') != user or not data.get('version'):
            return None
        return data

    def set_unlinked(self, user, now=None):
        """通知侧专用: 写墓碑。"""
        if not valid_user(user):
            raise ValueError('invalid user')
        now = time.time() if now is None else now
        self.ensure_trusted()
        self._write(self._path(f'unlinked-{digest(user)}.json'), {
            'version': 1, 'user': user, 'unlinked_at': now,
            'source': 'self',
        })

    def unlink_request(self, user):
        """**有效**的待处理解绑请求 (通知引擎据此立即停发), 否则 None。

        文件住在 Web 可写的 inbox, 所以内容不能被盲信: 必须 version 正确、
        ``user`` 与文件名里的哈希一致、且带一个数值型时间戳。伪造的请求
        最多只能让某个用户**停发** (fail closed), 不能把通知改投别处。
        请求**没有 TTL**: Webhook 离线多久就挡多久, 直到恢复后写成墓碑。
        """
        if not valid_user(user):
            return None
        data = self._read(self._path(f'unlink-{digest(user)}.json'))
        if not data or data.get('version') != 1 or data.get('user') != user:
            return None
        requested_at = data.get('requested_at')
        if not isinstance(requested_at, (int, float)):
            return None
        return data

    def has_unlink_request(self, user):
        return self.unlink_request(user) is not None

    def request_unlink(self, user, now=None):
        """Web 侧: 只**提出**解绑请求, 绝不碰权威绑定/墓碑文件。"""
        if not valid_user(user):
            return None
        now = time.time() if now is None else now
        self.ensure()
        with self.lock():
            self.cancel_codes(user)
            self._write(self._path(f'unlink-{digest(user)}.json'), {
                'version': 1, 'user': user, 'requested_at': now,
            })
        return {'state': 'unlinking'}

    def pending_unlink_requests(self):
        """列出所有 ``unlink-*`` 文件名; 内容损坏时 data 为 None (由消费方丢弃)。

        不能用 ``_entries()`` 的过滤版: 一个坏文件如果永远不被消费, 会一直
        留在 inbox 里。解绑请求不参与 TTL 清理 (见 ``gc``)。
        """
        try:
            names = [n for n in os.listdir(self.inbox_dir)
                     if n.startswith('unlink-') and n.endswith('.json')]
        except OSError:
            return []
        return [(name, self._read(os.path.join(self.inbox_dir, name)))
                for name in names]

    def read_unlink_request(self, name):
        return self._read(self._path(name))

    def drop_unlink_request(self, name):
        self._unlink(self._path(name))

    def apply_unlink(self, user, now=None):
        """通知侧专用: 在同一次 lock 内完成「删绑定 + 写墓碑 + 取消待发测试」。

        这是解绑唯一真正生效的地方; Web 侧调用 ``request_unlink`` 只会排队。
        """
        if not valid_user(user):
            return
        now = time.time() if now is None else now
        self.ensure_trusted()
        self._unlink(self._path(f'binding-{digest(user)}.json'))
        self.set_unlinked(user, now=now)
        self.cancel_codes(user)
        self.cancel_pending_tests(user, now=now)
        self._unlink(self._path(f'unlink-{digest(user)}.json'))

    # -- 测试通知队列 (inbox) -------------------------------------------
    def queue_test(self, user, now=None):
        """Web 侧: 校验频率后把一次测试推送排进队列。"""
        if not valid_user(user):
            return None, 'forbidden'
        now = time.time() if now is None else now
        self.ensure()
        with self.lock():
            # 「已提出解绑」与「明确解绑」都不能再排队测试推送 —— 通知引擎
            # 此刻已经停发, 排队只会产生一条注定失败的假请求。
            if (self.binding(user) is None or self.unlinked(user) is not None
                    or self.has_unlink_request(user)):
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

    def read_test_request(self, rid):
        return self._read(self._path(f'test-{rid}.req.json'))

    def claim_test_request(self, rid, data):
        """原子认领: 把 .req.json 换成 .claim.json, 防止重复/解绑后又被处理。"""
        req_path = self._path(f'test-{rid}.req.json')
        claim_path = self._path(f'test-{rid}.claim.json')
        self._write(claim_path, {'version': 1, 'user': data.get('user'),
                                 'requested_at': data.get('requested_at'),
                                 'claimed_at': time.time()})
        self._unlink(req_path)

    def set_test_result(self, rid, ok, error='', now=None):
        now = time.time() if now is None else now
        self._write(self._path(f'test-{rid}.res.json'), {
            'version': 1, 'ok': bool(ok), 'error': error or '', 'at': now,
        })
        self._unlink(self._path(f'test-{rid}.req.json'))
        self._unlink(self._path(f'test-{rid}.claim.json'))

    def drop_test_request(self, rid):
        self._unlink(self._path(f'test-{rid}.req.json'))
        self._unlink(self._path(f'test-{rid}.claim.json'))

    def cancel_pending_tests(self, user, now=None):
        """解绑时作废该用户所有尚未认领 / 已认领未回写的测试请求。"""
        now = time.time() if now is None else now
        for suffix in ('.req.json', '.claim.json'):
            for name, data in self._entries('test-', suffix):
                if data.get('user') != user:
                    continue
                rid = name[len('test-'):-len(suffix)]
                self._write(self._path(f'test-{rid}.res.json'), {
                    'version': 1, 'ok': False, 'error': 'cancelled', 'at': now,
                })
                self._unlink(self._path(name))

    def test_status(self, rid, user):
        if not isinstance(rid, str) or not REQUEST_ID_RE.fullmatch(rid):
            return {'state': 'error', 'error': ERROR_MESSAGES['unknown_request']}
        for suffix in ('.req.json', '.claim.json'):
            owner = self._read(self._path(f'test-{rid}{suffix}'))
            if owner is not None and owner.get('user') != user:
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
        if self._read(self._path(f'test-{rid}.req.json')) is not None or \
                self._read(self._path(f'test-{rid}.claim.json')) is not None:
            return {'state': 'pending'}
        return {'state': 'error', 'error': ERROR_MESSAGES['unknown_request']}

    # -- Webhook 事件去重 (trusted) -------------------------------------
    def seen_event(self, event_id):
        return self._read(self._path(f'event-{digest(event_id)}.json')) is not None

    def mark_event(self, event_id, now=None):
        now = time.time() if now is None else now
        self.ensure_trusted()
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
        # 解绑请求优先级最高: 用户点了「解除绑定」就立刻进入 unlinking,
        # 哪怕权威绑定记录还没被通知侧删除 (那要等下一轮消费)。
        if self.has_unlink_request(user):
            payload['state'] = 'unlinking'
            return payload
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
        # 「明确解绑」与「从未绑定」必须分开 —— 前者连旧配置也不能再用。
        if self.unlinked(user) is not None:
            payload['state'] = 'unlinked'
            payload['legacy_disabled'] = True
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
        events = []
        for base in (self.state_dir, self.inbox_dir):
            try:
                names = os.listdir(base)
            except OSError:
                continue
            for name in names:
                path = os.path.join(base, name)
                if name.startswith('event-'):
                    data = self._read(path)
                    try:
                        at = float(data.get('at', 0)) if data else 0
                    except (TypeError, ValueError):
                        at = 0
                    if at and now - at > EVENT_TTL_SECONDS:
                        self._unlink(path)
                    else:
                        events.append((at, path))
                    continue
                # unlink-* 绝不按 TTL 删除: Webhook 离线时它必须一直挡住旧
                # 地址, 直到恢复后被消费成永久墓碑。否则长时间停机会自动
                # 「复活」旧 LINE 目标。损坏的请求由 consume_requests() 丢弃。
                if name.startswith(('code-', 'result-', 'test-')) \
                        and name.endswith('.json'):
                    try:
                        age = now - os.path.getmtime(path)
                    except OSError:
                        continue
                    if age > STATE_FILE_TTL_SECONDS:
                        self._unlink(path)
        if len(events) > MAX_EVENT_FILES:
            events.sort()
            for _at, path in events[:len(events) - MAX_EVENT_FILES]:
                self._unlink(path)


# ── 给通知引擎用的轻量查询 (只读, 不建目录) -----------------------------
def bound_target(data_dir, user):
    """该用户自助绑定的 LINE User ID; 没有则空串。"""
    record = LinkStore(data_dir).binding(user)
    return record.get('line_user_id', '') if record else ''


def bound_users(data_dir):
    """所有已自助绑定的 Todo 用户 id (给能力表用, 不含 LINE ID)。"""
    return sorted(LinkStore(data_dir).bindings())


def explicitly_unlinked(data_dir, user):
    """是否**明确解绑**过 (墓碑)。通知引擎据此禁止回退旧地址。"""
    return LinkStore(data_dir).unlinked(user) is not None


def unlink_requested(data_dir, user):
    """是否**已提出但尚未完成**解绑。通知引擎据此立即停发 (Webhook 离线也生效)。"""
    return LinkStore(data_dir).unlink_request(user) is not None
