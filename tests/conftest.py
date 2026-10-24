"""Shared pytest fixtures: spin up a real server on an ephemeral port
with an isolated data directory, for each test."""
import importlib
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, quote

import pytest
import urllib.request
import urllib.error

SRC = Path(__file__).resolve().parent.parent / 'src'


def pytest_configure(config):
    config.addinivalue_line(
        'markers', 'server_env(env-dict): per-test environment for the `server` fixture')


def _request(base_url, method, path, body=None, headers=None):
    """Minimal HTTP client that bypasses any ambient proxy settings.
    Percent-encodes non-ASCII path characters like a browser would."""
    url = base_url + path
    parts = urlsplit(url)
    encoded_path = quote(parts.path, safe="/%")   # keep existing %XX escapes intact
    url = urlunsplit((parts.scheme, parts.netloc, encoded_path, parts.query, parts.fragment))
    data = None
    if body is not None:
        data = json.dumps(body).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    try:
        payload = json.loads(raw.decode('utf-8')) if raw else {}
    except json.JSONDecodeError:
        payload = {'_raw': raw.decode('utf-8', 'replace')}
    return status, payload


class Client:
    """Tiny API client around _request with convenient verbs."""

    def __init__(self, base_url):
        self.base = base_url

    def get(self, path, headers=None):
        return _request(self.base, 'GET', path, headers=headers)

    def post(self, path, body=None, headers=None):
        return _request(self.base, 'POST', path, body=body, headers=headers)

    def put(self, path, body=None, headers=None):
        return _request(self.base, 'PUT', path, body=body, headers=headers)

    def delete(self, path, headers=None):
        return _request(self.base, 'DELETE', path, headers=headers)


def _make_server(tmp_path, env):
    """Import (or reload) serve.py under a controlled environment and
    start a real HTTPServer on an ephemeral loopback port."""
    old_environ = dict(os.environ)
    old_syspath = list(sys.path)
    try:
        for k in list(os.environ):
            if k.startswith('Y8Y_'):
                del os.environ[k]
        os.environ.update(env)
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        if 'serve' in sys.modules:
            serve = importlib.reload(sys.modules['serve'])
        else:
            serve = importlib.import_module('serve')
        httpd = __import__('http.server', fromlist=['HTTPServer']).HTTPServer(
            ('127.0.0.1', 0), serve.TodolistHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return serve, httpd, f'http://127.0.0.1:{port}'
    finally:
        os.environ.clear()
        os.environ.update(old_environ)
        sys.path[:] = old_syspath


def _stop_server(serve, httpd):
    """Stop a test server (HTTP shutdown).

    Idempotent: tests may stop the server themselves (restart scenarios)
    before the fixture teardown runs."""
    try:
        httpd.shutdown()
        httpd.server_close()
    except Exception:
        pass


@pytest.fixture()
def server(tmp_path, request):
    """A running server. Per-test env via @pytest.mark.server_env({...})."""
    env = {
        'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
        'Y8Y_TRUSTED_PROXIES': '127.0.0.1',
        'Y8Y_TIMEZONE': 'Asia/Tokyo',
    }
    marker = request.node.get_closest_marker('server_env')
    if marker and marker.args:
        env.update(marker.args[0])
    env['Y8Y_DATA_DIR'] = str(tmp_path / 'data')
    serve, httpd, base = _make_server(tmp_path, env)
    client = Client(base)
    yield serve, httpd, client, tmp_path
    _stop_server(serve, httpd)


@pytest.fixture()
def dev_server(tmp_path):
    """A running dev-mode server (single local user, no auth headers)."""
    serve, httpd, base = _make_server(tmp_path, {
        'Y8Y_DEV_MODE': '1',
        'Y8Y_DATA_DIR': str(tmp_path / 'dev-data'),
    })
    yield serve, httpd, Client(base), tmp_path
    _stop_server(serve, httpd)


@pytest.fixture()
def legacy_server(tmp_path):
    """A server running against a fully fictional legacy-format data dir.

    The user mapping mirrors the realistic production migration
    (alice/bob), so legacy owner fields stay meaningful.
    """
    from legacy_fixture import LEGACY_ENV, build_legacy_data
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True)
    build_legacy_data(data_dir)
    env = dict(LEGACY_ENV)
    env['Y8Y_DATA_DIR'] = str(data_dir)
    serve, httpd, base = _make_server(tmp_path, env)
    yield serve, httpd, Client(base), tmp_path
    _stop_server(serve, httpd)
