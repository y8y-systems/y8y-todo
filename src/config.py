"""Centralized configuration for the y8y todo server.

Every deployment-specific value is read from an environment variable, so the
same code runs on a developer laptop, a VPS, or a container without edits.
Defaults are chosen so that a fresh checkout "just works" in dev mode, while
the production defaults are conservative (loopback bind, no implicit trust).

Environment reference (see docs/deployment.md for details):

  Y8Y_DEV_MODE        1/true/yes → development mode (single local user,
                       forced loopback bind, data under ./dev-data)
  Y8Y_DATA_DIR        task data root (default: OS user data dir; dev mode:
                       ./dev-data next to the source tree)
  Y8Y_PORT            listen port (default 8086, kept for API compatibility)
  Y8Y_BIND            listen address (default 127.0.0.1 — put a reverse
                       proxy in front for public access)
  Y8Y_PUBLIC_URL      externally reachable base URL, e.g.
                      https://todo.example.com (informational since the
                      Google Calendar integration was removed)
  Y8Y_TIMEZONE        IANA timezone for due dates (default Asia/Tokyo)
  Y8Y_USERS           "alice@example.com=alice,bob@example.com=bob" —
                       authenticating-proxy e-mail → local user id mapping
  Y8Y_AUTH_HEADER     header carrying the authenticated e-mail (default
                       Cf-Access-Authenticated-User-Email; only trusted when
                       the request comes from Y8Y_TRUSTED_PROXIES)
  Y8Y_TRUSTED_PROXIES comma-separated IPs allowed to set the auth header
                       (default 127.0.0.1)
  Y8Y_DEV_USER        identity used in dev mode (default "dev")
  Y8Y_CN_HOLIDAYS     1/true/yes → 中国法定节假日/调休显示总开关
                       (default on; the old build hardcoded this to one
                       fixed username — now it is configuration)
  Y8Y_CN_HOLIDAY_USERS
                       comma-separated identity ids that see Chinese
                       holidays by default; empty = every user (the UI
                       still lets each user override locally)

历史说明: Google Calendar 集成已于 2026-10 移除 (用户决定不再使用)。
旧数据目录里的 google_calendar/ 与任务 frontmatter 的 task_uid /
gcal_event_id 字段原样保留、不再同步; Y8Y_PUBLIC_URL / Y8Y_TIMEZONE
仅用于展示与兼容, 不再有 OAuth 回调。
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _flag(value):
    return (value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def default_data_dir(dev_mode):
    """Persistent data lives outside the source tree by default.

    Dev mode keeps its own ./dev-data directory inside the repo so that
    development data can never collide with a real deployment's data.
    """
    if dev_mode:
        return REPO_ROOT / 'dev-data'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'y8y-todo'
    if os.name == 'nt':
        local = os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local')
        return Path(local) / 'y8y-todo'
    return Path.home() / '.local' / 'share' / 'y8y-todo'


def parse_users(raw):
    """'alice@example.com=alice,bob@example.com=bob' → {'alice@example.com': 'alice', ...}"""
    users = {}
    for pair in (raw or '').split(','):
        email, sep, uid = pair.strip().partition('=')
        email, uid = email.strip().lower(), uid.strip()
        if sep and email and uid:
            users[email] = uid
    return users


class Config:
    """Immutable snapshot of the environment. Build once at startup."""

    def __init__(self, env=None):
        env = os.environ if env is None else env

        self.dev_mode = _flag(env.get('Y8Y_DEV_MODE'))

        data_dir = env.get('Y8Y_DATA_DIR', '').strip()
        self.data_dir = Path(data_dir).expanduser() if data_dir else default_data_dir(self.dev_mode)

        self.port = int(env.get('Y8Y_PORT', '8086'))
        # Loopback by default: public exposure must go through a reverse
        # proxy. Dev mode is always loopback, no exceptions.
        self.bind = '127.0.0.1' if self.dev_mode else (env.get('Y8Y_BIND', '127.0.0.1').strip() or '127.0.0.1')

        self.public_url = env.get('Y8Y_PUBLIC_URL', '').strip().rstrip('/')
        self.timezone = env.get('Y8Y_TIMEZONE', 'Asia/Tokyo').strip() or 'Asia/Tokyo'

        self.users = parse_users(env.get('Y8Y_USERS', ''))
        self.auth_header = env.get('Y8Y_AUTH_HEADER', 'Cf-Access-Authenticated-User-Email').strip()
        self.trusted_proxies = {
            ip.strip() for ip in env.get('Y8Y_TRUSTED_PROXIES', '127.0.0.1').split(',') if ip.strip()
        }
        self.dev_user = env.get('Y8Y_DEV_USER', 'dev').strip() or 'dev'

        # 中国法定节假日显示 (O3-P1): 旧实现写死「只给某个用户名看」, 公开
        # 部署下其他用户永远看不到节假日。现在总开关 + 可选用户名单; 前端
        # 仍可按用户本机覆盖 (localStorage), 服务端值只决定默认。
        self.cn_holidays = _flag(env.get('Y8Y_CN_HOLIDAYS', '1'))
        self.cn_holiday_users = {
            u.strip() for u in env.get('Y8Y_CN_HOLIDAY_USERS', '').split(',') if u.strip()
        }

        # Tasks/files without an explicit owner field (legacy data) are
        # attributed to the first configured user so old data stays visible.
        self.default_owner = self.dev_user if self.dev_mode else (next(iter(self.users.values()), ''))

    # ── derived paths (all under the data root) ──────────────────────────
    @property
    def lists_root(self):
        return self.data_dir / 'lists'

    @property
    def shopping_dir(self):
        return self.lists_root / 'shopping'

    @property
    def internal_tokens_file(self):
        return self.data_dir / 'internal_tokens.json'

    @property
    def persistent_projects_file(self):
        return self.data_dir / 'persistent_projects.json'

    def cn_holidays_enabled_for(self, user):
        """该用户默认是否显示中国节假日 (前端仍可本机覆盖)。

        总开关关 → 一律 False; 设了用户名单 → 只有名单内用户; 名单空 →
        所有用户 (公开部署的合理默认)。
        """
        if not self.cn_holidays:
            return False
        if self.cn_holiday_users:
            return user in self.cn_holiday_users
        return True

    def validate(self):
        """Return a list of startup problems (empty = OK)."""
        problems = []
        if self.dev_mode:
            return problems  # dev mode is always safe: loopback + single user
        if not self.users and not self.internal_tokens_file.is_file():
            problems.append(
                'production mode without any identity: set Y8Y_USERS '
                '(human users via reverse proxy) or provide '
                'internal_tokens.json (machine-to-machine API), '
                'otherwise nobody could ever authenticate'
            )
        return problems

    def summary(self):
        mode = 'DEV' if self.dev_mode else 'PROD'
        return (
            f'mode={mode} bind={self.bind}:{self.port} '
            f'data_dir={self.data_dir} users={len(self.users)} '
            f'trusted_proxies={sorted(self.trusted_proxies)}'
        )


def load_config(env=None):
    return Config(env)
