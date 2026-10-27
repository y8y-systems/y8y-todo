#!/usr/bin/env python3
"""进程级加固 —— 让同 UID 的其他进程读不到本进程的环境变量与内存。

为什么需要它
------------
通知调度器 (``notify_runner.py``) 的环境里有 LINE token 和 Resend API key。
内核判断"能不能读 ``/proc/<pid>/environ``"用的是 ``ptrace_may_access()``
(``kernel/ptrace.c``)::

    tcred = __task_cred(task);
    if (uid_eq(caller_uid, tcred->euid) && uid_eq(caller_uid, tcred->suid) &&
        uid_eq(caller_uid, tcred->uid)  && gid_eq(...) )
            goto ok;                      ← 同 UID: 直接放行
    if (ptrace_has_cap(tcred->user_ns, mode))
            goto ok;                      ← 不同 UID: 需要 CAP_SYS_PTRACE
    return -EPERM;

也就是说, **只要 Web 服务和通知调度器用同一个系统用户**, 一旦 Web 进程
被控制, 攻击者就能直接读出调度器的环境变量 —— 凭据等于公开。

(Yama 的 ``ptrace_scope`` 挡不住这条: ``yama_ptrace_access_check()`` 只在
``mode & PTRACE_MODE_ATTACH`` 时才拦截; 读 environ 走的是 READ 模式。)
(Python 启动到调用本模块之间有一个几十毫秒的窗口, 详见 docs/deployment.md
§5.2 —— 想彻底消除它需要让调度器换用独立系统用户。)

systemd 至今没有提供"让服务不可 dump"的单元选项, 但内核给了这个能力:
``prctl(PR_SET_DUMPABLE, 0)``。进程自己把 dumpable 清掉之后:

  * ``/proc/<pid>/environ`` → EACCES
  * ``/proc/<pid>/mem``    → EACCES
  * ptrace attach          → 拒绝 (不再依赖 Yama 的 ptrace_scope)
  * core dump              → 不再生成 (凭据不会随 core 落盘)

``/proc/<pid>/cmdline`` 与 ``/proc/<pid>/status`` **不受影响** —— systemd 的
进程监控、``ps`` 排查、CI 里读 exe/cmdline 都照常工作。

代价
----
出问题的时候没法用 gdb 挂上去, 也没有 core dump 可分析。调试时用
``Y8Y_NOTIFY_KEEP_DUMPABLE=1`` 临时关掉这道加固 (从环境变量读, 不需要改
任何文件)。

只用标准库 (ctypes); 非 Linux 平台上静默跳过, 不改变任何行为。
"""
import ctypes
import os

PR_SET_DUMPABLE = 4
PR_GET_DUMPABLE = 3

SUID_DUMP_USER = 1

#: 调试用的逃生阀 —— 设为 1 时不加固。
KEEP_DUMPABLE_ENV = 'Y8Y_NOTIFY_KEEP_DUMPABLE'

_TRUTHY = ('1', 'true', 'yes', 'on')


def _prctl(option, arg=0):
    """调 prctl(2)。拿不到 libc 时返回 -1 (不抛异常)。"""
    for name in ('libc.so.6', None):
        try:
            libc = ctypes.CDLL(name, use_errno=True) if name else ctypes.CDLL(None)
        except OSError:
            continue
        try:
            return libc.prctl(ctypes.c_int(option), ctypes.c_ulong(arg),
                              ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
        except (AttributeError, OSError):
            continue
    return -1


def dumpable():
    """当前进程的 dumpable 标志 (1 = 可被别人 dump; None = 取不到)。"""
    value = _prctl(PR_GET_DUMPABLE)
    return value if value >= 0 else None


def harden(env=None):
    """把本进程设为不可 dump。

    返回 True 表示加固已生效。跳过的情况 (非 Linux / 取不到 prctl /
    设了调试逃生阀) 返回 False —— 调用方不需要关心, 这是纵深防御而不是
    唯一防线。
    """
    env = os.environ if env is None else env
    if str(env.get(KEEP_DUMPABLE_ENV, '') or '').strip().lower() in _TRUTHY:
        return False
    return _prctl(PR_SET_DUMPABLE, 0) == 0


def allow_dumpable():
    """恢复可 dump 状态 —— 只给调试用 (内核允许任何进程改自己的标志)。"""
    return _prctl(PR_SET_DUMPABLE, SUID_DUMP_USER) == 0
