"""Google Calendar OAuth and storage helpers for the Todo web app.

Secrets and mutable OAuth state deliberately live under /var/lib/todolist/google_calendar,
never in the deployed code tree or git repository.
"""
import json
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

# Google dependencies are imported lazily so the existing Todo service remains
# available even if a deployment environment is temporarily missing the optional SDK.
def _google_sdk():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build
        return Request, Credentials, Flow, build
    except ModuleNotFoundError as exc:
        raise GoogleCalendarError('服务器缺少 Google Calendar SDK，管理员需安装 python3-google-auth、python3-google-auth-oauthlib、python3-googleapi') from exc

SCOPES = ['https://www.googleapis.com/auth/calendar']
CALENDAR_NAME = 'TODO'


def credentials_to_info(credentials):
    """兼容 Ubuntu 仓库旧版 google-auth：它没有 Credentials.to_json()。"""
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': list(credentials.scopes or SCOPES),
        'expiry': credentials.expiry.isoformat() if credentials.expiry else None,
    }


class GoogleCalendarError(RuntimeError):
    pass


class GoogleCalendarAuth:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.client_secret_path = os.path.join(data_dir, 'client_secret.json')
        self.tokens_path = os.path.join(data_dir, 'tokens.json')
        self.settings_path = os.path.join(data_dir, 'settings.json')
        self.states_path = os.path.join(data_dir, 'oauth_states.json')
        # 2026-09-20: Google 同步挪到后台线程后, JSON 读写 / token 刷新会和主线程并发
        # → 用 RLock 保护「读-改-写」序列, 避免 tokens.json 被交错写坏。
        self._lock = threading.RLock()

    def _read_json(self, path, default):
        with self._lock:
            try:
                with open(path, encoding='utf-8') as f:
                    value = json.load(f)
                return value if isinstance(value, dict) else default
            except (FileNotFoundError, json.JSONDecodeError):
                return default

    def _write_json(self, path, value):
        with self._lock:
            os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
            temp = f'{path}.tmp'
            with open(temp, 'w', encoding='utf-8') as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
            os.chmod(temp, 0o600)
            os.replace(temp, path)
            os.chmod(path, 0o600)

    def configured(self):
        return os.path.isfile(self.client_secret_path)

    def status(self, user):
        settings = self._read_json(self.settings_path, {})
        record = settings.get(user, {})
        has_token = user in self._read_json(self.tokens_path, {})
        return {
            'configured': self.configured(),
            'connected': bool(has_token and record.get('calendar_id')),
            'calendar_name': record.get('calendar_name', CALENDAR_NAME),
            'last_sync_at': record.get('last_sync_at', ''),
            'scope': record.get('scope', 'owner_and_shared'),
        }

    def begin_oauth(self, user, redirect_uri):
        if not self.configured():
            raise GoogleCalendarError('Google OAuth 客户端凭据未配置')
        _, _, Flow, _ = _google_sdk()
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(72)
        states = self._read_json(self.states_path, {})
        states[state] = {
            'user': user,
            'code_verifier': code_verifier,
            'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        }
        self._write_json(self.states_path, states)
        flow = Flow.from_client_secrets_file(
            self.client_secret_path, scopes=SCOPES, redirect_uri=redirect_uri, code_verifier=code_verifier,
        )
        url, _ = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent', state=state)
        return url

    def finish_oauth(self, state, code, redirect_uri):
        _, _, Flow, _ = _google_sdk()
        states = self._read_json(self.states_path, {})
        record = states.get(state)
        if not record or datetime.fromisoformat(record['expires_at']) < datetime.now(timezone.utc):
            raise GoogleCalendarError('授权状态已失效，请回到 Todo 重新连接')
        flow = Flow.from_client_secrets_file(
            self.client_secret_path, scopes=SCOPES, redirect_uri=redirect_uri, code_verifier=record.get('code_verifier'),
        )
        flow.fetch_token(code=code)
        tokens = self._read_json(self.tokens_path, {})
        tokens[record['user']] = credentials_to_info(flow.credentials)
        self._write_json(self.tokens_path, tokens)
        # 只有 token 已安全写入后才消费 state；失败时保留到 TTL 到期，避免误清理重试上下文。
        states.pop(state, None)
        self._write_json(self.states_path, states)
        return record['user']

    def _credentials(self, user):
        Request, Credentials, _, _ = _google_sdk()
        with self._lock:                      # 读-刷新-写 必须原子 (后台线程也会走这里)
            tokens = self._read_json(self.tokens_path, {})
            info = tokens.get(user)
            if not info:
                raise GoogleCalendarError('尚未连接 Google Calendar')
            credentials = Credentials.from_authorized_user_info(info, SCOPES)
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise GoogleCalendarError('Google 授权已失效，请重新连接')
                credentials.refresh(Request())
                tokens[user] = credentials_to_info(credentials)
                self._write_json(self.tokens_path, tokens)
            return credentials

    def service(self, user, timeout=None):
        """构建 Calendar API client。timeout(秒) 会下发给底层 httplib2 —
        原本没有超时, Google 侧卡住会把调用方(旧版是请求线程)无限期挂住。"""
        _, _, _, build = _google_sdk()
        creds = self._credentials(user)
        if timeout:
            import httplib2
            from google_auth_httplib2 import AuthorizedHttp
            http = AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout))
            return build('calendar', 'v3', http=http, cache_discovery=False)
        return build('calendar', 'v3', credentials=creds, cache_discovery=False)

    def ensure_calendar(self, user, timeout=None):
        settings = self._read_json(self.settings_path, {})
        record = settings.get(user, {})
        if record.get('calendar_id'):
            return record['calendar_id']
        service = self.service(user, timeout=timeout)
        created = service.calendars().insert(body={
            'summary': CALENDAR_NAME,
            'description': '由 y8y todo 双向同步管理，请勿删除扩展属性。',
            'timeZone': 'Asia/Tokyo',
        }).execute()
        record.update({'calendar_id': created['id'], 'calendar_name': created.get('summary', CALENDAR_NAME), 'scope': 'owner_and_shared'})
        settings[user] = record
        self._write_json(self.settings_path, settings)
        return created['id']

    def mark_synced(self, user):
        settings = self._read_json(self.settings_path, {})
        record = settings.get(user, {})
        record['last_sync_at'] = datetime.now(timezone.utc).isoformat()
        settings[user] = record
        self._write_json(self.settings_path, settings)
