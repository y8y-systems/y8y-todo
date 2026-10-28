"""Pure helpers for the on-disk task format (markdown + frontmatter).

These functions are the **single source of truth** for how a task file is
written and read.  They live in their own module (instead of inside
``serve.py``) so that other processes can use the exact same rules without
importing the HTTP server:

- ``serve.py``            — the web app
- ``notifications.py``    — the notification scheduler (a separate process,
  driven by a systemd timer)

Both modules import from here, so a format change can never drift between
the writer and the reader.

Nothing here touches the network, the clock, or global state; every function
takes explicit paths/values.  That keeps it trivially testable.

Format (unchanged since the 2026-10 public release — see README §Data
format):

---
title: <display title>          # optional; legacy files use the filename
created: YYYY-MM-DD
due: YYYY-MM-DD
due_end: YYYY-MM-DD             # optional, multi-day range
due_time: HH:MM                 # optional, 24h
color: green
status: todo|done
completed: YYYY-MM-DD
project: <project name>
owner: <user id>|shared
recurring:                      # list of "D" (1-31) or "MM-DD"
  - 5
  - 20
---
"""
import json
import os
import re

__all__ = [
    'atomic_write',
    'build_frontmatter',
    'display_title',
    'parse_frontmatter',
    'probe_writable',
    'scan_md_files',
    'task_owner',
    'can_access',
    'yaml_parse_scalar',
    'yaml_scalar',
]


# ── 原子写 ────────────────────────────────────────────────────────────
def _tmp_path(path):
    """同目录下的唯一临时文件名。

    名字带 pid + 随机后缀, 两个进程同时写同一个文件时不会互相踩踏,
    而且外部无法预先猜中名字去放一个同名符号链接 (配合 O_EXCL 拒绝
    已存在的路径)。
    """
    return f'{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp'


def atomic_write(path, text, fsync=False):
    """Write text to path atomically (tmp file + rename).

    A crash mid-write can never leave a half-written task or list file
    behind; readers see either the old or the new content, never a mix.
    Missing parent directories are created on demand (fresh deployments
    start with an empty data directory).

    The temporary file is opened with ``O_EXCL``, so a pre-planted
    symlink can never redirect the write somewhere else. That matters
    because the notification state directory is written by **two**
    service accounts (the web app and the scheduler — see
    docs/deployment.md §5.2). The resulting permissions are unchanged:
    ``os.open`` applies the process umask exactly like ``open()`` did.

    ``fsync=True`` additionally flushes the file (and the directory entry)
    to stable storage before returning. It costs a few milliseconds, so it
    is opt-in: the notification delivery state uses it because that file is
    the *only* record of "this notification was already attempted" — losing
    it to a power cut would mean sending the same LINE message twice.
    """
    tmp = _tmp_path(path)
    parent = os.path.dirname(tmp)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        if fsync:
            _fsync_dir(parent)
    except BaseException:
        # 写失败时不要把半截临时文件留在目录里。
        # 只删**我们刚创建**的那个 —— os.open 失败 (例如路径已存在) 时
        # 那个文件不是我们的, 不能动。
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fsync_dir(directory):
    """把目录项本身刷盘 —— 让 os.replace 的重命名真正落地。

    某些系统 (例如 macOS) 对目录 fd 的 fsync 会报 EINVAL, 那就跳过:
    文件内容已经 fsync 过了, 剩下的只是目录项的持久性。
    """
    if not directory:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def probe_writable(directory):
    """确认目录可写 —— 建一个临时文件再删掉。

    比 ``os.access()`` 可靠: 它模拟的正是 ``atomic_write`` 的真实动作
    (在目标目录里创建临时文件), 因此 ACL / setgid / 属主这些真实生效的
    规则都会被算进去。目录不存在 → 不可写。

    只在需要"写之前先确认"的地方用 (通知调度器在发出任何通知之前会
    探一次) —— 它会短暂地在目录里留下一个空文件。
    """
    if not os.path.isdir(directory):
        return False
    tmp = _tmp_path(os.path.join(directory, 'probe'))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return True


# ── frontmatter 序列化 ────────────────────────────────────────────────
def yaml_scalar(value):
    """Serialize a string as a YAML-safe frontmatter scalar.

    Plain (unquoted) values stay byte-identical to the legacy on-disk
    format. Values containing characters that would corrupt the
    frontmatter (newlines, CR, double quotes, backslashes, or
    leading/trailing whitespace that the line-based parser would strip)
    are written as a JSON double-quoted scalar — JSON escaping is a
    subset of YAML double-quoted escaping, and parse_frontmatter decodes
    it back symmetrically.

    This closes the whole class of frontmatter-injection bugs: a title
    like 'a\\n---' or 'x\\nkey: injected' can no longer truncate the
    value or fabricate fields.
    """
    if not isinstance(value, str):
        return value
    if value and not any(c in value for c in '\n\r"\\') and value == value.strip():
        return value
    return json.dumps(value, ensure_ascii=False)


def yaml_parse_scalar(value):
    """Decode a frontmatter scalar written by yaml_scalar (JSON
    double-quoted form). Plain values pass through unchanged."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except Exception:
            pass
    return value


def parse_frontmatter(text):
    """Parse YAML frontmatter from markdown content."""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', text, re.DOTALL)
    if not m:
        return {}, text.strip()
    fm = {}
    current_list_key = None
    current_list = []
    for line in m.group(1).strip().split('\n'):
        if line.startswith('  - '):
            # continuation of a list (e.g. recurring, children)
            if current_list_key is not None:
                current_list.append(yaml_parse_scalar(line[4:].strip()))
                fm[current_list_key] = current_list
            continue
        if ':' in line:
            key, _, val = line.partition(':')
            key = key.strip()
            val = yaml_parse_scalar(val.strip())
            if val == '':
                # key with no value, possibly followed by list items
                current_list_key = key
                current_list = []
                # only reset fm[key] if not already populated by previous list items
                if key not in fm:
                    fm[key] = current_list
            else:
                fm[key] = val
                current_list_key = None
                current_list = []
    body = m.group(2).strip()
    return fm, body


def build_frontmatter(fields):
    """Build YAML frontmatter string from fields dict.

    Scalar values go through yaml_scalar: plain values keep the legacy
    on-disk shape; values with newline/quote/backslash/edge-whitespace
    are JSON-quoted so they survive the line-based parser intact.
    """
    # Scalar fields written in a fixed order; project is an optional scalar.
    scalar_order = ('title', 'created', 'due', 'due_end', 'due_time', 'color', 'status', 'completed', 'task_uid', 'gcal_event_id', 'project', 'owner')
    lines = ['---']
    written = set()
    for k in scalar_order:
        v = fields.get(k)
        if v:
            lines.append(f'{k}: {yaml_scalar(v) if isinstance(v, str) else v}')
            written.add(k)
    # recurring as list or scalar
    if fields.get('recurring'):
        v = fields['recurring']
        if isinstance(v, list):
            lines.append('recurring:')
            for r in v:
                lines.append(f'  - {yaml_scalar(r) if isinstance(r, str) else r}')
        else:
            lines.append(f'recurring: {yaml_scalar(v) if isinstance(v, str) else v}')
        written.add('recurring')
    # children as list (brain-map edges, source → target)
    children = fields.get('children')
    if children:
        if isinstance(children, list):
            lines.append('children:')
            for c in children:
                lines.append(f'  - {yaml_scalar(c) if isinstance(c, str) else c}')
        else:
            # scalar fallback (shouldn't happen, but be safe)
            lines.append(f'children: {yaml_scalar(children) if isinstance(children, str) else children}')
        written.add('children')
    # any other unknown fields (preserve them)
    for k, v in fields.items():
        if k in written or k in ('recurring', 'children'):
            continue
        if v:
            lines.append(f'{k}: {yaml_scalar(v) if isinstance(v, str) else v}')
    lines.append('---')
    return '\n'.join(lines)


# ── 目录扫描 ──────────────────────────────────────────────────────────
def scan_md_files(folder_path, prefix=''):
    """递归扫描 folder_path 下所有 .md 文件（项目文件夹方案，2026-08-13）。

    返回 [(rel_path, fname), ...]，rel_path 相对 folder_path 的路径（含子目录），
    fname 是纯文件名。例：todo/纹眉/脚本.md → ('纹眉/脚本.md', '脚本.md')
    """
    results = []
    try:
        entries = sorted(os.listdir(folder_path))
    except FileNotFoundError:
        return results
    for entry in entries:
        full = os.path.join(folder_path, entry)
        if os.path.isdir(full):
            results.extend(scan_md_files(full, os.path.join(prefix, entry) if prefix else entry))
        elif entry.endswith('.md'):
            rel = os.path.join(prefix, entry) if prefix else entry
            results.append((rel, entry))
    return results


def display_title(fm, path):
    """任务的显示标题: frontmatter title 优先, 回退文件名(旧数据)。"""
    title = fm.get('title')
    if title:
        return title
    return os.path.basename(path)[:-3] if path.endswith('.md') else os.path.basename(path)


# ── 归属与可见性 ──────────────────────────────────────────────────────
def task_owner(fm, default_owner):
    """任务归属；无 owner 字段 = default_owner (旧数据零迁移兼容)。"""
    return fm.get('owner', default_owner)


def can_access(owner, user):
    """用户能看/改的任务：自己的 + 共享的。私人任务互不可见。"""
    return owner in (user, 'shared')
