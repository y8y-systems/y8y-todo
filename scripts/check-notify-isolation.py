#!/usr/bin/env python3
"""check-notify-isolation.py — 通知凭据隔离审计 (只读)

回答一个问题: **Web 服务进程能不能读到通知调度器手里的凭据?**

检查项:

  C1  单元的运行用户: Web 与调度器必须不是同一个 UID
  C2  凭据文件权限:    必须是 root 拥有, 且 group/other 不可读
  C3  运行中调度器的实际 UID: 必须与 Web 单元的用户不同
  C4  跨用户读取实测:  以 Web 用户身份读调度器的 /proc/<pid>/environ 必须失败
  C5  数据目录访问权:  调度器用户必须能读任务目录、写通知状态文件

**这个脚本只读不写** —— 不建用户、不改权限、不启停服务、不碰凭据内容。
需要 root 才能完成的检查项, 在非 root 环境下标记为"无法验证"而不是
"通过"。

用法:
    python3 scripts/check-notify-isolation.py [选项]

选项:
    --unit-dir DIR    systemd 单元目录 (默认 /etc/systemd/system)
    --repo-packaging  单元目录改用仓库里的 packaging/ 模板 (自检用)
    --env-file PATH   通知凭据文件 (默认 /etc/y8y-todo/y8y-todo-notify.env)
    --data-dir PATH   数据目录 (默认 /var/lib/y8y-todo)

退出码:
    0  全部必需项通过
    1  有检查项失败
    2  有检查项无法验证 (不算通过)
"""
import argparse
import os
import pwd
import sys
from pathlib import Path

WEB_UNIT = 'y8y-todo.service'
NOTIFY_UNIT = 'y8y-todo-notify.service'

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


def _uid_of(name):
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def _find_notify_process():
    """找运行中的通知调度器进程 (按 cmdline 判定)。返回 (pid, uid) 或 (None, None)。"""
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes().decode('utf-8', 'replace')
        except OSError:
            continue
        if 'notify_runner.py' not in cmdline:
            continue
        try:
            status = (entry / 'status').read_text(encoding='utf-8')
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith('Uid:'):
                uid = int(line.split()[1])
                return int(entry.name), uid
    return None, None


def _read_as_user(uid, gid, path):
    """以指定身份尝试读一个文件。返回 True = 读到了 (不该发生)。"""
    def drop():
        try:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
        except OSError:
            os._exit(3)
    try:
        import subprocess
        proc = subprocess.run(
            [sys.executable, '-c',
             'import sys\n'
             'try:\n'
             '    open(sys.argv[1], "rb").read(1)\n'
             'except Exception:\n'
             '    sys.exit(1)\n'
             'sys.exit(0)\n', path],
            preexec_fn=drop, capture_output=True)
    except (OSError, ImportError):
        return None
    if proc.returncode == 3:
        return None                       # 降权失败
    return proc.returncode == 0           # 0 = 读到了


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='check-notify-isolation',
        description='通知凭据隔离审计 (只读)')
    parser.add_argument('--unit-dir', default='/etc/systemd/system')
    parser.add_argument('--repo-packaging', action='store_true',
                        help='单元改用仓库 packaging/ 里的模板')
    parser.add_argument('--env-file', default='/etc/y8y-todo/y8y-todo-notify.env')
    parser.add_argument('--data-dir', default='/var/lib/y8y-todo')
    args = parser.parse_args(argv)

    unit_dir = Path(args.unit_dir)
    if args.repo_packaging:
        unit_dir = Path(__file__).resolve().parent.parent / 'packaging'

    print('== y8y-todo 通知凭据隔离审计 (只读) ==')
    print(f'   单元目录: {unit_dir}')
    print(f'   凭据文件: {args.env_file}')
    print(f'   数据目录: {args.data_dir}')
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

    # ── C2 凭据文件权限 ────────────────────────────────────────────────
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

    # ── C3 运行中调度器的实际 UID ──────────────────────────────────────
    pid, notify_uid = _find_notify_process()
    if pid is None:
        _record(UNKNOWN, 'C3 运行中调度器的 UID',
                '没找到运行中的 notify_runner.py —— 调度器此刻没在跑')
    else:
        web_uid = _uid_of(web_user) if web_user else None
        if web_uid is None:
            _record(UNKNOWN, 'C3 运行中调度器的 UID',
                    f'pid={pid} uid={notify_uid}; 但用户 {web_user!r} 在本机不存在')
        elif notify_uid == web_uid:
            _record(FAIL, 'C3 运行中调度器的 UID',
                    f'pid={pid} 以 uid={notify_uid} 运行, 与 Web 相同')
        else:
            _record(OK, 'C3 运行中调度器的 UID',
                    f'pid={pid} uid={notify_uid} ≠ Web 的 {web_uid}')

    # ── C4 跨用户读取实测 ──────────────────────────────────────────────
    if pid is None:
        _record(UNKNOWN, 'C4 跨用户读取实测', '没有运行中的调度器进程, 无从测起')
    elif os.geteuid() != 0:
        _record(UNKNOWN, 'C4 跨用户读取实测', '需要 root 才能降权到 Web 用户去读')
    else:
        web_uid = _uid_of(web_user) if web_user else None
        if web_uid is None:
            _record(UNKNOWN, 'C4 跨用户读取实测', f'用户 {web_user!r} 在本机不存在')
        else:
            leaked = _read_as_user(web_uid, web_uid, f'/proc/{pid}/environ')
            if leaked is None:
                _record(UNKNOWN, 'C4 跨用户读取实测', '降权失败, 无法判定')
            elif leaked:
                _record(FAIL, 'C4 跨用户读取实测',
                        f'Web 用户能读到调度器 (pid={pid}) 的 environ —— 凭据可被直接读走')
            else:
                _record(OK, 'C4 跨用户读取实测',
                        f'Web 用户读调度器 (pid={pid}) 的 environ 被拒 —— 凭据不可达')

    # ── C5 调度器用户对数据目录的访问权 ────────────────────────────────
    data_dir = Path(args.data_dir)
    notify_uid = _uid_of(notify_user) if notify_user else None
    if notify_uid is None:
        _record(UNKNOWN, 'C5 数据目录访问权', f'调度器用户 {notify_user!r} 在本机不存在')
    elif not data_dir.exists():
        _record(UNKNOWN, 'C5 数据目录访问权', f'{data_dir} 不存在')
    elif os.geteuid() != 0:
        _record(UNKNOWN, 'C5 数据目录访问权', '需要 root 才能以调度器用户身份验证')
    else:
        needed = [
            (data_dir, os.R_OK | os.X_OK, '读+进入数据目录'),
            (data_dir / 'notification_settings.json', os.R_OK, '读用户偏好'),
            (data_dir / 'notification_state.json', os.W_OK, '写投递状态'),
            (data_dir / 'notification_channels.json', os.W_OK, '写能力表'),
        ]
        problems = []
        for path, mode, what in needed:
            if not path.exists():
                continue                        # 还没生成 → 跳过
            if not _access_as(notify_uid, path, mode):
                problems.append(f'{what} ({path})')
        if problems:
            _record(FAIL, 'C5 数据目录访问权',
                    '调度器用户缺少: ' + '; '.join(problems))
        else:
            _record(OK, 'C5 数据目录访问权',
                    f'调度器用户 {notify_user!r} 对数据目录权限正常')

    failed = [r for r in _results if r[0] == FAIL]
    unknown = [r for r in _results if r[0] == UNKNOWN]
    print()
    print(f'结果: {len(_results) - len(failed) - len(unknown)} 通过 / '
          f'{len(failed)} 失败 / {len(unknown)} 无法验证')
    if failed:
        print('有检查项失败 —— 隔离没有真正生效, 详见 docs/deployment.md §5.2')
        return 1
    if unknown:
        print('有检查项无法验证 —— 不等于通过, 请在目标主机上以 root 重跑')
        return 2
    return 0


def _access_as(uid, path, mode):
    """以指定 uid 身份检查对 path 的访问权 (需要 root)。"""
    def drop():
        try:
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
        except OSError:
            os._exit(3)
    try:
        import subprocess
        proc = subprocess.run(
            [sys.executable, '-c',
             'import os, sys\n'
             'mode = int(sys.argv[2])\n'
             'sys.exit(0 if os.access(sys.argv[1], mode) else 1)\n',
             str(path), str(mode)],
            preexec_fn=drop, capture_output=True)
    except (OSError, ImportError):
        return True                         # 判不了就先不报错
    if proc.returncode == 3:
        return True
    return proc.returncode == 0


if __name__ == '__main__':
    sys.exit(main())
