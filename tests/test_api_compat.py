"""API compatibility with the y8ysystems machine contract.

The y8ysystems service pulls today's tasks via:
    GET http://127.0.0.1:8086/todo/api/tasks
    Header: X-Internal-Token: <hex>

The path prefix, response field names and default port must stay stable.
These tests guard that contract.
"""
import json

from conftest import _make_server, Client, _stop_server


def _machine_client(tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'internal_tokens.json').write_text(json.dumps({'alice': 'hex-token-abc'}))
    serve, httpd, base = _make_server(tmp_path, {
        'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
        'Y8Y_TRUSTED_PROXIES': '127.0.0.1',
        'Y8Y_DATA_DIR': str(data_dir),
    })
    return serve, httpd, Client(base)


MACHINE_HEADERS = {'X-Internal-Token': 'hex-token-abc'}


def test_tasks_endpoint_contract(tmp_path):
    serve, httpd, client = _machine_client(tmp_path)
    try:
        status, body = client.get('/todo/api/tasks', headers=MACHINE_HEADERS)
        assert status == 200
        # Top-level fields y8ysystems reads.
        for key in ('current_user', 'tasks', 'completed_tasks', 'sections'):
            assert key in body, f'missing contract field: {key}'
        assert body['current_user'] == 'alice'
        assert set(body['sections'].keys()) == {
            'overdue', 'today', 'this_week', 'future', 'nodue'}
    finally:
        _stop_server(serve, httpd)


def test_task_field_names_stable(tmp_path):
    serve, httpd, client = _machine_client(tmp_path)
    try:
        status, body = client.post('/todo/api/tasks', body={
            'title': '今日任务', 'due': '2026-10-16', 'color': 'blue',
            'content': '详情', 'owner': 'alice',
        }, headers=MACHINE_HEADERS)
        assert status == 201
        status, body = client.get('/todo/api/tasks', headers=MACHINE_HEADERS)
        task = body['tasks'][0]
        # Field names consumed by y8ysystems (/api/me/todo card).
        for key in ('id', 'title', 'due', 'due_end', 'due_time', 'created',
                    'color', 'status', 'recurring', 'project', 'owner', 'body'):
            assert key in task, f'missing task field: {key}'
        assert task['title'] == '今日任务'
        assert task['owner'] == 'alice'
    finally:
        _stop_server(serve, httpd)


def test_default_port_is_8086(tmp_path):
    """y8ysystems calls port 8086 directly; the default must not drift."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
    import config
    cfg = config.Config({})
    assert cfg.port == 8086


def test_machine_token_serves_shared_and_own_tasks(tmp_path):
    serve, httpd, client = _machine_client(tmp_path)
    try:
        client.post('/todo/api/tasks', body={
            'title': 'alice的任务', 'due': '2026-10-16', 'owner': 'alice'},
            headers={'Cf-Access-Authenticated-User-Email': 'alice@example.com'})
        client.post('/todo/api/tasks', body={
            'title': 'bob的任务', 'due': '2026-10-16', 'owner': 'bob'},
            headers={'Cf-Access-Authenticated-User-Email': 'bob@example.com'})
        status, body = client.get('/todo/api/tasks', headers=MACHINE_HEADERS)
        titles = [t['title'] for t in body['tasks']]
        assert 'alice的任务' in titles
        assert 'bob的任务' not in titles    # private tasks of other users excluded
    finally:
        _stop_server(serve, httpd)
