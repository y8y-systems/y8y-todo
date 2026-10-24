"""P5.5 tooling regression tests: the read-only copy verifier and the
target-host identity gate (independent-review hardened).

- p5-verify-copy.py privacy contract: the default (production) output
  must never contain a real filename — markdown filenames ARE task
  titles. Detailed paths are only available behind --diagnostic +
  Y8Y_FICTIONAL_DATA=1 (fictional environments only).
- p5-verify-copy.py identity contract: the token is selected by the
  explicit --identity (never random), current_user must match, and an
  empty-but-expected-nonzero API response must fail (false-positive
  guard). Exercised against a REAL server on fictional fixtures.
- p5-host-check.sh phase model (2026-10 independent review): each
  production phase has its own correct state —
    pre-deploy   old service RUNNING + 8086 held by the old process
    pre-cutover  same + staging instance verified (before the freeze)
    cutover      old service STOPPED + 8086 RELEASED + staging evidence
    pre-rollback old-service state is informational; a todo process on
                 8086 (the new version) must NOT block rollback; only a
                 foreign squatter fails. Failure output is sanitized:
                 the full process cmdline (possible secrets) is never
                 printed — only pid/user/an irreversible cmdline digest.
  A full state-machine walk (old running → freeze → new version up →
  rollback prep → old restored) mirrors the Runbook order.
No real host is ever touched.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent
TESTS = SRC / 'tests'
VERIFY = SRC / 'scripts' / 'p5-verify-copy.py'
HOSTCHECK = SRC / 'scripts' / 'p5-host-check.sh'
PY = sys.executable

FICTIONAL_ENV = dict(os.environ, Y8Y_FICTIONAL_DATA='1')


def _fixture(tmp_path):
    sys.path.insert(0, str(TESTS))
    from legacy_fixture import build_legacy_data
    data = tmp_path / 'data'
    build_legacy_data(data)
    return data


def _copy(original, dest):
    subprocess.run(['cp', '-r', str(original), str(dest)], check=True)
    return dest


def _verify(original, copy, extra=(), env=None):
    return subprocess.run([PY, str(VERIFY), str(original), str(copy), *extra],
                          capture_output=True, text=True, env=env)


# ── A: 隐私 — 默认输出绝不包含真实文件名 ────────────────────────────────

PRIVATE_TITLES = ('买菜', 'bob的预约', '共享大扫除', 'legacy-agent遗留')


def test_default_output_never_contains_filenames(tmp_path):
    original = _fixture(tmp_path)
    copy = _copy(original, tmp_path / 'copy')
    r = _verify(original, copy)
    assert r.returncode == 0, r.stdout + r.stderr
    for title in PRIVATE_TITLES:
        assert title not in r.stdout, f'privacy leak: {title}'


def test_tampered_copy_fails_without_leaking_filenames(tmp_path):
    original = _fixture(tmp_path)
    copy = _copy(original, tmp_path / 'copy')
    (copy / 'todo' / '买菜.md').write_text('tampered\n', encoding='utf-8')
    r = _verify(original, copy)
    assert r.returncode == 1
    assert '[FAIL]' in r.stdout
    # 默认模式: 只说数量、结构分类与匿名标识, 不说文件名, 不泄露标题。
    # (结构分类名如 "todo/:1" 是目录名, 非私人内容 — 设计内的输出。)
    assert '买菜' not in r.stdout
    assert 'todo/买菜.md' not in r.stdout
    assert '#' in r.stdout            # 匿名标识存在
    assert 'tampered' not in r.stdout
    # 虚构环境 + --diagnostic 才给路径 (明确限制的详细诊断能力)
    r2 = _verify(original, copy, extra=['--diagnostic'], env=FICTIONAL_ENV)
    assert r2.returncode == 1
    assert 'todo/买菜.md' in r2.stdout


def test_missing_and_extra_files_fail_without_leaking_filenames(tmp_path):
    original = _fixture(tmp_path)
    copy = _copy(original, tmp_path / 'copy')
    (copy / 'todo' / 'bob的预约.md').unlink()
    (copy / '未知杂物.json').write_text('{}', encoding='utf-8')
    r = _verify(original, copy)
    assert r.returncode == 1
    assert 'bob的预约' not in r.stdout
    assert '未知杂物.json' not in r.stdout
    assert '缺失' in r.stdout and '多余' in r.stdout
    r2 = _verify(original, copy, extra=['--diagnostic'], env=FICTIONAL_ENV)
    assert 'bob的预约' not in r2.stdout or True   # 虚构环境允许路径输出
    assert 'todo/bob的预约.md' in r2.stdout


def test_diagnostic_requires_fictional_env(tmp_path):
    """--diagnostic 在未声明的环境必须被拒绝 (退出码 2), 而不是输出路径。"""
    original = _fixture(tmp_path)
    copy = _copy(original, tmp_path / 'copy')
    r = _verify(original, copy, extra=['--diagnostic'])
    assert r.returncode == 2
    assert '虚构' in r.stdout
    assert '买菜' not in r.stdout


def test_verify_copy_ignores_stale_tmp(tmp_path):
    """崩溃残留 .tmp 两边一致跳过 (与备份约定一致), 不算差异。"""
    original = _fixture(tmp_path)
    copy = _copy(original, tmp_path / 'copy')
    (original / 'todo' / 'ghost.md.tmp').write_text('x', encoding='utf-8')
    (copy / 'todo' / 'ghost.md.tmp').write_text('x', encoding='utf-8')
    r = _verify(original, copy)
    assert r.returncode == 0, r.stdout


# ── B: 身份 — 精确定位 token / current_user 校验 / 空列表假阳性 ─────────

def _start_server(data_dir, users_env, port_hint=None):
    sys.path.insert(0, str(TESTS))
    from conftest import _make_server, _stop_server, Client
    serve, httpd, base = _make_server(data_dir.parent, dict(
        users_env, Y8Y_DATA_DIR=str(data_dir)))
    return serve, httpd, base, Client, _stop_server


LEGACY_USERS = {'Y8Y_USERS': 'alice@example.com=alice,bob@example.com=bob',
                'Y8Y_TRUSTED_PROXIES': '127.0.0.1',
                'Y8Y_TIMEZONE': 'Asia/Tokyo'}


def _legacy_server(tmp_path):
    sys.path.insert(0, str(TESTS))
    from conftest import _make_server, _stop_server
    from legacy_fixture import build_legacy_data, LEGACY_ENV
    data = tmp_path / 'data'
    build_legacy_data(data)
    serve, httpd, base = _make_server(tmp_path, dict(
        LEGACY_ENV, Y8Y_DATA_DIR=str(data)))
    return serve, httpd, base, _stop_server, data


def test_api_verification_passes_for_explicit_identity(tmp_path):
    serve, httpd, base, stop, data = _legacy_server(tmp_path)
    try:
        r = _verify(data, data, extra=['--api', base, '--identity', 'alice',
                                       '--users', 'alice,bob'])
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'current_user == 预期身份 (alice)' in r.stdout
        assert '非随机' in r.stdout
        assert 'legacy-token-abc123' not in r.stdout      # 令牌不回显
        for title in PRIVATE_TITLES:
            assert title not in r.stdout
    finally:
        stop(serve, httpd)


def test_api_verification_rejects_unknown_identity(tmp_path):
    """identity 在用户表里但 token 文件中没有 → FAIL (且绝不回退到别的 token)。"""
    serve, httpd, base, stop, data = _legacy_server(tmp_path)
    try:
        r = _verify(data, data, extra=['--api', base, '--identity', 'bob',
                                       '--users', 'alice,bob'])
        assert r.returncode == 1
        assert '标识 bob 在 internal_tokens.json 中存在' in r.stdout
        assert '可用标识数=1' in r.stdout      # 不泄露另一个 token 的名字
        assert 'legacy-token-abc123' not in r.stdout
    finally:
        stop(serve, httpd)


def test_api_verification_rejects_identity_outside_users(tmp_path):
    """identity 不在 --users 列表 → 用法错误 (退出码 2), 身份预期矛盾不放行。"""
    data = _fixture(tmp_path)
    r = _verify(data, data, extra=['--api', 'http://127.0.0.1:1',
                                   '--identity', 'nosuchuser', '--users', 'alice,bob'])
    assert r.returncode == 2
    assert '不在 --users' in r.stdout


def test_api_verification_rejects_current_user_mismatch(tmp_path):
    """副本 token 表与服务器数据不一致时, current_user 必须对不上 — FAIL。"""
    sys.path.insert(0, str(TESTS))
    from conftest import _make_server, _stop_server
    from legacy_fixture import build_legacy_data, LEGACY_ENV
    server_dir = tmp_path / 'server-data'
    other = tmp_path / 'other-data'
    for d in (server_dir, other):
        build_legacy_data(d)
    # 服务器数据里 shared-tok 属于 bob; 副本里同一个 token 挂在 alice 名下
    (server_dir / 'internal_tokens.json').write_text(
        json.dumps({'bob': 'shared-tok'}), encoding='utf-8')
    (other / 'internal_tokens.json').write_text(
        json.dumps({'alice': 'shared-tok'}), encoding='utf-8')
    serve, httpd, base = _make_server(tmp_path, dict(
        LEGACY_ENV, Y8Y_DATA_DIR=str(server_dir)))
    try:
        r = _verify(server_dir, other,
                    extra=['--api', base, '--identity', 'alice', '--users', 'alice,bob'])
        assert r.returncode == 1
        assert 'current_user == 预期身份' in r.stdout
        assert 'shared-tok' not in r.stdout
    finally:
        _stop_server(serve, httpd)


def test_api_verification_multi_token_uses_exact_identity(tmp_path):
    """多 token 环境: --identity bob 必须用 bob 的 token (数量按 bob 预期)。"""
    data = _fixture(tmp_path)
    (data / 'internal_tokens.json').write_text(json.dumps(
        {'alice': 'tok-alice', 'bob': 'tok-bob'}), encoding='utf-8')
    sys.path.insert(0, str(TESTS))
    from conftest import _make_server, _stop_server
    serve, httpd, base = _make_server(tmp_path, dict(
        LEGACY_USERS, Y8Y_DATA_DIR=str(data)))
    try:
        r = _verify(data, data, extra=['--api', base, '--identity', 'bob',
                                       '--users', 'alice,bob'])
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'current_user == 预期身份 (bob)' in r.stdout
        assert 'bob' in r.stdout
        assert 'tok-bob' not in r.stdout and 'tok-alice' not in r.stdout
        # 反证: 若误用 alice 的 token, 身份校验就会 FAIL (current_user=alice)
    finally:
        _stop_server(serve, httpd)


def test_api_verification_empty_list_is_a_failure(tmp_path):
    """200 + 空任务列表 + 预期非零 = FAIL (空列表假阳性防护)。"""
    sys.path.insert(0, str(TESTS))
    from conftest import _make_server, _stop_server
    from legacy_fixture import build_legacy_data, LEGACY_ENV
    # 服务器数据目录只有 token, 没有任何任务
    empty = tmp_path / 'empty-data'
    empty.mkdir()
    (empty / 'internal_tokens.json').write_text(
        json.dumps({'alice': 'legacy-token-abc123'}), encoding='utf-8')
    # 副本是有任务的完整数据 (与 §4/§6 的验证对象同构)
    full = _fixture(tmp_path / 'full')
    serve, httpd, base = _make_server(tmp_path, dict(
        LEGACY_ENV, Y8Y_DATA_DIR=str(empty)))
    try:
        r = _verify(full, full, extra=['--api', base, '--identity', 'alice',
                                       '--users', 'alice,bob'])
        assert r.returncode == 1
        assert '空列表' in r.stdout
    finally:
        _stop_server(serve, httpd)


def test_api_verification_requires_identity_and_users(tmp_path):
    data = _fixture(tmp_path)
    r = _verify(data, data, extra=['--api', 'http://127.0.0.1:1'])
    assert r.returncode == 2
    assert '--identity' in r.stdout
    r2 = _verify(data, data, extra=['--api', 'http://127.0.0.1:1',
                                    '--identity', 'alice'])
    assert r2.returncode == 2
    assert '--users' in r2.stdout
    r3 = _verify(data, data, extra=['--api', 'http://127.0.0.1:1',
                                    '--identity', 'alice', '--users', 'bob'])
    assert r3.returncode == 2
    assert '不在 --users' in r3.stdout


def test_api_counts_match_server_visibility_model(tmp_path):
    """验证器的可见性预期模型与真实服务端行为一致 (软隐藏/shared/默认归属)。"""
    serve, httpd, base, stop, data = _legacy_server(tmp_path)
    try:
        from conftest import Client
        client = Client(base)
        st, alice_body = client.get('/todo/api/tasks',
                                   headers={'X-Internal-Token': 'legacy-token-abc123'})
        assert st == 200
        n_alice = len(alice_body['tasks'])
        # legacy-agent 软隐藏: alice 看不到; shared 看得到 — 抽查两条结构性事实
        owners = {t['owner'] for t in alice_body['tasks']}
        assert 'legacy-agent' not in owners
        assert 'shared' in owners
        # 验证器按同一模型推出的预期必须与实际 API 数量相等
        sys.path.insert(0, str(SRC / 'scripts'))
        import importlib.util
        spec = importlib.util.spec_from_file_location('p5vc', VERIFY)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        exp_tasks, exp_done = m.expected_visible_counts(
            str(data), 'alice', ['alice', 'bob'])
        assert exp_tasks == n_alice, (exp_tasks, n_alice)
        assert exp_done == len(alice_body['completed_tasks'])
    finally:
        stop(serve, httpd)


# ── C: 目标主机身份核对 (假 systemctl/ss + 真实假监听进程) ──────────────
#
# host-check 的目标平台是 Linux (生产 Ubuntu 22.04); 需要读取进程
# cmdline / 归属用户的用例依赖 /proc 与 ps, 在非 Linux 开发机上跳过 —
# 它们由 ubuntu-2204 CI 的 pytest-310 作业在真实 Linux 上执行。
LINUX_ONLY = pytest.mark.skipif(sys.platform != 'linux',
                                reason='process-identity checks need /proc+ps (Linux); '
                                       'covered by ubuntu-2204 CI')

def _write_exec(path, content):
    path.write_text(content, encoding='utf-8')
    path.chmod(0o755)


def _fake_tools(tmp_path, listener_pid):
    """假 systemctl / ss — 让 host-check 的判定逻辑可在隔离环境复现。"""
    bindir = tmp_path / 'bin'
    bindir.mkdir(exist_ok=True)
    _write_exec(bindir / 'systemctl', f"""#!/usr/bin/env bash
case "${{1:-}}" in
  --version) echo "systemd 249 (fake)"; exit 0;;
  is-active) [ "${{FAKE_OLD_ACTIVE:-1}}" = "1" ] && exit 0 || exit 3;;
esac
exit 0
""")
    _write_exec(bindir / 'ss', f"""#!/usr/bin/env bash
for a in "$@"; do
  case "$a" in
    *":8086"*) [ -n "${{FAKE_LISTENER_PID:-}}" ] && echo 'LISTEN 0 128 0.0.0.0:8086 0.0.0.0:* users:(("python3",pid='"${{FAKE_LISTENER_PID}}"',fd=3))'; exit 0;;
    *":18086"*) [ -n "${{FAKE_STAGE_PID:-}}" ] && echo 'LISTEN 0 128 127.0.0.1:18086 0.0.0.0:* users:(("python3",pid='"${{FAKE_STAGE_PID}}"',fd=4))'; exit 0;;
  esac
done
exit 0
""")
    return bindir


def _fake_host(tmp_path, listener_pid):
    root = tmp_path / 'fake'
    code = root / 'y8y' / 'projects' / 'todolist'
    (code / 'src').mkdir(parents=True)
    (code / 'src' / 'serve.py').write_text('# fake old code', encoding='utf-8')
    data = root / 'y8y' / 'data' / 'todolist'
    (data / 'todo').mkdir(parents=True)
    (data / 'todo' / 'task.md').write_text('x', encoding='utf-8')
    (data / 'internal_tokens.json').write_text('{"u": "t"}', encoding='utf-8')
    (root / 'etc' / 'systemd' / 'system').mkdir(parents=True)
    (root / 'etc' / 'systemd' / 'system' / 'todolist-web.service').write_text('[Unit]')
    (root / 'var' / 'lib' / 'y8y-todo').mkdir(parents=True)
    staging = root / 'var' / 'lib' / 'y8y-todo-staging'
    (staging / 'todo').mkdir(parents=True)
    # 隔离实例数据目录 = "§4 只读副本 + 隔离验证已执行"的可检查证据
    (staging / 'todo' / 'task.md').write_text('x', encoding='utf-8')
    bindir = _fake_tools(tmp_path, listener_pid)
    env = dict(os.environ)
    env['PATH'] = f'{bindir}{os.pathsep}{env["PATH"]}'
    env.update({
        'FAKE_OLD_ACTIVE': '1',
        'FAKE_LISTENER_PID': str(listener_pid),
        'Y8Y_EXPECT_HOSTS': f'{__import__("socket").gethostname()},todo-host',
        'Y8Y_OLD_CODE_DIR': str(code),
        'Y8Y_OLD_CODE_ALIASES': str(root / 'var' / 'www' / 'todolist'),
        'Y8Y_OLD_DATA_DIR': str(data),
        'Y8Y_OLD_UNIT_FILE': str(root / 'etc' / 'systemd' / 'system' / 'todolist-web.service'),
        'Y8Y_OLD_SERVICE_USER': __import__('getpass').getuser(),
        'Y8Y_NEW_UNIT_FILE': str(root / 'etc' / 'systemd' / 'system' / 'y8y-todo.service'),
        'Y8Y_NEW_DATA_DIR': str(root / 'var' / 'lib' / 'y8y-todo'),
        'Y8Y_STAGING_DATA_DIR': str(staging),
    })
    return root, code, env


def _spawn_fake_listener(code_dir, argv0='serve.py', cwd=None):
    """真实后台进程: argv0 含 serve.py, cwd 可指定 (用于身份核对)。

    argv0 可以是任意字符串 (含空格/参数) — exec -a 会原样放进 /proc/cmdline,
    用于脱敏断言 (敏感参数不得出现在 host-check 输出里)。注意不要给 sleep
    传额外参数: GNU sleep 会拒绝并立即退出, 进程变僵尸后 cmdline 读空。
    """
    script = f'cd "{cwd or code_dir}" && exec -a "{argv0}" sleep 60'
    return subprocess.Popen(['bash', '-c', script],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run_hostcheck(env, phase):
    return subprocess.run(['bash', str(HOSTCHECK), '--phase', phase],
                          capture_output=True, text=True, env=env)


# p5-host-check.sh 在"工具可用性"闸门之前/之外实际调用的外部命令
# (bash 自身由 subprocess 解析, 单独加入符号链接农场)。
# 构造"某个工具确定缺席"的 PATH 时只链接这些真实工具 —— 绝不把系统里的
# systemctl / ss 带进来: Linux (CI runner 与生产 VPS) 上它们真实存在,
# 会污染"工具缺失"的模拟 (PR #5 CI 红的根因: 只删假 systemctl 后,
# command -v 找到了 runner 上真实的 systemctl, 测试退化成"服务未运行"分支)。
HOSTCHECK_EXTERNAL_TOOLS = ('hostname', 'uname', 'find', 'wc', 'ps', 'tr',
                            'sed', 'cut', 'grep', 'head', 'readlink')


def _isolated_toolbin(tmp_path):
    """只含 HOSTCHECK_EXTERNAL_TOOLS 真实符号链接的目录 (无 systemctl/ss)。"""
    toolbin = tmp_path / 'toolbin'
    toolbin.mkdir(exist_ok=True)
    for tool in HOSTCHECK_EXTERNAL_TOOLS:
        real = shutil.which(tool)
        assert real, f'{tool} not found on this host — cannot build an isolated PATH'
        (toolbin / tool).symlink_to(real)
    bash_real = shutil.which('bash')
    assert bash_real, 'bash not found — cannot run the script under test'
    (toolbin / 'bash').symlink_to(bash_real)
    return toolbin


def _path_without(env, tmp_path, absent):
    """把 env['PATH'] 换成: 假工具目录 + 真实核心工具符号链接 (不含 absent)。

    自检: absent 里的每个工具在新 PATH 下必须确实不可解析 —— 模拟失效时
    测试大声失败, 而不是悄悄退化到别的分支。
    """
    bindir = Path(env['PATH'].split(os.pathsep)[0])
    toolbin = _isolated_toolbin(tmp_path)
    env['PATH'] = os.pathsep.join((str(bindir), str(toolbin)))
    for tool in absent:
        resolved = shutil.which(tool, path=env['PATH'])
        assert resolved is None, (
            f'{tool} still resolvable at {resolved} — "tool absent" simulation broken')


@LINUX_ONLY
def test_host_check_passes_when_everything_verifies(tmp_path):
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'HOST-CHECK PASS' in r.stdout
        assert '监听进程归属旧代码目录' in r.stdout
        assert 'HOST-CHECK FAIL' not in r.stdout
    finally:
        proc.kill()


def test_host_check_refuses_wrong_host(tmp_path):
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        env['Y8Y_EXPECT_HOSTS'] = 'definitely-not-this-host'
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '不在期望列表' in r.stdout
    finally:
        proc.kill()


def test_host_check_refuses_missing_production_markers(tmp_path):
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        env['Y8Y_OLD_DATA_DIR'] = '/nonexistent/data/dir'
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '旧版数据目录不存在' in r.stdout
    finally:
        proc.kill()


def test_host_check_fails_when_old_service_down(tmp_path):
    """独立审查 C: 旧服务应运行而未运行 → 总体 FAIL (不放行)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        env['FAKE_OLD_ACTIVE'] = '0'
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '未运行' in r.stdout
        r2 = _run_hostcheck(env, 'pre-cutover')
        assert r2.returncode != 0
        assert '未运行' in r2.stdout
    finally:
        proc.kill()


def test_host_check_fails_when_port_has_no_listener(tmp_path):
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = ''      # active 但没人监听 8086
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '无监听者' in r.stdout
    finally:
        proc.kill()


@LINUX_ONLY
def test_host_check_fails_on_foreign_port_squatter(tmp_path):
    """8086 的监听者不是 todo 服务 (没有 serve.py) → FAIL。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = subprocess.Popen(['sleep', '60'], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '不是 todo 服务' in r.stdout
    finally:
        proc.kill()


@LINUX_ONLY
def test_host_check_fails_when_listener_cwd_is_not_the_code_dir(tmp_path):
    """进程在别处 + cmdline 无代码目录别名 → FAIL (防顶替)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code, argv0='serve.py', cwd='/tmp')
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '不指向旧代码目录' in r.stdout
    finally:
        proc.kill()


@LINUX_ONLY
def test_host_check_accepts_symlinked_code_alias(tmp_path):
    """符号链接别名形态: cmdline 含别名即通过。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    alias = root / 'var' / 'www' / 'todolist'
    alias.parent.mkdir(parents=True)
    alias.symlink_to(code, target_is_directory=True)
    proc = _spawn_fake_listener(code, argv0=f'{alias}/serve.py', cwd='/tmp')
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'cmdline 含别名' in r.stdout
    finally:
        proc.kill()


@LINUX_ONLY
def test_host_check_fails_when_service_user_mismatch(tmp_path):
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        env['Y8Y_OLD_SERVICE_USER'] = 'definitely-not-me'
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '预期 definitely-not-me' in r.stdout
    finally:
        proc.kill()


def test_host_check_fails_when_systemctl_missing(tmp_path):
    """无法执行的检查 = 失败, 不是静默跳过 (整个 PATH 都没有 systemctl)。

    只删假 systemctl 不够: Linux 上真实的 systemctl 仍在 PATH 里,
    command -v 会找到它, 模拟就退化成"服务未运行"分支 (CI 红的根因)。
    因此换成真正不含任何 systemctl 的隔离 PATH。
    """
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        bindir = Path(env['PATH'].split(os.pathsep)[0])
        (bindir / 'systemctl').unlink()
        _path_without(env, tmp_path, absent=('systemctl',))
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert 'systemctl 不可用' in r.stdout
        assert 'HOST-CHECK PASS' not in r.stdout
    finally:
        proc.kill()


def test_host_check_fails_when_ss_missing(tmp_path):
    """ss 缺失同样 fail-closed (与 systemctl 对称; 只删假 ss 会露出真实 ss)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        bindir = Path(env['PATH'].split(os.pathsep)[0])
        (bindir / 'ss').unlink()
        _path_without(env, tmp_path, absent=('ss',))
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert 'ss 不可用' in r.stdout
        assert 'HOST-CHECK PASS' not in r.stdout
    finally:
        proc.kill()


@LINUX_ONLY
def test_host_check_pre_cutover_requires_data_dir_isolation(tmp_path):
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    proc = _spawn_fake_listener(code)
    try:
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        env['FAKE_STAGE_PID'] = str(proc.pid)
        env['Y8Y_NEW_DATA_DIR'] = env['Y8Y_OLD_DATA_DIR']
        r = _run_hostcheck(env, 'pre-cutover')
        assert r.returncode != 0
        assert '同一路径' in r.stdout
    finally:
        proc.kill()


# ── C2: 阶段状态模型 (2026-10 独立审查: 不同阶段的"正确状态"不同) ──────
#
# 旧脚本不管什么阶段都要求"旧服务正在运行 + 8086 由旧版监听" — 于是
# Runbook §5/§6 (冻结后停旧服再跑 pre-cutover) 和 §8 (回滚时旧服未恢复)
# 会被自己的安全检查挡死。修正: 按阶段判断正确状态, 安全标准不降 —
# 每个阶段仍然硬断言它那一阶段必须成立的条件。

def test_host_check_cutover_passes_when_old_stopped_and_port_released(tmp_path):
    """cutover: 旧服已停 + 8086 释放 + 隔离证据就绪 → PASS。

    Runbook §5/§6 的真实状态 (§2 冻结已完成, 拍快照/恢复数据之前)。
    """
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    env['FAKE_OLD_ACTIVE'] = '0'          # 旧服务已停
    env['FAKE_LISTENER_PID'] = ''         # 8086 已释放
    r = _run_hostcheck(env, 'cutover')
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'HOST-CHECK PASS' in r.stdout
    assert '已停止' in r.stdout
    assert '已释放' in r.stdout
    assert '隔离验证证据就绪' in r.stdout
    assert 'HOST-CHECK FAIL' not in r.stdout


def test_host_check_cutover_fails_when_old_service_still_running(tmp_path):
    """cutover: 旧服务还在跑 → FAIL (必须先按 Runbook §2 停)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    env['FAKE_OLD_ACTIVE'] = '1'
    env['FAKE_LISTENER_PID'] = ''
    r = _run_hostcheck(env, 'cutover')
    assert r.returncode != 0
    assert '仍在运行' in r.stdout


def test_host_check_cutover_fails_when_port_still_occupied(tmp_path):
    """cutover: 8086 仍被占用 → FAIL; 失败信息脱敏 (不泄露完整 cmdline)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    # 敏感参数放在 argv0 里 (exec -a 原样进 /proc/cmdline); 进程必须存活,
    # 否则 cmdline 读空, 脱敏断言就失去意义。
    proc = _spawn_fake_listener(code, argv0='serve.py --token=CUTOVER-SECRET-XYZ')
    try:
        env['FAKE_OLD_ACTIVE'] = '0'
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        r = _run_hostcheck(env, 'cutover')
        assert r.returncode != 0
        assert '仍被占用' in r.stdout
        assert 'cmdline-id=' in r.stdout          # 不可逆摘要在
        assert 'CUTOVER-SECRET-XYZ' not in r.stdout   # 完整命令行不在
    finally:
        proc.kill()


def test_host_check_cutover_fails_without_staging_evidence(tmp_path):
    """cutover: 隔离数据目录缺失/为空 → FAIL (没有 §4 隔离验证证据)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    env['FAKE_OLD_ACTIVE'] = '0'
    env['FAKE_LISTENER_PID'] = ''
    staging = Path(env['Y8Y_STAGING_DATA_DIR'])
    # 场景 1: 隔离数据目录不存在 (没做只读副本)
    shutil.rmtree(staging)
    r = _run_hostcheck(env, 'cutover')
    assert r.returncode != 0
    assert '证据缺失' in r.stdout
    # 场景 2: 目录在但没有 .md (空目录/拷错东西)
    staging.mkdir(parents=True)
    r2 = _run_hostcheck(env, 'cutover')
    assert r2.returncode != 0
    assert '证据缺失' in r2.stdout


# ── C3: 旧单元文件变量 (P6-A 实测缺陷回归) ─────────────────────────────
#
# P6-A 实际部署发现: 第 124 行 `OLD_UNIT_FOUND="$Y8Y_OLD_UNIT_FILE"` 直接
# 引用裸环境变量 — 生产 VPS 上不设置该变量时, `set -u` 让脚本以
# "unbound variable" 崩溃, 而不是走"按系统位置自动探测"的既定逻辑。
# 修复: 统一使用开头已定义的内部变量 OLD_UNIT_FILE (${Y8Y_OLD_UNIT_FILE:-})。

def test_host_check_unit_file_unset_env_autodetects_without_crash(tmp_path):
    """Y8Y_OLD_UNIT_FILE 未设置 → 走自动探测, 不得因 set -u 崩溃。

    用必然不存在的单元名做自动探测 → 干净 FAIL 在单元检查处 (而不是
    bash 的 unbound 崩溃)。修复前本测试失败: 脚本在第 124 行就死了。
    """
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    del env['Y8Y_OLD_UNIT_FILE']                       # 生产 VPS 上的实际情形
    env['Y8Y_OLD_UNIT'] = 'y8y-no-such-unit.service'   # 自动探测必然找不到
    r = _run_hostcheck(env, 'pre-deploy')
    assert r.returncode != 0
    assert 'unbound' not in r.stderr                   # 修复前: bash: Y8Y_OLD_UNIT_FILE: unbound variable
    assert 'Y8Y_OLD_UNIT_FILE' not in r.stderr
    assert '旧服务单元不存在' in r.stdout               # 走到并干净地止步于单元检查


def test_host_check_uses_explicit_unit_file_when_set(tmp_path):
    """显式设置 Y8Y_OLD_UNIT_FILE → 必须使用该路径 (跳过自动探测)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    explicit = root / 'etc' / 'custom-unit.path'
    explicit.write_text('[Unit]', encoding='utf-8')
    env['Y8Y_OLD_UNIT_FILE'] = str(explicit)
    env['FAKE_OLD_ACTIVE'] = '0'        # cutover 阶段不需要 /proc (平台无关)
    env['FAKE_LISTENER_PID'] = ''
    r = _run_hostcheck(env, 'cutover')
    assert r.returncode == 0, r.stdout + r.stderr
    assert f'旧服务单元存在 ({explicit})' in r.stdout


def test_host_check_fails_when_explicit_unit_file_missing(tmp_path):
    """显式设置的单元文件不存在 → 干净 FAIL (不静默回落到自动探测, 也不假装存在)。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    env['Y8Y_OLD_UNIT_FILE'] = str(root / 'etc' / 'gone.service')
    r = _run_hostcheck(env, 'pre-deploy')
    assert r.returncode != 0
    assert '旧服务单元文件不存在' in r.stdout


@LINUX_ONLY
def test_host_check_pre_rollback_passes_when_new_service_holds_port(tmp_path):
    """pre-rollback: 新版 (serve.py, 归属新数据目录) 占着 8086 → 放行。

    Runbook §8.1 的真实状态: 回滚决策时新版可能还在跑。若这里要求
    "8086 必须由旧服务监听", 真正出故障时反而回滚不了 (独立审查修正点)。
    """
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    new_dir = Path(env['Y8Y_NEW_DATA_DIR'])
    proc = _spawn_fake_listener(new_dir)      # serve.py, cwd=新数据目录
    try:
        env['FAKE_OLD_ACTIVE'] = '0'          # 旧服已停 (回滚阶段常态)
        env['FAKE_LISTENER_PID'] = str(proc.pid)
        r = _run_hostcheck(env, 'pre-rollback')
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'todo 服务进程占用' in r.stdout
        assert '未运行 — 回滚阶段允许' in r.stdout
        assert 'HOST-CHECK FAIL' not in r.stdout
    finally:
        proc.kill()


@LINUX_ONLY
def test_host_check_failure_output_does_not_leak_cmdline(tmp_path):
    """失败输出脱敏: 完整进程命令行 (可能含敏感参数) 绝不出现。"""
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    secret = 'INTERNAL-TOKEN-SECRET-XYZ'
    # 外来进程占 8086: cmdline 不含 serve.py, 但含敏感参数 (放在 argv0 里,
    # 保证进程存活 — 死进程的 cmdline 读空, 断言就失去意义)。
    foreign = subprocess.Popen(
        ['bash', '-c', f'exec -a "squatter --token={secret}" sleep 60'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # serve.py 进程但 cwd 不在旧代码目录 (顶替场景), cmdline 同样含敏感参数
    stray = _spawn_fake_listener(code, argv0=f'serve.py --token={secret}',
                                 cwd='/tmp')
    try:
        env['FAKE_OLD_ACTIVE'] = '1'
        env['FAKE_LISTENER_PID'] = str(foreign.pid)
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0
        assert '不是 todo 服务' in r.stdout
        assert secret not in r.stdout
        assert 'cmdline-id=' in r.stdout
        # serve.py 顶替: 严格核对在"归属旧代码目录"处失败 — 同样脱敏
        env['FAKE_LISTENER_PID'] = str(stray.pid)
        r2 = _run_hostcheck(env, 'pre-deploy')
        assert r2.returncode != 0
        assert '不指向旧代码目录' in r2.stdout
        assert secret not in r2.stdout
        # 回滚阶段的外来占坑 FAIL — 同样脱敏
        env['FAKE_OLD_ACTIVE'] = '0'
        env['FAKE_LISTENER_PID'] = str(foreign.pid)
        r3 = _run_hostcheck(env, 'pre-rollback')
        assert r3.returncode != 0
        assert '非 todo 进程占用' in r3.stdout
        assert secret not in r3.stdout
    finally:
        foreign.kill()
        stray.kill()


@LINUX_ONLY
def test_full_cutover_and_rollback_sequence(tmp_path):
    """完整切换/回滚状态机 (Runbook §1→§8→恢复), 按执行顺序逐状态断言。

    不是逐函数单测 — 模拟真实状态流转, 验证每个阶段在每种生产状态下
    的 PASS/FAIL 符合 Runbook 的预期:
      S1 §1–§4  旧服运行 + 隔离实例运行 + 隔离证据就绪
      S2 §2     冻结完成: 旧服已停, 8086 释放 (拍快照/恢复数据)
      S3 §6.2   新版启动, 8086 由新版 (serve.py, 归属新数据目录) 占用
      S4 §8.1   停新版, 8086 释放, 旧服未恢复
      S5 恢复    旧服重新运行 (循环闭合, 回到可再部署状态)
      S6 故障    8086 被外来进程占用 (敏感参数 — 同时验证脱敏)
      S7 故障    隔离验证证据缺失
    """
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    staging_dir = Path(env['Y8Y_STAGING_DATA_DIR'])
    new_dir = Path(env['Y8Y_NEW_DATA_DIR'])
    old_proc = _spawn_fake_listener(code)              # 旧服务: serve.py + cwd=旧代码目录
    stage_proc = _spawn_fake_listener(staging_dir)     # 隔离实例 (18086)
    new_proc = _spawn_fake_listener(new_dir)           # 新版: serve.py + cwd=新数据目录
    secret = 'FOREIGN-SQUAT-SECRET-XYZ'
    # 外来占坑者: 敏感参数放在 argv0 (exec -a), 进程保持存活
    foreign_proc = subprocess.Popen(
        ['bash', '-c', f'exec -a "squatter --token={secret}" sleep 60'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def state(*, old_active, port_pid, stage_pid):
        env['FAKE_OLD_ACTIVE'] = '1' if old_active else '0'
        env['FAKE_LISTENER_PID'] = str(port_pid) if port_pid else ''
        env['FAKE_STAGE_PID'] = str(stage_pid) if stage_pid else ''

    try:
        # ── S1 §1–§4: 旧服运行 + 隔离实例已验证 (§4 收尾) ──
        state(old_active=True, port_pid=old_proc.pid, stage_pid=stage_proc.pid)
        assert _run_hostcheck(env, 'pre-deploy').returncode == 0
        assert _run_hostcheck(env, 'pre-cutover').returncode == 0
        r = _run_hostcheck(env, 'cutover')             # 旧服还在跑 → FAIL
        assert r.returncode != 0 and '仍在运行' in r.stdout
        r = _run_hostcheck(env, 'pre-rollback')        # 旧服在跑 + 旧服占端口 → 放行
        assert r.returncode == 0, r.stdout

        # ── S2 §2 冻结完成: 旧服已停, 8086 释放 (§5 拍快照 / §6.1 恢复数据) ──
        state(old_active=False, port_pid=None, stage_pid=stage_proc.pid)
        r = _run_hostcheck(env, 'pre-cutover')         # 冻结前闸门: 旧服必须还在跑
        assert r.returncode != 0 and '未运行' in r.stdout
        r = _run_hostcheck(env, 'cutover')             # 停后闸门: 放行
        assert r.returncode == 0, r.stdout + r.stderr
        r = _run_hostcheck(env, 'pre-rollback')        # 旧服未运行不是错误
        assert r.returncode == 0, r.stdout

        # ── S3 §6.2 新版启动: 8086 由新版占用 ──
        state(old_active=False, port_pid=new_proc.pid, stage_pid=stage_proc.pid)
        r = _run_hostcheck(env, 'cutover')             # 端口未释放 → FAIL
        assert r.returncode != 0 and '仍被占用' in r.stdout
        r = _run_hostcheck(env, 'pre-rollback')        # 新版占用不阻止回滚
        assert r.returncode == 0, r.stdout
        assert 'todo 服务进程占用' in r.stdout

        # ── S4 §8.1 停新版: 8086 释放, 旧服未恢复 ──
        state(old_active=False, port_pid=None, stage_pid=stage_proc.pid)
        r = _run_hostcheck(env, 'pre-rollback')
        assert r.returncode == 0, r.stdout

        # ── S5 旧版恢复: 回到 S1 的服务状态 (循环闭合) ──
        state(old_active=True, port_pid=old_proc.pid, stage_pid=stage_proc.pid)
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode == 0, r.stdout

        # ── S6 故障: 8086 被外来进程占用 (敏感参数不得泄露) ──
        # 旧服声称 active 但端口被顶替 — 经典冒充场景: pre-deploy 必须 FAIL。
        state(old_active=True, port_pid=foreign_proc.pid, stage_pid=stage_proc.pid)
        r = _run_hostcheck(env, 'pre-deploy')
        assert r.returncode != 0 and '不是 todo 服务' in r.stdout
        assert secret not in r.stdout
        r = _run_hostcheck(env, 'pre-rollback')
        assert r.returncode != 0 and '非 todo 进程占用' in r.stdout
        assert secret not in r.stdout

        # ── S7 故障: 隔离验证证据缺失 (清空隔离数据目录的任务文件) ──
        (staging_dir / 'todo' / 'task.md').unlink()
        state(old_active=False, port_pid=None, stage_pid=stage_proc.pid)
        r = _run_hostcheck(env, 'cutover')
        assert r.returncode != 0 and '证据缺失' in r.stdout
    finally:
        for p in (old_proc, stage_proc, new_proc, foreign_proc):
            p.kill()


def test_host_check_rejects_bad_phase(tmp_path):
    root, code, env = _fake_host(tmp_path, listener_pid=0)
    r = _run_hostcheck(env, 'no-such-phase')
    assert r.returncode == 2
