"""Logs may be copied into public CI: keep fictional task data out of output."""
import json
from datetime import datetime
from urllib.parse import quote

import pytest

from test_notifications import (
    ALICE, TZ, FakeProvider, ProviderError, _enabled, _make_scheduler, _write_task,
)
import notify_runner


@pytest.mark.parametrize('kind, expected', [('existing', 200), ('missing', 404), ('unknown', 200)])
def test_access_log_omits_resource_query_and_client_address(server, capsys, kind, expected):
    _, _, client, _ = server
    marker = 'FICTIONAL_TASK_LOG_CANARY'
    code, task = client.post('/todo/api/tasks', {'title': marker}, headers=ALICE)
    assert code == 201
    capsys.readouterr()
    path = ('/todo/api/' + marker if kind == 'unknown' else '/todo/api/tasks/' +
            (quote(task['id'], safe='') if kind == 'existing' else marker))
    code, _ = client.get(path + '?note=FICTIONAL_QUERY_CANARY', headers=ALICE)
    assert code == expected
    output = capsys.readouterr().err
    assert marker not in output
    assert 'FICTIONAL_QUERY_CANARY' not in output
    assert '127.0.0.1' not in output
    route = '/todo/api/other' if kind == 'unknown' else '/todo/api/tasks/:resource'
    assert f'[http] GET {route} status={code}' in output


@pytest.mark.parametrize('as_json', [False, True])
@pytest.mark.parametrize('mode', ['planned', 'sent', 'failed'])
def test_notification_output_omits_task_recipient_and_path(tmp_path, monkeypatch, capsys, as_json, mode):
    marker = 'FICTIONAL_NOTIFICATION_LOG_CANARY'
    data = tmp_path / 'fictional-data'
    _write_task(data, 'todo', 'fictional.md', title=marker, owner='shared',
                due='2026-11-08', due_time='09:30')
    provider = FakeProvider(fail_times=1 if mode == 'failed' else 0,
                            error=ProviderError(marker))
    scheduler = _make_scheduler(data, now=datetime(2026, 11, 8, 9, 0, tzinfo=TZ),
        providers={'fake': provider}, settings={'alice': _enabled(), 'bob': {'enabled': False}})
    monkeypatch.setattr(notify_runner, 'NotifyConfig', lambda: scheduler.config)
    monkeypatch.setattr(notify_runner, 'Scheduler', lambda *args, **kwargs: scheduler)
    args = ['--now', '2026-11-08T09:00:00']
    if as_json:
        args.append('--json')
    if mode == 'planned':
        args.append('--dry-run')
    assert notify_runner.main(args) == 0
    output = capsys.readouterr().out
    assert marker not in output
    assert 'alice' not in output
    assert str(data) not in output
    assert 'fake' in output
    if as_json:
        report = json.loads(output)
        assert report[mode][0]['channel'] == 'fake'
        assert report[mode][0]['status'] == mode
    else:
        assert {'planned': '[plan]', 'sent': '[sent]', 'failed': '[fail]'}[mode] in output
    if mode == 'failed':
        assert 'provider_failed' in output
    if mode != 'planned':
        # The private delivery record still has its original data and format.
        state = data / 'notify' / 'notification_state.json'
        assert marker in state.read_text(encoding='utf-8')
    if mode == 'sent':
        assert marker in provider.sent[0][1].body


@pytest.mark.parametrize('as_json', [False, True])
def test_test_notification_output_omits_recipient_and_raw_error(tmp_path, monkeypatch, capsys, as_json):
    from notifications import NotifyConfig
    monkeypatch.setattr(notify_runner, 'NotifyConfig', lambda: NotifyConfig({'Y8Y_DATA_DIR': str(tmp_path)}))
    monkeypatch.setattr(notify_runner, 'send_test', lambda *args: [
        {'channel': 'line', 'status': 'failed', 'error': 'FICTIONAL_ERROR_CANARY'}])
    args = ['--test', 'fictional-recipient'] + (['--json'] if as_json else [])
    assert notify_runner.main(args) == 1
    output = capsys.readouterr().out
    assert 'fictional-recipient' not in output
    assert 'FICTIONAL_ERROR_CANARY' not in output
    assert 'provider_failed' in output
    if as_json:
        assert json.loads(output)['results'][0]['status'] == 'failed'


def test_notification_state_failure_omits_private_path(tmp_path, monkeypatch, capsys):
    from notifications import NotifyConfig
    config = NotifyConfig({'Y8Y_DATA_DIR': str(tmp_path)})
    monkeypatch.setattr(notify_runner, 'NotifyConfig', lambda: config)
    def fail(*args, **kwargs):
        raise PermissionError(13, 'FICTIONAL_ERROR_CANARY', str(tmp_path))
    monkeypatch.setattr(notify_runner, 'publish_capabilities', fail)
    assert notify_runner.main([]) == 1
    output = capsys.readouterr().err
    assert str(tmp_path) not in output
    assert 'FICTIONAL_ERROR_CANARY' not in output
    assert 'capability_publish_failed errno=13' in output
