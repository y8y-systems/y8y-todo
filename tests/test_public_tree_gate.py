"""公开树隐私门（`scripts/check-public-tree.sh` → `check-public-tree.py`）的回归测试。

四件事必须一直成立，少一件这道门就等于没有：

  1. **真的接在 CI 上**，而且是 `gate / pytest` 的上游 —— 否则它红了 PR 照样
     合得进去，门形同虚设（这正是它长期没被运行的原因）。
  2. **真的会失败** —— 种一个违规文件，退出码必须非 0。
  3. **输出不含任何敏感值** —— 它跑在**公开的** CI 日志里。命中的**内容**
     永远不打印；**文件名**本身带敏感信息时也不能打印。
  4. **扫描器自己坏掉时必须红** —— 扫描没跑起来却报 CLEAN 是最坏的失败模式。

机器级（push 前）的隐私检查不在本仓库，见 `docs/development.md` 的说明。
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'check-public-tree.sh'
IMPL = ROOT / 'scripts' / 'check-public-tree.py'
WORKFLOW = ROOT / '.github' / 'workflows' / 'pytest.yml'

# 样例「敏感串」必须**拆开拼** —— 否则本文件自己就被门当成违规扫到了
# （门扫的是整个公开树，包括测试文件自己）。
_SECRET_EMAIL = 'nobody.here@' + 'gmail' + '.com'
_SECRET_KEY = '-----BEGIN RSA ' + 'PRIVATE KEY-----'
_SECRET_TOKEN = 'ghp_' + 'c' * 36
_SECRET_AWS = 'AKIA' + '0123456789ABCDEF'
# 家目录路径也要拆 —— 否则本文件自己就被门当成违规扫到了
_HOME_PATH = '/Users/' + 'someone/x'
_E2E_HOME = '/home/' + 'e2euser/y8y-data'


def _job_block(text, job_id):
    """取 workflow 文本里某个 job 的整块（job id 缩进 2 空格）。

    纯文本解析 —— 本仓的 CI 脚本也是这么做的（不引 pyyaml）。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line == f'  {job_id}:':
            start = i
            break
    assert start is not None, f'job {job_id!r} not found in workflow'
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip():
            continue
        if line.startswith('  ') and not line.startswith('   ') \
                and line.rstrip().endswith(':'):
            end = j
            break
    return '\n'.join(lines[start:end])


def _needs_of(block):
    for line in block.splitlines():
        if line.startswith('    needs:'):
            raw = line.split(':', 1)[1].strip()
            return [n.strip() for n in raw.strip('[]').split(',') if n.strip()]
    return []


def _make_tree(tmp_path):
    """搭一棵能独立跑的最小树。

    脚本按自身位置推导 ROOT，所以 **.sh 和 .py 都要复制过去**（.sh 只是
    wrapper，逻辑在 .py 里）。
    """
    root = tmp_path / 'tree'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(SCRIPT, root / 'scripts' / 'check-public-tree.sh')
    shutil.copy2(IMPL, root / 'scripts' / 'check-public-tree.py')
    baseline = root / 'rehearsal' / 'legacy-baseline'
    baseline.mkdir(parents=True)
    payload = baseline / 'payload.txt'
    payload.write_text('baseline\n', encoding='utf-8')
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    (baseline / 'MANIFEST.sha256').write_text(
        f'{digest}  payload.txt\n', encoding='utf-8')
    return root


def _run(root, env_extra=None):
    env = dict(os.environ)
    env.pop('Y8Y_DENY_LIST', None)
    env.update(env_extra or {})
    return subprocess.run(['bash', str(root / 'scripts' / 'check-public-tree.sh')],
                          cwd=str(root), capture_output=True, text=True,
                          env=env, timeout=120)


# ── 1. 真的接在 CI 上 ──────────────────────────────────────────────────

def test_privacy_gate_is_executed_by_ci():
    """CI 里必须有一个 job 真的执行这道门。"""
    text = WORKFLOW.read_text(encoding='utf-8')
    assert 'scripts/check-public-tree.sh' in text, \
        '公开树隐私门没有出现在任何 CI workflow 里 —— 它必须真的跑起来'


def test_privacy_gate_blocks_the_gate_job():
    """门必须是 `gate / pytest` 的上游 —— 否则它红了也拦不住 PR。"""
    text = WORKFLOW.read_text(encoding='utf-8')
    gate = _job_block(text, 'gate-pytest')

    runner_ids = []
    for line in text.splitlines():
        if line.startswith('  ') and not line.startswith('   ') \
                and line.rstrip().endswith(':'):
            job_id = line.strip().rstrip(':')
            if job_id.startswith('gate-'):
                continue
            block = _job_block(text, job_id)
            if 'scripts/check-public-tree.sh' in block:
                runner_ids.append(job_id)
    assert runner_ids, '找不到执行隐私门的 job'

    needs = _needs_of(gate)
    for job_id in runner_ids:
        assert job_id in needs, \
            f'隐私门跑在 job {job_id!r} 里，但它不在 gate / pytest 的 needs 里 —— ' \
            f'门红了 PR 照样合得进去'
        assert f'needs.{job_id}.result' in gate, \
            f'gate / pytest 没有读取 {job_id!r} 的 result'
        assert f'{job_id}=' in gate, \
            f'gate / pytest 没有把 {job_id!r} 的 result 传进 CI_GATE_UPSTREAM'


def test_gate_job_uses_always():
    """`if: always()` 少了的话，上游失败时门会被 skip —— 而 skipped 被 GitHub
    当成「已满足 Required Check」，门形同虚设。"""
    text = WORKFLOW.read_text(encoding='utf-8')
    gate = _job_block(text, 'gate-pytest')
    assert 'if: always()' in gate, 'gate / pytest 缺少 if: always()'


# ── 2. 真的会失败 ──────────────────────────────────────────────────────

def test_privacy_gate_passes_on_the_real_tree():
    """仓库本身就是干净的 —— 这道门在本仓上必须绿。"""
    r = _run(ROOT)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert 'PUBLIC-TREE CHECK: CLEAN' in r.stdout


def test_privacy_gate_fails_on_planted_violation(tmp_path):
    """种一个私人邮箱，门必须红。"""
    root = _make_tree(tmp_path)
    (root / 'leak.md').write_text(
        'reach me at ' + _SECRET_EMAIL + '\n', encoding='utf-8')

    r = _run(root)
    assert r.returncode != 0, '门没有拦住私人邮箱'
    assert 'PUBLIC-TREE CHECK: FAILURES' in r.stdout
    assert 'personal mail provider' in r.stdout, r.stdout[-2000:]
    # 要报位置: 文件 + 行号
    assert 'leak.md' in r.stdout, r.stdout[-2000:]
    assert 'line=1' in r.stdout, r.stdout[-2000:]


def test_privacy_gate_fails_on_private_key_material(tmp_path):
    """种一段私钥素材，门必须红。"""
    root = _make_tree(tmp_path)
    (root / 'key.pem').write_text(_SECRET_KEY + '\n', encoding='utf-8')

    r = _run(root)
    assert r.returncode != 0, '门没有拦住私钥素材'
    assert 'private key material' in r.stdout, r.stdout[-2000:]


def test_privacy_gate_fails_on_github_token(tmp_path):
    """种一个 GitHub token，门必须红。"""
    root = _make_tree(tmp_path)
    (root / 'cfg.py').write_text(
        'TOKEN = "' + _SECRET_TOKEN + '"\n', encoding='utf-8')

    r = _run(root)
    assert r.returncode != 0, '门没有拦住 GitHub token'
    assert 'github token' in r.stdout, r.stdout[-2000:]


def test_privacy_gate_fails_on_developer_home_path(tmp_path):
    """开发者家目录路径 —— 门最原始的那条规则，不能退化。"""
    root = _make_tree(tmp_path)
    (root / 'p.py').write_text('HOME = "' + _HOME_PATH + '"\n', encoding='utf-8')

    r = _run(root)
    assert r.returncode != 0, '门没有拦住开发者家目录路径'
    assert 'developer home path' in r.stdout, r.stdout[-2000:]


# ── 3. 输出脱敏 ────────────────────────────────────────────────────────

def test_privacy_gate_output_never_prints_the_match(tmp_path):
    """公开 CI 日志里不能出现命中的原文。"""
    root = _make_tree(tmp_path)
    (root / 'leak.md').write_text(
        'reach me at ' + _SECRET_EMAIL + '\n', encoding='utf-8')
    (root / 'key.pem').write_text(_SECRET_KEY + '\n', encoding='utf-8')
    (root / 'aws.txt').write_text(_SECRET_AWS + '\n', encoding='utf-8')
    (root / 'tok.txt').write_text('T = "' + _SECRET_TOKEN + '"\n',
                                  encoding='utf-8')

    r = _run(root)
    blob = r.stdout + r.stderr
    for secret in (_SECRET_EMAIL, _SECRET_KEY, _SECRET_AWS, _SECRET_TOKEN):
        assert secret not in blob, f'门的输出泄漏了命中的原文: {secret!r}'


def test_privacy_gate_output_never_prints_sensitive_file_name(tmp_path):
    """**文件名**本身带敏感信息时也不能打印出来。

    这是文件级扫描和文件**名**扫描的区别 —— 名字里带邮箱的探针，
    输出里只能看到打码后的样子。
    """
    root = _make_tree(tmp_path)
    (root / ('probe-' + _SECRET_EMAIL + '.txt')).write_text(
        'nothing to see\n', encoding='utf-8')

    r = _run(root)
    blob = r.stdout + r.stderr
    assert r.returncode != 0, '带敏感信息的文件名必须被拦'
    assert 'email-like file name' in r.stdout, r.stdout[-2000:]
    assert _SECRET_EMAIL not in blob, '敏感**文件名**被打印了'
    # 本地部分也不该出现
    assert 'nobody.here' not in blob, '文件名的可识别部分被打印了'


def test_privacy_gate_output_never_prints_sensitive_file_name_in_repo(tmp_path):
    """在真实仓里种一个带邮箱的文件名 —— 同样不能出现在输出里。"""
    planted = ROOT / ('privacy-probe-' + _SECRET_EMAIL + '.txt')
    planted.write_text('nothing to see\n', encoding='utf-8')
    try:
        r = _run(ROOT)
        blob = r.stdout + r.stderr
        assert r.returncode != 0, '带敏感信息的文件名必须被拦'
        assert 'email-like file name' in r.stdout, r.stdout[-2000:]
        assert _SECRET_EMAIL not in blob
        assert 'nobody.here' not in blob
    finally:
        planted.unlink(missing_ok=True)


def test_privacy_gate_redacts_deny_list_pattern(tmp_path):
    """deny-list 里的 pattern 本身可能就是私人邮箱 —— 也不能进公开日志。"""
    root = _make_tree(tmp_path)
    (root / 'notes.md').write_text(
        'the boss mailbox is boss@private-company.example\n', encoding='utf-8')

    deny = tmp_path / 'deny.txt'
    deny.write_text('boss@private-company\\.example\n', encoding='utf-8')

    r = _run(root, env_extra={'Y8Y_DENY_LIST': str(deny)})
    blob = r.stdout + r.stderr
    assert r.returncode != 0, 'deny-list 命中时门必须红'
    assert 'deny-list pattern #1' in r.stdout, r.stdout[-2000:]
    assert 'notes.md' in r.stdout, r.stdout[-2000:]
    assert 'boss@private-company.example' not in blob, '命中的原文被打印了'
    assert 'boss@private-company\\.example' not in blob, \
        'deny-list 的 pattern 被打印了'


def test_privacy_gate_does_not_print_invalid_deny_list_pattern(tmp_path):
    """pattern 写坏了也不能把原文打出来（它本身可能就是私人邮箱）。"""
    root = _make_tree(tmp_path)
    (root / 'notes.md').write_text('nothing to see\n', encoding='utf-8')

    deny = tmp_path / 'deny.txt'
    deny.write_text('boss@private-company(\n', encoding='utf-8')

    r = _run(root, env_extra={'Y8Y_DENY_LIST': str(deny)})
    blob = r.stdout + r.stderr
    assert r.returncode != 0, '无效 pattern 必须让门红'
    assert '不是合法正则' in r.stdout, r.stdout[-2000:]
    assert 'boss@private-company(' not in blob, '无效 pattern 的原文被打印了'


def test_privacy_gate_handles_weird_file_names(tmp_path):
    """文件名带冒号 / 换行 / 特殊字符时不能崩，也不能把路径切错。"""
    root = _make_tree(tmp_path)
    weird = [
        'probe:colon.txt',
        'probe\nnewline.txt',
        'probe space.txt',
        'probe"quote.txt',
        "probe'quote.txt",
    ]
    for name in weird:
        (root / name).write_text(
            'reach me at ' + _SECRET_EMAIL + '\n', encoding='utf-8')

    r = _run(root)
    blob = r.stdout + r.stderr
    assert r.returncode != 0, '违规必须被拦'
    assert 'Traceback' not in blob, '扫描器崩了: ' + blob[-2000:]
    assert _SECRET_EMAIL not in blob
    # 换行不能把一行输出裂成多行 —— 每条发现都该是 "path=… line=…"
    for line in r.stdout.splitlines():
        if line.strip().startswith('path='):
            assert '\n' not in line


# ── 4. 扫描器坏掉时必须红 ──────────────────────────────────────────────

def test_privacy_gate_fails_closed_when_scan_fails(tmp_path):
    """扫描命令失败 → 门必须失败。

    旧版 shell 实现里 `hit` 写在 $(...) 子 shell 里会丢掉 fail=1 ——
    扫描失败时门会误报 CLEAN。现在必须红。
    """
    stub_dir = tmp_path / 'stub-bin'
    stub_dir.mkdir()
    stub = stub_dir / 'git'
    stub.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        f'  rev-parse) echo "{ROOT}" ;;\n'
        '  ls-files) echo "boom" >&2; exit 3 ;;\n'
        'esac\n'
        'exit 0\n', encoding='utf-8')
    os.chmod(stub, 0o755)

    env = dict(os.environ)
    env['PATH'] = f'{stub_dir}{os.pathsep}{env.get("PATH", "")}'
    r = subprocess.run(['bash', str(SCRIPT)], cwd=str(ROOT),
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode != 0, '扫描器失败时门必须红（不能报 CLEAN）'
    assert 'CLEAN' not in r.stdout, r.stdout[-2000:]
    assert 'FAIL' in r.stdout, r.stdout[-2000:]


def test_privacy_gate_reports_scan_failure_without_leaking(tmp_path):
    """扫描失败的报错本身也不能带敏感值。"""
    stub_dir = tmp_path / 'stub-bin2'
    stub_dir.mkdir()
    stub = stub_dir / 'git'
    stub.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        f'  rev-parse) echo "{ROOT}" ;;\n'
        f'  ls-files) echo "token {os.environ.get("_SECRET_TOKEN", "")}" >&2; '
        'exit 3 ;;\n'
        'esac\n'
        'exit 0\n', encoding='utf-8')
    os.chmod(stub, 0o755)

    env = dict(os.environ)
    env['PATH'] = f'{stub_dir}{os.pathsep}{env.get("PATH", "")}'
    r = subprocess.run(['bash', str(SCRIPT)], cwd=str(ROOT),
                       capture_output=True, text=True, env=env, timeout=120)
    blob = r.stdout + r.stderr
    assert r.returncode != 0
    assert _SECRET_TOKEN not in blob, '扫描失败的报错里带出了敏感值'


# ── 扫描范围 ────────────────────────────────────────────────────────────

def test_privacy_gate_ignores_pytest_tmp():
    """`.pytest-tmp/` 是本仓 pytest 的 --basetemp，**落在仓内**。

    它在 .gitignore 里 → 门必须不扫它，否则「跑完测试再跑门」就会误报。
    """
    d = ROOT / '.pytest-tmp'
    d.mkdir(exist_ok=True)
    planted = d / 'planted-by-test.md'
    planted.write_text('reach me at ' + _SECRET_EMAIL + '\n', encoding='utf-8')
    try:
        r = _run(ROOT)
        assert r.returncode == 0, \
            '门把 .pytest-tmp/ 当成了要发布的源码: ' + r.stdout[-2000:]
        assert 'PUBLIC-TREE CHECK: CLEAN' in r.stdout
    finally:
        planted.unlink(missing_ok=True)


def test_privacy_gate_ignores_gitignored_ci_artifacts():
    """CI 产物（pytest-output.txt）里全是 runner 的家目录路径（形如 /home/<runner>/…）。

    它在 .gitignore 里 → 门必须不扫它，否则每次 CI 都误报。
    """
    artifact = ROOT / 'pytest-output.txt'
    existed = artifact.exists()
    original = artifact.read_text(encoding='utf-8') if existed else None
    artifact.write_text('collected in /home/<runner>/work/x\n', encoding='utf-8')
    try:
        r = _run(ROOT)
        assert r.returncode == 0, \
            '门把 gitignore 掉的 CI 产物也扫了: ' + r.stdout[-2000:]
    finally:
        if existed:
            artifact.write_text(original, encoding='utf-8')
        else:
            artifact.unlink(missing_ok=True)


def test_privacy_gate_flags_untracked_but_not_ignored_file():
    """新建但**还没**被 ignore 的文件必须照扫 —— 提交前自查要拦的正是它。"""
    probe = ROOT / 'privacy-gate-probe.txt'
    probe.write_text('reach me at ' + _SECRET_EMAIL + '\n', encoding='utf-8')
    try:
        r = _run(ROOT)
        assert r.returncode != 0, '未跟踪但未 ignore 的文件必须被扫到'
        assert 'personal mail provider' in r.stdout, r.stdout[-2000:]
        assert _SECRET_EMAIL not in (r.stdout + r.stderr)
    finally:
        probe.unlink(missing_ok=True)


# ── 例外规则不能失效 ────────────────────────────────────────────────────

def test_privacy_gate_allows_the_canonical_repo_url(tmp_path):
    """本仓自己的规范 URL 是允许的（例外规则不能写反）。"""
    root = _make_tree(tmp_path)
    (root / 'README.md').write_text(
        'git clone https://github.com/y8y-systems/y8y-todo.git y8y-todo\n',
        encoding='utf-8')

    r = _run(root)
    assert r.returncode == 0, \
        '本仓自己的规范 URL 不该被拦: ' + r.stdout[-2000:]


def test_privacy_gate_allows_ci_runner_home_in_workflow(tmp_path):
    """p4 演练 workflow 自建临时 CI 用户（形如 /home/<ciuser>/）—— 例外要生效。"""
    root = _make_tree(tmp_path)
    wf = root / '.github' / 'workflows'
    wf.mkdir(parents=True)
    (wf / 'p4.yml').write_text(
        'env:\n  NEW_DATA: ' + _E2E_HOME + '\n', encoding='utf-8')

    r = _run(root)
    assert r.returncode == 0, \
        'CI 自建用户的 home 不该被拦: ' + r.stdout[-2000:]


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
