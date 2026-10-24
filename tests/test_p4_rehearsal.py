"""Unit tests for the P4 rehearsal driver's pure logic.

The HTTP-level phases (old-check / new-check / rollback-check) are
exercised end-to-end by the p4-upgrade-rehearsal CI workflow against
real old/new servers; these tests pin the filesystem-level helpers —
especially the backup/restore format contract (independent review
findings A and C) and the fictional Google credential handling.
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / 'scripts' / 'p4-rehearsal.py'


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location('p4_rehearsal_under_test', SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.PASS.clear()
    m.FAIL.clear()
    m.FINDING.clear()
    return m


def test_build_data_creates_full_legacy_shape(mod, tmp_path):
    data = tmp_path / 'todolist'
    mod.build_data(str(data))
    for rel in ('todo/买牛奶.md', 'todo/提交周报.md', 'todo/备注-周五.md',
                'todo/项目甲/项目任务一.md', 'todo/已同步任务.md',
                'recurring/每月缴费.md', 'done/2026-09-15/旧已完成任务.md',
                'lists/groceries/meta.json', 'lists/shopping/meta.json',
                'persistent_projects.json', 'internal_tokens.json',
                'google_calendar/client_secret.json',
                'google_calendar/tokens.json',
                'google_calendar/settings.json',
                'google_calendar/oauth_states.json'):
        assert (data / rel).is_file(), rel
    raw = (data / 'todo' / '已同步任务.md').read_text(encoding='utf-8')
    assert 'task_uid: uid-fictional-123' in raw
    assert 'gcal_event_id: evt-fictional-123' in raw


def test_diff_snapshots_detects_vanished_content(mod, tmp_path):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    mod.snapshot(str(data), str(tmp_path / 'a.json'))
    (data / 'todo' / '买牛奶.md').unlink()
    mod.snapshot(str(data), str(tmp_path / 'b.json'))
    rc = mod.diff_snapshots(str(tmp_path / 'a.json'), str(tmp_path / 'b.json'))
    assert rc == 1
    assert mod.FAIL, 'a vanished file must fail the comparison'
    assert any('消失' in f for f in mod.FAIL)


def test_diff_snapshots_treats_done_archive_as_move_not_loss(mod, tmp_path):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    mod.snapshot(str(data), str(tmp_path / 'a.json'))
    src = data / 'todo' / '买牛奶.md'
    dst = data / 'done' / '2026-10-18' / '买牛奶.md'
    dst.parent.mkdir(parents=True)
    src.rename(dst)
    mod.snapshot(str(data), str(tmp_path / 'b.json'))
    rc = mod.diff_snapshots(str(tmp_path / 'a.json'), str(tmp_path / 'b.json'))
    assert rc == 0
    assert not mod.FAIL
    assert not mod.FINDING


# ── 备份/恢复格式约定 (审查问题 A) ────────────────────────────────────────

def test_backup_restore_roundtrip_with_documented_format(mod, tmp_path):
    """Runbook §3/§4 记录的命令必须产出可被 restore 消费的归档。"""
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    # 与 Runbook 文档中完全相同的调用形式
    mod.backup(str(data), str(tar))
    assert tar.is_file()
    manifest = json.loads((tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'))
    # 约定 1: 归档根直接是数据目录内容, 不多包一层
    with tarfile.open(tar, 'r:gz') as t:
        top = {m.name.split('/')[0] for m in t.getmembers()}
    assert 'todo' in top and 'lists' in top and 'internal_tokens.json' in top
    assert 'd' not in top and 'todolist' not in top, 'archive must not wrap the data dir'
    # 约定 2: 清单是 JSON {相对路径: sha256}
    assert 'todo/买牛奶.md' in manifest
    assert manifest['todo/买牛奶.md'] == mod.sha256_file(str(data / 'todo' / '买牛奶.md'))
    # 恢复目标 = 数据目录本身 (旧版程序要求直接含 todo/ 等)
    dest = tmp_path / 'restored'
    mod.restore(str(tar), str(dest))
    assert not mod.FAIL
    assert (dest / 'todo' / '买牛奶.md').is_file()
    for rel in manifest:
        assert (dest / rel).is_file(), rel
        assert (dest / rel).read_bytes() == (data / rel).read_bytes()


def test_backup_archive_and_manifest_cover_the_same_fileset(mod, tmp_path, capsys):
    """P5.5: 归档与清单的文件集合必须一致 — 尤其 .tmp  crash 残留。

    旧实现: 清单跳过 .tmp 但 tar 原样打包 → 残留一个 .tmp 就让恢复
    因「归档内容与清单不一致」FATAL (备份不可用)。现在两边同一集合。
    """
    data = tmp_path / 'd'
    mod.build_data(str(data))
    # 制造 atomic_write 崩溃残留 (旧版/serve.py 的 .tmp 中间产物)
    (data / 'todo' / 'ghost.md.tmp').write_text('半截任务', encoding='utf-8')
    (data / 'google_calendar' / 'tokens.json.tmp').write_text('{}', encoding='utf-8')
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    manifest = json.loads((tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'))
    with tarfile.open(tar, 'r:gz') as t:
        archived = {m.name for m in t.getmembers() if m.isfile()}
    assert not any(n.endswith('.tmp') for n in archived), archived
    assert not any(k.endswith('.tmp') for k in manifest), list(manifest)
    assert archived == set(manifest), 'archive/manifest fileset mismatch'
    # 因此这份含 .tmp 残留的备份必须可恢复
    dest = tmp_path / 'restored'
    rc = mod.restore(str(tar), str(dest))
    assert rc == 0
    assert not mod.FAIL
    assert (dest / 'todo' / '买牛奶.md').is_file()
    out = capsys.readouterr().out
    assert '跳过 2 个 .tmp 残留' in out


def test_restore_rejects_gnu_sha256sum_text_manifest(mod, tmp_path, capsys):
    """问题 A: Runbook 旧命令产出的 sha256sum 文本必须被明确拒绝。"""
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'legacy.tar.gz'
    mod.backup(str(data), str(tar))
    # 用 GNU sha256sum 文本覆盖清单 (模拟 Runbook §3 的旧命令)
    lines = [f'{h}  ./{rel}' for rel, h in
             json.loads((tmp_path / 'legacy.tar.gz.sha256').read_text()).items()]
    (tmp_path / 'legacy.tar.gz.sha256').write_text('\n'.join(lines) + '\n')
    dest = tmp_path / 'restored'
    with pytest.raises(SystemExit) as exc:
        mod.restore(str(tar), str(dest))
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert 'JSON' in out and '备份' in out, 'must point at the documented backup command'


def test_restore_rejects_wrapped_archive(mod, tmp_path, capsys):
    """问题 A: 'tar czf -C <父目录> <数据目录名>' 多包一层必须被明确拒绝。"""
    data = tmp_path / 'd'
    mod.build_data(str(data))
    # 构造带包裹层的归档 + 合法清单 (清单路径 todo/... 在包裹层里对不上)
    wrapped = tmp_path / 'wrapped.tar.gz'
    with tarfile.open(wrapped, 'w:gz') as t:
        t.add(str(data), arcname='todolist')
    mod.backup(str(data), str(tmp_path / 'x.tar.gz'))
    manifest = json.loads((tmp_path / 'x.tar.gz.sha256').read_text(encoding='utf-8'))
    (tmp_path / 'wrapped.tar.gz.sha256').write_text(
        json.dumps(manifest, ensure_ascii=False))
    dest = tmp_path / 'restored'
    with pytest.raises(SystemExit) as exc:
        mod.restore(str(wrapped), str(dest))
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert 'todolist/' in out and 'backup' in out
    # 被拒绝时不得往 dest 里写任何东西
    assert not any(dest.rglob('*')), 'rejected restore must not write into dest'


def test_restore_rejects_missing_manifest(mod, tmp_path, capsys):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    (tmp_path / 'b.tar.gz.sha256').unlink()
    with pytest.raises(SystemExit) as exc:
        mod.restore(str(tar), str(tmp_path / 'restored'))
    assert exc.value.code == 2
    assert '备份' in capsys.readouterr().out


# ── 恢复安全性: 先验证后写入 (独立审查问题 1) ─────────────────────────────

def test_failed_restore_leaves_destination_untouched(mod, tmp_path, capsys):
    """损坏归档 + --force: 校验失败, 恢复目标必须逐字节不变。"""
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    # 构造只损坏一个文件的归档, 沿用原清单
    with tarfile.open(tar, 'r:gz') as t:
        members = [(m, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
    broken = tmp_path / 'broken.tar.gz'
    with tarfile.open(broken, 'w:gz') as t:
        for m, blob in members:
            if m.name == 'todo/买牛奶.md':
                blob = b'---\nCORRUPTED\n---\n\ncorrupted\n'
            info = tarfile.TarInfo(m.name)
            info.size = len(blob)
            t.addfile(info, __import__('io').BytesIO(blob))
    (tmp_path / 'broken.tar.gz.sha256').write_text(
        (tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'),
        encoding='utf-8')
    # 预置恢复目标 (含一个归档里没有的文件 — 必须原样保留)
    dest = tmp_path / 'dest'
    (dest / 'todo').mkdir(parents=True)
    (dest / 'todo' / '仅此一份.md').write_text('precious\n', encoding='utf-8')
    before = {str(p.relative_to(dest)): p.read_bytes()
              for p in dest.rglob('*') if p.is_file()}
    rc = mod.restore(str(broken), str(dest), force=True)
    assert rc == 1
    after = {str(p.relative_to(dest)): p.read_bytes()
             for p in dest.rglob('*') if p.is_file()}
    assert after == before, 'a failed restore must not modify the destination'
    assert '恢复目标未被修改' in capsys.readouterr().out


def test_restore_refuses_non_empty_destination_without_force(mod, tmp_path, capsys):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'keep.txt').write_text('do not touch\n', encoding='utf-8')
    with pytest.raises(SystemExit) as exc:
        mod.restore(str(tar), str(dest))
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert '非空' in out and '--force' in out
    assert (dest / 'keep.txt').read_text(encoding='utf-8') == 'do not touch\n'
    # --force + 完好归档 → 正常恢复
    rc = mod.restore(str(tar), str(dest), force=True)
    assert rc == 0
    assert (dest / 'todo' / '买牛奶.md').is_file()


def test_restore_rejects_archive_content_not_matching_manifest(mod, tmp_path, capsys):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    # 归档里多一个清单之外的文件 → 必须拒绝
    extra = tmp_path / 'extra.tar.gz'
    with tarfile.open(tar, 'r:gz') as t:
        members = [(m, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
    with tarfile.open(extra, 'w:gz') as t:
        for m, blob in members:
            info = tarfile.TarInfo(m.name)
            info.size = len(blob)
            t.addfile(info, __import__('io').BytesIO(blob))
        stray = b'stray\n'
        info = tarfile.TarInfo('stray-file.txt')
        info.size = len(stray)
        t.addfile(info, __import__('io').BytesIO(stray))
    (tmp_path / 'extra.tar.gz.sha256').write_text(
        (tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'),
        encoding='utf-8')
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'keep.txt').write_text('precious\n', encoding='utf-8')
    before = {str(p.relative_to(dest)): p.read_bytes()
              for p in dest.rglob('*') if p.is_file()}
    with pytest.raises(SystemExit) as exc:
        mod.restore(str(extra), str(dest), force=True)
    assert exc.value.code == 2
    after = {str(p.relative_to(dest)): p.read_bytes()
             for p in dest.rglob('*') if p.is_file()}
    assert after == before


def test_restore_cli_returns_nonzero_on_corrupted_archive(mod, tmp_path):
    """CLI 退出码契约: 损坏归档必须非零退出 (CI 的 tee 才可能变红)。"""
    import subprocess
    import sys as _sys
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    broken = tmp_path / 'broken.tar.gz'
    with tarfile.open(tar, 'r:gz') as t:
        members = [(m, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
    with tarfile.open(broken, 'w:gz') as t:
        for m, blob in members:
            if m.name == 'todo/买牛奶.md':
                blob = b'CORRUPTED\n'
            info = tarfile.TarInfo(m.name)
            info.size = len(blob)
            t.addfile(info, __import__('io').BytesIO(blob))
    (tmp_path / 'broken.tar.gz.sha256').write_text(
        (tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'),
        encoding='utf-8')
    dest = tmp_path / 'dest'
    dest.mkdir()
    r = subprocess.run(
        [_sys.executable, str(SCRIPT), 'restore', str(broken), str(dest), '--force'],
        capture_output=True, text=True)
    assert r.returncode != 0, 'a corrupted archive must fail the CLI'
    assert '未被修改' in r.stdout


def test_failed_restore_does_not_create_nonexistent_destination(mod, tmp_path, capsys):
    """目标原本不存在时, 失败恢复连空目录都不能留下 (目标保持"不存在")。"""
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    with tarfile.open(tar, 'r:gz') as t:
        members = [(m, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
    broken = tmp_path / 'broken.tar.gz'
    with tarfile.open(broken, 'w:gz') as t:
        for m, blob in members:
            if m.name == 'todo/买牛奶.md':
                blob = b'CORRUPTED\n'
            info = tarfile.TarInfo(m.name)
            info.size = len(blob)
            t.addfile(info, __import__('io').BytesIO(blob))
    (tmp_path / 'broken.tar.gz.sha256').write_text(
        (tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'),
        encoding='utf-8')
    dest = tmp_path / 'never-existed'
    assert not dest.exists()
    rc = mod.restore(str(broken), str(dest), force=True)
    assert rc == 1
    assert not dest.exists(), 'a failed restore must not even create the destination'
    assert '未被修改' in capsys.readouterr().out


def test_restore_cli_rejects_truncated_archive(mod, tmp_path):
    """截断的 gzip (EOFError) 也必须被明确拒绝且不动目标。"""
    import subprocess
    import sys as _sys
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    raw = tar.read_bytes()
    truncated = tmp_path / 'trunc.tar.gz'
    truncated.write_bytes(raw[:len(raw) // 2])
    (tmp_path / 'trunc.tar.gz.sha256').write_text(
        (tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'),
        encoding='utf-8')
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'keep.txt').write_text('precious\n', encoding='utf-8')
    r = subprocess.run(
        [_sys.executable, str(SCRIPT), 'restore', str(truncated), str(dest), '--force'],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert '归档损坏' in r.stdout
    assert '未被修改' in r.stdout
    assert [p.name for p in dest.rglob('*') if p.is_file()] == ['keep.txt']


def test_restore_force_warns_about_leftover_files(mod, tmp_path, capsys):
    """--force 覆盖合并: 归档外遗留文件必须被 [WARN] 明确列出 (防静默混合)。"""
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    dest = tmp_path / 'dest'
    (dest / 'todo').mkdir(parents=True)
    (dest / 'todo' / '珍贵-勿删.md').write_text('precious\n', encoding='utf-8')
    rc = mod.restore(str(tar), str(dest), force=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert '[WARN]' in out and '遗留文件' in out
    assert '珍贵-勿删.md' in out, 'leftover file must be listed by name'
    # 归档内容恢复完整, 遗留文件仍在但被点名
    assert (dest / 'todo' / '买牛奶.md').is_file()
    assert (dest / 'todo' / '珍贵-勿删.md').is_file()


def test_cli_prints_result_summary_for_backup_and_restore(mod, tmp_path):
    """CLI 输出契约: backup/restore 也要打印「结果:」汇总 (CI 摘要有依赖)。"""
    import subprocess
    import sys as _sys
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    r = subprocess.run(
        [_sys.executable, str(SCRIPT), 'backup', str(data), str(tar)],
        capture_output=True, text=True)
    assert r.returncode == 0
    assert '结果:' in r.stdout
    dest = tmp_path / 'dest'
    r = subprocess.run(
        [_sys.executable, str(SCRIPT), 'restore', str(tar), str(dest)],
        capture_output=True, text=True)
    assert r.returncode == 0
    assert '结果:' in r.stdout
    assert '恢复完成' in r.stdout


# ── 虚构 Google 凭据 (审查问题 C) ─────────────────────────────────────────

def test_backup_manifest_covers_google_credentials(mod, tmp_path):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    manifest = json.loads((tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'))
    creds = [rel for rel in manifest if rel.startswith('google_calendar/')]
    assert len(creds) == 4, f'credentials must be in the manifest, got {creds}'
    # 清单只含哈希, 不含任何凭据内容
    blob = (tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8')
    assert 'FICTIONAL' not in blob
    # 恢复后逐文件校验通过 (含凭据)
    dest = tmp_path / 'restored'
    mod.restore(str(tar), str(dest))
    assert not mod.FAIL


def test_restore_detects_corrupted_credential(mod, tmp_path):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    tar = tmp_path / 'b.tar.gz'
    mod.backup(str(data), str(tar))
    # 破坏归档中的一个凭据文件后, 恢复必须失败 (且不动目标)
    with tarfile.open(tar, 'r:gz') as t:
        members = [(m, t.extractfile(m).read()) for m in t.getmembers()
                   if m.isfile()]
    broken = tmp_path / 'broken.tar.gz'
    with tarfile.open(broken, 'w:gz') as t:
        for m, blob in members:
            if m.name == 'google_calendar/tokens.json':
                blob = b'{"alice": "CORRUPTED"}'
            info = tarfile.TarInfo(m.name)
            info.size = len(blob)
            t.addfile(info, __import__('io').BytesIO(blob))
    # 沿用原备份的 JSON 清单 (被破坏的文件哈希对不上 → 必须失败)
    (tmp_path / 'broken.tar.gz.sha256').write_text(
        (tmp_path / 'b.tar.gz.sha256').read_text(encoding='utf-8'),
        encoding='utf-8')
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'keep.txt').write_text('precious\n', encoding='utf-8')
    rc = mod.restore(str(broken), str(dest), force=True)
    assert rc == 1
    assert any('恢复完整性' in f or '归档完整性' in f for f in mod.FAIL)
    # 目标未被修改 (先验证后写入)
    assert [p.name for p in dest.rglob('*') if p.is_file()] == ['keep.txt']


def test_creds_check_passes_on_fictional_dataset(mod, tmp_path):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    rc = mod.creds_check(str(data))
    assert rc == 0
    assert not mod.FAIL


def test_creds_check_fails_on_missing_credential(mod, tmp_path):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    (data / 'google_calendar' / 'tokens.json').unlink()
    rc = mod.creds_check(str(data))
    assert rc == 1
    assert any('凭据文件齐全' in f for f in mod.FAIL)


def test_creds_check_fails_on_non_fictional_content(mod, tmp_path):
    data = tmp_path / 'd'
    mod.build_data(str(data))
    (data / 'google_calendar' / 'tokens.json').write_text(
        '{"alice": {"refresh_token": "1//real-looking-token"}}', encoding='utf-8')
    rc = mod.creds_check(str(data))
    assert rc == 1
    assert any('虚构' in f for f in mod.FAIL)


# ── 生产形态凭据校验 creds-verify (独立审查问题 2) ────────────────────────

def _prod_like_dataset(mod, tmp_path):
    """虚构但**不带 fictional 字样**的凭据 (生产形态)。"""
    data = tmp_path / 'prod'
    mod.build_data(str(data))
    gcal = data / 'google_calendar'
    (gcal / 'client_secret.json').write_text(
        '{"installed": {"client_id": "123456.apps.googleusercontent.com", '
        '"client_secret": "GOCSPX-reallooking"}}', encoding='utf-8')
    (gcal / 'tokens.json').write_text(
        '{"alice": {"token": "ya29.reallooking", '
        '"refresh_token": "1//reallooking"}}', encoding='utf-8')
    return data


def test_creds_verify_passes_on_production_like_credentials(mod, tmp_path, capsys):
    data = _prod_like_dataset(mod, tmp_path)
    tar = tmp_path / 'prod.tar.gz'
    mod.backup(str(data), str(tar))
    dest = tmp_path / 'restored'
    mod.restore(str(tar), str(dest))
    rc = mod.creds_verify(str(tar), str(dest))
    assert rc == 0
    assert not mod.FAIL
    out = capsys.readouterr().out
    assert '内容未输出' in out
    # 绝不输出任何凭据内容
    assert 'GOCSPX' not in out and 'ya29' not in out and '1//' not in out


def test_creds_verify_fails_on_missing_or_corrupted_credential(mod, tmp_path):
    data = _prod_like_dataset(mod, tmp_path)
    tar = tmp_path / 'prod.tar.gz'
    mod.backup(str(data), str(tar))
    dest = tmp_path / 'restored'
    mod.restore(str(tar), str(dest))
    (dest / 'google_calendar' / 'tokens.json').unlink()
    rc = mod.creds_verify(str(tar), str(dest))
    assert rc == 1
    assert any('凭据文件存在' in f for f in mod.FAIL)
    # 内容被改 → 哈希不符
    dest2 = tmp_path / 'restored2'
    mod.restore(str(tar), str(dest2))
    (dest2 / 'google_calendar' / 'tokens.json').write_text(
        '{"alice": {"token": "tampered"}}', encoding='utf-8')
    mod.FAIL.clear()
    rc = mod.creds_verify(str(tar), str(dest2))
    assert rc == 1
    assert any('SHA-256' in f for f in mod.FAIL)


def test_creds_check_rejects_what_creds_verify_accepts(mod, tmp_path):
    """同一份生产形态凭据: creds-verify 通过, creds-check (CI 守卫) 拒绝。"""
    data = _prod_like_dataset(mod, tmp_path)
    tar = tmp_path / 'prod.tar.gz'
    mod.backup(str(data), str(tar))
    dest = tmp_path / 'restored'
    mod.restore(str(tar), str(dest))
    assert mod.creds_verify(str(tar), str(dest)) == 0
    mod.FAIL.clear()
    assert mod.creds_check(str(dest)) == 1   # CI 守卫: 无 fictional 标记
    assert any('虚构' in f for f in mod.FAIL)


# ── O3-N3: 跨文件系统恢复 (Codex 复现 Errno 18 的修复) ──────────────────
#
# 根因: 旧 restore 在 tempfile.TemporaryDirectory() (/tmp 常为 tmpfs) 解压,
# 再把顶层文件 os.replace 进目标 — 跨文件系统 OSError [Errno 18], 且循环
# 中途失败留下"部分恢复"。
# 修复契约 (以下测试逐条钉死):
#   R1 staging 建在目标文件系统内 (dest 父目录), 不使用 /tmp
#   R2 全过程不存在跨文件系统 os.replace (模拟器强制同 st_dev)
#   R3 写目标前在 staging 内重验 SHA-256
#   R4 切换写入中途失败 → 完整回滚, 目标逐字节不变, 无部分恢复
#   R5 损坏快照 / 权限不足 → fail closed, 目标不动, staging 不残留
#   R6 非空目标完整保全; 恢复后终检失败也回滚

def _tree_hashes(root):
    out = {}
    for p in sorted(Path(root).rglob('*')):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _backup_of(mod, tmp_path, name='d'):
    data = tmp_path / name
    mod.build_data(str(data))
    tar = tmp_path / f'{name}.tar.gz'
    mod.backup(str(data), str(tar))
    return tar


def test_staging_lives_in_target_filesystem(mod, tmp_path, monkeypatch):
    """R1: staging 必须建在 dest 的父目录 (同文件系统), 不用 /tmp。"""
    tar = _backup_of(mod, tmp_path)
    seen = {}
    real_extract = mod._safe_extractall

    def spy(tarobj, dest):
        seen['dest'] = dest
        # staging 目录此时可能尚未创建 — 不存在时取其父目录 (同文件系统)
        probe = dest if os.path.isdir(dest) else os.path.dirname(dest)
        seen['st_dev'] = os.stat(probe).st_dev
        return real_extract(tarobj, dest)

    monkeypatch.setattr(mod, '_safe_extractall', spy)
    dest = tmp_path / 'restored'
    rc = mod.restore(str(tar), str(dest))
    assert rc == 0
    assert 'dest' in seen, 'extract target must be observed'
    staging = Path(seen['dest'])
    assert staging.parent == tmp_path, f'staging must be dest\'s sibling: {staging}'
    assert staging.name.startswith('.restored.restore-staging-'), staging.name
    # 与目标同文件系统 (st_dev 一致, 解压时记录)
    assert seen['st_dev'] == dest.stat().st_dev
    # 清理: 成功后 staging 不残留
    assert not staging.exists()


def test_restore_never_crosses_filesystems(mod, tmp_path, monkeypatch):
    """R2: 模拟跨文件系统 os.replace (Errno 18) — 恢复必须仍然成功
    (证明实现不再依赖 /tmp → 目标的跨文件系统 rename)。"""
    tar = _backup_of(mod, tmp_path)
    real_replace = os.replace

    def same_dev_replace(src, dst):
        def dev_of(path):
            if os.path.isdir(path):
                return os.stat(path).st_dev
            if os.path.exists(path):
                return os.stat(path).st_dev
            return os.stat(os.path.dirname(path) or '.').st_dev
        if dev_of(src) != dev_of(dst):
            raise OSError(18, 'Invalid cross-device link')
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, 'replace', same_dev_replace)
    dest = tmp_path / 'restored'
    rc = mod.restore(str(tar), str(dest))
    assert rc == 0
    assert not mod.FAIL
    assert (dest / 'todo' / '买牛奶.md').is_file()


def test_midswitch_failure_rolls_back_completely(mod, tmp_path, monkeypatch, capsys):
    """R4: 切换写入中途失败 (故障注入) → 完整回滚: 目标逐字节不变,
    无"冒充成功的部分恢复", staging 不残留。"""
    tar = _backup_of(mod, tmp_path)
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'todo').mkdir()
    (dest / 'todo' / '珍贵-勿删.md').write_text('precious\n', encoding='utf-8')
    (dest / 'internal_tokens.json').write_text('{"u": "old"}\n', encoding='utf-8')
    before = _tree_hashes(dest)
    real_replace = os.replace
    calls = {'n': 0}

    def flaky_replace(src, dst):
        calls['n'] += 1
        if calls['n'] == 3:          # 第 3 个文件写入时磁盘/权限类故障
            raise OSError(28, 'No space left on device')
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, 'replace', flaky_replace)
    rc = mod.restore(str(tar), str(dest), force=True)
    assert rc == 1
    assert calls['n'] >= 3, 'fault must have fired'
    after = _tree_hashes(dest)
    assert after == before, 'a mid-switch failure must roll back byte-identically'
    out = capsys.readouterr().out
    assert '已完整回滚' in out
    # staging / 备份现场不残留
    leftovers = [p.name for p in tmp_path.iterdir()
                 if 'restore-staging' in p.name or 'restore-rollback' in p.name]
    assert not leftovers, f'staging leftovers: {leftovers}'


def test_staging_reverify_failure_fails_closed(mod, tmp_path, monkeypatch):
    """R3: staging 侧重验发现哈希不符 → 不进入写入阶段, 目标不动。"""
    tar = _backup_of(mod, tmp_path)
    real_verify = mod.verify_snapshot
    files = json.loads((tmp_path / 'd.tar.gz.sha256').read_text(encoding='utf-8'))
    tampered = dict(files)
    first = sorted(tampered)[0]
    tampered[first] = '0' * 64        # 清单放行, 但 staging 重验必须抓住

    def bad_verify(path):
        # 结构/集合/逐文件哈希全部"通过" (返回被篡改的清单), 只在 staging
        # 重验时暴露 — 模拟解压损坏/介质错误
        real_verify(path)
        return tampered

    monkeypatch.setattr(mod, 'verify_snapshot', bad_verify)
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'keep.txt').write_text('precious\n', encoding='utf-8')
    before = _tree_hashes(dest)
    rc = mod.restore(str(tar), str(dest), force=True)
    assert rc == 1
    assert _tree_hashes(dest) == before, 'staging re-verify failure must not touch dest'
    assert not list(tmp_path.glob('.dest.restore-staging-*'))


def test_permission_failure_fails_closed(mod, tmp_path, monkeypatch):
    """R5: 解压阶段权限不足 → fail closed, 目标不动, staging 清理。"""
    tar = _backup_of(mod, tmp_path)
    calls = {'n': 0}
    real_extract = mod._safe_extractall

    def denied_second_call(tarobj, dest):
        calls['n'] += 1
        if calls['n'] == 1:
            return real_extract(tarobj, dest)   # verify_snapshot 阶段正常
        raise PermissionError(13, 'Permission denied')   # staging 解压阶段失败

    monkeypatch.setattr(mod, '_safe_extractall', denied_second_call)
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'keep.txt').write_text('precious\n', encoding='utf-8')
    before = _tree_hashes(dest)
    rc = mod.restore(str(tar), str(dest), force=True)
    assert rc == 1
    assert calls['n'] == 2
    assert _tree_hashes(dest) == before
    assert not list(tmp_path.glob('.dest.restore-staging-*')), 'staging must be cleaned'


def test_final_verification_failure_rolls_back(mod, tmp_path, monkeypatch):
    """R6: 切换后终检哈希不符 (并发篡改等) → 回滚, 目标保持原样。"""
    tar = _backup_of(mod, tmp_path)
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'todo').mkdir(parents=True)
    (dest / 'todo' / '旧文件.md').write_text('old\n', encoding='utf-8')
    before = _tree_hashes(dest)
    real_switch = mod._switch_into_place

    def switch_then_tamper(staging, dest_dir, files, backup_root):
        written_new, replaced, created_dirs, dest_existed = real_switch(
            staging, dest_dir, files, backup_root)
        # 模拟"写入成功但目标侧内容被外部改动" — 终检必须抓住并回滚
        victim = os.path.join(dest_dir, sorted(files)[0])
        with open(victim, 'wb') as f:
            f.write(b'tampered-after-switch\n')
        return written_new, replaced, created_dirs, dest_existed

    monkeypatch.setattr(mod, '_switch_into_place', switch_then_tamper)
    rc = mod.restore(str(tar), str(dest), force=True)
    assert rc == 1
    assert _tree_hashes(dest) == before, 'final-verify failure must roll back'


def test_final_verification_oserror_rolls_back_honestly(mod, tmp_path, monkeypatch, capsys):
    """问题 A: 终检读取 OSError → 必须回滚, 且不得谎称"目标未被修改"。

    旧实现让终检的 sha256_file OSError 掉进外层 except: 不回滚、删掉
    backup_root、还打印"恢复目标未被修改" — 而切换早已发生。
    """
    tar = _backup_of(mod, tmp_path)
    dest = tmp_path / 'dest'
    dest.mkdir()
    (dest / 'todo').mkdir(parents=True)
    (dest / 'todo' / '买牛奶.md').write_text('precious-old\n', encoding='utf-8')
    before = _tree_hashes(dest)
    real_hash = mod.sha256_file

    def flaky_hash(path):
        # 只让"目标侧终检"读取失败 (staging 侧重验/备份清单不受影响)
        if str(path).startswith(str(dest) + os.sep):
            raise OSError(5, 'Injected I/O error during final verification')
        return real_hash(path)

    monkeypatch.setattr(mod, 'sha256_file', flaky_hash)
    rc = mod.restore(str(tar), str(dest), force=True)
    out = capsys.readouterr().out
    assert rc == 1
    # 切换已发生 → 必须完整回滚, 旧内容逐字节回来
    assert _tree_hashes(dest) == before, 'final-check OSError must roll back'
    # 话术诚实: 报告回滚, 不得声称"未被修改" (此刻目标已被写过)
    assert '已完整回滚' in out
    assert '未被修改' not in out, 'post-switch failure must not claim "untouched"'
    # 回滚完整 → 现场必须清理, 不留 staging/rollback 目录
    assert not list(tmp_path.glob('.dest.restore-staging-*')), \
        'complete rollback must clean staging and backup_root'


def test_incomplete_rollback_keeps_backup_and_reports(mod, tmp_path, monkeypatch, capsys):
    """问题 B: 终检失败后回滚 OSError → 保留 backup_root, 逐条报告未复原路径。

    回滚不完整时清理 backup_root 等于毁掉旧文件唯一副本 — 必须保留现场,
    且报告哪些文件未能恢复 (只报路径, 不输出内容)。
    """
    tar = _backup_of(mod, tmp_path)
    dest = tmp_path / 'dest'
    (dest / 'todo').mkdir(parents=True)
    old = dest / 'todo' / '买牛奶.md'
    old.write_text('precious-old\n', encoding='utf-8')
    old_hash = hashlib.sha256(old.read_bytes()).hexdigest()
    real_switch = mod._switch_into_place
    real_replace = mod.os.replace
    state = {'fail_replace': False}

    def flaky_replace(src, dst):
        if state['fail_replace']:
            raise OSError(13, 'Injected permission denied during rollback')
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, 'replace', flaky_replace)

    def switch_then_tamper(staging, dest_dir, files, backup_root):
        result = real_switch(staging, dest_dir, files, backup_root)
        # 终检失败 (哈希不符) → 进入回滚; 回滚的 os.replace 此刻必失败
        victim = os.path.join(dest_dir, sorted(files)[0])
        with open(victim, 'wb') as f:
            f.write(b'tampered-after-switch\n')
        state['fail_replace'] = True
        return result

    monkeypatch.setattr(mod, '_switch_into_place', switch_then_tamper)
    rc = mod.restore(str(tar), str(dest), force=True)
    out = capsys.readouterr().out
    assert rc == 1
    # 现场保留: 未还原旧文件的唯一副本还在
    keeps = list(tmp_path.glob('.dest.restore-staging-*-rollback'))
    assert len(keeps) == 1, f'backup_root must be kept for manual recovery: {keeps}'
    keep = keeps[0]
    assert (keep / 'todo' / '买牛奶.md').is_file()
    assert hashlib.sha256(
        (keep / 'todo' / '买牛奶.md').read_bytes()).hexdigest() == old_hash
    # 报告: 回滚不完整 + 人工恢复指引 + 未复原路径 (不输出内容)
    assert '回滚不完整' in out
    assert '人工恢复' in out
    assert '买牛奶.md' in out, 'failed paths must be reported'
    assert 'precious-old' not in out, 'never echo file contents'
    assert '未被修改' not in out, 'incomplete rollback must not claim "untouched"'


def test_cross_filesystem_restore_real(mod, tmp_path):
    """真实跨文件系统恢复 (可选): 设 Y8Y_CROSS_FS_PARENT 指向另一个文件
    系统上的目录 (如挂载的卷) 时运行 — 旧实现在此必现 Errno 18。"""
    parent = os.environ.get('Y8Y_CROSS_FS_PARENT')
    if not parent:
        pytest.skip('Y8Y_CROSS_FS_PARENT not set (no second filesystem provided)')
    parent = Path(parent)
    if not parent.is_dir():
        pytest.skip(f'Y8Y_CROSS_FS_PARENT not a directory: {parent}')
    tar = _backup_of(mod, tmp_path)
    dest = parent / f'cross-fs-restore-{os.getpid()}'
    try:
        rc = mod.restore(str(tar), str(dest))
        assert rc == 0
        assert not mod.FAIL
        assert (dest / 'todo' / '买牛奶.md').is_file()
        for rel, want in json.loads(
                (tmp_path / 'd.tar.gz.sha256').read_text(encoding='utf-8')).items():
            assert (dest / rel).is_file(), rel
            assert hashlib.sha256((dest / rel).read_bytes()).hexdigest() == want
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_restore_into_empty_and_nonexistent_targets_still_ok(mod, tmp_path):
    """目标不存在 / 空目录 / 非空+force 三条 happy path 不受重构影响。"""
    tar = _backup_of(mod, tmp_path)
    # 不存在
    dest = tmp_path / 'fresh'
    assert mod.restore(str(tar), str(dest)) == 0
    assert (dest / 'todo' / '买牛奶.md').is_file()
    # 空目录
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert mod.restore(str(tar), str(empty)) == 0
    # 非空 + force → 覆盖合并 + 遗留告警 (行为不变)
    dest2 = tmp_path / 'dest2'
    (dest2 / 'todo').mkdir(parents=True)
    (dest2 / 'todo' / '遗留.md').write_text('keep\n', encoding='utf-8')
    assert mod.restore(str(tar), str(dest2), force=True) == 0
    assert (dest2 / 'todo' / '遗留.md').read_text(encoding='utf-8') == 'keep\n'
    assert (dest2 / 'todo' / '买牛奶.md').is_file()
