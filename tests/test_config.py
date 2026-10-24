"""Configuration layer: defaults, parsing, validation."""
import importlib
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / 'src'


def _fresh_config(env):
    old = dict(os.environ)
    try:
        for k in list(os.environ):
            if k.startswith('Y8Y_'):
                del os.environ[k]
        os.environ.update(env)
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        if 'config' in sys.modules:
            config = importlib.reload(sys.modules['config'])
        else:
            config = importlib.import_module('config')
        return config.Config(env)
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_defaults_are_production_safe():
    cfg = _fresh_config({})
    assert cfg.dev_mode is False
    assert cfg.bind == '127.0.0.1'          # loopback by default
    assert cfg.port == 8086                 # y8ysystems contract
    assert cfg.users == {}
    assert '127.0.0.1' in cfg.trusted_proxies


def test_data_dir_default_outside_source_tree():
    cfg = _fresh_config({})
    assert not str(cfg.data_dir).startswith(str(SRC))
    assert str(cfg.data_dir)  # some OS user data location


def test_dev_mode_data_dir_inside_repo_but_isolated():
    cfg = _fresh_config({'Y8Y_DEV_MODE': '1'})
    assert cfg.dev_mode is True
    assert cfg.bind == '127.0.0.1'          # forced, no exceptions
    assert cfg.dev_user == 'dev'
    # dev-data lives in the repo (gitignored) so dev data can never
    # collide with a real deployment's data directory.
    assert str(cfg.data_dir).startswith(str(SRC.parent))


def test_env_overrides():
    cfg = _fresh_config({
        'Y8Y_DATA_DIR': '/tmp/y8y-test-data',
        'Y8Y_PORT': '9999',
        'Y8Y_BIND': '0.0.0.0',
        'Y8Y_TIMEZONE': 'America/New_York',
        'Y8Y_PUBLIC_URL': 'https://todo.example.com/',
    })
    assert cfg.data_dir == Path('/tmp/y8y-test-data')
    assert cfg.port == 9999
    assert cfg.bind == '0.0.0.0'
    assert cfg.timezone == 'America/New_York'


def test_users_parsing():
    cfg = _fresh_config({'Y8Y_USERS': ' Alice@Example.com = alice , bob@example.com=bob '})
    assert cfg.users == {'alice@example.com': 'alice', 'bob@example.com': 'bob'}
    assert cfg.default_owner == 'alice'     # first configured user


def test_validate_rejects_prod_without_identities(tmp_path):
    cfg = _fresh_config({'Y8Y_DATA_DIR': str(tmp_path / 'empty')})
    problems = cfg.validate()
    assert problems, 'prod mode with no users and no token file must refuse to start'


def test_validate_allows_dev_mode_always():
    cfg = _fresh_config({'Y8Y_DEV_MODE': '1'})
    assert cfg.validate() == []


def test_validate_allows_machine_only_deployment(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'internal_tokens.json').write_text('{"ci": "tok"}')
    cfg = _fresh_config({'Y8Y_DATA_DIR': str(data)})
    assert cfg.validate() == []             # token file alone is a valid identity source


# ── 中国节假日显示偏好 (O3-P1) ──────────────────────────────────────────

def test_cn_holidays_default_on_for_every_user():
    """默认所有用户都显示中国节假日 (旧实现写死单一用户名, 已移除)。"""
    cfg = _fresh_config({})
    assert cfg.cn_holidays is True
    assert cfg.cn_holiday_users == set()
    assert cfg.cn_holidays_enabled_for('alice') is True
    assert cfg.cn_holidays_enabled_for('bob') is True
    assert cfg.cn_holidays_enabled_for('dev') is True


def test_cn_holidays_master_switch_off():
    cfg = _fresh_config({'Y8Y_CN_HOLIDAYS': '0'})
    assert cfg.cn_holidays is False
    assert cfg.cn_holidays_enabled_for('alice') is False


def test_cn_holidays_user_list_restricts_default():
    """Y8Y_CN_HOLIDAY_USERS: 只有名单内用户默认可见 (存量部署迁移用)。"""
    cfg = _fresh_config({'Y8Y_CN_HOLIDAY_USERS': 'alice, carol'})
    assert cfg.cn_holidays_enabled_for('alice') is True
    assert cfg.cn_holidays_enabled_for('carol') is True
    assert cfg.cn_holidays_enabled_for('bob') is False
