"""Security regression tests: path traversal, static file guard.

These are the regression guards for the vulnerabilities found in the
Round-1 investigation (S2: API path traversal). Every user-controlled
path segment must be rejected when it escapes the data directory.
"""
ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}


def _get(client, path):
    return client.get(path, headers=ALICE)


def test_task_id_traversal_rejected(server):
    serve, httpd, client, tmp = server
    # Plain traversal
    status, _ = _get(client, '/todo/api/tasks/..%2f..%2f..%2fetc%2fpasswd')
    assert status == 404
    # Double-encoded traversal
    status, _ = _get(client, '/todo/api/tasks/%252e%252e%252f%252e%252e%252fetc%252fpasswd')
    assert status in (400, 404)
    # Traversal with mixed separators
    status, _ = _get(client, '/todo/api/tasks/....//....//etc/passwd')
    assert status in (400, 404)


def test_task_id_absolute_path_rejected(server):
    serve, httpd, client, tmp = server
    status, _ = _get(client, '/todo/api/tasks/%2Fetc%2Fpasswd')
    assert status in (400, 404)


def test_project_traversal_rejected_on_create(server):
    serve, httpd, client, tmp = server
    status, _ = client.post('/todo/api/tasks',
                            body={'title': 'evil', 'project': '../../evil'}, headers=ALICE)
    assert status == 400
    # No directory was created outside the data root.
    assert not (tmp / 'evil').exists()
    assert not (tmp.parent / 'evil').exists()


def test_project_absolute_path_rejected_on_create(server):
    serve, httpd, client, tmp = server
    status, _ = client.post('/todo/api/tasks',
                            body={'title': 'evil', 'project': '/tmp/evil'}, headers=ALICE)
    assert status == 400


def test_list_name_traversal_rejected(server):
    serve, httpd, client, tmp = server
    status, _ = client.post('/todo/api/lists',
                            body={'name': '../evil-list'}, headers=ALICE)
    assert status == 400
    status, _ = client.post('/todo/api/lists',
                            body={'name': '/abs-list'}, headers=ALICE)
    assert status == 400


def test_list_id_traversal_rejected(server):
    serve, httpd, client, tmp = server
    for path in [
        '/todo/api/lists/..%2f..%2fetc/items',
        '/todo/api/lists/..%2f..%2fsecret',
    ]:
        status, _ = _get(client, path)
        assert status in (400, 404), path


def test_static_guard_blocks_sensitive_files(server):
    serve, httpd, client, tmp = server
    # Source code, git internals, backups must never be downloadable.
    for path in [
        '/todo/serve.py',
        '/todo/config.py',
        '/todo/.git/config',
        '/todo/.gitignore',
        '/todo/../etc/passwd',
        '/todo/%2e%2e/%2e%2e/etc/passwd',
    ]:
        status, _ = client.get(path)
        assert status == 404, f'{path} must be hard-blocked'


def test_static_guard_blocks_encoded_traversal_without_fallback(server):
    """The 2026-09-24 incident: encoded traversal used to fall through to
    the SPA fallback and answer 200. It must now be a hard 404."""
    serve, httpd, client, tmp = server
    status, body = client.get('/todo/%2e%2e/%2e%2e/etc/passwd')
    assert status == 404
    # SPA fallback serves index.html for unknown paths — but the body of a
    # blocked path must NOT be the index page.
    assert '<!DOCTYPE html>' not in body.get('_raw', '')


def test_index_html_served(server):
    serve, httpd, client, tmp = server
    status, body = client.get('/todo/')
    assert status == 200
    assert 'y8yTODO' in body.get('_raw', '')


def test_symlink_escape_blocked(server):
    """A symlink inside the data dir pointing outside must not be served."""
    import os
    serve, httpd, client, tmp = server
    data = tmp / 'data'
    (data / 'todo').mkdir(parents=True, exist_ok=True)
    secret = tmp / 'secret.txt'
    secret.write_text('TOPSECRET')
    os.symlink(str(secret), str(data / 'todo' / 'link.md'))
    # The symlinked file must not be readable through the API.
    status, _ = _get(client, '/todo/api/tasks/link.md')
    assert status == 404
