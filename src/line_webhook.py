#!/usr/bin/env python3
"""LINE Messaging API Webhook —— 独立常驻服务 (系统用户 ``y8y-notify``)。

这个进程是**唯一**同时持有 Channel Secret 与 Channel Access Token 的地方:

* Channel Secret 用来验证 ``X-Line-Signature`` (对**原始请求体**做
  HMAC-SHA256 + base64, 用 ``hmac.compare_digest`` 常量时间比较)。
* Channel Access Token 复用 ``notifications.LineProvider`` 发测试推送 ——
  不重复实现 LINE API 客户端。

它**不是 Todo API 的一部分**, 也**不是** Web 进程的另一个入口:

* 只监听回环地址 (默认 127.0.0.1:8087), 由反向代理把**唯一**的公开路径
  ``/line/webhook`` 转过来; 其它路径一律 404。
* 没有任何调用 Web 内部接口的 HTTP 客户端。Web ↔ 本进程只通过
  ``<data>/notify/line-link/`` 下的单写者文件通信 (见 line_link.py)。
* 请求体上限 64 KiB、每请求最多 20 个事件、只接受 1:1 聊天、事件 ID
  去重、按 LINE User ID 限失败次数。日志**不记录消息正文、LINE ID、头**。

绑定关系只能由「Web 进程为已认证用户创建的一次性绑定码」+「签名验证过的
Webhook 事件里的 source.userId」共同产生 —— 客户端既不能指定 LINE User
ID, 也不能绕过 Todo 用户认证去改别人的绑定。

Usage:
    python3 src/line_webhook.py                       # systemd (常驻)
    python3 src/line_webhook.py --data-dir /tmp/x    # 本地调试

Exit codes:
    0  正常退出 (SIGTERM/SIGINT)
    2  配置缺失 (未设置 Channel Secret)
"""
import argparse
import base64
import collections
import hashlib
import hmac
import http.server
import json
import os
import signal
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from notifications import (                                          # noqa: E402
    LineProvider,
    Message,
    NotifyConfig,
    redact_secrets,
)
from procguard import harden as harden_process                       # noqa: E402
import line_link                                                      # noqa: E402

WEBHOOK_PATH = '/line/webhook'
SIGNATURE_HEADER = 'X-Line-Signature'
MAX_BODY = 64 * 1024
MAX_EVENTS_PER_REQUEST = 20
#: 同一个 LINE 账号 10 分钟内最多 8 次无效绑定码尝试。
MAX_FAILED_ATTEMPTS = 8
FAILED_ATTEMPT_WINDOW = 600
#: 固定测试消息 —— 客户端不能指定正文。
TEST_SUBJECT = 'y8y Todo LINE 测试通知'
TEST_BODY = ('【y8y Todo】LINE 绑定测试通知。\n'
             '收到这条消息，说明这个 Todo 账号与 LINE 的连接正常。')

_CODE_PREFIXES = ('y8y-link:', 'y8y-link ', 'y8y link:', 'y8y link ',
                  '绑定码:', '绑定码 ')


def verify_signature(secret, body, provided):
    """LINE Webhook 签名验证: base64(HMAC-SHA256(secret, raw_body))。"""
    if not secret or not provided or not isinstance(provided, str):
        return False
    try:
        expected = base64.b64encode(
            hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()
        ).decode('ascii')
    except (AttributeError, TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, provided.strip())


def extract_code(text):
    """从用户发的文本里取绑定码。**不记录原文**, 不匹配就返回空串。"""
    candidate = (text or '').strip()
    lowered = candidate.lower()
    for prefix in _CODE_PREFIXES:
        if lowered.startswith(prefix):
            candidate = candidate[len(prefix):].strip()
            break
    return candidate if line_link.CODE_RE.fullmatch(candidate) else ''


def event_key(event):
    """重放去重用键。优先 webhookEventId, 没有就用事件内容的稳定哈希。"""
    raw = event.get('webhookEventId')
    if isinstance(raw, str) and 0 < len(raw) <= 128:
        return raw
    try:
        blob = json.dumps(event, sort_keys=True, ensure_ascii=False).encode('utf-8')
    except (TypeError, ValueError):
        blob = repr(event).encode('utf-8', 'replace')
    return 'hash:' + hashlib.sha256(blob).hexdigest()


class WebhookApp:
    """与 HTTP 层解耦的业务逻辑, 便于离线单元测试。"""

    def __init__(self, config, transport=None, store=None):
        self.config = config
        self.transport = transport
        self.store = store or line_link.LinkStore(config.data_dir)
        self.stop = threading.Event()
        self._failed = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()
        self.errors = 0
        self.invalid_signatures = 0

    # -- 能力表 / 生命周期 ---------------------------------------------
    def start(self):
        # 注意: PR_SET_DUMPABLE 只在真正独立的常驻进程 (main) 里做。
        # 单元测试在 pytest 进程内直接构造本类, 不该把测试进程也加固。
        self.store.ensure_trusted()
        problem = self.store.verify_trusted_layout()
        if problem:
            # fail closed: 绝不在一个 Web 可写 / 属主不对的目录里写权威绑定。
            raise line_link.TrustedLayoutError(problem)
        self.store.publish_capabilities()
        thread = threading.Thread(target=self._background, name='line-link-consumer',
                                  daemon=True)
        thread.start()
        return thread

    def _background(self):
        last_publish = time.time()
        last_gc = time.time()
        while not self.stop.wait(0.4):
            try:
                # 先处理解绑请求: 之后排队的测试推送立即看到墓碑。
                self.consume_requests()
                self.consume_tests()
                now = time.time()
                if now - last_publish >= line_link.CAPABILITY_REFRESH_SECONDS:
                    self.store.publish_capabilities(now=now)
                    last_publish = now
                if now - last_gc >= 600:
                    self.store.gc(now=now)
                    last_gc = now
            except Exception:                       # noqa: BLE001
                # 后台线程绝不能因为一个坏文件而死掉; 也不把异常内容写日志。
                self.errors += 1

    # -- 暴力猜测防护 ---------------------------------------------------
    def _allow_attempt(self, line_id, now=None):
        now = time.time() if now is None else now
        with self._lock:
            queue = self._failed[line_id]
            while queue and now - queue[0] > FAILED_ATTEMPT_WINDOW:
                queue.popleft()
            return len(queue) < MAX_FAILED_ATTEMPTS

    def note_failed_attempt(self, line_id, now=None):
        now = time.time() if now is None else now
        with self._lock:
            queue = self._failed[line_id]
            while queue and now - queue[0] > FAILED_ATTEMPT_WINDOW:
                queue.popleft()
            queue.append(now)
            # 防止有人用大量随机 LINE 账号把内存撑爆
            if len(self._failed) > 5000:
                for key in list(self._failed)[:1000]:
                    self._failed.pop(key, None)

    # -- Webhook 事件 ---------------------------------------------------
    def handle_event(self, event):
        """处理一个 LINE 事件。返回固定状态串 (不含任何消息内容)。"""
        if not isinstance(event, dict):
            return 'ignored'
        key = event_key(event)
        if self.store.seen_event(key):
            return 'duplicate'
        source = event.get('source')
        source = source if isinstance(source, dict) else {}
        if source.get('type') != 'user':
            # 群聊 / 房间: 绑定码可能泄露给群里其他人, 一律不接受。
            self.store.mark_event(key)
            return 'not_one_to_one'
        line_id = source.get('userId')
        if not line_link.valid_line_id(line_id):
            self.store.mark_event(key)
            return 'invalid_source'
        message = event.get('message')
        message = message if isinstance(message, dict) else {}
        if message.get('type') != 'text' or (event.get('type') != 'message'):
            self.store.mark_event(key)
            return 'ignored'
        code = extract_code(message.get('text'))
        if not code:
            self.store.mark_event(key)
            return 'ignored'
        if not self._allow_attempt(line_id):
            self.store.mark_event(key)
            return 'rate_limited'
        outcome = self._redeem(code, line_id)
        if outcome in ('invalid_code', 'expired'):
            self.note_failed_attempt(line_id)
        self.store.mark_event(key)
        return outcome

    def _redeem(self, code, line_id):
        """在跨进程锁内一次性完成「认领 → 校验 → 写绑定 → 写结果」。"""
        store = self.store
        now = time.time()
        store.ensure_trusted()
        with store.lock():
            found = store.read_code(code)
            if found is None:
                return 'invalid_code'
            code_hash = found['hash']
            record = found['record']
            user = record.get('user')
            if not line_link.valid_user(user):
                store.delete_code(code_hash)
                return 'invalid_code'
            try:
                expires_at = float(record.get('expires_at', 0))
            except (TypeError, ValueError):
                expires_at = 0
            if expires_at <= now:
                store.delete_code(code_hash)
                store.set_result(code_hash, False, 'expired', user=user, now=now)
                return 'expired'
            bindings = store.bindings()
            for other, bound in bindings.items():
                if other != user and bound.get('line_user_id') == line_id:
                    store.delete_code(code_hash)
                    store.set_result(code_hash, False, 'line_account_in_use',
                                     user=user, now=now)
                    return 'line_account_in_use'
            if user in bindings and not record.get('replace'):
                store.delete_code(code_hash)
                store.set_result(code_hash, False, 'already_bound',
                                 user=user, now=now)
                return 'already_bound'
            store.set_binding(user, line_id, now=now)
            store.delete_code(code_hash)
            store.cancel_codes(user)
            store.set_result(code_hash, True, '', user=user, now=now)
            return 'bound'

    # -- 解绑请求队列 ---------------------------------------------------
    def consume_requests(self):
        """通知侧**唯一**真正执行解绑的地方: 删绑定 + 写墓碑 + 取消待发测试。

        Web 侧只往 inbox 写 ``unlink-*`` 请求, 从不碰权威目录。所有动作在
        同一把跨进程锁里完成, 与 ``consume_tests`` 的认领互斥。

        inbox 是 Web 可写的: 文件名与内容都必须校验, 否则 A 用户可以塞一个
        内容为 ``user=bob`` 的请求, 借通知侧之手把 B 用户解绑。只有
        ``unlink-<sha256(user)>.json`` 且内容自洽才算有效请求。
        """
        for name, _listed in self.store.pending_unlink_requests():
            with self.store.lock():
                request = self.store.read_unlink_request(name)
                if request is None:
                    # 已被别的线程消费, 或文件损坏 —— 后者要删掉, 免得永远留着。
                    self.store.drop_unlink_request(name)
                    continue
                user = request.get('user')
                expected = (f'unlink-{line_link.digest(user)}.json'
                            if line_link.valid_user(user) else '')
                if not expected or name != expected:
                    self.store.drop_unlink_request(name)
                    continue
                self.store.apply_unlink(user)
        return [(n, d) for n, d in self.store.pending_unlink_requests() if d is not None]

    # -- 测试推送队列 ---------------------------------------------------
    def consume_tests(self):
        """消费 Web 排入的测试请求。复用 LineProvider, 不碰去重状态文件。

        竞态处理: 「复核绑定 + 认领请求」与解绑的 ``apply_unlink`` 用同一把
        跨进程 flock 串行化。认领后即使紧接着解绑, ``LineProvider.send``
        也会重新解析收件人 —— 墓碑让它 fail closed, 不会发往旧地址。
        """
        now = time.time()
        for rid, _listed in self.store.pending_test_requests():
            if not line_link.REQUEST_ID_RE.fullmatch(rid):
                self.store.drop_test_request(rid)
                continue
            with self.store.lock():
                request = self.store.read_test_request(rid)
                if request is None:
                    continue                       # 已被解绑取消
                user = request.get('user')
                try:
                    requested_at = float(request.get('requested_at', 0))
                except (TypeError, ValueError):
                    requested_at = 0
                if now - requested_at > line_link.TEST_REQUEST_TTL_SECONDS:
                    self.store.set_test_result(rid, False, 'request_expired', now=now)
                    continue
                if (not line_link.valid_user(user)
                        or self.store.binding(user) is None
                        or self.store.unlinked(user) is not None):
                    self.store.set_test_result(rid, False, 'not_bound', now=now)
                    continue
                self.store.claim_test_request(rid, request)
            # 锁外调用 LINE API, 避免长时间占用跨进程锁。
            try:
                provider = LineProvider(self.config, self.transport)
                provider.send(user, Message('test', TEST_SUBJECT, TEST_BODY))
            except Exception as exc:                # noqa: BLE001
                detail = redact_secrets(str(exc), self.config.secrets())[:120]
                self.store.set_test_result(rid, False, 'send_failed:' + detail,
                                           now=now)
            else:
                self.store.set_test_result(rid, True, '', now=now)
        return self.store.pending_test_requests()


class WebhookHandler(http.server.BaseHTTPRequestHandler):
    """只暴露 ``POST /line/webhook``; 其它方法/路径一律拒绝。"""

    server_version = 'y8y-line-webhook'
    sys_version = ''

    def log_message(self, fmt, *args):
        # 绝不记录消息正文、身份头、LINE ID 或签名值。
        return

    def _reply(self, status, body=b''):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _reject(self):
        self._reply(404)

    def do_GET(self):
        self._reject()

    def do_HEAD(self):
        self._reject()

    def do_PUT(self):
        self._reject()

    def do_DELETE(self):
        self._reject()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip('/') != WEBHOOK_PATH:
            self._reject()
            return
        if (self.headers.get('Transfer-Encoding') or '').lower() == 'chunked':
            self._reply(400)
            return
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except (TypeError, ValueError):
            self._reply(400)
            return
        if length <= 0:
            self._reply(400)
            return
        if length > MAX_BODY:
            self._reply(413)
            return
        app = self.server.app                                  # type: ignore[attr-defined]
        signatures = self.headers.get_all(SIGNATURE_HEADER) or []
        if len(signatures) != 1:
            # 缺签名 / 重复签名也算一次被拒的签名尝试 (便于运维观察刷接口)。
            app.invalid_signatures += 1
            self._reply(401)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._reply(400)
            return
        if not verify_signature(app.config.line_channel_secret, body, signatures[0]):
            app.invalid_signatures += 1
            self._reply(401)
            return
        try:
            payload = json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            self._reply(400)
            return
        if not isinstance(payload, dict):
            self._reply(400)
            return
        events = payload.get('events')
        if not isinstance(events, list) or len(events) > MAX_EVENTS_PER_REQUEST:
            self._reply(400)
            return
        for event in events:
            try:
                app.handle_event(event)
            except Exception:                                  # noqa: BLE001
                app.errors += 1
        # LINE 以 2xx 判定投递成功; 单个事件失败也不重试 (绑定码是一次性的)。
        self._reply(200)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='line_webhook', description='y8y Todo LINE 自助绑定 Webhook')
    parser.add_argument('--data-dir', default='', help='覆盖 Y8Y_DATA_DIR')
    parser.add_argument('--bind', default='', help='覆盖监听地址 (默认 127.0.0.1)')
    parser.add_argument('--port', type=int, default=0, help='覆盖监听端口 (默认 8087)')
    return parser.parse_args(argv)


def build_app(config, transport=None):
    app = WebhookApp(config, transport=transport)
    app.start()
    return app


def make_server(app, bind, port):
    httpd = http.server.ThreadingHTTPServer((bind, port), WebhookHandler)
    httpd.daemon_threads = True
    httpd.app = app                                            # type: ignore[attr-defined]
    return httpd


def main(argv=None):
    # 常驻进程一启动就不可 dump: 同 UID 的其它进程读不到 environ 里的
    # Channel Secret / Access Token (见 src/procguard.py、deployment.md §5.2)。
    harden_process()
    args = parse_args(argv if argv is not None else sys.argv[1:])
    config = NotifyConfig()
    if args.data_dir:
        config.data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    if not config.line_channel_secret:
        print('[line-webhook] 未配置 Channel Secret —— 不启动 (签名无法验证)',
              file=sys.stderr)
        return 2
    bind = (args.bind or config.line_webhook_bind or '127.0.0.1').strip()
    port = args.port or config.line_webhook_port
    try:
        app = build_app(config)
    except line_link.TrustedLayoutError as exc:
        # fail closed: 权威绑定目录归错人 / 组可写时绝不启动。
        print(f'[line-webhook] 绑定状态目录权限自检失败，拒绝启动: {exc}',
              file=sys.stderr)
        print('  请按 docs/line-self-link.md §4 重建 3770 / 2750 / 2770 三级目录,',
              file=sys.stderr)
        print('  确认 <notify>/ 父目录属主是 y8y-notify (sticky 不限制目录所有者),',
              file=sys.stderr)
        print('  并用 check-notify-isolation.py --line-link-layout 复核后重启。',
              file=sys.stderr)
        return 2
    httpd = make_server(app, bind, port)

    def shutdown(*_unused):
        app.stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f'[line-webhook] listening on {bind}:{port}{WEBHOOK_PATH} '
          f'data_dir={config.data_dir}', flush=True)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        app.stop.set()
        httpd.server_close()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:                                   # noqa: BLE001
        secrets_values = [os.environ.get('Y8Y_NOTIFY_LINE_TOKEN', ''),
                          os.environ.get('Y8Y_NOTIFY_EMAIL_API_KEY', ''),
                          os.environ.get('Y8Y_NOTIFY_LINE_CHANNEL_SECRET', '')]
        print(f'[line-webhook] fatal: {redact_secrets(str(exc), secrets_values)}',
              file=sys.stderr)
        sys.exit(1)
