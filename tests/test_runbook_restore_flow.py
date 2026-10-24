"""P5.5: the Runbook §6.1 safe-restore flow, rehearsed on fictional data.

Covers the independent-review findings D (mis-deletion risk) and B
(verify the backup BEFORE moving the target):
- the snapshot is fully verified (archive structure + fileset + per-file
  sha256) in an isolated temp dir BEFORE the restore target is touched —
  a corrupted archive or a wrong manifest must leave an existing target
  byte-identical (not moved, not overwritten, not replaced by a partial
  directory)
- first run into a missing target restores from the snapshot
- re-run with a non-empty target preserves first (mv), never deletes
- re-run with an existing preserve copy STOPS instead of eating data
- interrupted state (target gone, snapshot intact) re-runs cleanly
- the OLD production directory is never touched at any point
All data is fictional (legacy fixture); no real host is involved.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
SAFE_RESTORE = SRC / 'scripts' / 'p5-safe-restore.sh'
REHEARSAL = SRC / 'scripts' / 'p4-rehearsal.py'
PY = sys.executable


def _tree_hashes(root):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _fixture(tmp_path):
    sys.path.insert(0, str(SRC / 'tests'))
    from legacy_fixture import build_legacy_data
    data = tmp_path / 'old-data'
    build_legacy_data(data)
    return data


def _snapshot(tmp_path, data):
    tar = tmp_path / 'snapshot.tar.gz'
    r = subprocess.run([PY, str(REHEARSAL), 'backup', str(data), str(tar)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return tar


def _safe_restore(snapshot, dest, original=None):
    cmd = ['bash', str(SAFE_RESTORE), str(snapshot), str(dest)]
    if original:
        cmd += ['--original', str(original)]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_first_run_restores_into_missing_target(tmp_path):
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    old_before = _tree_hashes(old)
    dest = tmp_path / 'new-data'
    r = _safe_restore(snapshot, dest, original=old)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'SAFE-RESTORE OK' in r.stdout
    # 恢复结果 == 快照内容 (即 == 旧目录内容)
    assert _tree_hashes(dest) == old_before
    # 旧生产目录全程未被触碰
    assert _tree_hashes(old) == old_before


def test_rerun_with_nonempty_target_preserves_instead_of_deleting(tmp_path):
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    old_before = _tree_hashes(old)
    dest = tmp_path / 'new-data'
    assert _safe_restore(snapshot, dest).returncode == 0
    # 模拟新版已经写入: 一个新任务 + 一个被修改的文件
    (dest / 'todo' / '新版新任务.md').write_text('new\n', encoding='utf-8')
    written = sorted(p.name for p in (dest / 'todo').iterdir())
    # 重跑 (非空目标, 且没有更早的保全副本)
    r = _safe_restore(snapshot, dest)
    assert r.returncode == 0, r.stdout + r.stderr
    assert '已保全' in r.stdout
    keeps = list(tmp_path.glob('new-data-pre-restore-*'))
    assert len(keeps) == 1, keeps
    keep = keeps[0]
    # 保全副本里新版写入的数据还在 (没被删除)
    assert (keep / 'todo' / '新版新任务.md').is_file()
    assert sorted(p.name for p in (keep / 'todo').iterdir()) == written
    # 正式目录回到快照内容; 旧目录依旧没动
    assert _tree_hashes(dest) == old_before
    assert _tree_hashes(old) == old_before


def test_rerun_with_existing_preserve_copy_stops(tmp_path):
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    dest = tmp_path / 'new-data'
    assert _safe_restore(snapshot, dest).returncode == 0
    (dest / 'todo' / '第一次重跑的遗留.md').write_text('x\n', encoding='utf-8')
    # 第一次重跑: 非空 → 保全 → 恢复
    assert _safe_restore(snapshot, dest).returncode == 0
    assert len(list(tmp_path.glob('new-data-pre-restore-*'))) == 1
    # 第二次重跑: 已有保全副本 → 必须停止, 不许把第二份也卷进去
    (dest / 'todo' / '第二次的遗留.md').write_text('y\n', encoding='utf-8')
    dest_before = _tree_hashes(dest)
    r = _safe_restore(snapshot, dest)
    assert r.returncode == 1
    assert '已存在更早的保全副本' in r.stdout
    assert '人工确认' in r.stdout
    # 停止时目标内容保持原样 (没被恢复覆盖, 也没被删)
    assert _tree_hashes(dest) == dest_before


def test_interrupted_state_reruns_cleanly(tmp_path):
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    old_before = _tree_hashes(old)
    dest = tmp_path / 'new-data'
    assert _safe_restore(snapshot, dest).returncode == 0
    # 模拟"mv 完成但 restore 未运行"的中断现场: 手动重演该状态
    interrupted = tmp_path / 'interrupted-dest'
    interrupted.mkdir()
    (interrupted / 'todo').mkdir()
    (interrupted / 'todo' / '部分文件.md').write_text('半截\n', encoding='utf-8')
    # 快照仍然完好, 目标缺失 → 重跑应直接恢复到新目录
    r = _safe_restore(snapshot, interrupted)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _tree_hashes(interrupted) == old_before
    assert snapshot.is_file() and (tmp_path / 'snapshot.tar.gz.sha256').is_file()


def test_refuses_symlinked_target(tmp_path):
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    real = tmp_path / 'real-dest'
    real.mkdir()
    link = tmp_path / 'link-dest'
    link.symlink_to(real, target_is_directory=True)
    r = _safe_restore(snapshot, link)
    assert r.returncode == 1
    assert '符号链接' in r.stdout
    assert list(real.iterdir()) == []      # 没有写进链接目标


def test_refuses_snapshot_without_manifest(tmp_path):
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    (tmp_path / 'snapshot.tar.gz.sha256').unlink()
    dest = tmp_path / 'new-data'
    r = _safe_restore(snapshot, dest)
    assert r.returncode == 2
    assert '清单' in r.stdout
    assert not dest.exists()


# ── 独立审查问题 B: 移动目标之前必须先验证快照 ─────────────────────────

def _nonempty_dest(tmp_path, name='new-data'):
    """一个"已有价值数据"的非空恢复目标 (虚构)。"""
    dest = tmp_path / name
    (dest / 'todo').mkdir(parents=True)
    (dest / 'todo' / '已有任务.md').write_text('precious\n', encoding='utf-8')
    (dest / 'internal_tokens.json').write_text('{"u": "t"}', encoding='utf-8')
    return dest


def test_corrupted_archive_never_touches_nonempty_target(tmp_path):
    """损坏归档 + 非空目标 → 预验证拒绝; 目标逐字节不变, 不产生保全副本。"""
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    old_before = _tree_hashes(old)
    dest = _nonempty_dest(tmp_path)
    dest_before = _tree_hashes(dest)
    # 截断归档 (gzip 数据不完整) + 原清单副本
    raw = snapshot.read_bytes()
    broken = tmp_path / 'broken.tar.gz'
    broken.write_bytes(raw[:len(raw) // 2])
    shutil.copy(str(snapshot) + '.sha256', str(broken) + '.sha256')
    r = _safe_restore(broken, dest)
    assert r.returncode != 0
    assert '预验证' in r.stdout
    # 关键断言: 目标原样 (没被 mv/覆盖/删除, 也没被不完整替代目录顶替)
    assert _tree_hashes(dest) == dest_before
    assert not list(tmp_path.glob('new-data-pre-restore-*'))
    # 旧生产目录同样没动
    assert _tree_hashes(old) == old_before


def test_wrong_manifest_hash_never_touches_nonempty_target(tmp_path):
    """清单 SHA-256 错误 + 非空目标 → 预验证拒绝; 目标不变。"""
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    dest = _nonempty_dest(tmp_path)
    dest_before = _tree_hashes(dest)
    # 归档完好, 但清单里一个文件的哈希是错的
    badman = tmp_path / 'badman.tar.gz'
    shutil.copy(str(snapshot), str(badman))
    manifest = json.loads(
        (tmp_path / 'snapshot.tar.gz.sha256').read_text(encoding='utf-8'))
    first = sorted(manifest)[0]
    manifest[first] = '0' * 64
    (tmp_path / 'badman.tar.gz.sha256').write_text(
        json.dumps(manifest), encoding='utf-8')
    r = _safe_restore(badman, dest)
    assert r.returncode != 0
    assert '预验证' in r.stdout
    assert _tree_hashes(dest) == dest_before
    assert not list(tmp_path.glob('new-data-pre-restore-*'))


def test_verify_subcommand_validates_without_creating_target(tmp_path):
    """p4-rehearsal.py verify: 好快照 → 0; 坏快照 → 非 0; 都不创建目标。"""
    old = _fixture(tmp_path)
    snapshot = _snapshot(tmp_path, old)
    dest = tmp_path / 'must-not-exist'
    r = subprocess.run([PY, str(REHEARSAL), 'verify', str(snapshot)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert '归档完整性验证' in r.stdout
    assert '结果:' in r.stdout
    assert not dest.exists()
    # 截断归档 → verify 也必须非 0, 且什么都不创建
    raw = snapshot.read_bytes()
    broken = tmp_path / 'broken.tar.gz'
    broken.write_bytes(raw[:len(raw) // 3])
    shutil.copy(str(snapshot) + '.sha256', str(broken) + '.sha256')
    r2 = subprocess.run([PY, str(REHEARSAL), 'verify', str(broken)],
                        capture_output=True, text=True)
    assert r2.returncode != 0
    assert not dest.exists()


# ── 恢复失败的三种语义 (退出码 1 必须在输出中明确区分) ─────────────────

def _script_with_stub_tool(tmp_path, restore_stdout, restore_rc=1):
    """把 p5-safe-restore.sh 复制到一个临时目录, 旁边放一个 stub
    p4-rehearsal.py (verify 恒过, restore 按给定输出/退出码失败)。

    只测脚本自身的失败分支话术 — 真实工具的故障路径由
    tests/test_p4_rehearsal.py 的注入测试覆盖。
    """
    work = tmp_path / 'stubbin'
    work.mkdir()
    shutil.copy(str(SAFE_RESTORE), str(work / 'p5-safe-restore.sh'))
    stub = work / 'p4-rehearsal.py'
    stub.write_text(
        'import sys\n'
        "if sys.argv[1] == 'verify':\n"
        "    print('归档完整性验证 (stub)')\n"
        '    sys.exit(0)\n'
        'sys.stdout.write(' + repr(restore_stdout) + ')\n'
        f'sys.exit({restore_rc})\n',
        encoding='utf-8')
    snapshot = tmp_path / 'snapshot.tar.gz'
    snapshot.write_bytes(b'stub')
    (tmp_path / 'snapshot.tar.gz.sha256').write_text('{}', encoding='utf-8')
    return work / 'p5-safe-restore.sh', snapshot


def test_restore_failure_without_preserve_copy_reports_case_a(tmp_path):
    """语义 a: restore 在既有 (空) 目标上失败, 无保全副本 → 目标保持恢复前状态。"""
    script, snapshot = _script_with_stub_tool(
        tmp_path, '[FATAL] 恢复失败: OSError: injected\n')
    dest = tmp_path / 'new-data'
    dest.mkdir()          # 空目标: 不走保全分支, 无 KEEP
    before = _tree_hashes(dest)
    r = subprocess.run(['bash', str(script), str(snapshot), str(dest)],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert '工具未留下部分写入' in r.stdout
    assert '恢复目标保持恢复前状态' in r.stdout
    assert '未创建任何保全副本' in r.stdout
    assert _tree_hashes(dest) == before
    assert not list(tmp_path.glob('new-data-pre-restore-*'))


def test_restore_failure_after_preserve_reports_case_b(tmp_path):
    """语义 b: 旧目录已移到保全路径后新恢复失败 → 旧数据完好保留在保全副本。"""
    script, snapshot = _script_with_stub_tool(
        tmp_path, '[FATAL] 恢复失败: OSError: injected\n')
    dest = tmp_path / 'new-data'
    (dest / 'todo').mkdir(parents=True)
    (dest / 'todo' / '已有任务.md').write_text('precious\n', encoding='utf-8')
    keep_before = _tree_hashes(dest)
    r = subprocess.run(['bash', str(script), str(snapshot), str(dest)],
                       capture_output=True, text=True)
    assert r.returncode == 1
    keeps = list(tmp_path.glob('new-data-pre-restore-*'))
    assert len(keeps) == 1, keeps
    # 保全副本完好 (mv 未删除任何内容), 且被明确告知
    assert _tree_hashes(keeps[0]) == keep_before
    assert '旧数据完好保留在' in r.stdout
    assert str(keeps[0]) in r.stdout
    assert '工具未留下部分写入' in r.stdout


def test_incomplete_rollback_reports_case_c_manual_intervention(tmp_path):
    """语义 c: 恢复后回滚本身不完整 → 需人工介入, 不得删除任何目录/现场。"""
    script, snapshot = _script_with_stub_tool(
        tmp_path,
        '[FATAL] 回滚不完整 (2 个文件未还原) — '
        '旧内容现场保留在 /scene/.dest.restore-staging-1-rollback, 请人工恢复!\n'
        '[WARN] 保留回滚现场以便人工恢复: '
        '/scene/.dest.restore-staging-1-rollback\n')
    dest = tmp_path / 'new-data'
    (dest / 'todo').mkdir(parents=True)
    (dest / 'todo' / '已有任务.md').write_text('precious\n', encoding='utf-8')
    keep_before = _tree_hashes(dest)
    r = subprocess.run(['bash', str(script), str(snapshot), str(dest)],
                       capture_output=True, text=True)
    assert r.returncode == 1
    # 必须停在人工介入分支, 而不是"工具已回滚, 目标保持恢复前状态"
    assert '需要人工介入' in r.stdout
    assert '不得删除任何目录' in r.stdout
    assert '回滚现场' in r.stdout
    assert '保全副本同样完好' in r.stdout   # 保全副本也一并告知
    keeps = list(tmp_path.glob('new-data-pre-restore-*'))
    assert len(keeps) == 1, keeps
    assert str(keeps[0]) in r.stdout
    assert _tree_hashes(keeps[0]) == keep_before
