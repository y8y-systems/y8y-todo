#!/usr/bin/env python3
"""check-notify-isolation.py — 通知凭据隔离审计 (只读)

回答一个问题: **Web 进程能不能读到通知调度器手里的凭据?**

检查项:

  C1  单元的运行用户: Web 与调度器必须不是同一个 UID
  C2  凭据文件权限:    必须是 root 拥有, 且 group/other 不可读;
                       并且 Web 单元**不得**引用凭据文件
  C3  受控测试进程的实际 UID: 必须为调度器 UID 且与 Web UID 不同
  C4  跨用户读取实测:  以 Web 用户身份读测试进程的 environ 必须明确被拒
  C5  调度器对数据目录: 必须能读任务目录与用户偏好, 且**能写状态目录**
  C6  Web 对状态目录:   必须能读能力表、写用户偏好
  C7  通知单元的可写范围: 不得覆盖整个数据目录 (那里有机器凭据)

不改持久状态: 启动只含虚构环境的短暂探针; 不建用户、不改权限、不启停单元、不读真实凭据。

**Fail closed**: 需要 root 才能完成的检查项, 在非 root 环境下标记为
"无法验证"而不是"通过"; 判定不了 (降权失败、子进程起不来) 同样算
"无法验证"。退出码 2 = 有无法验证项 —— **不等于通过**。

用法:
    python3 scripts/check-notify-isolation.py [选项]

选项:
    --unit-dir DIR    systemd 单元目录 (默认 /etc/systemd/system)
    --repo-packaging  单元目录改用仓库里的 packaging/ 模板 (自检用)
    --env-file PATH   通知凭据文件 (默认 /etc/y8y-todo/y8y-todo-notify.env)
    --data-dir PATH   数据目录 (默认 /var/lib/y8y-todo)
    --state-dir PATH  通知状态目录 (默认 <data-dir>/notify)

退出码:
    0  全部必需项通过
    1  有检查项失败
    2  有检查项无法验证 (不算通过)
"""
import argparse
import grp
import os
import pwd
import select
import subprocess
import sys
from pathlib import Path

WEB_UNIT = 'y8y-todo.service'
NOTIFY_UNIT = 'y8y-todo-notify.service'

#: 凭据文件在 Web 单元里出现即视为失败
NOTIFY_ENV_NAME = 'y8y-todo-notify.env'

OK, FAIL, UNKNOWN = 'OK', 'FAIL', 'UNKNOWN'

_results = []


def _record(status, label, detail):
    _results.append((status, label, detail))
    print(f'[{status:^7}] {label}\n            {detail}')


def _unit_value(path, key):
    """取单元里某个指令的值 (只看 [Service] 段)。"""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        if section != 'Service':
            continue
        if line.startswith(key + '='):
            return line[len(key) + 1:].strip()
    return None


def _unit_values(path, key):
    """取单元里某个指令的**所有**值 (例如 ReadWritePaths= 可以有多条)。"""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return []
    section = None
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        if section != 'Service':
            continue
        if line.startswith(key + '='):
            out.append(line[len(key) + 1:].strip())
    return out


def _uid_gid(name):
    """用户 → (uid, gid, 组列表)。

    组列表**必须**包含附加组: 状态目录是靠共享组授权的, 而共享组对
    Web 账号来说通常是附加组 —— 只保留主组的话, 审计会误判成
    "没有权限"。用户不存在 → None。
    """
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    try:
        groups = list(os.getgrouplist(name, entry.pw_gid))
    except (OSError, AttributeError):
        groups = [entry.pw_gid]
    return entry.pw_uid, entry.pw_gid, groups


def _probe_isolation(web_ids, notify_ids):
    """Keep a synthetic, credential-free notify-user process alive for C3/C4."""
    if os.geteuid() != 0 or not web_ids or not notify_ids or not Path('/proc/self/status').exists():
        return None, None
    child = None
    try:
        child = subprocess.Popen(
            [sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.read(1)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'Y8Y_AUDIT_SYNTHETIC': 'probe'},
            preexec_fn=_drop_to(*notify_ids), text=True)
        ready, _, _ = select.select([child.stdout], [], [], 5)
        if not ready or child.stdout.readline().strip() != 'ready':
            return None, None
        status = Path(f'/proc/{child.pid}/status').read_text(encoding='utf-8')
        uids = [int(x) for x in next(x for x in status.splitlines() if x.startswith('Uid:')).split()[1:]]
        uid_ok = len(uids) == 4 and all(uid == notify_ids[0] for uid in uids) and notify_ids[0] != web_ids[0]
        denied = _read_as_user(*web_ids, f'/proc/{child.pid}/environ')
        if child.poll() is not None:
            denied = None
        return uid_ok, None if denied is None else not denied
    except (OSError, ValueError, StopIteration):
        return None, None
    finally:
        if child is not None:
            child.stdin.close()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child.stdout.close()


def _drop_to(uid, gid, groups):
    """在子进程里降权。降不下来就以特殊退出码 3 结束。

    附加组要**保留** —— 状态目录靠共享组授权, 丢掉附加组就等于把
    "有权限"测成"没权限"。
    """
    def drop():
        try:
            os.setgroups(list(groups or []))
            os.setgid(gid)
            os.setuid(uid)
        except OSError:
            os._exit(3)
    return drop


def _read_as_user(uid, gid, groups, path):
    """以指定身份尝试读一个文件。

    返回 True = 读到了 (不该发生); False = 读不到 (符合预期);
    **None = 判断不了** (降权失败 / 子进程起不来) —— 判断不了绝不等于
    "读到了", 更不等于"安全"。
    """
    try:
        import subprocess
        proc = subprocess.run(
            [sys.executable, '-c',
             'import errno, sys\n'
             'try:\n'
             '    open(sys.argv[1], "rb").read(1)\n'
             'except OSError as exc:\n'
             '    sys.exit(10 if exc.errno in (errno.EACCES, errno.EPERM) else 11)\n'
             'sys.exit(0)\n', path],
            preexec_fn=_drop_to(uid, gid, groups), capture_output=True)
    except (OSError, ImportError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 10:
        return False                      # 仅权限拒绝才算安全
    return None                           # 消失、降权失败等均无法判定


def _access_as(uid, gid, groups, path, mode):
    """以指定身份检查对 path 的访问权。

    返回 True = 有权限; False = 没有; **None = 判断不了**。

    ⚠️ 判定不了的时候必须返回 None, 不能返回 True —— 返回 True 等于
    把"没查出来"当成"没问题", 那是把审计变成安慰剂 (fail open)。
    """
    try:
        import subprocess
        proc = subprocess.run(
            [sys.executable, '-c',
             'import os, sys\n'
             'mode = int(sys.argv[2])\n'
             'sys.exit(0 if os.access(sys.argv[1], mode) else 1)\n',
             str(path), str(mode)],
            preexec_fn=_drop_to(uid, gid, groups), capture_output=True)
    except (OSError, ImportError):
        return None                         # 判不了 → 判不了
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    return None                             # 异常退出 → 判不了


def _path_covers(candidate, target):
    """candidate 这个可写路径是否覆盖了 target (同一路径或它的父目录)。"""
    try:
        cand = os.path.normpath(candidate)
        tgt = os.path.normpath(target)
    except (TypeError, ValueError):
        return False
    if cand == tgt:
        return True
    return tgt.startswith(cand.rstrip('/') + '/')


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='check-notify-isolation',
        description='通知凭据隔离审计 (只读)')
    parser.add_argument('--unit-dir', default='/etc/systemd/system')
    parser.add_argument('--repo-packaging', action='store_true',
                        help='单元改用仓库 packaging/ 里的模板')
    parser.add_argument('--env-file', default='/etc/y8y-todo/y8y-todo-notify.env')
    parser.add_argument('--data-dir', default='/var/lib/y8y-todo')
    parser.add_argument('--state-dir', default='',
                        help='通知状态目录 (默认 <data-dir>/notify)')
    args = parser.parse_args(argv)
    _results.clear()

    unit_dir = Path(args.unit_dir)
    if args.repo_packaging:
        unit_dir = Path(__file__).resolve().parent.parent / 'packaging'

    data_dir = Path(args.data_dir)
    state_dir = Path(args.state_dir) if args.state_dir else data_dir / 'notify'

    print('== y8y-todo 通知凭据隔离审计 (只读) ==')
    print(f'   单元目录: {unit_dir}')
    print(f'   凭据文件: {args.env_file}')
    print(f'   数据目录: {data_dir}')
    print(f'   状态目录: {state_dir}')
    print()

    # ── C1 单元里的运行用户 ────────────────────────────────────────────
    web_unit = unit_dir / WEB_UNIT
    notify_unit = unit_dir / NOTIFY_UNIT
    web_user = _unit_value(web_unit, 'User')
    notify_user = _unit_value(notify_unit, 'User')
    if web_user is None or notify_user is None:
        _record(UNKNOWN, 'C1 单元的运行用户',
                f'读不到单元 ({web_unit} / {notify_unit}) —— 装了吗?')
    elif web_user == notify_user:
        _record(FAIL, 'C1 单元的运行用户',
                f'Web 与调度器都是 {web_user!r}: 同 UID 时内核放行对 '
                f'/proc/<pid>/environ 的读取, 被攻破的 Web 进程能直接拿到凭据')
    else:
        _record(OK, 'C1 单元的运行用户',
                f'Web={web_user!r} 调度器={notify_user!r} —— 不同 UID')

    # ── C2 凭据文件权限 + Web 单元不得引用它 ──────────────────────────
    env_path = Path(args.env_file)
    try:
        info = env_path.stat()
        mode = info.st_mode & 0o777
        owner = info.st_uid
    except OSError as exc:
        _record(UNKNOWN, 'C2 凭据文件权限', f'{env_path} 读不到 ({exc.strerror})')
    else:
        if owner != 0:
            _record(FAIL, 'C2 凭据文件权限',
                    f'{env_path} 的属主 uid={owner} —— 必须是 root (systemd 以 root 读它)')
        elif mode & 0o077:
            _record(FAIL, 'C2 凭据文件权限',
                    f'{env_path} 权限 {mode:04o} —— group/other 不可读')
        else:
            _record(OK, 'C2 凭据文件权限', f'{env_path} 权限 {mode:04o}, 属主 root')

    web_unit_text = ''
    try:
        web_unit_text = web_unit.read_text(encoding='utf-8')
    except OSError:
        pass
    if not web_unit_text:
        _record(UNKNOWN, 'C2b Web 单元不引用凭据文件',
                f'读不到 {web_unit} —— 没法确认')
    elif NOTIFY_ENV_NAME in web_unit_text:
        _record(FAIL, 'C2b Web 单元不引用凭据文件',
                f'{web_unit} 里出现了 {NOTIFY_ENV_NAME} —— Web 进程会拿到凭据')
    else:
        _record(OK, 'C2b Web 单元不引用凭据文件',
                f'{web_unit} 没有引用 {NOTIFY_ENV_NAME}')

    # A controlled child has only a fictional environment; it cannot send.
    web_ids = _uid_gid(web_user) if web_user else None
    notify_ids = _uid_gid(notify_user) if notify_user else None
    uid_ok, denied = _probe_isolation(web_ids, notify_ids)
    for label, result, good, bad in (
        ('C3 探针的 UID', uid_ok, '探针实际 UID 与配置一致且不同于 Web', '探针 UID 不符或与 Web 相同'),
        ('C4 跨用户读取实测', denied, 'Web 用户读取虚构探针 environ 被拒', 'Web 用户能读取探针 environ'),
    ):
        _record(UNKNOWN if result is None else OK if result else FAIL, label,
                '无法验证 (需要 root 和 Linux /proc)' if result is None else good if result else bad)

    # ── C5 调度器对数据目录 / 状态目录的访问权 ─────────────────────────
    notify_ids = _uid_gid(notify_user) if notify_user else None
    if notify_ids is None:
        _record(UNKNOWN, 'C5 调度器的访问权',
                f'调度器用户 {notify_user!r} 在本机不存在')
    elif os.geteuid() != 0:
        _record(UNKNOWN, 'C5 调度器的访问权',
                '需要 root 才能以调度器用户身份验证')
    elif not data_dir.exists():
        _record(UNKNOWN, 'C5 调度器的访问权', f'{data_dir} 不存在')
    else:
        n_uid, n_gid, n_groups = notify_ids
        problems = []
        undecidable = []
        needed = [
            (data_dir, os.X_OK, '穿越数据目录'),
            (state_dir, os.R_OK | os.X_OK, '进入状态目录'),
            (state_dir, os.W_OK | os.X_OK, '在状态目录里创建文件 (投递状态)'),
        ]
        for path, mode, what in needed:
            if not path.exists():
                undecidable.append(f'{what} (missing)')
                continue
            verdict = _access_as(n_uid, n_gid, n_groups, path, mode)
            if verdict is None:
                undecidable.append(f'{what} ({path})')
            elif not verdict:
                problems.append(f'{what} ({path})')
        settings = state_dir / 'notification_settings.json'
        if settings.exists():
            verdict = _access_as(n_uid, n_gid, n_groups, settings, os.R_OK)
            if verdict is None:
                undecidable.append('settings permission probe unavailable')
            elif not verdict:
                problems.append('settings inaccessible')
        # The scheduler recursively scans both trees. Never log task names.
        for folder in ('todo', 'recurring'):
            root = data_dir / folder
            if not root.is_dir() or root.is_symlink():
                undecidable.append(f'{folder} missing or symlink')
                continue
            def onerror(_exc):
                undecidable.append(f'{folder} traversal failed')
            for current, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
                for name in dirs + files:
                    if (Path(current) / name).is_symlink():
                        undecidable.append(f'{folder} contains a symlink')
                checks = [(Path(current), os.R_OK | os.X_OK)]
                checks.extend((Path(current) / name, os.R_OK) for name in files if name.endswith('.md'))
                for path, mode in checks:
                    verdict = _access_as(n_uid, n_gid, n_groups, path, mode)
                    if verdict is None:
                        undecidable.append(f'{folder} permission probe unavailable')
                    elif not verdict:
                        problems.append(f'{folder} task tree inaccessible')
                        break
        # Root write permits replacing tokens even when the file itself is read-only.
        for path, mode, label in ((data_dir, os.W_OK, 'data root writable'),
                                  (data_dir / 'internal_tokens.json', os.R_OK, 'token readable'),
                                  (data_dir / 'internal_tokens.json', os.W_OK, 'token writable')):
            if not path.exists():
                undecidable.append(f'{label}: path missing')
                continue
            verdict = _access_as(n_uid, n_gid, n_groups, path, mode)
            if verdict is None:
                undecidable.append(f'{label}: cannot check')
            elif verdict:
                problems.append(label)
        # 状态目录的属组必须同时装得下两个账号 —— 否则一边能写另一边不能
        if state_dir.exists():
            try:
                info = state_dir.stat()
                try:
                    group = grp.getgrgid(info.st_gid)
                    group_name = group.gr_name
                    members = set(group.gr_mem)
                except KeyError:
                    group_name = f'gid={info.st_gid}'
                    members = set()
                for who, ids in ((notify_user, notify_ids), (web_user, web_ids)):
                    if not who or ids is None:
                        continue
                    if ids[0] == info.st_uid:
                        continue                    # 属主 —— 天然有权限
                    if ids[1] == info.st_gid:
                        continue                    # 主组就是目录的属组
                    if info.st_gid in ids[2]:
                        continue                    # 附加组里有目录的属组
                    if who in members:
                        continue
                    problems.append(
                        f'{who!r} 不在状态目录的属组 {group_name!r} 里')
            except OSError as exc:
                undecidable.append(f'读状态目录的属组失败 ({exc})')
        if problems:
            _record(FAIL, 'C5 调度器的访问权',
                    '权限检查失败: ' + '; '.join(problems))
        elif undecidable:
            _record(UNKNOWN, 'C5 调度器的访问权',
                    '判不了: ' + '; '.join(undecidable))
        else:
            _record(OK, 'C5 调度器的访问权',
                    f'调度器用户 {notify_user!r} 能读任务目录并写状态目录')

    # ── C6 Web 对状态目录的访问权 ──────────────────────────────────────
    if web_ids is None:
        _record(UNKNOWN, 'C6 Web 的访问权', f'Web 用户 {web_user!r} 在本机不存在')
    elif os.geteuid() != 0:
        _record(UNKNOWN, 'C6 Web 的访问权', '需要 root 才能以 Web 用户身份验证')
    elif not state_dir.exists():
        _record(UNKNOWN, 'C6 Web 的访问权',
                f'{state_dir} 不存在 —— 通知状态目录还没建好 '
                '(docs/deployment.md §5.2.2)')
    else:
        w_uid, w_gid, w_groups = web_ids
        problems = []
        undecidable = []
        needed = [
            (state_dir, os.W_OK | os.X_OK, '在状态目录里创建文件 (用户偏好)'),
            (state_dir / 'notification_channels.json', os.R_OK, '读能力表'),
            (state_dir / 'notification_settings.json', os.R_OK, '读用户偏好'),
        ]
        for path, mode, what in needed:
            if not path.exists():
                continue
            verdict = _access_as(w_uid, w_gid, w_groups, path, mode)
            if verdict is None:
                undecidable.append(f'{what} ({path})')
            elif not verdict:
                problems.append(f'{what} ({path})')
        if problems:
            _record(FAIL, 'C6 Web 的访问权', 'Web 用户缺少: ' + '; '.join(problems))
        elif undecidable:
            _record(UNKNOWN, 'C6 Web 的访问权', '判不了: ' + '; '.join(undecidable))
        else:
            _record(OK, 'C6 Web 的访问权',
                    f'Web 用户 {web_user!r} 能读能力表并写用户偏好')

    # ── C7 通知单元的可写范围不得覆盖整个数据目录 ──────────────────────
    rw_paths = _unit_values(notify_unit, 'ReadWritePaths')
    if not rw_paths:
        _record(UNKNOWN, 'C7 通知单元的可写范围',
                f'{notify_unit} 里没有 ReadWritePaths= —— 没法判断')
    else:
        too_broad = []
        for raw in rw_paths:
            for one in raw.split():
                path = one.lstrip('-')
                if _path_covers(path, str(data_dir)):
                    too_broad.append(one)
        if too_broad:
            _record(FAIL, 'C7 通知单元的可写范围',
                    f'通知单元的可写路径 {", ".join(too_broad)} 覆盖了整个数据目录 '
                    f'{data_dir} —— 那里还有 internal_tokens.json 这种机器凭据, '
                    f'调度器不该能替换它')
        else:
            _record(OK, 'C7 通知单元的可写范围',
                    f'可写路径 {", ".join(rw_paths)} 不覆盖数据目录 {data_dir}')

    failed = [r for r in _results if r[0] == FAIL]
    unknown = [r for r in _results if r[0] == UNKNOWN]
    print()
    print(f'结果: {len(_results) - len(failed) - len(unknown)} 通过 / '
          f'{len(failed)} 失败 / {len(unknown)} 无法验证')
    if failed:
        print('有检查项失败 —— 隔离没有真正生效, 详见 docs/deployment.md §5.2')
        return 1
    if unknown:
        print('有检查项无法验证 —— **不等于通过**, 请在目标主机上以 root 重跑')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
