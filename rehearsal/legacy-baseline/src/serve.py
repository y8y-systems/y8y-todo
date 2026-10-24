#!/usr/bin/env python3
"""Todolist web app - self-hosted task manager for /var/lib/todolist/"""
import http.server
import json
import os
import queue
import re
import threading
import time
import urllib.parse
import uuid
from datetime import date, datetime

from google_calendar import GoogleCalendarAuth, GoogleCalendarError

import sys
sys.path.insert(0, '/var/lib/todolist-legacy/shared/chrome')   # canonical 共享壳 (2026-09-11; /srv/todolist-legacy-chrome 兼容软链)
from serve_asset import resolve_shared_asset
from static_guard import block_reason, resolve_public_file

# 2026-09-11 迁移: 单一 canonical 树 (生产=工作树) + 数据分离
TODOLIST_DIR = '/var/lib/todolist-legacy'      # 持久数据家 (旧 /var/lib/todolist 保留兼容软链)
STATIC_ROOT = os.path.dirname(os.path.abspath(__file__))   # canonical 源码树 = 生产 (静态资源根)

# ── 静态文件的公开清单（2026-09-24 收归共享守卫 static_guard）───────────────
# 原来是把 STATIC_ROOT（整个项目目录）当静态根、无白名单地拼路径 ——
# 于是 /serve.py、/.git/config、/backup/* 都能被下载（实测确认，md5 与本地文件一致）。
# 现在**默认拒绝**：只有下列路径放行；敏感路径（.git/、*.py、backup/…）由
# static_guard 硬拦 —— 连 SPA fallback 都不给（否则扫描器仍拿到 200）。
_PUBLIC_PREFIXES = ('/assets/',)
_PUBLIC_FILES = ('/', '/index.html')


def classify_static(rel_path):
    """把 URL 相对路径分类，供 do_GET / do_HEAD 共用。

    返回 (kind, res)：
      ('public',    (Path, content_type)) — 可以服务
      ('sensitive', None)                 — 敏感/穿越路径：硬拒，**不做 SPA fallback**
      ('missing',   None)                 — 不在白名单 / 不存在：调用方可自行 fallback
    """
    norm = '/' + rel_path.lstrip('/')
    # ① 先解码再查穿越：`/%2e%2e/etc/passwd` 这类编码穿越必须在**这里**就认出来，
    #    否则会掉进 'missing' → SPA fallback → 返回 200（扫描器会以为路径存在）。
    #    2026-09-24 实测踩到：`/../etc/passwd` 曾因 fallback 返回 200。
    try:
        decoded = urllib.parse.unquote(norm)
    except Exception:                                            # noqa: BLE001
        decoded = norm
    if '..' in decoded.split('/') or block_reason(decoded):
        return 'sensitive', None
    res = resolve_public_file(STATIC_ROOT, norm, _PUBLIC_PREFIXES, _PUBLIC_FILES)
    if res:
        return 'public', res
    return 'missing', None
GOOGLE_CALENDAR_DIR = os.path.join(TODOLIST_DIR, 'google_calendar')
GOOGLE_AUTH = GoogleCalendarAuth(GOOGLE_CALENDAR_DIR)
GOOGLE_CALLBACK_URI = 'https://todo.example.com/todo/api/google-calendar/oauth/callback'
# 列表系统 (2026-08-18 升级定案): 购物升级为「列表」, 每个列表一个目录, 可设归属(self/shared)
LISTS_ROOT = os.path.join(TODOLIST_DIR, 'lists')
# 购物兼容别名目录 = lists/shopping (旧 shopping/ 数据迁移后统一在列表系统内)
SHOPPING_DIR = os.path.join(LISTS_ROOT, 'shopping')   # 购物列表独立数据目录 (2026-08-18)

# Cloudflare Access 登录邮箱 → 用户身份
# ─────────────────────────────────────────────────────────────────────────
# ⚠️  Handoff redaction (2026-10-15): 真实邮箱已脱敏为占位符
#     Step-5 须自主设计独立部署的身份配置方案(环境变量 / 配置中心)
#     生产真实邮箱不得写回源码,详见 docs/SECURITY_AUDIT.md §3.1
# ─────────────────────────────────────────────────────────────────────────
ACCESS_USERS = {
    '__ALICE_CF_ACCESS_EMAIL__':  'alice',   # 占位符: Step-5 须设计独立配置方案
    '__BOB_CF_ACCESS_EMAIL__':  'bob',   # 占位符: Step-5 须设计独立配置方案
}
VALID_OWNERS = ('alice', 'bob', 'shared', 'legacy-agent')

# 内部机器间认证 (2026-08-14, legacy machine integration): X-Internal-Token header → 用户
# token 文件: /var/lib/todolist/internal_tokens.json  {"alice": "<hex>", "bob": "<hex>"}
# 仅用于 legacy machine client → 主机的服务间调用，永不对外公开
INTERNAL_TOKENS_FILE = os.path.join(TODOLIST_DIR, 'internal_tokens.json')

def _load_internal_tokens():
    try:
        with open(INTERNAL_TOKENS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

INTERNAL_TOKENS = _load_internal_tokens()

def _task_owner(fm):
    """任务归属；无 owner 字段 = alice（旧数据零迁移）"""
    return fm.get('owner', 'alice')

def _user_can_access(owner, user):
    """用户能看/改的任务：自己的 + 共享的。私人任务互不可见。
    legacy-agent（历史机器日历, 2026-08-14）：仅 alice 可见，bob看不到。
    legacy-bridge（legacy-bridge 身份, 2026-08-14）：只能碰 legacy-agent 日历，其他一概不可见/不可改。"""
    if user == 'legacy-bridge':
        return owner == 'legacy-agent'
    if owner == 'legacy-agent':
        return user == 'alice'
    return owner in (user, 'shared')

def _list_visible(list_owner, user):
    """列表可见性（2026-08-18 列表系统定案）：
    - 普通用户(alice/bob): 只能看 自己 或 shared 的列表
    - legacy-bridge: 全部列表都能看（不按归属过滤, 拍板）
    """
    if user == 'legacy-bridge':
        return True
    return list_owner in (user, 'shared')

def _can_access_shopping(owner, user):
    """购物条目访问控制（2026-08-18 列表系统化后保持兼容别名）。

    购物列表升级为「列表系统」后，条目可见性 = 所属列表的可见性。
    此函数按条目 owner 判——普通用户看 自己+shared；legacy-bridge 全部可见。
    """
    return _list_visible(owner, user)

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
                current_list.append(line[4:].strip())
                fm[current_list_key] = current_list
            continue
        if ':' in line:
            key, _, val = line.partition(':')
            key = key.strip()
            val = val.strip()
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


VALID_COLORS = ('yellow', 'green', 'pink', 'purple', 'orange', 'red', 'blue', 'cyan')

PERSISTENT_PROJECTS_FILE = os.path.join(TODOLIST_DIR, 'persistent_projects.json')

def _load_persistent_projects():
    """持久项目表: {user: [project, ...]} — 2026-08-12 从 localStorage 迁到后端."""
    try:
        with open(PERSISTENT_PROJECTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def get_persistent_projects(user):
    """某用户的持久项目列表 (只读, 不存在返回 [])."""
    data = _load_persistent_projects()
    val = data.get(user, [])
    return val if isinstance(val, list) else []

def save_persistent_projects(user, projects):
    """整体覆盖某用户的持久项目列表; 只保留字符串, 去重保序."""
    clean = []
    for p in projects:
        if isinstance(p, str) and p.strip() and p not in clean:
            clean.append(p.strip())
    data = _load_persistent_projects()
    data[user] = clean
    with open(PERSISTENT_PROJECTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return clean


def cascade_project_color(project, color, user, skip_fname=None):
    """项目级颜色联动: 把同 project 的所有任务(当前用户可访问)改成同一颜色.

    2026-08-11: 改一个任务的颜色 → 同一个项目所有任务跟着变.
    只改 todo/ 和 recurring/ (done/ 已完成任务不动); 只改当前用户能访问的
    (自己 + shared), 不碰对方私人任务. 当前任务由调用方先改好, 这里跳过.
    """
    if not project:
        return
    for folder in ('todo', 'recurring'):
        folder_path = os.path.join(TODOLIST_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        for rel_path, fname in _scan_md_files(folder_path):
            if fname == skip_fname or rel_path == skip_fname:
                continue
            fpath = os.path.join(folder_path, rel_path)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    raw = f.read()
                fm, body = parse_frontmatter(raw)
            except Exception:
                continue
            if fm.get('project', '') != project:
                continue
            if not _user_can_access(_task_owner(fm), user):
                continue
            if fm.get('color') == color:
                continue
            fm['color'] = color
            content_full = build_frontmatter(fm) + '\n\n' + body + '\n'
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(content_full)


def build_frontmatter(fields):
    """Build YAML frontmatter string from fields dict."""
    # Scalar fields written in a fixed order; project is an optional scalar.
    scalar_order = ('created', 'due', 'due_end', 'due_time', 'color', 'status', 'completed', 'task_uid', 'gcal_event_id', 'project', 'owner')
    lines = ['---']
    written = set()
    for k in scalar_order:
        v = fields.get(k)
        if v:
            lines.append(f'{k}: {v}')
            written.add(k)
    # recurring as list or scalar
    if fields.get('recurring'):
        v = fields['recurring']
        if isinstance(v, list):
            lines.append('recurring:')
            for r in v:
                lines.append(f'  - {r}')
        else:
            lines.append(f'recurring: {v}')
        written.add('recurring')
    # children as list (brain-map edges, source → target)
    children = fields.get('children')
    if children:
        if isinstance(children, list):
            lines.append('children:')
            for c in children:
                lines.append(f'  - {c}')
        else:
            # scalar fallback (shouldn't happen, but be safe)
            lines.append(f'children: {children}')
        written.add('children')
    # any other unknown fields (preserve them)
    for k, v in fields.items():
        if k in written or k in ('recurring', 'children'):
            continue
        if v:
            lines.append(f'{k}: {v}')
    lines.append('---')
    return '\n'.join(lines)

def _scan_md_files(folder_path, prefix=''):
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
            results.extend(_scan_md_files(full, os.path.join(prefix, entry) if prefix else entry))
        elif entry.endswith('.md'):
            rel = os.path.join(prefix, entry) if prefix else entry
            results.append((rel, entry))
    return results


def _list_dir(list_id):
    return os.path.join(LISTS_ROOT, list_id)

def list_meta(list_id):
    """读列表元数据 meta.json → {name, owner, created}；不存在返回 None"""
    p = os.path.join(_list_dir(list_id), 'meta.json')
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def list_all_meta():
    """返回所有列表的 {id: meta}"""
    if not os.path.isdir(LISTS_ROOT):
        return {}
    out = {}
    for d in sorted(os.listdir(LISTS_ROOT)):
        full = os.path.join(LISTS_ROOT, d)
        if not os.path.isdir(full):
            continue
        m = list_meta(d)
        if m is None:
            # 目录无 meta.json → 不是列表（如 .DS_Store 等），跳过
            continue
        out[d] = m
    return out

def list_items(list_id):
    """列某列表下条目：一条 = 一个 <uuid>.md，frontmatter 极简(status/created/owner)，
    标题/内容 = 文件正文 body 原文（title 直接取自内容，无长度限制, 2026-08-20）。
    旧格式兼容：若文件名为真实标题（非 uuid），title 仍取 fname[:-3]。
    条目不在 todo/recurring/done 体系，不参与任务统计。"""
    items = []
    d = _list_dir(list_id)
    if not os.path.isdir(d):
        return items
    for fname in sorted(os.listdir(d)):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(d, fname)
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                raw = f.read()
            fm, body = parse_frontmatter(raw)
            base = fname[:-3]
            # 新格式: uuid 文件名 → title = body 原文; 旧格式: 非 uuid 文件名 → title = 文件名
            is_uuid = re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', base)
            title = body.strip() if (is_uuid and body) else base
            status = fm.get('status', 'todo')
            # 未完成按创建时间；已完成按完成时间。旧数据没有 completed 时，以文件修改时间兜底。
            # 这样「✓ 已完成」区始终从最近勾选的条目开始展示。
            _mtime = os.path.getmtime(fpath)
            items.append({
                'id': fname,
                'title': title,
                'created': fm.get('created', ''),
                'completed': fm.get('completed', ''),
                'owner': _task_owner(fm),
                'status': status,
                '_sort': ((fm.get('completed', '') if status == 'done' else fm.get('created', '')), _mtime),
            })
        except Exception:
            continue
    # 未完成按创建日期、已完成按完成时间，均从新到旧；同一秒再按 mtime。
    items.sort(key=lambda it: it.pop('_sort'), reverse=True)
    return items

def list_shopping():
    """购物列表 = lists/shopping 列表的兼容别名（2026-08-18 升级后）"""
    return list_items('shopping')

def ensure_shopping_list():
    """确保 lists/shopping 列表存在 + meta.json（旧 shopping/ 数据迁移到列表系统）。

    2026-08-18 升级：购物从 /var/lib/todolist/shopping/ 迁移到 /var/lib/todolist/lists/shopping/
    （旧购物条目 owner 恒 shared，迁移时补 meta.json；若旧文件里没 owner 也保持 shared）。
    """
    new_dir = _list_dir('shopping')
    os.makedirs(new_dir, exist_ok=True)
    if not os.path.isfile(os.path.join(new_dir, 'meta.json')):
        # 迁移旧 /var/lib/todolist/shopping/*.md（若存在）
        legacy_dir = os.path.join(TODOLIST_DIR, 'shopping')
        if os.path.isdir(legacy_dir):
            for fn in os.listdir(legacy_dir):
                if fn.endswith('.md'):
                    src = os.path.join(legacy_dir, fn)
                    dst = os.path.join(new_dir, fn)
                    if not os.path.exists(dst):
                        try:
                            with open(src, 'r', encoding='utf-8') as f:
                                raw = f.read()
                            # 旧条目若无 owner 字段则补 shared
                            if not re.search(r'^owner:', raw, re.M):
                                raw = '---\nowner: shared\n' + raw[4:] if raw.startswith('---') else '---\nowner: shared\n' + raw
                            with open(dst, 'w', encoding='utf-8') as f:
                                f.write(raw)
                        except Exception:
                            pass
        meta = {'name': '购物', 'owner': 'shared', 'created': date.today().isoformat()}
        with open(os.path.join(new_dir, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
    return list_meta('shopping')


def list_tasks():
    """List active tasks from todo/ and recurring/ directories (递归含项目文件夹)."""
    tasks = []
    for folder_name, default_recurring in [('todo', False), ('recurring', True)]:
        folder = os.path.join(TODOLIST_DIR, folder_name)
        if not os.path.isdir(folder):
            continue
        for rel_path, fname in _scan_md_files(folder):
            fpath = os.path.join(folder, rel_path)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    raw = f.read()
                fm, body = parse_frontmatter(raw)
                if fm.get('status', 'todo') != 'todo':
                    continue
                tasks.append({
                    'id': rel_path,
                    'title': fname[:-3],
                    'due': fm.get('due', ''),
                    'due_end': fm.get('due_end', ''),
                    'due_time': fm.get('due_time', ''),
                    'created': fm.get('created', ''),
                    'color': fm.get('color', 'green'),
                    'status': fm.get('status', 'todo'),
                    'recurring': fm.get('recurring', ''),
                    'project': fm.get('project', ''),
                    'owner': _task_owner(fm),
                    'children': fm.get('children', []) if isinstance(fm.get('children'), list) else [],
                    'body': body,
                    'folder': folder_name,
                    'x': int(fm['x']) if fm.get('x', '').lstrip('-').isdigit() else None,
                    'y': int(fm['y']) if fm.get('y', '').lstrip('-').isdigit() else None,
                })
            except Exception as e:
                tasks.append({
                    'id': rel_path,
                    'title': fname[:-3],
                    'due': '',
                    'color': 'green',
                    'status': 'todo',
                    'body': f'[Error reading file: {e}]',
                    'folder': folder_name,
                })
    return tasks

def list_completed_tasks():
    """List completed one-off tasks from done/YYYY-MM-DD directories (递归含项目文件夹)."""
    tasks = []
    done_root = os.path.join(TODOLIST_DIR, 'done')
    if not os.path.isdir(done_root):
        return tasks
    for completed_date in sorted(os.listdir(done_root), reverse=True):
        folder = os.path.join(done_root, completed_date)
        if not os.path.isdir(folder):
            continue
        for rel_path, fname in _scan_md_files(folder):
            fpath = os.path.join(folder, rel_path)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    raw = f.read()
                fm, body = parse_frontmatter(raw)
                if fm.get('status', 'done') != 'done':
                    continue
                tasks.append({
                    'id': f'done/{completed_date}/{rel_path}',
                    'title': fname[:-3],
                    'due': fm.get('due', ''),
                    'due_end': fm.get('due_end', ''),
                    'due_time': fm.get('due_time', ''),
                    'created': fm.get('created', ''),
                    'color': fm.get('color', 'green'),
                    'status': 'done',
                    'recurring': fm.get('recurring', ''),
                    'project': fm.get('project', ''),
                    'owner': _task_owner(fm),
                    'completed': fm.get('completed', completed_date),
                    'children': fm.get('children', []) if isinstance(fm.get('children'), list) else [],
                    'body': body,
                    'folder': 'done',
                    'x': int(fm['x']) if fm.get('x', '').lstrip('-').isdigit() else None,
                    'y': int(fm['y']) if fm.get('y', '').lstrip('-').isdigit() else None,
                })
            except Exception:
                continue
    return tasks

def classify_tasks(tasks):
    """Classify tasks into sections: overdue, today, this_week, future, nodue."""
    today = date.today()
    sections = {'overdue': [], 'today': [], 'this_week': [], 'future': [], 'nodue': []}
    for t in tasks:
        due_str = t.get('due', '')
        if not due_str:
            sections['nodue'].append(t)
            continue
        try:
            due_date = datetime.strptime(due_str, '%Y-%m-%d').date()
        except ValueError:
            sections['nodue'].append(t)
            continue
        if due_date < today:
            sections['overdue'].append(t)
        elif due_date == today:
            sections['today'].append(t)
        elif due_date <= date(today.year, today.month, today.day) + __import__('datetime').timedelta(days=7):
            sections['this_week'].append(t)
        else:
            sections['future'].append(t)
    return sections


# ── Google Calendar initial sync (2026-09-06: design-note + shared → 专用 TODO 日历) ──
def _gcal_active_tasks(user):
    """Only sync the connected user's own tasks plus shared tasks; never bob/legacy-agent."""
    return [task for task in list_tasks() if task.get('owner') in (user, 'shared')]


def _gcal_task_path(task):
    return os.path.join(TODOLIST_DIR, task['folder'], task['id'])


def _gcal_write_task_meta(task, updates):
    path = _gcal_task_path(task)
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read()
    fm, body = parse_frontmatter(raw)
    fm.update(updates)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(build_frontmatter(fm) + '\n\n' + body + '\n')


def _gcal_task_uid(task):
    path = _gcal_task_path(task)
    with open(path, 'r', encoding='utf-8') as f:
        fm, _ = parse_frontmatter(f.read())
    return fm.get('task_uid', '')


# 本地颜色 → Google Calendar colorId 映射 (2026-09-16 定案 A+B):
# 事件标题带项目名、颜色跟随 Todo 卡片色, 让日历上同名子任务一眼可分。
GCAL_COLOR_IDS = {
    'lavender': '1', 'sage': '2', 'grape': '3', 'flamingo': '4', 'banana': '5',
    'tangerine': '6', 'peacock': '7', 'graphite': '8', 'blueberry': '9', 'basil': '10', 'tomato': '11',
    'yellow': '5', 'green': '10', 'pink': '4', 'purple': '3',
    'orange': '6', 'red': '11', 'blue': '9', 'cyan': '7',
}


def _gcal_event_body(task, task_uid):
    """Convert one Todo task to a Google all-day/timed event. Google all-day end is exclusive."""
    due = task.get('due', '')
    if not due:
        return None
    project = (task.get('project') or '').strip()
    # 项目子任务标题会重名(三个「大纲」), 带项目前缀才能在日历上区分。
    summary = f'{project} ▸ {task["title"]}' if project else task['title']
    event = {
        'summary': summary,
        'description': task.get('body', ''),
        'extendedProperties': {'private': {
            'todo_app': 'y8y-todolist',
            'task_uid': task_uid,
            'owner': task.get('owner', ''),
        }},
    }
    color_id = GCAL_COLOR_IDS.get((task.get('color') or '').strip().lower())
    if color_id:
        event['colorId'] = color_id
    if task.get('due_time'):
        start = f"{due}T{task['due_time']}:00+09:00"
        event['start'] = {'dateTime': start, 'timeZone': 'Asia/Tokyo'}
        event['end'] = {'dateTime': start, 'timeZone': 'Asia/Tokyo'}
    else:
        end = task.get('due_end') or due
        end_date = (datetime.strptime(end, '%Y-%m-%d').date() + __import__('datetime').timedelta(days=1)).isoformat()
        event['start'] = {'date': due}
        event['end'] = {'date': end_date}
    return event


def _gcal_event_key(event):
    start = event.get('start', {})
    return (event.get('summary', '').strip(), start.get('date') or start.get('dateTime', '')[:10])


def _gcal_task_key(task):
    return (task.get('title', '').strip(), task.get('due', ''))


def _gcal_initial_preview(user):
    service = GOOGLE_AUTH.service(user)
    calendar_id = GOOGLE_AUTH.ensure_calendar(user)
    events = service.events().list(calendarId=calendar_id, singleEvents=True, showDeleted=False, maxResults=2500).execute().get('items', [])
    tasks = _gcal_active_tasks(user)
    mapped_ids = set()
    todo_export = []
    task_keys = {}
    for task in tasks:
        path = _gcal_task_path(task)
        with open(path, encoding='utf-8') as f:
            fm, _ = parse_frontmatter(f.read())
        if fm.get('gcal_event_id'):
            mapped_ids.add(fm['gcal_event_id'])
            continue
        if not task.get('due'):
            continue
        task_keys.setdefault(_gcal_task_key(task), []).append(task)
        todo_export.append({'task_id': task['id'], 'title': task['title'], 'due': task['due'], 'source': 'todo'})
    google_import = []
    conflicts = []
    for event in events:
        if event.get('id') in mapped_ids:
            continue
        private = event.get('extendedProperties', {}).get('private', {})
        if private.get('todo_app') == 'y8y-todolist':
            continue
        candidate = {'event_id': event.get('id', ''), 'title': event.get('summary', '(无标题)'), 'due': (event.get('start', {}).get('date') or event.get('start', {}).get('dateTime', '')[:10]), 'source': 'google'}
        if task_keys.get(_gcal_event_key(event)):
            candidate['task_id'] = task_keys[_gcal_event_key(event)][0]['id']
            conflicts.append(candidate)
        else:
            google_import.append(candidate)
    return {'calendar_name': 'TODO', 'todo_to_google': todo_export, 'google_to_todo': google_import, 'conflicts': conflicts}


def _gcal_delete_event(user, fm, timeout=None):
    """Best effort remote deletion after local completion; never roll back a completed Todo.
    返回 True = 已处理(或无需处理); False = 本次失败(调用方可重试)。"""
    event_id = fm.get('gcal_event_id', '')
    if not event_id or user == 'legacy-bridge':
        return True
    try:
        calendar_id = GOOGLE_AUTH.ensure_calendar(user, timeout=timeout)
        GOOGLE_AUTH.service(user, timeout=timeout).events().delete(calendarId=calendar_id, eventId=event_id).execute()
        return True
    except Exception as e:
        # 保留 gcal_event_id 以便后续同步层重试；本地完成永远优先。
        print(f'Google Calendar event deletion pending: {type(e).__name__}: {e}', flush=True)
        return False


def _gcal_task_from_path(folder, task_id, fpath, fm, body):
    return {
        'id': task_id, 'folder': folder, 'title': os.path.basename(fpath)[:-3],
        'due': fm.get('due', ''), 'due_end': fm.get('due_end', ''), 'due_time': fm.get('due_time', ''),
        'owner': _task_owner(fm), 'body': body,
        'project': fm.get('project', ''), 'color': fm.get('color', ''),
    }


def _gcal_is_not_found(exc):
    """判断 Google API 异常是否为「事件不存在(404)」。"""
    resp = getattr(exc, 'resp', None)
    if getattr(resp, 'status', None) == 404:
        return True
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError) and exc.resp is not None and exc.resp.status == 404:
            return True
    except Exception:
        pass
    text = str(exc)
    return '404' in text or 'notFound' in text or 'Not Found' in text


def _gcal_upsert_task(user, task, timeout=None):
    """Best effort immediate Todo→Google write; local Todo remains authoritative on failure.
    返回 True = 已同步(或按规则跳过); False = 本次同步失败(调用方可重试)。"""
    if user == 'legacy-bridge' or task.get('owner') not in (user, 'shared') or not task.get('due'):
        return True
    try:
        path = _gcal_task_path(task)
        with open(path, encoding='utf-8') as f:
            fm, _ = parse_frontmatter(f.read())
        task_uid = fm.get('task_uid') or str(uuid.uuid4())
        # frontmatter 是项目归属与卡片颜色的权威来源(部分调用点不带这两项)。
        task['project'] = fm.get('project', '') or task.get('project', '')
        task['color'] = fm.get('color', '') or task.get('color', '')
        event_body = _gcal_event_body(task, task_uid)
        if not event_body:
            return True
        calendar_id = GOOGLE_AUTH.ensure_calendar(user, timeout=timeout)
        service = GOOGLE_AUTH.service(user, timeout=timeout)
        event_id = fm.get('gcal_event_id', '')
        if event_id:
            try:
                event = service.events().update(calendarId=calendar_id, eventId=event_id, body=event_body).execute()
            except Exception as e:
                # 事件在 Google 侧已经不存在了 —— 最典型就是「完成删事件 → 恢复未完成」
                # 残留了旧的 gcal_event_id, 或用户自己在 Google 日历里删掉过事件。
                # 不重建的话这个任务会永远 404 同步不上、日历上再也不出现 (2026-09-20 修)。
                if not _gcal_is_not_found(e):
                    raise
                print(f'Google 事件不存在, 重新创建: {event_id}', flush=True)
                event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        else:
            event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        _gcal_write_task_meta(task, {'task_uid': task_uid, 'gcal_event_id': event['id']})
        GOOGLE_AUTH.mark_synced(user)
        return True
    except Exception as e:
        print(f'Google Calendar Todo push pending: {type(e).__name__}: {e}', flush=True)
        return False


# ── Google 同步后台队列 (2026-09-20) ─────────────────────────────
# 为什么: 单线程 HTTPServer 下, 在请求里同步打 Google API (~330ms/次, 且原本无超时)
# 会把整个 8086 冻住 —— 拖拽/勾选/改标题都受连累。本地 md 是权威源, Google 晚 ~350ms
# 跟上对人类无感, 所以同步移出请求线程。
#  - 同一任务只保留最新一次待同步(连续拖拽天然合并, 不排队积压)
#  - 失败重试一次(间隔 2s), 仍失败只写日志, 不阻塞任何人
GCAL_SYNC_TIMEOUT_SEC = 15
GCAL_SYNC_RETRY_DELAY_SEC = 2
_gcal_pending = {}          # key → (seq, job)   最新待同步意图
_gcal_queued = {}           # key → 已入队/处理中的 seq
_gcal_seq = 0
_gcal_queue = queue.Queue()
_gcal_queue_lock = threading.Lock()
_gcal_worker_started = False


def _gcal_run_job(job):
    kind, user, payload = job
    if kind == 'delete':
        return _gcal_delete_event(user, payload, timeout=GCAL_SYNC_TIMEOUT_SEC)
    return _gcal_upsert_task(user, payload, timeout=GCAL_SYNC_TIMEOUT_SEC)


def _gcal_sync_worker():
    while True:
        key = _gcal_queue.get()
        requeue = False
        try:
            with _gcal_queue_lock:
                entry = _gcal_pending.pop(key, None)
            if entry:
                seq, job = entry
                if not _gcal_run_job(job):
                    time.sleep(GCAL_SYNC_RETRY_DELAY_SEC)
                    if not _gcal_run_job(job):
                        print(f'Google Calendar 同步重试仍失败(已放弃, 本地数据不受影响): {key}', flush=True)
                # 处理期间又来了更新的意图 → 重新入队, 不能让它烂在 _gcal_pending 里
                # (这是本实现最容易踩的坑: 不重排 = 那次改动永远不同步)
                with _gcal_queue_lock:
                    newer = _gcal_pending.get(key)
                    if newer and newer[0] > seq:
                        _gcal_queued[key] = newer[0]
                        requeue = True
                    else:
                        _gcal_queued.pop(key, None)
        except Exception as e:                     # worker 绝不能死
            print(f'Google Calendar 同步 worker 异常: {type(e).__name__}: {e}', flush=True)
            with _gcal_queue_lock:
                _gcal_queued.pop(key, None)
        finally:
            _gcal_queue.task_done()
        if requeue:
            _gcal_queue.put(key)


def _gcal_enqueue_job(key, kind, user, payload):
    """把一次 Todo→Google 动作排到后台 (调用方立即返回, 不等 Google)。
    同一 key 只保留最新意图; 已在队列/处理中时不重复入队 —— worker 收尾会自己重排。"""
    global _gcal_worker_started, _gcal_seq
    with _gcal_queue_lock:
        if not _gcal_worker_started:
            threading.Thread(target=_gcal_sync_worker, name='gcal-sync', daemon=True).start()
            _gcal_worker_started = True
        _gcal_seq += 1
        seq = _gcal_seq
        _gcal_pending[key] = (seq, (kind, user, payload))
        if key in _gcal_queued:
            return
        _gcal_queued[key] = seq
    _gcal_queue.put(key)


def _gcal_task_key_from_path(fpath):
    """upsert 与 delete 共用同一个 key 空间(任务文件路径), 保证「最新意图赢」:
    例如拖拽(upsert)后立刻勾完成(delete), delete 会顶掉还没跑的 upsert,
    否则后台的 upsert 会在事件删掉之后又把它插回来。"""
    return 'task::' + os.path.abspath(fpath)


def _gcal_enqueue(user, task):
    """Todo→Google 写入/更新 (后台)。"""
    if user == 'legacy-bridge' or task.get('owner') not in (user, 'shared') or not task.get('due'):
        return
    try:
        key = _gcal_task_key_from_path(_gcal_task_path(task))
    except Exception:
        return
    _gcal_enqueue_job(key, 'upsert', user, task)


def _gcal_enqueue_delete(user, fm, fpath=''):
    """Todo 完成/删除 → 删掉对应 Google 事件 (后台)。"""
    event_id = (fm or {}).get('gcal_event_id', '')
    if not event_id or user == 'legacy-bridge':
        return
    # 只带走 event_id 的副本: 调用方在入队后会 fm.pop('gcal_event_id'),
    # 直接传 fm 引用会被那次 pop 掏空 (异步 = 真要小心可变对象共享)。
    key = _gcal_task_key_from_path(fpath) if fpath else 'delete::' + event_id
    _gcal_enqueue_job(key, 'delete', user, {'gcal_event_id': event_id})


def _gcal_apply_initial_export(user):
    """Apply the reviewed Todo→Google portion only. Conflicts/imports need explicit later resolutions."""
    preview = _gcal_initial_preview(user)
    if preview['google_to_todo'] or preview['conflicts']:
        raise GoogleCalendarError('首次同步预览已变化，包含 Google 导入项或冲突，需重新确认')
    service = GOOGLE_AUTH.service(user)
    calendar_id = GOOGLE_AUTH.ensure_calendar(user)
    tasks_by_id = {task['id']: task for task in _gcal_active_tasks(user)}
    created = []
    for candidate in preview['todo_to_google']:
        task = tasks_by_id.get(candidate['task_id'])
        if not task:
            continue
        task_uid = _gcal_task_uid(task) or str(uuid.uuid4())
        event_body = _gcal_event_body(task, task_uid)
        if not event_body:
            continue
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        _gcal_write_task_meta(task, {'task_uid': task_uid, 'gcal_event_id': event['id']})
        created.append({'task_id': task['id'], 'event_id': event['id'], 'title': task['title']})
    GOOGLE_AUTH.mark_synced(user)
    return {'created': created, 'count': len(created)}


class TodolistHandler(http.server.BaseHTTPRequestHandler):

    def _current_user(self):
        """Map Cloudflare Access authenticated email → user id (alice/bob).
        Also accepts X-Internal-Token (machine-to-machine legacy-bridge, 2026-08-14).
        Returns None when unauthenticated or unknown identity."""
        email = (self.headers.get('Cf-Access-Authenticated-User-Email') or '').strip().lower()
        if email in ACCESS_USERS:
            return ACCESS_USERS[email]
        token = (self.headers.get('X-Internal-Token') or '').strip()
        if token:
            # INTERNAL_TOKENS 存储方向: {user: token}，反向查找
            for user, tok in INTERNAL_TOKENS.items():
                if tok == token:
                    return user
        return None

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _send_html(self, content, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(content.encode('utf-8'))

    def _send_error(self, msg, status=400):
        self._send_json({'error': msg}, status)

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode('utf-8')
        return json.loads(raw)

    def _parse_path(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip('/')
        return path

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        path = self._parse_path()

        # 共享 y8y-chrome 品牌资产 (header.css/tokens.css/theme.js/logo_zoom.png) → /srv 单源
        _shared = resolve_shared_asset(self.path)
        if _shared:
            _fp, _ct = _shared
            with open(_fp, 'rb') as _f:
                _data = _f.read()
            self.send_response(200)
            self.send_header('Content-Type', _ct)
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.send_header('Content-Length', str(len(_data)))
            self.end_headers()
            self.wfile.write(_data)
            return

        # Google OAuth 回调必须先于普通 API 鉴权处理：浏览器从 Google 跳回时，
        # Cloudflare Access 已认证 cookie 会自动带回；state 则把授权码绑定到发起用户。
        if path == '/todo/api/google-calendar/oauth/callback':
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get('error'):
                self._send_html('<!doctype html><meta charset="utf-8"><title>Google Calendar</title><p>Google 授权被取消。请回到 Todo 设置重试。</p>', 400)
                return
            state = (query.get('state') or [''])[0]
            code = (query.get('code') or [''])[0]
            try:
                user = GOOGLE_AUTH.finish_oauth(state, code, GOOGLE_CALLBACK_URI)
                calendar_id = GOOGLE_AUTH.ensure_calendar(user)
                self._send_html('<!doctype html><meta charset="utf-8"><title>Google Calendar 已连接</title><p>✓ Google Calendar 已连接，专用 TODO 日历已就绪。</p><p>请回到 Todo 设置，查看首次双向导入预览并确认。</p>')
            except GoogleCalendarError as e:
                self._send_html(f'<!doctype html><meta charset="utf-8"><title>Google Calendar</title><p>授权失败：{str(e)}</p>', 400)
            except Exception as e:
                # 不写 state/code/token；只记录异常类型与消息，供服务端诊断 OAuth token exchange。
                print(f'Google OAuth callback failed: {type(e).__name__}: {e}', flush=True)
                self._send_html('<!doctype html><meta charset="utf-8"><title>Google Calendar</title><p>授权失败，请回到 Todo 设置重试。</p>', 500)
            return

        # Google Calendar 连接状态（只暴露状态，永不返回 token/client secret）。
        if path == '/todo/api/google-calendar/status':
            user = self._current_user()
            if user is None or user == 'legacy-bridge':
                self._send_error('Unauthorized', 401)
                return
            self._send_json(GOOGLE_AUTH.status(user))
            return

        # Google Calendar 首次同步只读预览：不写 Todo、不创建/修改 Google 事件。
        if path == '/todo/api/google-calendar/initial-preview':
            user = self._current_user()
            if user is None or user == 'legacy-bridge':
                self._send_error('Unauthorized', 401)
                return
            try:
                self._send_json(_gcal_initial_preview(user))
            except GoogleCalendarError as e:
                self._send_error(str(e), 400)
            except Exception as e:
                print(f'Google Calendar preview failed: {type(e).__name__}: {e}', flush=True)
                self._send_error('无法读取 Google Calendar 预览', 502)
            return

        # API: 列表系统 (2026-08-18 升级: 购物升级为「列表」, 可设归属) — 列所有可见列表
        if path == '/todo/api/lists':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized: Cloudflare Access login required', 401)
                return
            ensure_shopping_list()   # 预置购物列表始终可用（迁移旧数据 + 补 meta.json）
            lists = []
            for lid, meta in list_all_meta().items():
                lo = meta.get('owner', 'alice')
                if not _list_visible(lo, user):
                    continue
                lists.append({
                    'id': lid,
                    'name': meta.get('name', lid),
                    'owner': lo,
                    'created': meta.get('created', ''),
                })
            self._send_json({'lists': lists, 'current_user': user})
            return

        # API: 列表条目 (2026-08-18)
        m_item = re.match(r'^/todo/api/lists/([^/]+)/items$', path)
        if m_item:
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized: Cloudflare Access login required', 401)
                return
            lid = urllib.parse.unquote(m_item.group(1))
            meta = list_meta(lid)
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', 'alice')
            if not _list_visible(lo, user):
                self._send_error('Forbidden', 403)
                return
            items = [t for t in list_items(lid) if _can_access_shopping(t['owner'], user)]
            self._send_json({'list': {'id': lid, 'name': meta.get('name', lid), 'owner': lo},
                             'items': items, 'current_user': user})
            return

        # API: 购物列表 (2026-08-18 独立购物数据域, 不参与任务系统; 升级后 = lists/shopping 兼容别名)
        if path == '/todo/api/shopping':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized: Cloudflare Access login required', 401)
                return
            ensure_shopping_list()
            items = [t for t in list_shopping() if _can_access_shopping(t['owner'], user)]
            self._send_json({'items': items, 'current_user': user})
            return

        # API: persistent-projects (2026-08-12: localStorage → 后端, 跨设备同步)
        if path == '/todo/api/persistent-projects':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized: Cloudflare Access login required', 401)
                return
            self._send_json({'projects': get_persistent_projects(user)})
            return

        # API: list tasks
        if path == '/todo/api/tasks':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized: Cloudflare Access login required', 401)
                return
            tasks = [t for t in list_tasks() if _user_can_access(t['owner'], user)]
            completed_tasks = [t for t in list_completed_tasks() if _user_can_access(t['owner'], user)]
            sections = classify_tasks(tasks)
            self._send_json({
                'current_user': user,
                'tasks': tasks,
                'completed_tasks': completed_tasks,
                'sections': {k: [t['id'] for t in v] for k, v in sections.items()},
            })
            return

        # API: get single task
        m = re.match(r'^/todo/api/tasks/(.+)', path)
        if m:
            fname = urllib.parse.unquote(m.group(1))
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized: Cloudflare Access login required', 401)
                return
            for folder in ('todo', 'recurring'):
                fpath = os.path.join(TODOLIST_DIR, folder, fname)
                if os.path.isfile(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    fm, body = parse_frontmatter(raw)
                    owner = _task_owner(fm)
                    if not _user_can_access(owner, user):
                        self._send_error('Forbidden', 403)
                        return
                    self._send_json({
                        'id': fname,
                        'title': os.path.basename(fname)[:-3],
                        'due': fm.get('due', ''),
                        'due_end': fm.get('due_end', ''),
                        'created': fm.get('created', ''),
                        'color': fm.get('color', 'green'),
                        'status': fm.get('status', 'todo'),
                        'recurring': fm.get('recurring', ''),
                        'project': fm.get('project', ''),
                        'owner': owner,
                        'body': body,
                        'folder': folder,
                    })
                    return
            self._send_error('Task not found', 404)
            return

        # Serve static files（2026-09-24：从「整个目录都是静态根」改为共享守卫 + 默认拒绝）
        if path.startswith('/todo/'):
            rel_path = path[6:] or 'index.html'
        else:
            rel_path = path.lstrip('/') or 'index.html'

        kind, res = classify_static(rel_path)
        if kind == 'sensitive':
            # 敏感路径（.git/、*.py、backup/…）：硬 404，**不给 SPA fallback**
            self._send_error('Not found', 404)
            return
        if kind == 'missing':
            # 保持原有行为：未知路径交给前端（SPA fallback → index.html）
            kind, res = classify_static('index.html')
        if kind != 'public' or res is None:
            self._send_error('Not found', 404)
            return
        file_path = res[0]

        ext_map = {
            '.html': 'text/html; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.png': 'image/png',
            '.svg': 'image/svg+xml',
            '.woff2': 'font/woff2',
        }
        _, ext = os.path.splitext(file_path)
        content_type = ext_map.get(ext, 'application/octet-stream')

        try:
            with open(file_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            # 字体是静态资产（文件名不变内容不变）→ 长缓存；其他文件保持 no-store
            cache_control = ('public, max-age=31536000, immutable'
                             if ext == '.woff2'
                             else 'no-cache, no-store, must-revalidate')
            self.send_header('Cache-Control', cache_control)
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self._send_error('Not found', 404)

    def do_HEAD(self):
        # Cloudflare Tunnel sends HEAD probe before GET to check origin liveness.
        # Python BaseHTTPRequestHandler's default do_HEAD() returns empty reply,
        # which causes cloudflared to EOF → 502 Bad Gateway at the edge.
        # Mirror do_GET()'s routing but skip writing the body.
        path = self._parse_path()

        # 共享品牌资产 (HEAD 探活): 命中返回头, 不写 body
        _shared = resolve_shared_asset(self.path)
        if _shared:
            _fp, _ct = _shared
            with open(_fp, 'rb') as _f:
                _data = _f.read()
            self.send_response(200)
            self.send_header('Content-Type', _ct)
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.send_header('Content-Length', str(len(_data)))
            self.end_headers()
            return

        # API health check (same shape as do_GET)
        if path == '/todo/api/tasks':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            return

        # Static file HEAD（2026-09-24：改走共享守卫；原来是无白名单地拼 STATIC_ROOT）
        # stream file bytes so Content-Length matches body
        if path == '/' or path == '/todo/' or path == '/todo':
            path = '/todo/index.html'
        if not path.startswith('/todo/'):
            path = '/todo' + path
        rel = path[len('/todo/'):] or 'index.html'

        kind, res = classify_static(rel)
        if kind == 'missing':
            kind, res = classify_static('index.html')   # 与 do_GET 一致：SPA fallback
        if kind != 'public' or res is None:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', '9')
            self.end_headers()
            return

        full_path = str(res[0])
        try:
            with open(full_path, 'rb') as f:
                data = f.read()
            self.send_response(200)
            if full_path.endswith('.html'):
                self.send_header('Content-Type', 'text/html; charset=utf-8')
            elif full_path.endswith('.js'):
                self.send_header('Content-Type', 'application/javascript')
            elif full_path.endswith('.css'):
                self.send_header('Content-Type', 'text/css')
            elif full_path.endswith('.woff2'):
                self.send_header('Content-Type', 'font/woff2')
            else:
                self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            # 与 do_GET 一致：woff2 长缓存，其余 no-store
            cache_control = ('public, max-age=31536000, immutable'
                             if full_path.endswith('.woff2')
                             else 'no-cache, no-store, must-revalidate')
            self.send_header('Cache-Control', cache_control)
            self.end_headers()
        except FileNotFoundError:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', '9')
            self.end_headers()

    def do_POST(self):
        path = self._parse_path()
        body_data = self._read_body()
        user = self._current_user()
        if user is None:
            self._send_error('Unauthorized: Cloudflare Access login required', 401)
            return

        # Google Calendar: 启动标准 Web OAuth 授权。只返回 Google 授权 URL，不暴露凭据。
        if path == '/todo/api/google-calendar/oauth/start':
            if user == 'legacy-bridge':
                self._send_error('Forbidden', 403)
                return
            try:
                self._send_json({'authorization_url': GOOGLE_AUTH.begin_oauth(user, GOOGLE_CALLBACK_URI)})
            except GoogleCalendarError as e:
                self._send_error(str(e), 400)
            return

        # Google Calendar: 已由用户在 Telegram 明确确认后才允许执行首次写入。
        if path == '/todo/api/google-calendar/initial-apply':
            if user == 'legacy-bridge' or body_data.get('confirm') is not True:
                self._send_error('Forbidden', 403)
                return
            try:
                self._send_json(_gcal_apply_initial_export(user))
            except GoogleCalendarError as e:
                self._send_error(str(e), 409)
            except Exception as e:
                print(f'Google Calendar initial apply failed: {type(e).__name__}: {e}', flush=True)
                self._send_error('首次同步失败，未完成的条目可重试', 502)
            return

        # 列表系统: 新建列表 (2026-08-18 升级: 每个列表可设归属(self/shared)
        if path == '/todo/api/lists':
            name = body_data.get('name', '').strip()
            if not name:
                self._send_error('List name is required')
                return
            # owner: self(默认)->当前用户, 或 shared; legacy-bridge 不能建列表
            owner = body_data.get('owner', 'self')
            if user == 'legacy-bridge':
                self._send_error('Forbidden: legacy-bridge cannot create lists', 403)
                return
            if owner == 'shared':
                owner = 'shared'
            else:
                owner = user   # self → 当前用户 (alice/bob)
            lid = name
            d = _list_dir(lid)
            if os.path.isdir(d):
                self._send_error('List with that name already exists', 409)
                return
            os.makedirs(d, exist_ok=True)
            meta = {'name': name, 'owner': owner, 'created': date.today().isoformat()}
            with open(os.path.join(d, 'meta.json'), 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
            self._send_json({'id': lid, 'name': name, 'owner': owner, 'created': meta['created']}, 201)
            return

        # 列表系统: 某列表加条目 (2026-08-18)
        m_item = re.match(r'^/todo/api/lists/([^/]+)/items$', path)
        if m_item:
            lid = urllib.parse.unquote(m_item.group(1))
            meta = list_meta(lid)
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', 'alice')
            if not _list_visible(lo, user):
                self._send_error('Forbidden', 403)
                return
            # legacy-bridge 强制查重（同 list 内同 title 不重复；done 恢复 todo）
            dedupe = True if user == 'legacy-bridge' else body_data.get('dedupe', False)
            title = body_data.get('title', '').strip()
            if not title:
                self._send_error('Item title is required')
                return
            # 条目 = <uuid>.md，标题/内容 = 文件正文 body 原文 (2026-08-20: 长文本无上限)
            # 文件名固定 uuid → 无系统级文件名长度限制；title 直接读 body 内容
            fname = f'{uuid.uuid4()}.md'
            if dedupe:
                for it in list_items(lid):
                    if it['title'] == title and _can_access_shopping(it['owner'], user):
                        if it['status'] == 'done':
                            it_path = os.path.join(_list_dir(lid), it['id'])
                            try:
                                with open(it_path, 'r', encoding='utf-8') as f:
                                    raw = f.read()
                                fm, body = parse_frontmatter(raw)
                                fm['status'] = 'todo'
                                with open(it_path, 'w', encoding='utf-8') as f:
                                    f.write(build_frontmatter(fm) + '\n\n' + body)
                            except Exception:
                                pass
                        self._send_json({'duplicate': True, 'existing_id': it['id'], 'message': '已在清单中'}, 200)
                        return
            fm = {'status': 'todo', 'created': date.today().isoformat(), 'owner': lo}
            frontmatter = build_frontmatter(fm)
            fpath = os.path.join(_list_dir(lid), fname)
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(frontmatter + '\n\n' + title)
            self._send_json({'id': fname, 'message': 'Item added'}, 201)
            return

        # Create task
        if path == '/todo/api/tasks':
            title = body_data.get('title', '').strip()
            if not title:
                self._send_error('Title is required')
                return

            due = body_data.get('due', '').strip()
            due_end = body_data.get('due_end', '').strip()
            due_time = body_data.get('due_time', '').strip()
            color = body_data.get('color', 'green').strip()
            if color not in VALID_COLORS:
                color = 'green'
            content = body_data.get('content', title)
            recurring = body_data.get('recurring', False)
            project = body_data.get('project', '').strip()
            # owner: default = current user; only allow self or shared
            # (2026-08-14: alice 可创建 legacy-agent 归属任务 —— 历史机器日历)
            # (2026-08-14: legacy-bridge 桥身份强制 owner=legacy-agent, 不碰其他)
            owner = body_data.get('owner', user)
            if user == 'legacy-bridge':
                owner = 'legacy-agent'
            elif owner not in (user, 'shared') and not (owner == 'legacy-agent' and user == 'alice'):
                self._send_error('Forbidden: cannot create task for another user', 403)
                return

            fname = title.replace('/', '_').replace(' ', '') + '.md'
            folder = 'recurring' if recurring else 'todo'

            fm = {'status': 'todo', 'color': color, 'created': date.today().isoformat(), 'owner': owner}
            if due:
                fm['due'] = due
            if due_end:
                # 结束日期必须 ≥ 开始日期；若 < due 则忽略
                if not due or due_end >= due:
                    fm['due_end'] = due_end
            if due_time:
                fm['due_time'] = due_time
            if project:
                fm['project'] = project
            if recurring:
                fm['recurring'] = body_data.get('recurring_dates', [])

            frontmatter = build_frontmatter(fm)
            content_full = frontmatter + '\n\n' + content + '\n'

            # 项目文件夹方案 (2026-08-13): 有 project 的一次性任务存到 todo/<project>/ 子目录
            if project and not recurring:
                proj_dir = os.path.join(TODOLIST_DIR, 'todo', project)
                os.makedirs(proj_dir, exist_ok=True)
                fpath = os.path.join(proj_dir, fname)
                task_id = f'{project}/{fname}'
            else:
                fpath = os.path.join(TODOLIST_DIR, folder, fname)
                task_id = fname
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(content_full)
            _gcal_enqueue(user, {
                'id': task_id, 'folder': folder, 'title': title, 'due': due, 'due_end': due_end,
                'due_time': due_time, 'owner': owner, 'body': content,
                'project': project, 'color': color,
            })

            self._send_json({'id': task_id, 'message': 'Task created'}, 201)
            return

        # 购物列表: 新增物品 (2026-08-18, 定案: 购物 = 列表系统内 shopping 列表, 归属可变)
        if path == '/todo/api/shopping':
            ensure_shopping_list()
            # owner 由列表归属决定（lists/shopping meta.json）；默认 shared 保持旧行为
            owner = (list_meta('shopping') or {}).get('owner', 'shared')
            if user == 'legacy-bridge':
                # legacy-bridge 强制查重（2026-08-18:「看有没有重复，有就不用加2次」）
                dedupe = True
            else:
                # Web UI 默认不查重（允许用户建同名条目，如「牛奶」和「牛奶(2)」手动区分）
                dedupe = body_data.get('dedupe', False)
            title = body_data.get('title', '').strip()
            if not title:
                self._send_error('Item title is required')
                return
            # 条目 = <uuid>.md，标题/内容 = 文件正文 (2026-08-20: 长文本无上限)
            # 购物别名与列表系统一致: 文件名固定 uuid, title 直接读 body 内容
            fname = f'{uuid.uuid4()}.md'
            # 查重（2026-08-18: 「看有没有重复，有就不用加2次」）：
            # 同 title 的条目已存在 → 不重复添加；
            # 若已购(done)则恢复为待购(todo)，让同一物品永远只有一条
            if dedupe:
                for it in list_shopping():
                    if it['title'] == title and _can_access_shopping(it['owner'], user):
                        if it['status'] == 'done':
                            it_path = os.path.join(SHOPPING_DIR, it['id'])
                            try:
                                with open(it_path, 'r', encoding='utf-8') as f:
                                    raw = f.read()
                                fm, body = parse_frontmatter(raw)
                                fm['status'] = 'todo'
                                with open(it_path, 'w', encoding='utf-8') as f:
                                    f.write(build_frontmatter(fm) + '\n\n' + body)
                            except Exception:
                                pass
                        self._send_json({'duplicate': True, 'existing_id': it['id'], 'message': '已在购物清单中'}, 200)
                        return
            fm = {'status': 'todo', 'created': date.today().isoformat(), 'owner': owner}
            frontmatter = build_frontmatter(fm)
            os.makedirs(SHOPPING_DIR, exist_ok=True)
            fpath = os.path.join(SHOPPING_DIR, fname)
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(frontmatter + '\n\n' + title)
            self._send_json({'id': fname, 'message': 'Item added'}, 201)
            return

        # Mark as done
        m = re.match(r'^/todo/api/tasks/(.+)/done', path)
        if m:
            fname = urllib.parse.unquote(m.group(1))
            for folder in ('todo', 'recurring'):
                fpath = os.path.join(TODOLIST_DIR, folder, fname)
                if os.path.isfile(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    fm, body = parse_frontmatter(raw)
                    if not _user_can_access(_task_owner(fm), user):
                        self._send_error('Forbidden', 403)
                        return
                    fm['status'] = 'done'
                    fm['completed'] = date.today().isoformat()

                    # Check if recurring
                    if 'recurring' in fm and fm['recurring']:
                        # Recurring logic: update due to next date
                        recurring_dates = fm['recurring']
                        if isinstance(recurring_dates, str):
                            recurring_dates = [recurring_dates]
                        old_due = fm.get('due', '')
                        if old_due:
                            try:
                                old_due_date = datetime.strptime(old_due, '%Y-%m-%d').date()
                                current_year = old_due_date.year
                                next_date = None
                                for rd in recurring_dates:
                                    parts = rd.split('-')
                                    if len(parts) == 1:
                                        rd_date = date(current_year, old_due_date.month, int(parts[0]))
                                    else:
                                        rd_date = date(current_year, int(parts[0]), int(parts[1]))
                                    if rd_date > old_due_date:
                                        if next_date is None or rd_date < next_date:
                                            next_date = rd_date
                                if next_date is None:
                                    for rd in recurring_dates:
                                        parts = rd.split('-')
                                        if len(parts) == 1:
                                            rd_date = date(current_year + 1, old_due_date.month, int(parts[0]))
                                        else:
                                            rd_date = date(current_year + 1, int(parts[0]), int(parts[1]))
                                        if next_date is None or rd_date < next_date:
                                            next_date = rd_date
                                if next_date:
                                    fm['due'] = next_date.isoformat()
                                    fm['status'] = 'todo'
                                    del fm['completed']
                            except ValueError:
                                pass

                        # 当前 recurring 实例完成：删掉对应 Google 事件；下一期将以新的 due 创建新事件。
                        _gcal_enqueue_delete(user, fm, fpath)
                        fm.pop('gcal_event_id', None)
                        frontmatter = build_frontmatter(fm)
                        content_full = frontmatter + '\n\n' + body + '\n'
                        with open(fpath, 'w', encoding='utf-8') as f:
                            f.write(content_full)
                        # 真的把「下一期」的事件建出来 (2026-09-20 修): 原来只删不建, 下一期
                        # 在 Google 日历上就消失了。key 与上面的 delete 相同 → 若 delete 还没跑,
                        # 这次 upsert 会把它顶掉(等于把旧事件改到下一期日期), 既不会重复也不会漏。
                        _gcal_enqueue(user, _gcal_task_from_path('recurring', fname, fpath, fm, body))
                        self._send_json({'message': 'Recurring task updated', 'next_due': fm.get('due', '')})
                    else:
                        # Move to done/ (保留项目子目录: done/日期/项目/文件名.md)
                        frontmatter = build_frontmatter(fm)
                        content_full = frontmatter + '\n\n' + body + '\n'
                        today_str = date.today().isoformat()
                        done_dir = os.path.join(TODOLIST_DIR, 'done', today_str)
                        done_path = os.path.join(done_dir, fname)
                        os.makedirs(os.path.dirname(done_path), exist_ok=True)
                        with open(done_path, 'w', encoding='utf-8') as f:
                            f.write(content_full)
                        os.remove(fpath)
                        # 本地归档已成功后，才删除用户确认要移除的 Google 事件。(后台执行, 不堵请求)
                        _gcal_enqueue_delete(user, fm, fpath)
                        self._send_json({'message': 'Task completed'})
                    return
            self._send_error('Task not found', 404)
            return

        self._send_error('Not found', 404)

    def do_PUT(self):
        path = self._parse_path()
        body_data = self._read_body()
        user = self._current_user()
        if user is None:
            self._send_error('Unauthorized: Cloudflare Access login required', 401)
            return

        # 列表系统: 修改列表归属或条目 (2026-08-18)
        # 列表归属更改: PUT /todo/api/lists/<id>  {owner?}
        m_listmeta = re.match(r'^/todo/api/lists/([^/]+)$', path)
        if m_listmeta:
            lid = urllib.parse.unquote(m_listmeta.group(1))
            meta = list_meta(lid)
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', 'alice')
            # 列表所有者本人才能改归属；legacy-bridge 不可建/不可改归属
            if user == 'legacy-bridge' or (lo != 'shared' and lo != user):
                self._send_error('Forbidden', 403)
                return
            if 'owner' in body_data:
                ov = body_data['owner']
                if ov == 'shared':
                    meta['owner'] = 'shared'
                elif ov in ('self', user):
                    meta['owner'] = user
                elif ov in ('alice', 'bob') and ov == user:
                    meta['owner'] = user
                else:
                    self._send_error('Forbidden: cannot reassign list', 403)
                    return
                # 列表归属变化 → 所有条目 owner 同步更新
                for it in list_items(lid):
                    it_path = os.path.join(_list_dir(lid), it['id'])
                    try:
                        with open(it_path, 'r', encoding='utf-8') as f:
                            raw = f.read()
                        ifm, ibody = parse_frontmatter(raw)
                        ifm['owner'] = meta['owner']
                        with open(it_path, 'w', encoding='utf-8') as f:
                            f.write(build_frontmatter(ifm) + '\n\n' + ibody + '\n')
                    except Exception:
                        pass
            with open(os.path.join(_list_dir(lid), 'meta.json'), 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False)
            self._send_json({'id': lid, 'name': meta.get('name', lid), 'owner': meta['owner']})
            return

        # 列表条目更新: PUT /todo/api/lists/<id>/items/<item>  {status?|title?}
        m_listitem = re.match(r'^/todo/api/lists/([^/]+)/items/(.+)', path)
        if m_listitem:
            lid = urllib.parse.unquote(m_listitem.group(1))
            fname = urllib.parse.unquote(m_listitem.group(2))
            meta = list_meta(lid)
            if meta is None:
                self._send_error('List not found', 404)
                return
            if not _list_visible(meta.get('owner', 'alice'), user):
                self._send_error('Forbidden', 403)
                return
            fpath = os.path.join(_list_dir(lid), fname)
            if not os.path.isfile(fpath):
                self._send_error('Item not found', 404)
                return
            with open(fpath, 'r', encoding='utf-8') as f:
                raw = f.read()
            fm, body = parse_frontmatter(raw)
            if not _can_access_shopping(_task_owner(fm), user):
                self._send_error('Forbidden', 403)
                return
            if 'status' in body_data:
                previous_status = fm.get('status', 'todo')
                v = body_data['status'] if body_data['status'] in ('todo', 'done') else 'todo'
                fm['status'] = v
                if v == 'done' and previous_status != 'done':
                    fm['completed'] = datetime.now().astimezone().isoformat(timespec='microseconds')
                elif v == 'todo':
                    fm.pop('completed', None)
            # 改标题 = 改文件正文 body（文件名固定 uuid，不移动文件, 2026-08-20）
            if 'title' in body_data:
                new_title = body_data['title'].strip()
                if new_title:
                    body = new_title
            frontmatter = build_frontmatter(fm)
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(frontmatter + '\n\n' + body)
            self._send_json({'id': fname, 'message': 'Item updated'})
            return

        # 购物列表兼容别名: PUT /todo/api/shopping/<item> = lists/shopping 条目
        m_shop = re.match(r'^/todo/api/shopping/(.+)', path)
        if m_shop:
            ensure_shopping_list()
            fname = urllib.parse.unquote(m_shop.group(1))
            fpath = os.path.join(SHOPPING_DIR, fname)
            if not os.path.isfile(fpath):
                self._send_error('Item not found', 404)
                return
            with open(fpath, 'r', encoding='utf-8') as f:
                raw = f.read()
            fm, body = parse_frontmatter(raw)
            if not _can_access_shopping(_task_owner(fm), user):
                self._send_error('Forbidden', 403)
                return
            if 'status' in body_data:
                previous_status = fm.get('status', 'todo')
                v = body_data['status'] if body_data['status'] in ('todo', 'done') else 'todo'
                fm['status'] = v
                if v == 'done' and previous_status != 'done':
                    fm['completed'] = datetime.now().astimezone().isoformat(timespec='microseconds')
                elif v == 'todo':
                    fm.pop('completed', None)
            # 改标题 = 改文件正文 body（文件名固定 uuid，不移动文件, 2026-08-20）
            if 'title' in body_data:
                new_title = body_data['title'].strip()
                if new_title:
                    body = new_title
            frontmatter = build_frontmatter(fm)
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write(frontmatter + '\n\n' + body)
            self._send_json({'id': fname, 'message': 'Item updated'})
            return

        # API: 持久项目整体覆盖 (2026-08-12: localStorage → 后端)
        if path == '/todo/api/persistent-projects':
            projects = body_data.get('projects', [])
            saved = save_persistent_projects(user, projects)
            self._send_json({'projects': saved})
            return

        m = re.match(r'^/todo/api/tasks/(.+)', path)
        if m:
            fname = urllib.parse.unquote(m.group(1))
            # 已完成任务 (done/日期/.../文件.md): 支持编辑 + status=todo 恢复 (2026-08-18)
            if fname.startswith('done/'):
                rel = fname[len('done/'):]  # <date>/[project/]<fname.md>
                fpath = os.path.join(TODOLIST_DIR, 'done', rel)
                if not os.path.isfile(fpath):
                    self._send_error('Task not found', 404)
                    return
                with open(fpath, 'r', encoding='utf-8') as f:
                    raw = f.read()
                fm, body = parse_frontmatter(raw)
                if not _user_can_access(_task_owner(fm), user):
                    self._send_error('Forbidden', 403)
                    return
                # 恢复为未完成: 移到 todo/ 目录, 去掉 completed
                if body_data.get('status') == 'todo':
                    fm['status'] = 'todo'
                    fm.pop('completed', None)
                    fname_only = os.path.basename(fpath)
                    proj = fm.get('project', '')
                    tdir = os.path.join(TODOLIST_DIR, 'todo', proj) if proj else os.path.join(TODOLIST_DIR, 'todo')
                    os.makedirs(tdir, exist_ok=True)
                    tpath = os.path.join(tdir, fname_only)
                    with open(tpath, 'w', encoding='utf-8') as f:
                        f.write(build_frontmatter(fm) + '\n\n' + body + '\n')
                    os.remove(fpath)
                    # 恢复为未完成 → 把 Google 事件同步回来 (2026-09-20 修):
                    # 「完成」会删掉事件, 恢复不重建的话这个任务会永远同步 404、日历上再也不出现。
                    # key 与 delete 共用(todo 文件路径) → 「完成→马上恢复」时 upsert 会顶掉还没跑的
                    # delete, 不会出现「删了又建」的重复事件; 若 delete 已经跑过, upsert 撞 404 会自动重建。
                    restored_id = os.path.relpath(tpath, os.path.join(TODOLIST_DIR, 'todo'))
                    _gcal_enqueue(user, _gcal_task_from_path('todo', restored_id, tpath, fm, body))
                    self._send_json({'id': fname_only, 'message': 'Task restored', 'status': 'todo'})
                    return
                # 否则原地编辑已完成任务
                if 'owner' in body_data:
                    new_owner = body_data['owner']
                    if user == 'legacy-bridge':
                        new_owner = 'legacy-agent'
                    elif new_owner not in (user, 'shared') and not (new_owner == 'legacy-agent' and user == 'alice'):
                        self._send_error('Forbidden: cannot reassign task to another user', 403)
                        return
                    fm['owner'] = new_owner
                if 'due' in body_data:
                    fm['due'] = body_data['due']
                if 'due_end' in body_data:
                    v = body_data['due_end'].strip()
                    due = fm.get('due', '')
                    if v and (not due or v >= due):
                        fm['due_end'] = v
                    else:
                        fm.pop('due_end', None)
                if 'due_time' in body_data:
                    v = body_data['due_time'].strip()
                    if v:
                        fm['due_time'] = v
                    else:
                        fm.pop('due_time', None)
                if 'color' in body_data:
                    v = body_data['color'].strip()
                    fm['color'] = v if v in VALID_COLORS else 'green'
                if 'project' in body_data:
                    v = body_data['project'].strip()
                    if v:
                        fm['project'] = v
                    else:
                        fm.pop('project', None)
                if 'content' in body_data:
                    body = body_data['content']
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(build_frontmatter(fm) + '\n\n' + body + '\n')
                self._send_json({'id': fname, 'message': 'Task updated'})
                return
            for folder in ('todo', 'recurring'):
                fpath = os.path.join(TODOLIST_DIR, folder, fname)
                if os.path.isfile(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    fm, body = parse_frontmatter(raw)
                    if not _user_can_access(_task_owner(fm), user):
                        self._send_error('Forbidden', 403)
                        return

                    # Update owner: only allow self or shared (legacy-bridge 强制保持 legacy-agent;
                    # 2026-08-15: alice 可把任务改成 legacy-agent, 与 POST 校验对称 —— 修复编辑 legacy-agent 任务被洗成 alice)
                    if 'owner' in body_data:
                        new_owner = body_data['owner']
                        if user == 'legacy-bridge':
                            new_owner = 'legacy-agent'
                        elif new_owner not in (user, 'shared') and not (new_owner == 'legacy-agent' and user == 'alice'):
                            self._send_error('Forbidden: cannot reassign task to another user', 403)
                            return
                        fm['owner'] = new_owner

                    # Update fields from request
                    if 'due' in body_data:
                        fm['due'] = body_data['due']
                    if 'due_end' in body_data:
                        v = body_data['due_end'].strip()
                        due = fm.get('due', '')
                        if v and (not due or v >= due):
                            fm['due_end'] = v
                        else:
                            fm.pop('due_end', None)
                    if 'due_time' in body_data:
                        v = body_data['due_time'].strip()
                        if v:
                            fm['due_time'] = v
                        else:
                            fm.pop('due_time', None)
                    if 'color' in body_data:
                        v = body_data['color'].strip()
                        fm['color'] = v if v in VALID_COLORS else 'green'
                        # 项目级联动: 同 project 任务一起变色 (2026-08-11)
                        cascade_project_color(fm.get('project', ''), fm['color'], user, skip_fname=fname)
                    if 'project' in body_data:
                        v = body_data['project'].strip()
                        if v:
                            fm['project'] = v
                        else:
                            fm.pop('project', None)
                    # 项目文件夹方案 (2026-08-13): 计算最终路径
                    #  - project 变了 → 移动到 todo/<新project>/（一次性任务）
                    #  - title 变了 → 改名
                    # 两者可同时发生，统一在写入前计算目标路径
                    new_fname = None
                    if 'title' in body_data:
                        new_title = body_data['title'].strip()
                        if new_title:
                            new_fname = new_title.replace('/', '_').replace(' ', '') + '.md'
                            body = body_data.get('content', body)
                    target_dir = os.path.dirname(fpath)
                    if folder == 'todo':
                        # 一次性任务: 有 project → 项目子目录; 无 project → 平铺 todo/ 根
                        target_dir = os.path.join(TODOLIST_DIR, 'todo', fm['project']) if fm.get('project') else os.path.join(TODOLIST_DIR, 'todo')
                    target_fname = new_fname if new_fname else os.path.basename(fname)
                    target_fpath = os.path.join(target_dir, target_fname)
                    if target_fpath != fpath:
                        os.makedirs(target_dir, exist_ok=True)
                        frontmatter = build_frontmatter(fm)
                        content_full = frontmatter + '\n\n' + body + '\n'
                        with open(target_fpath, 'w', encoding='utf-8') as f:
                            f.write(content_full)
                        os.remove(fpath)
                        new_id = os.path.relpath(target_fpath, os.path.join(TODOLIST_DIR, folder))
                        _gcal_enqueue(user, _gcal_task_from_path(folder, new_id, target_fpath, fm, body))
                        self._send_json({'id': new_id, 'message': 'Task updated'})
                        return
                    if 'content' in body_data:
                        body = body_data['content']

                    frontmatter = build_frontmatter(fm)
                    content_full = frontmatter + '\n\n' + body + '\n'
                    with open(fpath, 'w', encoding='utf-8') as f:
                        f.write(content_full)
                    _gcal_enqueue(user, _gcal_task_from_path(folder, fname, fpath, fm, body))
                    self._send_json({'id': fname, 'message': 'Task updated'})
                    return
            self._send_error('Task not found', 404)
            return

        self._send_error('Not found', 404)

    def do_DELETE(self):
        path = self._parse_path()
        user = self._current_user()
        if user is None:
            self._send_error('Unauthorized: Cloudflare Access login required', 401)
            return

        # 列表系统: 删除整个列表 (2026-08-18)
        m_listdel = re.match(r'^/todo/api/lists/([^/]+)$', path)
        if m_listdel:
            lid = urllib.parse.unquote(m_listdel.group(1))
            meta = list_meta(lid)
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', 'alice')
            # 列表所有者本人可删；legacy-bridge 不可删
            if user == 'legacy-bridge' or (lo != 'shared' and lo != user):
                self._send_error('Forbidden', 403)
                return
            import shutil
            shutil.rmtree(_list_dir(lid), ignore_errors=True)
            self._send_json({'message': 'List deleted'})
            return

        # 列表系统: 删除某列表条目 (2026-08-18)
        m_listitem = re.match(r'^/todo/api/lists/([^/]+)/items/(.+)', path)
        if m_listitem:
            lid = urllib.parse.unquote(m_listitem.group(1))
            fname = urllib.parse.unquote(m_listitem.group(2))
            meta = list_meta(lid)
            if meta is None:
                self._send_error('List not found', 404)
                return
            if not _list_visible(meta.get('owner', 'alice'), user):
                self._send_error('Forbidden', 403)
                return
            fpath = os.path.join(_list_dir(lid), fname)
            if not os.path.isfile(fpath):
                self._send_error('Item not found', 404)
                return
            with open(fpath, 'r', encoding='utf-8') as f:
                raw = f.read()
            fm, body = parse_frontmatter(raw)
            if not _can_access_shopping(_task_owner(fm), user):
                self._send_error('Forbidden', 403)
                return
            os.remove(fpath)
            self._send_json({'message': 'Item deleted'})
            return

        # 购物列表: 删除物品 (2026-08-18, 兼容别名 = lists/shopping 条目)
        m_shop = re.match(r'^/todo/api/shopping/(.+)', path)
        if m_shop:
            ensure_shopping_list()
            fname = urllib.parse.unquote(m_shop.group(1))
            fpath = os.path.join(SHOPPING_DIR, fname)
            if not os.path.isfile(fpath):
                self._send_error('Item not found', 404)
                return
            with open(fpath, 'r', encoding='utf-8') as f:
                raw = f.read()
            fm, body = parse_frontmatter(raw)
            if not _can_access_shopping(_task_owner(fm), user):
                self._send_error('Forbidden', 403)
                return
            os.remove(fpath)
            self._send_json({'message': 'Item deleted'})
            return

        m = re.match(r'^/todo/api/tasks/(.+)', path)
        if m:
            fname = urllib.parse.unquote(m.group(1))
            for folder in ('todo', 'recurring'):
                fpath = os.path.join(TODOLIST_DIR, folder, fname)
                if os.path.isfile(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    fm, body = parse_frontmatter(raw)
                    if not _user_can_access(_task_owner(fm), user):
                        self._send_error('Forbidden', 403)
                        return
                    os.remove(fpath)
                    self._send_json({'message': 'Task deleted'})
                    return
            self._send_error('Task not found', 404)
            return
        self._send_error('Not found', 404)

    def log_message(self, format, *args):
        if '/todo/api/' in args[0]:
            super().log_message(format, *args)


if __name__ == '__main__':
    port = 8086
    server = http.server.HTTPServer(('0.0.0.0', port), TodolistHandler)
    print(f'Todolist server running on port {port}...')
    print(f'API: http://localhost:{port}/todo/api/tasks')
    print(f'UI:  http://localhost:{port}/todo/')
    server.serve_forever()
