#!/usr/bin/env python3
"""P4 生产切换前隔离演练驱动 — 新旧版本双向数据兼容性验证 (stdlib only)。

本脚本服务于 docs/upgrade-runbook.md 描述的升级/回滚流程:
在完全隔离的环境里, 用虚构但结构贴近真实历史系统的数据, 依次验证:

  旧版(vendored 匿名化兼容基线 rehearsal/legacy-baseline/, 行为等价
  于历史旧版 — 身份已通用化, 见该目录 README)读写旧数据
    → 备份 + 完整性验证
    → 迁移到新版数据目录
    → 新版完整生命周期
    → 回滚 drill A (恢复升级前备份 → 旧版)
    → 回滚 drill B (不恢复, 旧版直接读新版写过的数据 → 风险核查)

旧版代码位置说明: 基线以普通文件形式 vendored 在仓库内 (CI 用
manifest sha256 校验完整性), 因此演练不依赖 git 历史 — 从干净快照
开始的公开仓库同样可以完整复现本流程。

子命令:
  build-data <data-dir>                 生成虚构旧版格式数据集
  snapshot <data-dir> <out.json>        数据目录快照 (path → sha256)
  diff-snapshots <old.json> <new.json>  升级前后结构比对 (新增/移除/移动/修改)
  backup <data-dir> <out.tar.gz>        备份并校验 (tar + JSON sha256 清单)
  restore <tar.gz> <dest-dir> [--force] 恢复并校验 (先验证后写入, 见下)
  verify <tar.gz>                       只读验证快照 (不动任何目标; 恢复前预检)
  creds-check <data-dir>                [仅 CI] 数据集凭据齐全且为虚构内容
  creds-verify <tar.gz> <data-dir>      [生产] 恢复后凭据与归档清单一致性
  old-check <base> <token-user>         旧版 API 验证 (读全部 + 真实写操作)
  new-check <base> <email> <token-user> 新版完整生命周期验证
  rollback-check <base> <token-user>    回滚后旧版读取新版数据的风险核查

备份/恢复格式约定 (Runbook §3/§4/§9 与 CI 共用, 唯一受支持格式):
  归档根目录直接是数据目录内容 (todo/ recurring/ done/ lists/
  google_calendar/ internal_tokens.json ...); 清单 <tar>.sha256 是 JSON
  {相对路径: sha256}, 覆盖全部文件含 Google 凭据 (只记哈希, 不记内容)。
  restore 对不符合约定的归档 (裸 tar + sha256sum 文本、多包一层目录、
  内容与清单不符、归档损坏) 明确拒绝并指出正确做法, 不会静默恢复错位。

restore 安全语义 (独立审查问题 1/B 修复): **先验证, 后写入** —
  隔离解压 → 布局/文件集合/逐文件哈希全部通过 → 才创建/写入目标;
  任何**写入前**验证失败都保证恢复目标不被修改 (不存在则不创建, 存在则
  字节不变)。**写入后**失败 (切换中途/终检不符/终检读取异常) 一律回滚到
  写入前状态; 回滚本身不完整时保留现场并逐条报告未复原路径 (只报路径),
  由人工恢复 — 不谎称"未被修改", 不清理现场 (问题 A/B 修复)。
  非空目标默认拒绝覆盖 (防误覆盖), 确认要覆盖时加 --force; --force 是
  覆盖合并语义: 归档内文件覆盖同名旧文件, 归档外遗留文件会被明确告警,
  防止新旧文件静默混合。
  verify 子命令 (问题 B 修复): 与 restore 内部同一条验证代码路径, 但
  **完全不接受目标目录** — 供 p5-safe-restore.sh 在移动非空恢复目标之前
  先行调用: 损坏备份/无效清单在这一步就被拒绝, 目标原样不动。

凭据检查的两个层次 (问题 2 修复):
  creds-check  仅 CI — 防真实凭据混入虚构数据集 (要求 fictional 标记);
  creds-verify 生产 — 恢复后凭据与归档清单的存在性 + SHA-256 一致性,
                不依赖任何内容标记, 不输出凭据内容。

判定: FAIL = 演练承诺必须成立的条件被破坏 (CI 红);
      FINDING = 已确认的回滚风险, 记入报告与 Runbook, 不使 CI 红。
全程虚构数据; Google Calendar 只用虚构凭据 (全部含 fictional 标记),
不接入真实账号; 无 SDK 时同步静默失败, 无网络请求。
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zlib
from datetime import date, timedelta
from urllib.parse import quote

PASS = []
FAIL = []
FINDING = []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    mark = 'PASS' if ok else 'FAIL'
    print(f'  [{mark}] {name}' + (f'  ({detail})' if detail and not ok else ''))
    return ok


def finding(code, name, detail=''):
    FINDING.append(f'{code}: {name}')
    print(f'  [FINDING {code}] {name}' + (f'  — {detail}' if detail else ''))


def req(base, method, path, body=None, headers=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method)
    r.add_header('Content-Type', 'application/json')
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(r, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode() or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode() or '{}')
        except json.JSONDecodeError:
            return e.code, {'_raw': raw.decode('utf-8', 'replace')}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


# ── build-data ────────────────────────────────────────────────────────────

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def build_data(root):
    """虚构旧版格式数据集: 覆盖旧版全部数据形态, 不含任何真实内容。"""
    today = date.today()
    due = (today + timedelta(days=3)).isoformat()
    _write(os.path.join(root, 'todo', '买牛奶.md'),
           '---\nstatus: todo\ncolor: yellow\ncreated: 2026-09-01\n'
           f'due: {due}\nowner: alice\n---\n\n下班买牛奶\n')
    _write(os.path.join(root, 'todo', '提交周报.md'),
           '---\nstatus: todo\ncolor: green\ncreated: 2026-09-02\n'
           f'due: {due}\nowner: bob\n---\n\n周五前提交周报\n')
    _write(os.path.join(root, 'todo', '备注-周五.md'),
           '---\nstatus: todo\ncolor: blue\ncreated: 2026-09-03\n'
           'owner: shared\n---\n\n共享备注\n')
    _write(os.path.join(root, 'todo', '项目甲', '项目任务一.md'),
           '---\nstatus: todo\ncolor: pink\ncreated: 2026-09-04\n'
           f'due: {due}\nproject: 项目甲\nowner: alice\n---\n\n项目甲的第一个任务\n')
    _write(os.path.join(root, 'todo', '已同步任务.md'),
           '---\nstatus: todo\ncolor: green\ncreated: 2026-09-05\n'
           f'due: {due}\nowner: alice\ntask_uid: uid-fictional-123\n'
           'gcal_event_id: evt-fictional-123\n---\n\n带有 Google 关联字段的任务\n')
    _write(os.path.join(root, 'recurring', '每月缴费.md'),
           '---\nstatus: todo\ncolor: orange\ncreated: 2026-08-01\n'
           f'due: {today.replace(day=1).isoformat()}\nowner: alice\n'
           'recurring:\n  - 1\n  - 15\n---\n\n每月 1 号和 15 号缴费\n')
    _write(os.path.join(root, 'done', '2026-09-15', '旧已完成任务.md'),
           '---\nstatus: done\ncolor: green\ncreated: 2026-09-10\n'
           'completed: 2026-09-15\nowner: alice\n---\n\n上个月已完成的事情\n')
    for lid, meta, items in (
        ('groceries', {'name': '杂货', 'owner': 'shared', 'created': '2026-08-20'},
         [('3f6b1c2e-1111-4a2b-8c3d-000000000001', '牛奶', 'todo'),
          ('3f6b1c2e-1111-4a2b-8c3d-000000000002', '鸡蛋', 'done')]),
        ('shopping', {'name': '购物', 'owner': 'shared', 'created': '2026-08-20'},
         [('3f6b1c2e-2222-4a2b-8c3d-000000000003', '纸巾', 'todo')]),
    ):
        d = os.path.join(root, 'lists', lid)
        os.makedirs(d, exist_ok=True)
        _write(os.path.join(d, 'meta.json'),
               json.dumps(meta, ensure_ascii=False, indent=2) + '\n')
        for iid, title, status in items:
            _write(os.path.join(d, f'{iid}.md'),
                   f'---\nstatus: {status}\ncreated: 2026-09-01\n'
                   f'owner: {meta["owner"]}\n---\n\n{title}\n')
    _write(os.path.join(root, 'persistent_projects.json'),
           json.dumps({'alice': ['项目甲'], 'bob': []}, ensure_ascii=False, indent=2) + '\n')
    _write(os.path.join(root, 'internal_tokens.json'),
           json.dumps({'alice': 'p4-token-alice-fictional',
                       'bob': 'p4-token-bob-fictional'}, ensure_ascii=False) + '\n')
    # 虚构 Google Calendar 凭据 (问题 C): 全部含 fictional 标记, 随数据一起
    # 备份/恢复/逐文件校验 — 只验哈希, 任何步骤都不输出内容。
    gcal = os.path.join(root, 'google_calendar')
    _write(os.path.join(gcal, 'client_secret.json'),
           '{"installed": {"client_id": "fictional-client-id", '
           '"client_secret": "FICTIONAL-SECRET-DO-NOT-USE"}}')
    _write(os.path.join(gcal, 'tokens.json'),
           '{"alice": {"token": "FICTIONAL-ACCESS-TOKEN", '
           '"refresh_token": "FICTIONAL-REFRESH-TOKEN"}}')
    _write(os.path.join(gcal, 'settings.json'),
           '{"_note": "fictional rehearsal data", "alice": {"enabled": true}}')
    _write(os.path.join(gcal, 'oauth_states.json'),
           '{"_note": "fictional rehearsal data"}')
    print(f'虚构旧版数据集已生成: {root}')


# ── snapshot / diff ───────────────────────────────────────────────────────

def snapshot(root, out):
    files = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith('.tmp'):
                continue
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            files[rel] = {'sha256': sha256_file(p), 'size': os.path.getsize(p)}
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(files, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f'快照: {len(files)} 个文件 → {out}')


def diff_snapshots(old_path, new_path):
    with open(old_path, encoding='utf-8') as f:
        old = json.load(f)
    with open(new_path, encoding='utf-8') as f:
        new = json.load(f)
    old_by_hash = {}
    for rel, meta in old.items():
        old_by_hash.setdefault(meta['sha256'], []).append(rel)
    new_by_hash = {}
    for rel, meta in new.items():
        new_by_hash.setdefault(meta['sha256'], []).append(rel)

    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    common = sorted(set(old) & set(new))
    modified = [r for r in common if old[r]['sha256'] != new[r]['sha256']]
    # 移除的文件若哈希仍在树里 = 移动 (如完成归档), 不算丢失
    vanished = [r for r in removed if old[r]['sha256'] not in new_by_hash]
    moved = [r for r in removed if old[r]['sha256'] in new_by_hash]

    print(f'升级前后比对: 新增 {len(added)} / 移除 {len(removed)} '
          f'(其中移动 {len(moved)}) / 修改 {len(modified)} / 未变 {len(common) - len(modified)}')
    for r in added:
        print(f'  + {r}')
    for r in moved:
        print(f'  → {r}  (内容未变, 位置变为 {new_by_hash[old[r]["sha256"]]})')
    for r in modified:
        print(f'  ~ {r}  (内容已修改)')
    for r in vanished:
        print(f'  - {r}  (内容在升级后消失!)')
    check('升级后无文件内容凭空消失', not vanished,
          f'vanished={vanished}')
    if modified:
        finding('DIFF', '部分旧文件被修改',
                '下列文件内容变化 — 需与演练自身写操作逐一核对: '
                + ', '.join(modified[:8]) + ('…' if len(modified) > 8 else ''))
    return 0 if not FAIL else 1


# ── backup / restore ──────────────────────────────────────────────────────

def _safe_extractall(tar, dest):
    # 3.12+/3.11.4+ 支持 filter=; 旧版直接解压 (本项目的归档只含受控内容)
    try:
        tar.extractall(dest, filter='data')
    except TypeError:
        tar.extractall(dest)


# ── 备份/恢复格式约定 (Runbook §3/§4/§9 与本脚本、CI 共用同一套) ──────────
#   归档 (tar.gz): 根目录直接是数据目录内容 (todo/ recurring/ done/ lists/
#                 google_calendar/ internal_tokens.json ...), 不多包一层。
#   清单 (<tar>.sha256): JSON dict {相对路径: sha256}, 覆盖数据目录内全部
#                 文件 (含 google_calendar 凭据 — 只记哈希, 不记内容)。
#   restore 会校验两者; 不符合约定的归档会被明确拒绝, 不会静默恢复错位。

def _data_dir_layout(path):
    """已解压内容的顶层是否就是一个数据目录 (而非多包一层的包裹目录)。"""
    entries = set(os.listdir(path))
    markers = {'todo', 'recurring', 'done', 'lists', 'internal_tokens.json',
               'persistent_projects.json', 'google_calendar'}
    return bool(entries & markers)


def _collect_backup_files(data_dir):
    """数据目录里应进入备份的文件集合 (相对路径, 不含 .tmp)。

    .tmp 是 atomic_write / OAuth 写入的中间产物 (崩溃残留), 不是任务
    数据。**归档与清单必须覆盖完全相同的集合**: 早期版本清单跳过
    .tmp 但 tar 原样打包, 残留一个 .tmp 就会让恢复因"归档内容与清单
    不一致"而失败 — 演练数据集和生产数据都不允许这种不确定性。
    """
    files = {}
    for dirpath, _dirnames, filenames in os.walk(data_dir):
        for name in filenames:
            if name.endswith('.tmp'):
                continue
            p = os.path.join(dirpath, name)
            files[os.path.relpath(p, data_dir)] = p
    return files


def backup(data_dir, out_tar):
    found = _collect_backup_files(data_dir)
    files = {rel: sha256_file(p) for rel, p in found.items()}
    with tarfile.open(out_tar, 'w:gz') as tar:
        # 与清单**同一集合**: 只打包非 .tmp 文件 (相对路径保持目录结构),
        # 空目录不单独产生成员 — 恢复时按需创建。
        for rel in sorted(found):
            tar.add(found[rel], arcname=rel)
    skipped = sum(1 for dp, _dn, fn in os.walk(data_dir)
                  for n in fn if n.endswith('.tmp'))
    with open(out_tar + '.sha256', 'w', encoding='utf-8') as f:
        json.dump(files, f, ensure_ascii=False, indent=1, sort_keys=True)
    # 完整性验证: 重新解开逐一比对 (含 google_calendar 凭据 — 只比对哈希)
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(out_tar, 'r:gz') as tar:
            _safe_extractall(tar, td)
        bad = []
        for rel, want in files.items():
            p = os.path.join(td, rel)
            if not os.path.isfile(p) or sha256_file(p) != want:
                bad.append(rel)
        check('备份完整性验证 (解开逐一比对 sha256, 含 Google 凭据)', not bad,
              f'bad={bad[:5]}')
    creds = sum(1 for rel in files if rel.startswith('google_calendar/'))
    tmp_note = f'; 跳过 {skipped} 个 .tmp 残留' if skipped else ''
    print(f'备份完成: {out_tar} ({len(files)} 个文件, 其中 {creds} 个 '
          f'google_calendar 凭据文件, 全部逐文件 sha256 校验{tmp_note}; '
          f'内容未输出)')
    return 0 if not FAIL else 1


def _load_manifest(tar_path):
    """读取并校验备份清单; 不符合约定时给出可操作的错误 (问题 A 修复)。"""
    manifest_path = tar_path + '.sha256'
    if not os.path.isfile(manifest_path):
        print(f'[FATAL] 缺少清单文件 {manifest_path}')
        print('        备份必须由本脚本生成: '
              'python3 scripts/p4-rehearsal.py backup <数据目录> <归档.tar.gz>')
        print('        (Runbook §3 — 裸 tar + sha256sum 文本格式不受支持)')
        sys.exit(2)
    raw = open(manifest_path, encoding='utf-8').read()
    try:
        files = json.loads(raw)
        if not isinstance(files, dict):
            raise ValueError('not a dict')
    except (json.JSONDecodeError, ValueError):
        print(f'[FATAL] 清单 {manifest_path} 不是本脚本生成的 JSON 格式')
        print('        (检测到 GNU sha256sum 文本输出? 备份必须由本脚本生成 —')
        print('         见 Runbook §3 的格式约定; 恢复工具只消费统一格式)')
        sys.exit(2)
    return files


def verify_snapshot(tar_path):
    """**只读**验证快照 (不触碰任何恢复目标) — 清单 → 隔离解压 → 布局 →
    文件集合 → 逐文件 sha256 全部通过才返回 files dict; 任何一步不通过都
    保证"什么都没发生" (目标不存在则不创建, 存在则字节不变)。

    独立审查问题 B 修复: p5-safe-restore.sh 在移动非空目标之前必须先调
    本函数 — 损坏备份/无效清单不得导致已有恢复目标被提前移动。

    退出约定: 结构性问题 (缺清单 / 非 JSON 清单 / 归档损坏 / 多包一层
    目录 / 不像数据目录 / 归档与清单不符) → sys.exit(2); 逐文件哈希不
    匹配 → check() 记 FAIL 并返回 None (调用方按失败处理, 退出码 1)。
    """
    files = _load_manifest(tar_path)
    with tempfile.TemporaryDirectory() as td:
        try:
            with tarfile.open(tar_path, 'r:gz') as tar:
                _safe_extractall(tar, td)
        except (tarfile.TarError, EOFError, OSError, zlib.error) as e:
            print(f'[FATAL] 归档损坏或不是有效的 tar.gz: {type(e).__name__}: {e}')
            print('        恢复目标未被修改。请换用完好的备份重试。')
            sys.exit(2)
        if not _data_dir_layout(td):
            top = sorted(os.listdir(td))
            wrapped = [d for d in top if os.path.isdir(os.path.join(td, d))
                       and _data_dir_layout(os.path.join(td, d))]
            if wrapped:
                print(f'[FATAL] 归档多包了一层目录: {wrapped[0]}/')
                print('        备份必须用: tar 的根直接是数据目录内容 — 请用本脚本')
                print('        的 backup 子命令生成 (Runbook §3), 而不是')
                print('        "tar czf ... -C <父目录> <数据目录名>"。')
                sys.exit(2)
            print(f'[FATAL] 归档内容不像一个数据目录 (顶层: {top[:6]})')
            sys.exit(2)
        extracted = set()
        for dirpath, _dirnames, filenames in os.walk(td):
            for name in filenames:
                extracted.add(os.path.relpath(os.path.join(dirpath, name), td))
        missing = sorted(set(files) - extracted)
        extra = sorted(extracted - set(files))
        if missing or extra:
            print(f'[FATAL] 归档内容与清单不一致: 缺 {len(missing)} 个, '
                  f'多 {len(extra)} 个 (例: missing={missing[:3]}, extra={extra[:3]})')
            print('        恢复目标未被修改。归档与清单不配套 — 请用同一备份')
            print('        产生的 <tar>.sha256。')
            sys.exit(2)
        bad = [rel for rel, want in files.items()
               if sha256_file(os.path.join(td, rel)) != want]
        if bad:
            check('归档完整性验证 (隔离解压后逐文件比对 sha256, 含 Google 凭据)',
                  False, f'{len(bad)} 个文件哈希不匹配 (例: {bad[:3]})')
            print('        恢复目标未被修改 — 验证先于任何写入。请换用完好备份。')
            return None
        check('归档完整性验证 (隔离解压后逐文件比对 sha256, 含 Google 凭据)', True)
    return files


class RestoreSwitchError(Exception):
    """切换写入失败 (已尝试回滚)。

    failed = 回滚后仍未复原的目标路径列表 (删不掉的新文件 + 还原不了的
    旧文件); 非空 = 回滚不完整, 现场必须保留, 由人工恢复。
    unrestored 保留计数语义 (== len(failed))。
    """

    def __init__(self, message, failed=None):
        super().__init__(message)
        self.failed = list(failed or [])
        self.unrestored = len(self.failed)


def _rmtree_quiet(path):
    shutil.rmtree(path, ignore_errors=True)


def _report_incomplete_rollback(failed, backup_root):
    """回滚不完整的统一报告 — 只列路径, 不输出任何文件内容。

    返回 True: 调用方必须保留现场 (backup_root), 因为旧内容的唯一副本
    可能只在里面; 清理它等于毁掉人工恢复的原料。
    """
    print(f'[FATAL] 回滚不完整 ({len(failed)} 个文件未还原) — '
          f'旧内容现场保留在 {backup_root}, 请人工恢复!')
    for p in failed[:10]:
        print(f'        未还原: {p}')
    if len(failed) > 10:
        print(f'        …另有 {len(failed) - 10} 个未还原 '
              f'(见上方逐条 FATAL, 均只报告路径)')
    return True


def _rollback_switch(dest_dir, written_new, replaced, created_dirs, dest_existed):
    """切换写入失败后的完整回滚: 删本次写入 → 还原被覆盖文件 → 清理新建目录。

    返回未能复原的目标路径列表 (空 = 目标已恢复到写入前状态):
      - 本次写入的新文件删除失败 — 目标里会多出不该有的文件
      - 被覆盖的旧文件还原失败 — 目标里是回滚后的新内容或文件缺失
    任一类失败都意味着自动回滚不完整 — 调用方必须保留现场并报告,
    由人工恢复; 只报告路径, 不输出文件内容。
    """
    failed = []
    for dst in reversed(written_new):
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError as e:
            failed.append(dst)
            print(f'[FATAL] 回滚失败: 本次写入的 {dst} 未能删除 ({e}) — 请人工检查!')
    for bak, dst in replaced:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(bak, dst)
        except OSError as e:
            failed.append(dst)
            print(f'[FATAL] 回滚失败: {dst} 未能从 {bak} 还原 ({e}) — 请人工检查!')
    for d in reversed(created_dirs):
        try:
            os.rmdir(d)      # 只删本次新建的空目录; 非空 (异常现场) 会失败并保留
        except OSError:
            pass
    if not dest_existed:
        try:
            os.rmdir(dest_dir)   # 目标原本不存在 → 回滚后必须继续不存在
        except OSError:
            pass
    return failed


def _switch_into_place(staging, dest_dir, files, backup_root):
    """把 staging 内已验证内容合并写入 dest_dir (O3-N3 核心)。

    设计要点 (对应 Codex 复现的 OSError [Errno 18] Invalid cross-device link):
    - **staging 建在 dest_dir 的父目录** — 与目标同一文件系统, 因此
      os.replace 是同文件系统内的瞬时替换。旧实现用 tempfile.Temporary-
      Directory() (/tmp 在 Linux 上常为 tmpfs) 再 os.replace, 跨文件系统
      直接 Errno 18, 且循环中途失败会留下**部分恢复** (部分文件已写、
      工具却可能报告成功路径)。
    - 每个将被覆盖的旧文件先复制进 backup_root (同文件系统); 任何 OSError
      → 完整回滚 (删新建、还原旧内容) 后抛 RestoreSwitchError — 失败后
      目标逐字节回到写入前, 绝不会留下"冒充成功的部分恢复"。回滚本身
      不完整时 RestoreSwitchError.failed 列出未复原路径, 现场必须保留。
    """
    dest_existed = os.path.isdir(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)
    written_new, replaced, created_dirs = [], [], []
    try:
        for rel in sorted(files):
            src = os.path.join(staging, rel)
            dst = os.path.join(dest_dir, rel)
            parent = os.path.dirname(dst)
            if not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
                created_dirs.append(parent)
            if os.path.exists(dst):
                bak = os.path.join(backup_root, rel)
                bak_parent = os.path.dirname(bak)
                if not os.path.isdir(bak_parent):
                    os.makedirs(bak_parent, exist_ok=True)
                    created_dirs.append(bak_parent)
                shutil.copy2(dst, bak)     # 旧内容先进备份 (可回滚)
                replaced.append((bak, dst))
            os.replace(src, dst)           # 同文件系统瞬时替换
            written_new.append(dst)
    except OSError as e:
        failed = _rollback_switch(dest_dir, written_new, replaced,
                                  created_dirs, dest_existed)
        raise RestoreSwitchError(str(e), failed)
    return written_new, replaced, created_dirs, dest_existed


def restore(tar_path, dest_dir, force=False):
    """恢复备份 — **先验证, 后写入** (独立审查问题 1/B 修复; O3-N3 加固)。

    顺序:
      1. 目标目录策略 (非空且无 --force → 拒绝; 此阶段无任何写入)
      2. 快照全面验证 (verify_snapshot: 隔离解压 + 布局/集合/逐文件哈希;
         绝不触碰 dest_dir)
      3. **目标文件系统内 staging**: 解压到 <dest 父目录>/.<dest>.restore-
         staging-<pid> — 与目标同文件系统 (跨文件系统恢复 fix)
      4. staging 内**重验 SHA-256** (写目标前的最后一道闸)
      5. 切换写入: 同文件系统 os.replace; 每个被覆盖文件先备份, 任何
         失败完整回滚 — 损坏快照/权限不足/写入中断都 fail closed
      6. 恢复后终检 (目标侧逐文件 sha256, 含 Google 凭据) + 遗留文件告警

    失败语义 (独立审查问题 A/B 修复, 不得夸大):
      - **写入前**失败 (目标策略/快照验证/staging 重验): 恢复目标**不被
        修改** — 损坏的备份不会破坏目标。
      - **写入后**失败 (切换中途 / 终检哈希不符 / 终检读取异常 OSError /
        切换后其它 OSError): 一律**回滚到写入前状态**; 回滚完整时目标
        与写入前一致。回滚本身不完整 (删不掉新文件 / 还原不了旧文件)
        时保留 backup_root 现场、逐条报告未复原路径 (只报路径不报内容),
        由人工恢复 — 此时**不得**声称"目标未被修改", 也不得清理现场。

    非空目标默认拒绝 (防误覆盖), 确认要覆盖时加 --force; --force 属于
    覆盖合并语义: 归档内文件覆盖同名旧文件, 归档外的遗留文件会被明确
    列出告警 (防新旧文件静默混合)。
    """
    # ── 阶段 1: 目标目录策略 (不做任何写入; 目标不存在也不创建) ──
    if os.path.exists(dest_dir) and not os.path.isdir(dest_dir):
        print(f'[FATAL] 恢复目标已存在且不是目录: {dest_dir}')
        sys.exit(2)
    if os.path.isdir(dest_dir) and os.listdir(dest_dir) and not force:
        print(f'[FATAL] 恢复目标 {dest_dir} 非空 '
              f'({len(os.listdir(dest_dir))} 个条目) — 拒绝覆盖。')
        print('        请清空目标、换一个空目录, 或确认要用归档内容覆盖时')
        print('        加 --force (内容本身仍会先通过全部校验才会写入)。')
        sys.exit(2)
    # ── 阶段 2: 快照全面验证 (与 verify 子命令同一代码路径; 此阶段绝不
    #    触碰 dest_dir — 不存在则不创建, 存在则字节不变) ──
    files = verify_snapshot(tar_path)
    if files is None:
        return 1
    # ── 阶段 3: staging = 目标文件系统内的临时目录 (O3-N3) ──
    # 旧实现在 /tmp (常为 tmpfs) 解压再 os.replace 进目标 — 跨文件系统即
    # OSError [Errno 18] Invalid cross-device link, 且中途失败留下部分恢复。
    # 现在 staging 是 dest 的"邻居": 同文件系统, 替换瞬时且可回滚。
    dest_dir = os.path.normpath(dest_dir)
    parent = os.path.dirname(dest_dir) or '.'
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as e:
        print(f'[FATAL] 无法创建目标父目录 {parent}: {e}')
        print('        恢复目标未被修改。')
        return 1
    base = os.path.basename(dest_dir)
    staging = os.path.join(parent, f'.{base}.restore-staging-{os.getpid()}')
    backup_root = f'{staging}-rollback'
    if os.path.exists(staging) or os.path.exists(backup_root):
        print(f'[FATAL] staging 目录已存在: {staging}')
        print('        可能有另一个恢复进程正在运行 — 恢复目标未被修改。')
        return 1
    keep_backup = False
    # 切换现场记账: 阶段 5 之后目标已被写过 — 此后任何失败都必须回滚,
    # 而不是谎称"未被修改" (独立审查问题 A: 终检 OSError 曾掉进外层
    # 处理直接返回, 留下已修改的目标+被删除的备份)。
    written_new, replaced, created_dirs, dest_existed = [], [], [], True
    switched = False
    try:
        with tarfile.open(tar_path, 'r:gz') as tar:
            _safe_extractall(tar, staging)
        # ── 阶段 4: staging 侧重验 SHA-256 (写目标前的最后一道闸) ──
        bad = [rel for rel, want in files.items()
               if not os.path.isfile(os.path.join(staging, rel))
               or sha256_file(os.path.join(staging, rel)) != want]
        if bad:
            check('staging 完整性验证 (写入目标前在 staging 内重验 sha256)', False,
                  f'{len(bad)} 个文件哈希不匹配 (例: {bad[:3]})')
            print('        恢复目标未被修改 — 不进入写入阶段。')
            return 1
        check('staging 完整性验证 (写入目标前在 staging 内重验 sha256)', True)
        # ── 阶段 5: 切换写入 (同文件系统; 失败完整回滚) ──
        leftovers = sorted(
            os.path.relpath(os.path.join(dp, n), dest_dir)
            for dp, _dn, fn in os.walk(dest_dir) for n in fn
            if os.path.relpath(os.path.join(dp, n), dest_dir) not in files)
        try:
            written_new, replaced, created_dirs, dest_existed = _switch_into_place(
                staging, dest_dir, files, backup_root)
            switched = True
        except RestoreSwitchError as e:
            print(f'[FATAL] 写入目标失败 (已尝试回滚): {e}')
            if e.failed:
                keep_backup = _report_incomplete_rollback(e.failed, backup_root)
            else:
                print('        已完整回滚 — 恢复目标与写入前一致 '
                      '(切换已发生并被完整撤销, 不是"未被修改")。')
            return 1
        # 恢复位置语义: dest_dir 本身就是旧版程序的数据目录 (必须直接含 todo/ 等)
        if not _data_dir_layout(dest_dir):
            print(f'[FATAL] 恢复目标 {dest_dir} 不像一个数据目录 '
                  '(缺少 todo/ lists/ 等) — 恢复目标必须是数据目录本身,')
            print('        不是它的父目录 (旧版程序要求数据目录直接含 todo/)。')
            sys.exit(2)
        # 恢复后终检: 目标侧再验一次 (含 Google 凭据); 不过 → 回滚
        # 终检自身读取异常 (OSError) 与哈希不符同等对待 — 无法证明恢复
        # 完整就必须回滚; 此刻切换已发生, 绝不允许掉进"目标未被修改"的
        # 虚假话术 (独立审查问题 A)。
        final_error = None
        bad = []
        try:
            bad = [rel for rel, want in files.items()
                   if not os.path.isfile(os.path.join(dest_dir, rel))
                   or sha256_file(os.path.join(dest_dir, rel)) != want]
        except OSError as e:
            final_error = f'{type(e).__name__}: {e}'
        check('恢复完整性验证 (目标侧逐一比对 sha256, 含 Google 凭据)',
              not bad and final_error is None,
              final_error or f'bad={bad[:5]}')
        if final_error is not None or bad:
            failed = _rollback_switch(dest_dir, written_new, replaced,
                                      created_dirs, dest_existed)
            if failed:
                keep_backup = _report_incomplete_rollback(failed, backup_root)
            else:
                print('        已完整回滚 — 恢复目标与写入前一致 '
                      '(终检失败/异常后的切换已被完整撤销)。')
            return 1
    except (OSError, tarfile.TarError, EOFError, zlib.error) as e:
        print(f'[FATAL] 恢复失败: {type(e).__name__}: {e}')
        if switched:
            # 切换已发生 — 与终检失败同等对待: 回滚; 不完整则保留现场
            failed = _rollback_switch(dest_dir, written_new, replaced,
                                      created_dirs, dest_existed)
            if failed:
                keep_backup = _report_incomplete_rollback(failed, backup_root)
            else:
                print('        已完整回滚 — 恢复目标与写入前一致。')
        else:
            print('        恢复目标未被修改 (失败先于任何写入)。')
        return 1
    finally:
        _rmtree_quiet(staging)
        if keep_backup:
            print(f'[WARN] 保留回滚现场以便人工恢复: {backup_root}')
        else:
            _rmtree_quiet(backup_root)
    if leftovers:
        # --force 覆盖合并: 归档外的遗留文件仍在目标里 — 明确告警,
        # 防止新旧文件静默混合 (操作员需人工确认它们的去留)。
        print(f'[WARN] 目标中存在 {len(leftovers)} 个归档之外的遗留文件 '
              f'(覆盖合并后仍保留): {leftovers[:5]}'
              + ('…' if len(leftovers) > 5 else ''))
        print('       请人工核对这些文件是否应保留 — 恢复目标本应恰好等于归档内容。')
    creds = sum(1 for rel in files if rel.startswith('google_calendar/'))
    print(f'恢复完成: {dest_dir} ({len(files)} 个文件, 其中 {creds} 个 '
          f'google_calendar 凭据文件已校验; 内容未输出)')
    return 0 if not FAIL else 1


# ── old-check (旧版 API) ──────────────────────────────────────────────────

def old_check(base, token_user):
    """旧版对旧数据: 读全部 + 真实写操作 (创建/编辑/完成/恢复/列表)。

    旧版 header 认证在净化基线里不可用 (占位邮箱大小写不匹配),
    因此走内部 token 通道 —— 这也是真实生产里 y8ysystems 使用的路径。
    """
    H = {'X-Internal-Token': _token(token_user)}
    print('== 旧版读取旧数据 ==')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    check('旧版 GET /todo/api/tasks → 200', st == 200, f'got {st}')
    titles = [t['title'] for t in body.get('tasks', [])]
    for want in ('买牛奶', '备注-周五', '已同步任务', '项目任务一'):
        check(f'旧版可见任务「{want}」', want in titles, f'got {titles}')
    check('旧版看不到他人私有任务 (提交周报 = bob 私有)',
          '提交周报' not in titles, f'got {titles}')
    done_titles = [t['title'] for t in body.get('completed_tasks', [])]
    check('旧版可见已完成任务', '旧已完成任务' in done_titles, f'got {done_titles}')
    check('旧版契约字段完整',
          {'current_user', 'tasks', 'completed_tasks', 'sections'} <= set(body))
    st, lists = req(base, 'GET', '/todo/api/lists', headers=H)
    check('旧版可见自定义列表', st == 200 and any(l['name'] == '杂货' for l in lists.get('lists', [])),
          f'got {st} {lists}')
    st, shop = req(base, 'GET', '/todo/api/shopping', headers=H)
    check('旧版可见购物条目', st == 200 and '纸巾' in [i['title'] for i in shop.get('items', [])])

    # 第二用户视角: 私有/共享可见性 (多用户生产形态)
    W = {'X-Internal-Token': _token('bob')}
    st, wbody = req(base, 'GET', '/todo/api/tasks', headers=W)
    wtitles = [t['title'] for t in wbody.get('tasks', [])]
    check('旧版 bob 可见自己的私有任务', '提交周报' in wtitles, f'got {wtitles}')
    check('旧版 bob 看不到 alice 的私有任务', '买牛奶' not in wtitles, f'got {wtitles}')
    check('旧版 bob 可见共享任务', '备注-周五' in wtitles, f'got {wtitles}')

    print('== 旧版写操作 (证明数据集可被旧版真实使用) ==')
    st, created = req(base, 'POST', '/todo/api/tasks',
                      {'title': '旧版新建任务', 'due': (date.today() + timedelta(days=2)).isoformat()},
                      headers=H)
    check('旧版创建任务 → 201', st == 201, f'got {st}')
    old_new_id = created.get('id', '')
    check('旧版新建任务按标题命名 (无 uid 后缀)', old_new_id == '旧版新建任务.md', f'got {old_new_id}')
    st, _ = req(base, 'PUT', f'/todo/api/tasks/{quote(old_new_id)}',
                {'title': '旧版改名任务', 'content': '改过的内容'}, headers=H)
    check('旧版改名 → 200', st == 200, f'got {st}')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    names = [t['title'] for t in body.get('tasks', [])]
    check('旧版改名后按新文件名显示', '旧版改名任务' in names, f'got {names}')
    st, done = req(base, 'POST', f'/todo/api/tasks/{quote("旧版改名任务.md")}/done', headers=H)
    check('旧版完成任务 → 200', st == 200, f'got {st}')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    check('旧版完成后移出活动列表', '旧版改名任务' not in [t['title'] for t in body.get('tasks', [])])
    st, _ = req(base, 'PUT', '/todo/api/tasks/done%2F'
                + quote(date.today().isoformat()) + '%2F'
                + quote('旧版改名任务.md'),
                {'status': 'todo'}, headers=H)
    check('旧版恢复已完成任务 → 200', st == 200, f'got {st}')
    st, _ = req(base, 'POST', '/todo/api/lists/groceries/items',
                {'title': '旧版加的条目'}, headers=H)
    check('旧版列表加条目 → 201', st == 201, f'got {st}')
    st, _ = req(base, 'POST', '/todo/api/shopping', {'title': '旧版购物品'}, headers=H)
    check('旧版购物加物品 → 201', st == 201, f'got {st}')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    gcal_task = [t for t in body.get('tasks', []) if t['title'] == '已同步任务']
    check('旧版读取带 gcal 字段的任务不报错',
          len(gcal_task) == 1 and 'Error reading' not in gcal_task[0].get('body', ''))
    print(f'\n旧版验证: {len(PASS)} 通过, {len(FAIL)} 失败, {len(FINDING)} 发现')
    return 0 if not FAIL else 1


def _token(user):
    path = os.environ.get('P4_TOKENS_FILE', '')
    if path and os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f).get(user, '')
    return {'alice': 'p4-token-alice-fictional',
            'bob': 'p4-token-bob-fictional'}.get(user, '')


# ── new-check (新版完整生命周期) ───────────────────────────────────────────

def new_check(base, email, token_user, bob_email='bob@example.com'):
    H = {'Cf-Access-Authenticated-User-Email': email}
    M = {'X-Internal-Token': _token(token_user)}
    W = {'Cf-Access-Authenticated-User-Email': bob_email}
    today = date.today()
    due = (today + timedelta(days=3)).isoformat()
    state = {}

    print('== 新版读取旧数据 (升级后第一眼) ==')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    check('新版 GET /todo/api/tasks → 200', st == 200, f'got {st}')
    titles = [t['title'] for t in body.get('tasks', [])]
    for want in ('买牛奶', '备注-周五', '项目任务一', '已同步任务'):
        check(f'新版以文件名回退正确显示「{want}」', want in titles, f'got {titles}')
    check('新版看不到他人私有任务 (提交周报 = bob 私有)',
          '提交周报' not in titles, f'got {titles}')
    st, wbody = req(base, 'GET', '/todo/api/tasks', headers=W)
    wtitles = [t['title'] for t in wbody.get('tasks', [])]
    check('新版 bob 可见自己的私有任务', '提交周报' in wtitles, f'got {wtitles}')
    check('新版 bob 看不到 alice 的私有任务', '买牛奶' not in wtitles, f'got {wtitles}')
    check('新版 bob 可见共享任务', '备注-周五' in wtitles, f'got {wtitles}')
    done_titles = [t['title'] for t in body.get('completed_tasks', [])]
    check('新版可见历史已完成任务', '旧已完成任务' in done_titles, f'got {done_titles}')
    st, shop = req(base, 'GET', '/todo/api/shopping', headers=H)
    check('新版可见购物条目', '纸巾' in [i['title'] for i in shop.get('items', [])])
    st, lists = req(base, 'GET', '/todo/api/lists', headers=H)
    check('新版可见自定义列表', any(l['name'] == '杂货' for l in lists.get('lists', [])))
    st, pp = req(base, 'GET', '/todo/api/persistent-projects', headers=H)
    check('新版继承持久项目', pp.get('projects') == ['项目甲'], f'got {pp}')

    print('== 新版完整生命周期 ==')
    st, a = req(base, 'POST', '/todo/api/tasks', {'title': '同名任务', 'due': due}, headers=H)
    st2, b = req(base, 'POST', '/todo/api/tasks', {'title': '同名任务', 'due': due}, headers=H)
    check('同名任务创建两个独立文件 (P2 行为)', st == 201 and st2 == 201
          and a.get('id') != b.get('id'), f'{a.get("id")} vs {b.get("id")}')
    state['same_a'], state['same_b'] = a.get('id', ''), b.get('id', '')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    check('两个同名任务都可见', len([t for t in body['tasks'] if t['title'] == '同名任务']) == 2)
    check('新任务 id 带 uid 后缀', '-' in state['same_a'], f'got {state["same_a"]}')

    st, ren = req(base, 'PUT', f'/todo/api/tasks/{quote(state["same_a"])}',
                  {'title': '改名后的任务'}, headers=H)
    check('新版改名 → 200', st == 200, f'got {st}')
    check('新版改名后文件名/id 稳定 (uid 保留)',
          ren.get('id', '').startswith(state['same_a'].rsplit('-', 1)[0] + '-')
          or ren.get('id') == state['same_a'], f'got {ren.get("id")}')

    st, done = req(base, 'POST', f'/todo/api/tasks/{quote(state["same_b"])}/done', headers=H)
    check('新版完成任务 → 200', st == 200, f'got {st}')
    check('完成响应返回可解析归档 id', done.get('id', '').startswith('done/'))
    st, _ = req(base, 'PUT', f'/todo/api/tasks/{quote(done.get("id", ""))}',
                {'status': 'todo'}, headers=H)
    check('新版恢复任务 → 200', st == 200, f'got {st}')

    st, proj = req(base, 'POST', '/todo/api/tasks',
                   {'title': '新项目任务', 'due': due, 'project': '项目甲'}, headers=H)
    check('新版项目任务创建 → 201', st == 201, f'got {st}')
    check('项目任务落在项目子目录', proj.get('id', '').startswith('项目甲/'))

    st, rec = req(base, 'POST', '/todo/api/tasks',
                  {'title': '新重复任务', 'due': date(today.year, today.month, 1).isoformat(),
                   'recurring': True, 'recurring_dates': [1, 15]}, headers=H)
    check('新版重复任务创建 → 201', st == 201, f'got {st}')
    st, recd = req(base, 'POST', f'/todo/api/tasks/{quote(rec.get("id", ""))}/done', headers=H)
    check('新版重复任务推进 → 200', st == 200 and recd.get('next_due', '') >
          date(today.year, today.month, 1).isoformat(),
          f'got {st} {recd.get("next_due")}')

    st, _ = req(base, 'POST', '/todo/api/lists/groceries/items', {'title': '新版加的条目'}, headers=H)
    check('新版列表加条目 → 201', st == 201, f'got {st}')
    st, _ = req(base, 'POST', '/todo/api/shopping', {'title': '新版购物品'}, headers=H)
    check('新版购物加物品 → 201', st == 201, f'got {st}')
    st, _ = req(base, 'PUT', '/todo/api/persistent-projects', {'projects': ['项目甲', '新项目']}, headers=H)
    check('新版持久项目写入 → 200', st == 200, f'got {st}')

    print('== y8ysystems 机器契约 ==')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=M)
    check('契约端点 → 200', st == 200, f'got {st}')
    for key in ('current_user', 'tasks', 'completed_tasks', 'sections'):
        check(f'契约字段 {key}', key in body)
    check('契约 sections 完整',
          set(body.get('sections', {}).keys()) == {'overdue', 'today', 'this_week', 'future', 'nodue'})
    if body.get('tasks'):
        for key in ('id', 'title', 'due', 'due_end', 'due_time', 'created',
                    'color', 'status', 'recurring', 'project', 'owner', 'body'):
            check(f'任务字段 {key}', key in body['tasks'][0])

    print('== 旧数据文件在新版写操作后的保持性 ==')
    # 触发一次对「已同步任务」的重写 (改色), 验证 gcal 字段不丢
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    synced = [t for t in body['tasks'] if t['title'] == '已同步任务']
    if synced:
        st, _ = req(base, 'PUT', f'/todo/api/tasks/{quote(synced[0]["id"])}',
                    {'color': 'purple'}, headers=H)
        check('新版编辑旧 gcal 任务 → 200', st == 200, f'got {st}')
    print(f'\n新版验证: {len(PASS)} 通过, {len(FAIL)} 失败, {len(FINDING)} 发现')
    return 0 if not FAIL else 1


# ── rollback-check (旧版读新版写过的数据) ─────────────────────────────────

def rollback_check(base, token_user, data_dir=''):
    """drill B: 不恢复备份, 旧版直接读新版写过的数据。

    PASS = 回滚安全性承诺; FINDING = 已确认的回滚风险 (Runbook 有缓解措施)。
    data_dir 可选: 给定则直接读盘验证 frontmatter 细节。
    """
    H = {'X-Internal-Token': _token(token_user)}
    print('== 回滚 drill B: 旧版直接读取新版写过的数据 ==')
    st, body = req(base, 'GET', '/todo/api/tasks', headers=H)
    check('旧版能启动并服务新版数据目录 → 200', st == 200, f'got {st}')
    tasks = body.get('tasks', [])
    broken = [t['title'] for t in tasks if 'Error reading file' in t.get('body', '')]
    check('无文件解析错误 (旧版不崩、不丢文件)', not broken, f'broken={broken}')
    titles = [t['title'] for t in tasks]
    legacy_visible = [t for t in ('买牛奶', '备注-周五', '项目任务一', '已同步任务') if t in titles]
    check('旧版任务文件全部仍可读', len(legacy_visible) == 4, f'visible={legacy_visible}')

    # F1: 新格式文件名 → 旧版显示 slug+uid
    new_style = [t for t in tasks if re.search(r'-[0-9a-f]{8}$', t['title'])]
    if new_style:
        finding('F1', '回滚后新格式任务的标题显示为 <slug>-<8位uid>',
                f'旧版从文件名派生标题, 例: {new_style[0]["title"]!r}; '
                'frontmatter 里的真实 title 字段仍在文件中, 重新升级后自动恢复')
    else:
        check('(无新格式文件需要回滚显示)', True)

    st, done_body = req(base, 'GET', '/todo/api/tasks', headers=H)
    done_titles = [t['title'] for t in done_body.get('completed_tasks', [])]
    check('done/ 归档在新版写入后仍被旧版读取', '旧已完成任务' in done_titles, f'got {done_titles}')

    st, lists = req(base, 'GET', '/todo/api/lists', headers=H)
    check('自定义列表仍可读', st == 200 and any(l['name'] == '杂货' for l in lists.get('lists', [])))
    st, items = req(base, 'GET', '/todo/api/lists/groceries/items', headers=H)
    item_titles = [i['title'] for i in items.get('items', [])]
    check('列表条目 (含新版添加的) 仍可读', '新版加的条目' in item_titles, f'got {item_titles}')
    st, shop = req(base, 'GET', '/todo/api/shopping', headers=H)
    check('购物条目仍可读', '新版购物品' in [i['title'] for i in shop.get('items', [])])
    st, pp = req(base, 'GET', '/todo/api/persistent-projects', headers=H)
    check('持久项目仍可读', '新项目' in pp.get('projects', []), f'got {pp}')

    print('== 回滚后旧版写操作的风险实证 ==')
    # F2: 旧版改名 → uid 丢失 + title 字段变陈旧
    new_task = next((t for t in tasks if re.search(r'-[0-9a-f]{8}$', t['title'])), None)
    if new_task:
        old_id = new_task['id']
        st, resp = req(base, 'PUT', f'/todo/api/tasks/{quote(old_id)}',
                       {'title': '回滚后旧版改名'}, headers=H)
        new_id = resp.get('id', '')
        new_title = '回滚后旧版改名'

        def _disk_title(fname):
            if not data_dir:
                return None
            for folder in ('todo', 'recurring'):
                p = os.path.join(data_dir, folder, fname)
                if os.path.isfile(p):
                    with open(p, encoding='utf-8') as f:
                        raw = f.read()
                    m = re.search(r'^title:\s*(.*)$', raw, re.MULTILINE)
                    return m.group(1).strip() if m else ''
            return None

        title_before = _disk_title(old_id)
        if st == 200:
            has_uid = bool(re.search(r'-[0-9a-f]{8}\.md$', new_id))
            if not has_uid:
                finding('F2', '旧版改名会重写文件名为标题式, uid 后缀丢失',
                        f'{old_id!r} → {new_id!r}; 重新升级后该任务 id 改变')
                title_after = _disk_title(new_id)
                if title_after is not None and title_after != new_title:
                    finding('F2b', '旧版改名后 frontmatter title 保留旧值 (不同步)',
                            f'文件已改名为 {new_id!r}, 但 title 字段仍是 '
                            f'{title_after!r} (用户意图的新标题是 {new_title!r}) — '
                            '重新升级后新版会显示旧标题, 需人工校对')
                elif title_after is not None:
                    check('旧版改名后 title 字段与新标题一致', True)
            else:
                check('旧版改名保留 uid (意外之喜)', True)
        else:
            check('旧版改名请求本身成功', False, f'got {st}')

    # F3: 旧版同名创建 → 静默覆盖
    st, first = req(base, 'POST', '/todo/api/tasks', {'title': '回滚覆盖验证'}, headers=H)
    st2, second = req(base, 'POST', '/todo/api/tasks',
                      {'title': '回滚覆盖验证', 'content': '第二个'}, headers=H)
    if st == 201 and st2 == 201 and first.get('id') == second.get('id'):
        finding('F3', '旧版同名创建静默覆盖 (P2 已修的行为在回滚后复现)',
                '两个请求都返回 201 但只落一个文件 — 回滚期必须在旧版里冻结写入')
    else:
        check('旧版同名创建不互相覆盖', first.get('id') != second.get('id'),
              f'{first.get("id")} vs {second.get("id")}')

    # F4: 旧版认证模型无受信代理检查 — 代码审查结论 (源码 824-837 行)。
    # 净化基线把真实邮箱换成了占位符且大小写不匹配, 无法行为复现, 如实标注。
    finding('F4', '回滚后身份 header 可被伪造 (P1 安全修复丢失) — 代码审查结论',
            '旧版 _current_user 对任意来源的 Cf-Access-Authenticated-User-Email '
            '无条件采信 (无 Y8Y_TRUSTED_PROXIES 检查); 净化基线的占位邮箱大小写'
            '不匹配导致无法行为复现; 回滚期间必须由反向代理保证 header 只来自'
            '认证后的代理, 且旧版写盘为非原子写 (P2 修复同样丢失)')
    print(f'\n回滚核查: {len(PASS)} 通过, {len(FAIL)} 失败, {len(FINDING)} 发现')
    return 0 if not FAIL else 1


# ── creds-check (虚构 Google 凭据, 仅 CI 用) ──────────────────────────────

CREDS_FILES = ('client_secret.json', 'tokens.json', 'settings.json',
               'oauth_states.json')


def creds_check(data_dir):
    """CI 专用: 验证数据集里的 Google 凭据齐全且**仍为虚构内容**。

    只输出通过/失败与文件数量 — 绝不输出任何凭据内容。
    注意: 这是演练数据集的卫生检查 (防真实凭据混入 fixture);
    **不能**用于生产备份验证 — 生产凭据不含 fictional 标记, 请用
    creds-verify (以归档清单 + SHA-256 为准)。
    """
    gdir = os.path.join(data_dir, 'google_calendar')
    if not os.path.isdir(gdir):
        check('google_calendar 目录存在', False, f'{gdir} 缺失')
        return 1
    present = [f for f in CREDS_FILES
               if os.path.isfile(os.path.join(gdir, f))]
    check(f'虚构 Google 凭据文件齐全 ({len(present)}/{len(CREDS_FILES)})',
          len(present) == len(CREDS_FILES), f'present={present}')
    non_fictional = []
    for f in CREDS_FILES:
        p = os.path.join(gdir, f)
        if os.path.isfile(p):
            text = open(p, encoding='utf-8').read()
            if 'fictional' not in text.lower():
                non_fictional.append(f)
    check('凭据文件均为虚构内容 (无真实凭据混入演练)', not non_fictional,
          f'non-fictional={non_fictional}')
    print(f'  google_calendar: {len(present)}/{len(CREDS_FILES)} 个虚构凭据'
          f'文件, 内容未输出')
    return 0 if not FAIL else 1


# ── creds-verify (生产恢复后的凭据完整性, 不依赖 fictional 标记) ──────────

def creds_verify(tar_path, data_dir):
    """生产恢复后的 Google 凭据完整性验证 (独立审查问题 2 修复)。

    以归档清单为准: 清单覆盖的 google_calendar 文件必须存在于
    data_dir 且 SHA-256 与清单一致。**不要求**任何内容标记 — 生产
    凭据本来就不会带 fictional 字样; 也绝不输出凭据内容, 只报
    通过/失败与文件数量。
    """
    files = _load_manifest(tar_path)
    creds = {rel: h for rel, h in files.items()
             if rel.startswith('google_calendar/')}
    if not creds:
        check('归档清单包含 google_calendar 凭据记录', False,
              '清单中没有任何凭据记录 — 若该部署未配置 Google 同步则属正常, '
              '否则说明备份不完整')
        return 1
    missing = [rel for rel in creds
               if not os.path.isfile(os.path.join(data_dir, rel))]
    bad = [rel for rel in creds if rel not in missing
           and sha256_file(os.path.join(data_dir, rel)) != creds[rel]]
    check(f'凭据文件存在 ({len(creds) - len(missing)}/{len(creds)})',
          not missing, f'missing={missing}')
    check('凭据文件 SHA-256 与归档清单一致', not bad, f'bad={bad}')
    print(f'  google_calendar: {len(creds)} 个凭据文件经归档清单核验; '
          f'内容未输出')
    return 0 if not FAIL else 1


# ── main ──────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    rc = None
    try:
        if cmd == 'build-data':
            build_data(args[0])
            rc = 0
        elif cmd == 'snapshot':
            snapshot(args[0], args[1])
            rc = 0
        elif cmd == 'diff-snapshots':
            rc = diff_snapshots(args[0], args[1])
        elif cmd == 'backup':
            rc = backup(args[0], args[1])
        elif cmd == 'restore':
            force = '--force' in args
            pos = [a for a in args if not a.startswith('--')]
            rc = restore(pos[0], pos[1], force=force)
        elif cmd == 'verify':
            rc = 0 if verify_snapshot(args[0]) is not None else 1
        elif cmd == 'creds-check':
            rc = creds_check(args[0])
        elif cmd == 'creds-verify':
            rc = creds_verify(args[0], args[1])
        elif cmd == 'old-check':
            rc = old_check(args[0], args[1])
        elif cmd == 'new-check':
            rc = new_check(args[0], args[1], args[2])
        elif cmd == 'rollback-check':
            data_dir = args[2] if len(args) > 2 else ''
            rc = rollback_check(args[0], args[1], data_dir)
        else:
            print(f'unknown command: {cmd}')
            return 2
    except IndexError:
        print(__doc__)
        return 2
    print(f'\n结果: {len(PASS)} 通过, {len(FAIL)} 失败, {len(FINDING)} 发现')
    if FAIL:
        print('失败项:', FAIL)
        return 1
    return rc if rc is not None else 0


if __name__ == '__main__':
    sys.exit(main())
