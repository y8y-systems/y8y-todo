"""CI Gate 结构性自检 —— 保证 `gate / *` 汇总 job 与各 workflow 的实际
测试 job 保持一致。

背景 (为什么需要这个测试):
  Ruleset 的 Required Status Checks 固定为 4 个稳定名字:
    gate / pytest   gate / ubuntu   gate / e2e   gate / upgrade
  内部测试 job 增删改名时, **Agent 负责同步各 gate 的 needs**, Ruleset 不动。

  但「Agent 负责」如果没有机器校验, 迟早会漏 —— 漏了就是**门悄悄失效**:
  新加的测试没被任何 gate 覆盖, 失败也拦不住合并。

  所以这里把「gate 的 needs == 该 workflow 的全部测试 job」变成一条
  **会被 CI 跑到的断言**: 漏改 → 本测试失败 → `gate / pytest` 变红 →
  PR 合不进去。

检查项:
  1. 每个 workflow 恰好一个 gate job
  2. gate 显示名符合 `gate / <slug>` 约定
  3. gate 有 `if: always()`
     (否则上游失败时 gate 被 skip, 而 GitHub 把 skipped 当成
      「已满足 Required Check」→ 门形同虚设)
  4. gate 的 needs 恰好等于该 workflow 的全部非 gate job
  5. gate 的 RESULT_* env 变量与 needs 一一对应
  6. 判定脚本 scripts/ci-gate-check.sh 存在且可执行
  7. 判定脚本的行为: failure / cancelled / skipped 都必须让门失败

第 7 条尤其重要 —— 它锁住「skipped 不得被当成通过」这个语义。
本测试只读仓库文件 + 在临时目录跑判定脚本, 不碰任何真实环境。
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_DIR = os.path.join(ROOT, ".github", "workflows")
GATE_SCRIPT = os.path.join(ROOT, "scripts", "ci-gate-check.sh")

# 稳定名称约定 —— Ruleset 里配的就是这四个
EXPECTED_GATE_NAMES = {
    "pytest.yml": "gate / pytest",
    "ubuntu-2204.yml": "gate / ubuntu",
    "linux-deploy-e2e.yml": "gate / e2e",
    "p4-upgrade-rehearsal.yml": "gate / upgrade",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _parse_workflow(text):
    """极简 YAML 解析 —— 只取我们需要的: job id 列表、每个 job 的
    name / needs / if / env。workflow 结构很规整 (缩进 2 = job,
    缩进 4 = job 的属性), 不值得为此引入 pyyaml 依赖。
    """
    lines = text.splitlines()
    jobs = {}
    in_jobs = False
    cur = None
    in_env = False
    env_indent = 0

    for raw in lines:
        if not in_jobs:
            if re.match(r"^jobs:\s*$", raw):
                in_jobs = True
            continue
        # jobs 块结束 (顶层键)
        if raw and not raw[0].isspace() and not raw.startswith("#"):
            in_jobs = False
            cur = None
            continue
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", raw)
        if m:
            cur = m.group(1)
            jobs[cur] = {"needs": [], "env": {}, "if": None, "name": None}
            in_env = False
            continue
        if cur is None:
            continue

        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()

        if indent == 4:
            in_env = False
            m = re.match(r"^name:\s*(.*)$", stripped)
            if m:
                jobs[cur]["name"] = m.group(1).strip().strip("'\"")
                continue
            m = re.match(r"^if:\s*(.*)$", stripped)
            if m:
                jobs[cur]["if"] = m.group(1).strip()
                continue
            m = re.match(r"^needs:\s*\[(.*)\]\s*$", stripped)
            if m:
                jobs[cur]["needs"] = [
                    n.strip() for n in m.group(1).split(",") if n.strip()
                ]
                continue
            continue

        # `env:` 块在 step 下面 (缩进 8), 它的键在缩进 10。
        # 用**相对缩进**判断 (比 env: 深一层就是它的键), 不写死具体数字 ——
        # workflow 里 step 的嵌套层级变了也不会误判。
        if in_env:
            if re.match(r"^env:\s*$", stripped):
                continue
            if indent > env_indent:
                m = re.match(r"^([A-Za-z0-9_]+):\s*(.*)$", stripped)
                if m:
                    jobs[cur]["env"][m.group(1)] = m.group(2).strip()
                    continue
            in_env = False

        if re.match(r"^env:\s*$", stripped):
            in_env = True
            env_indent = indent
            continue

    return jobs


def _workflow_files():
    if not os.path.isdir(WORKFLOW_DIR):
        return []
    return [
        os.path.join(WORKFLOW_DIR, f)
        for f in sorted(os.listdir(WORKFLOW_DIR))
        if f.endswith((".yml", ".yaml"))
    ]


def _gate_jobs(jobs):
    return {jid: j for jid, j in jobs.items() if jid.startswith("gate-")}


# ── 1. 每个 workflow 恰好一个 gate, 名字符合约定 ──

@pytest.mark.parametrize("wf_name,expected_name", sorted(EXPECTED_GATE_NAMES.items()))
def test_workflow_has_exactly_one_gate(wf_name, expected_name):
    path = os.path.join(WORKFLOW_DIR, wf_name)
    assert os.path.isfile(path), f"缺少 workflow: {wf_name}"
    jobs = _parse_workflow(_read(path))
    gates = _gate_jobs(jobs)
    assert len(gates) == 1, (
        f"{wf_name} 期望恰好 1 个 gate job, 实际 {len(gates)} 个: {sorted(gates)}"
    )
    gid = next(iter(gates))
    assert gates[gid]["name"] == expected_name, (
        f"{wf_name} 的 gate 显示名是 {gates[gid]['name']!r}, "
        f"期望 {expected_name!r} (Ruleset 按这个名字匹配)"
    )


# ── 2. gate 的 needs 覆盖该 workflow 的全部测试 job ──

@pytest.mark.parametrize("wf_name", sorted(EXPECTED_GATE_NAMES))
def test_gate_needs_cover_every_test_job(wf_name):
    """核心断言 —— Agent 改了内部测试 job 却忘了同步 gate 时, 这里会红。"""
    path = os.path.join(WORKFLOW_DIR, wf_name)
    jobs = _parse_workflow(_read(path))
    gates = _gate_jobs(jobs)
    assert gates, f"{wf_name} 没有 gate job"
    gid = next(iter(gates))
    gate = gates[gid]

    test_jobs = {jid for jid in jobs if not jid.startswith("gate-")}
    needs = set(gate["needs"])

    missing = sorted(test_jobs - needs)
    extra = sorted(needs - test_jobs)
    assert not missing, (
        f"{wf_name}: 测试 job {missing} 没有被 gate '{gid}' 覆盖 — "
        f"新增/删除/重命名测试 job 时, 必须同步 gate 的 needs "
        f"(见 AGENTS.md §4.1)"
    )
    assert not extra, (
        f"{wf_name}: gate '{gid}' 的 needs 里有不存在的 job {extra}"
    )


# ── 3. gate 必须有 if: always() ──

@pytest.mark.parametrize("wf_name", sorted(EXPECTED_GATE_NAMES))
def test_gate_uses_always(wf_name):
    """没有 if: always() → 上游失败时 gate 被 skip,
    而 GitHub 把 skipped 当成「已满足 Required Check」→ 门形同虚设。"""
    path = os.path.join(WORKFLOW_DIR, wf_name)
    jobs = _parse_workflow(_read(path))
    gid = next(iter(_gate_jobs(jobs)))
    cond = jobs[gid]["if"] or ""
    assert "always()" in cond, (
        f"{wf_name}: gate '{gid}' 的 if 是 {cond!r}, 必须包含 always() "
        f"(否则上游失败时 gate 会被 skip, skipped 会被 GitHub 当成满足门)"
    )


# ── 4. RESULT_* env 与 needs 一一对应 ──

@pytest.mark.parametrize("wf_name", sorted(EXPECTED_GATE_NAMES))
def test_gate_result_env_matches_needs(wf_name):
    """gate 的 env 里每个 RESULT_* 对应一个 needs 条目 —— 判定脚本靠它取值。"""
    path = os.path.join(WORKFLOW_DIR, wf_name)
    jobs = _parse_workflow(_read(path))
    gid = next(iter(_gate_jobs(jobs)))
    gate = jobs[gid]
    result_vars = {
        k[len("RESULT_"):] for k in gate["env"] if k.startswith("RESULT_")
    }
    # env 里的 RESULT_<label> 必须引用一个真实存在的 needs job
    referenced = set()
    for key, value in gate["env"].items():
        if not key.startswith("RESULT_"):
            continue
        m = re.search(r"needs\.([A-Za-z0-9_-]+)\.result", value)
        assert m, (
            f"{wf_name}: gate '{gid}' 的 env {key} 没有引用 needs.<job>.result"
        )
        referenced.add(m.group(1))
    assert referenced == set(gate["needs"]), (
        f"{wf_name}: gate '{gid}' 的 RESULT_* 引用了 {sorted(referenced)}, "
        f"但 needs 是 {sorted(gate['needs'])} — 两者必须一一对应"
    )
    assert result_vars, f"{wf_name}: gate '{gid}' 没有任何 RESULT_* env"


# ── 5. 判定脚本存在且可执行 ──

def test_gate_script_exists_and_executable():
    assert os.path.isfile(GATE_SCRIPT), "缺少 scripts/ci-gate-check.sh"
    assert os.access(GATE_SCRIPT, os.X_OK), (
        "scripts/ci-gate-check.sh 不可执行 (chmod +x)"
    )


# ── 6. 判定脚本的行为: failure / cancelled / skipped 都必须让门失败 ──

@pytest.mark.parametrize(
    "upstream,expected_rc",
    [
        ("a=success", 0),
        ("a=success b=success", 0),
        ("a=failure", 1),
        ("a=cancelled", 1),
        # 关键: skipped 绝不能被当成通过
        ("a=skipped", 1),
        ("a=success b=skipped", 1),
        ("a=success b=failure", 1),
    ],
)
def test_gate_script_behavior(upstream, expected_rc):
    env = dict(os.environ, CI_GATE_UPSTREAM=upstream)
    proc = subprocess.run(
        ["bash", GATE_SCRIPT],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert proc.returncode == expected_rc, (
        f"CI_GATE_UPSTREAM={upstream!r} → exit {proc.returncode}, "
        f"期望 {expected_rc}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


def test_gate_script_fails_closed_when_unset():
    """判定不了 → 必须失败, 不能默认通过。"""
    env = {k: v for k, v in os.environ.items() if k != "CI_GATE_UPSTREAM"}
    proc = subprocess.run(
        ["bash", GATE_SCRIPT],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )
    assert proc.returncode != 0, (
        "CI_GATE_UPSTREAM 未设置时必须失败 (Fail Closed), 实际通过了"
    )
