"""Safety guard for the destructive E2E script (scripts/e2e-api.py).

The `full` phase creates/completes/restores tasks on its target, so it
must refuse to run against a non-empty instance unless explicitly
overridden. These tests pin that contract.
"""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'e2e-api.py'


@pytest.fixture()
def e2e_mod():
    spec = importlib.util.spec_from_file_location('e2e_api_under_test', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_req(monkeypatch, mod, tasks=0, completed=0, status=200):
    calls = []

    def fake_req(base, method, path, body=None, headers=None):
        calls.append(path)
        return status, {'tasks': [{}] * tasks,
                        'completed_tasks': [{}] * completed}

    monkeypatch.setattr(mod, 'req', fake_req)
    return calls


def test_guard_passes_on_empty_instance(e2e_mod, monkeypatch):
    calls = _stub_req(monkeypatch, e2e_mod)
    e2e_mod.guard_isolated_instance('http://127.0.0.1:1', 'tok')
    assert calls == ['/todo/api/tasks']


def test_guard_blocks_non_empty_instance(e2e_mod, monkeypatch):
    _stub_req(monkeypatch, e2e_mod, tasks=2, completed=1)
    with pytest.raises(SystemExit) as excinfo:
        e2e_mod.guard_isolated_instance('http://127.0.0.1:1', 'tok')
    assert excinfo.value.code == 3


def test_guard_honours_explicit_override(e2e_mod, monkeypatch):
    _stub_req(monkeypatch, e2e_mod, tasks=2)
    monkeypatch.setenv('Y8Y_E2E_ALLOW_NONEMPTY', '1')
    e2e_mod.guard_isolated_instance('http://127.0.0.1:1', 'tok')  # no exit


def test_guard_defers_to_real_checks_on_auth_failure(e2e_mod, monkeypatch):
    # 401/网络问题交给后续正式检查去报, 守卫不抢先失败
    _stub_req(monkeypatch, e2e_mod, status=401)
    e2e_mod.guard_isolated_instance('http://127.0.0.1:1', 'wrong-token')
