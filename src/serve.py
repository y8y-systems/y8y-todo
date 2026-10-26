#!/usr/bin/env python3
"""Todolist web app - self-hosted task manager.

All deployment-specific values (data directory, port, bind address, users,
timezone, public URL) come from config.py / environment variables.
See docs/deployment.md for the full reference.
"""
import hmac
import http.server
import json
import os
import re
import sys
import threading
import urllib.parse
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import Config

# Notification preferences (per user) + the channel capability table.
#
# ⚠️ The web process never holds third-party credentials (LINE token,
# Resend key): those live only in the scheduler's environment. The
# scheduler publishes a capability table (which users can use which
# channel) into the data dir; the API below just reads it.
from notifications import (
    Capabilities,
    SettingsStore as NotificationSettingsStore,
    normalize_settings,
    registered_channels,
)

# On-disk task format helpers live in their own module so that other
# processes (the notification scheduler) read/write tasks with exactly the
# same rules — one source of truth, no drift between writer and reader.
# The names below stay importable from `serve` (tests and scripts rely on
# `serve.parse_frontmatter` / `serve.build_frontmatter` / `serve.atomic_write`).
from taskstore import (
    atomic_write,
    build_frontmatter,
    display_title as _display_title,
    parse_frontmatter,
    scan_md_files as _scan_md_files,
    task_owner as _task_owner_of,
    can_access as _can_access_of,
    yaml_parse_scalar as _yaml_parse_scalar,
    yaml_scalar as _yaml_scalar,
)

# Vendored shared UI shell (icons/tokens/header CSS, theme engine) lives
# inside this repo — no deployment-specific sys.path entries.
VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'vendor', 'y8y-shared-chrome')
sys.path.insert(0, VENDOR_DIR)
from serve_asset import cache_control_for, resolve_shared_asset
from static_guard import block_reason, resolve_public_file

CONFIG = Config()
STATIC_ROOT = os.path.dirname(os.path.abspath(__file__))   # 源码树 = 静态资源根

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


# ── 持久数据家（全部来自 CONFIG.data_dir，默认在源码仓库之外）──────────────
TODOLIST_DIR = str(CONFIG.data_dir)
# 列表系统 (2026-08-18 升级定案): 购物升级为「列表」, 每个列表一个目录, 可设归属(self/shared)
LISTS_ROOT = str(CONFIG.lists_root)
# 购物兼容别名目录 = lists/shopping (旧 shopping/ 数据迁移后统一在列表系统内)
SHOPPING_DIR = str(CONFIG.shopping_dir)
INTERNAL_TOKENS_FILE = str(CONFIG.internal_tokens_file)
PERSISTENT_PROJECTS_FILE = str(CONFIG.persistent_projects_file)
# 通知: 每用户偏好 + 渠道能力表 (调度器发布, 只读)
NOTIFICATION_SETTINGS_FILE = os.path.join(TODOLIST_DIR, 'notification_settings.json')
NOTIFICATION_CHANNELS_FILE = os.path.join(TODOLIST_DIR, 'notification_channels.json')


def notification_settings_payload(user):
    """某用户的通知设置 + 该用户可用的渠道列表 (不含任何凭据)。"""
    store = NotificationSettingsStore(NOTIFICATION_SETTINGS_FILE)
    caps = Capabilities(NOTIFICATION_CHANNELS_FILE)
    available = caps.enabled()
    return {
        'settings': store.get(user),
        'channels': caps.channels_for(user),
        'available': available,
        'hint': ('通知由服务器上的定时器发送；如需开启请联系管理员。'
                 if not available else ''),
    }



# ── 安全工具 ────────────────────────────────────────────────────────────
def safe_join(root, *parts):
    """Join user-supplied path segments under root.

    Rejects absolute paths, '..' traversal and symlink escapes by resolving
    the result and verifying it stays inside root. Returns None on refusal.
    Every API endpoint that turns request data into a filesystem path goes
    through this helper.
    """
    if not parts:
        return None
    root_real = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_real, *parts))
    if candidate != root_real and not candidate.startswith(root_real + os.sep):
        return None
    return candidate


def checked_list_dir(lid):
    """Resolve a user-supplied list id to its directory, or None on refusal."""
    return safe_join(LISTS_ROOT, lid)


# ── 并发写保护 ──────────────────────────────────────────────────────────
# HTTPServer 是单线程的(请求天然串行); DATA_LOCK 覆盖所有「读-改-写」关键区，
# 防御未来的并发写入路径(后台线程/未来多线程服务器), 并保证 P2 起引入的
# 原子写语义在任何调用顺序下都成立。
DATA_LOCK = threading.RLock()


# ── 任务标题与存储标识分离 (2026-10-17) ─────────────────────────────────
# 旧方案: 文件名 = 标题(去/去空格) → 同名任务互相覆盖。
# 新方案: 文件名 = 可读 slug + 8 位随机 uid(唯一); 显示标题存 frontmatter
# `title` 字段。旧文件无 title 字段 → 回退文件名派生(零迁移兼容)。
# 控制符/行分隔符(\u2028/\u2029/DEL)也替换: 它们在某些工具链里会被
# 当作换行, 进文件名会造成跨平台隐患。
_SLUG_BAD_CHARS = re.compile(r'[\/\\:*?"<>|\x00-\x1f\x7f\u2028\u2029]')
_SLUG_MAX_LEN = 60


def _task_slug(title):
    """从标题生成安全可读的文件名片段。返回 '' 表示标题无可用字符。"""
    slug = _SLUG_BAD_CHARS.sub('_', title).strip()
    slug = slug.strip('.')      # 绝不产生 "." / ".." 这类文件名
    if len(slug) > _SLUG_MAX_LEN:
        slug = slug[:_SLUG_MAX_LEN].rstrip()
    return slug


def _unique_task_path(directory, title):
    """在 directory 下为 title 找一个不冲突的任务文件路径。

    同名任务各自获得独立文件(uid 区分), 已有的任何文件都不会被覆盖。
    """
    while True:
        slug = _task_slug(title)
        uid = uuid.uuid4().hex[:8]
        fname = f'{slug}-{uid}.md' if slug else f'{uid}.md'
        fpath = os.path.join(directory, fname)
        if not os.path.exists(fpath):
            return fpath


def _clean_project_name(name):
    """项目名会成为目录名: 拒绝换行/控制符与 . / .. (返回 None → 调用方 400)。

    标题不落文件路径所以没这层限制; 项目名是路径组件, 带换行的目录名
    会污染数据家并与各种工具链冲突。('/' 嵌套沿用旧行为, 历史数据可读。)"""
    if not name:
        return name
    if _SLUG_BAD_CHARS.search(name) or name != name.strip() or name in ('.', '..'):
        return None
    return name


def _resolve_move_target(target_path, fm):
    """无损移动: 目标路径已被占用时, 生成 uid 变体而不是覆盖。

    完成/恢复/项目移动三类操作都可能把文件搬到一个已存在同名文件的
    位置 —— 旧版(文件名=标题)数据在同名完成、跨日期恢复、同项目迁移
    时必然撞车, 直接 atomic_write 会静默吃掉目标文件。

    冲突时: 新文件名 = <原名>-<uid8>.md, 并把当前显示标题钉进 frontmatter
    title(旧文件原标题来自文件名, 改名后必须写进去才不会丢显示)。
    返回 (最终路径, 可能更新后的 fm)。调用方持 DATA_LOCK, 检查+写入原子。
    """
    if not os.path.exists(target_path):
        return target_path, fm
    directory = os.path.dirname(target_path)
    stem = os.path.basename(target_path)
    if stem.endswith('.md'):
        stem = stem[:-3]
    while True:
        candidate = os.path.join(directory, f'{stem}-{uuid.uuid4().hex[:8]}.md')
        if not os.path.exists(candidate):
            break
    fm = dict(fm)
    fm['title'] = _display_title(fm, target_path)   # 钉住显示标题, 改名不丢标题
    return candidate, fm


# ── 机器间认证 (X-Internal-Token header → 用户) ─────────────────────────
# token 文件: <data_dir>/internal_tokens.json  {"user": "<hex>"}
# 供同机/同内网的其他服务调用 (如 y8ysystems 拉取今日任务)。
def _load_internal_tokens():
    try:
        with open(INTERNAL_TOKENS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


INTERNAL_TOKENS = _load_internal_tokens()


# 反向代理认证邮箱 → 用户身份
# ─────────────────────────────────────────────────────────────────────────
# 身份 header 只在请求来自受信代理 (Y8Y_TRUSTED_PROXIES) 时才被采信 ——
# 公网直连携带伪造 header 一律视为未认证 (见 _current_user)。
# ─────────────────────────────────────────────────────────────────────────
AUTH_USERS = CONFIG.users   # {email: user_id}

# 任务/列表归属: 自己的 + shared 可见; 私人内容互不可见。
# (历史遗留说明: 旧数据里 owner=legacy-agent / legacy-bridge 相关的文件保留在盘上,
#  但配置中不再存在可匹配它们的用户 → 对所有人不可见 (软隐藏),
#  由部署者显式决定如何处置, 服务端永不自动删除数据。)


def _task_owner(fm):
    """任务归属；无 owner 字段 = 第一个配置用户（旧数据零迁移兼容）"""
    return _task_owner_of(fm, CONFIG.default_owner)


def _user_can_access(owner, user):
    """用户能看/改的任务：自己的 + 共享的。私人任务互不可见。"""
    return _can_access_of(owner, user)


def _list_visible(list_owner, user):
    """列表可见性: 只能看自己或 shared 的列表。"""
    return list_owner in (user, 'shared')


def _can_access_shopping(owner, user):
    """购物条目访问控制 = 所属列表的可见性。"""
    return _list_visible(owner, user)

VALID_COLORS = ('yellow', 'green', 'pink', 'purple', 'orange', 'red', 'blue', 'cyan')

# ── recurring_dates 格式 (O3-N2 修复) ──────────────────────────────────
# 受支持的条目格式 (与旧版完成推进逻辑语义完全一致, 零数据迁移):
#   "D"      月内第 D 天 (1-31), 锚定当前 due 所在月份 (当年→次年同月)
#   "MM-DD"  每年 MM 月 DD 日 (合法月历日; 02-29 合法, 02-30 非法)
# JSON API 传入 int 等价于其十进制字符串; 磁盘标量 (recurring: 03-01)
# 按单元素列表处理 (旧数据兼容)。
# 旧实现零验证: 非法条目 ('' / 'abc' / '02-30' / '1-2-3' / 嵌套结构) 会被
# 接受, 完成时 int()/date() 抛 ValueError 被 except 静默吞掉 → 任务卡在
# recurring/ 里 status=done, 列表/默认日历不可见 (O3 复现)。现在:
#   POST 写入前验证, 非法 → 400; 完成时推不出下一期 → 400 且不落盘,
#   任务保持未完成、始终可见 (失败响亮, 绝不静默)。
RECURRING_DAY_MIN, RECURRING_DAY_MAX = 1, 31


class RecurringRuleError(Exception):
    """recurring 规则无法为当前 due 推出下一期 (数据错误, 必须响亮失败)。"""


def _recurring_rule_date(rd, year, due_month):
    """单条规则 → year 年的具体日期; 构造成日期时抛 RecurringRuleError。"""
    parts = str(rd).strip().split('-')
    try:
        if len(parts) == 1:
            return date(year, due_month, int(parts[0]))
        if len(parts) == 2:
            return date(year, int(parts[0]), int(parts[1]))
    except ValueError:
        raise RecurringRuleError(
            f'recurring 规则 {rd!r} 在 {year} 年不是合法日期')
    raise RecurringRuleError(f'recurring 规则 {rd!r} 格式不受支持')


def _valid_recurring_entry(entry):
    """写入前格式校验: 'D' (1-31) 或 'MM-DD' (合法月历日)。int 等价十进制串。"""
    if isinstance(entry, bool):
        return False
    if isinstance(entry, int):
        entry = str(entry)
    if not isinstance(entry, str):
        return False
    parts = entry.strip().split('-')
    try:
        if len(parts) == 1:
            day = int(parts[0])
            return RECURRING_DAY_MIN <= day <= RECURRING_DAY_MAX
        if len(parts) == 2:
            # 2000 是闰年 → 02-29 合法, 02-30 / 13-01 非法
            date(2000, int(parts[0]), int(parts[1]))
            return True
    except ValueError:
        return False
    return False


def normalize_recurring_dates(value):
    """API 入参 → (cleaned_list, None) 或 (None, 错误消息)。

    合法输入统一为字符串列表 (去重保序); 单字符串标量按旧数据语义包装成
    单元素列表; 其余一切 (空列表 / 非列表 / 非法条目) 一律报错。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        return None, ('recurring_dates 必须是非空列表: 每项为月内第几天 '
                      '("1"-"31") 或每年日期 ("MM-DD")')
    cleaned = []
    for entry in value:
        if not _valid_recurring_entry(entry):
            return None, (f'invalid recurring_dates entry: {entry!r} — '
                          '每项必须是 "1"-"31" (月内第几天) 或 "MM-DD" (每年日期)')
        text = str(entry).strip()
        if text not in cleaned:
            cleaned.append(text)
    return cleaned, None


def _next_recurring_date(recurring_dates, old_due_date):
    """旧版推进语义完整保留: 'D' 锚定 due 所在月, 'MM-DD' 为每年日期;
    先找当年晚于 due 的最近日期, 没有则取次年同一批规则的最近日期。
    任何规则无法构造成日期 → RecurringRuleError (调用方必须 400 响亮失败,
    不得把任务写成 status=done 卡在 recurring/ 里)。"""
    current_year = old_due_date.year
    next_date = None
    for rd in recurring_dates:
        rd_date = _recurring_rule_date(rd, current_year, old_due_date.month)
        if rd_date > old_due_date and (next_date is None or rd_date < next_date):
            next_date = rd_date
    if next_date is None:
        for rd in recurring_dates:
            rd_date = _recurring_rule_date(rd, current_year + 1, old_due_date.month)
            if next_date is None or rd_date < next_date:
                next_date = rd_date
    return next_date

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
    with DATA_LOCK:
        data = _load_persistent_projects()
        data[user] = clean
        atomic_write(PERSISTENT_PROJECTS_FILE, json.dumps(data, ensure_ascii=False, indent=2))
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
            atomic_write(fpath, build_frontmatter(fm) + '\n\n' + body + '\n')


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

    2026-08-18 升级：购物从旧 shopping/ 目录迁移到 lists/shopping/
    （旧购物条目 owner 恒 shared，迁移时补 meta.json；若旧文件里没 owner 也保持 shared）。
    检查-建目录-迁移-写 meta 全程持锁, 避免与并发的列表写操作交错。
    """
    new_dir = _list_dir('shopping')
    with DATA_LOCK:
        os.makedirs(new_dir, exist_ok=True)
        if not os.path.isfile(os.path.join(new_dir, 'meta.json')):
            # 迁移旧 shopping/*.md（若存在）
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
                                atomic_write(dst, raw)
                            except Exception:
                                pass
            meta = {'name': '购物', 'owner': 'shared', 'created': date.today().isoformat()}
            atomic_write(os.path.join(new_dir, 'meta.json'),
                         json.dumps(meta, ensure_ascii=False))
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
                    'title': _display_title(fm, fname),
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
                    'title': _display_title(fm, fname),
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




class TodolistHandler(http.server.BaseHTTPRequestHandler):

    def _current_user(self):
        """Resolve the request's user identity, or None when unauthenticated.

        Three authentication paths, in order:

        1. Dev mode (Y8Y_DEV_MODE=1): single local user, no credentials
           needed. The server binds to loopback in this mode, so it is
           only reachable from the machine it runs on.

        2. X-Internal-Token: machine-to-machine credential shared with
           trusted services (e.g. y8ysystems pulling today's tasks).
           Accepted from any source — the token itself is the secret.

        3. Identity header (default Cf-Access-Authenticated-User-Email):
           set by an authenticating reverse proxy (Cloudflare Access, or
           any proxy that injects the header after verifying the user).
           Only trusted when the request arrives from an address in
           Y8Y_TRUSTED_PROXIES — a public client connecting directly and
           forging the header is treated as unauthenticated.
        """
        if CONFIG.dev_mode:
            return CONFIG.dev_user

        token = (self.headers.get('X-Internal-Token') or '').strip()
        if token:
            # INTERNAL_TOKENS 存储方向: {user: token}，反向查找
            for user, tok in INTERNAL_TOKENS.items():
                if tok and hmac.compare_digest(tok, token):
                    return user

        client_ip = self.client_address[0] if self.client_address else ''
        if client_ip in CONFIG.trusted_proxies:
            # 身份头必须恰好出现一次。get() 只取第一个值 — 若代理误用
            # "追加"语义 (Caddy 的 header_up +X) 把客户端伪造值放在前面,
            # 采信第一个值就等于采信伪造身份 (实测确认)。同值重复尚可
            # 宽容, 冲突值一律拒绝 — 正确代理只发一个值。
            values = self.headers.get_all(CONFIG.auth_header) or []
            if len(values) > 1:
                uniq = {v.strip().lower() for v in values if v.strip()}
                if len(uniq) > 1:
                    return None
            email = (values[0] if values else '').strip().lower()
            if email in AUTH_USERS:
                return AUTH_USERS[email]

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

        # 共享 y8y-chrome 品牌资产 (header.css/tokens.css/theme.js/y8y-logo.svg) → /srv 单源
        _shared = resolve_shared_asset(self.path)
        if _shared:
            _fp, _ct = _shared
            with open(_fp, 'rb') as _f:
                _data = _f.read()
            self.send_response(200)
            self.send_header('Content-Type', _ct)
            # 内容寻址的字体（文件名带内容哈希）→ immutable 长缓存；
            # 其余共享资产 1 天。
            self.send_header('Cache-Control', cache_control_for(_fp))
            self.send_header('Content-Length', str(len(_data)))
            self.end_headers()
            self.wfile.write(_data)
            return

        # Google Calendar 集成已于 2026-10 移除 (用户决定不再使用)。
        # 历史数据 (google_calendar/ 目录、task_uid / gcal_event_id 字段)
        # 原样保留在磁盘上; 这里对所有遗留路径给出明确的 410 Gone,
        # 永不发起任何 Google 网络请求, 也不读取/输出任何旧凭据内容。
        if path.startswith('/todo/api/google-calendar/'):
            self._send_json({
                'error': 'Google Calendar integration has been removed',
                'detail': '本版本已移除 Google Calendar 同步; 历史任务数据'
                          '(含 task_uid/gcal_event_id)未受影响。',
            }, 410)
            return

        # API: 列表系统 — 列所有可见列表
        if path == '/todo/api/lists':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            ensure_shopping_list()   # 预置购物列表始终可用（迁移旧数据 + 补 meta.json）
            lists = []
            for lid, meta in list_all_meta().items():
                lo = meta.get('owner', CONFIG.default_owner)
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

        # API: 列表条目
        m_item = re.match(r'^/todo/api/lists/([^/]+)/items$', path)
        if m_item:
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            lid = urllib.parse.unquote(m_item.group(1))
            meta = list_meta(lid) if checked_list_dir(lid) else None
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', CONFIG.default_owner)
            if not _list_visible(lo, user):
                self._send_error('Forbidden', 403)
                return
            items = [t for t in list_items(lid) if _can_access_shopping(t['owner'], user)]
            self._send_json({'list': {'id': lid, 'name': meta.get('name', lid), 'owner': lo},
                             'items': items, 'current_user': user})
            return

        # API: 购物列表 (lists/shopping 兼容别名)
        if path == '/todo/api/shopping':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            ensure_shopping_list()
            items = [t for t in list_shopping() if _can_access_shopping(t['owner'], user)]
            self._send_json({'items': items, 'current_user': user})
            return

        # API: persistent-projects (跨设备同步)
        if path == '/todo/api/persistent-projects':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            self._send_json({'projects': get_persistent_projects(user)})
            return

        # API: 通知设置 (每用户偏好; 凭据不在 Web 进程里)
        if path == '/todo/api/notification-settings':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            self._send_json(notification_settings_payload(user))
            return

        # API: list tasks
        if path == '/todo/api/tasks':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            tasks = [t for t in list_tasks() if _user_can_access(t['owner'], user)]
            completed_tasks = [t for t in list_completed_tasks() if _user_can_access(t['owner'], user)]
            sections = classify_tasks(tasks)
            self._send_json({
                'current_user': user,
                'tasks': tasks,
                'completed_tasks': completed_tasks,
                'sections': {k: [t['id'] for t in v] for k, v in sections.items()},
                # O3-P1: 每用户显示偏好默认值 (中国节假日)。前端可在本机覆盖;
                # 旧实现把节假日硬编码给单一用户名, 已移除。
                'preferences': {'cn_holidays': CONFIG.cn_holidays_enabled_for(user)},
            })
            return

        # API: get single task
        m = re.match(r'^/todo/api/tasks/(.+)', path)
        if m:
            fname = urllib.parse.unquote(m.group(1))
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            for folder in ('todo', 'recurring'):
                fpath = safe_join(TODOLIST_DIR, folder, fname)
                if fpath and os.path.isfile(fpath):
                    with open(fpath, 'r', encoding='utf-8') as f:
                        raw = f.read()
                    fm, body = parse_frontmatter(raw)
                    owner = _task_owner(fm)
                    if not _user_can_access(owner, user):
                        self._send_error('Forbidden', 403)
                        return
                    self._send_json({
                        'id': fname,
                        'title': _display_title(fm, fname),
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
            # 内容寻址的字体（文件名带哈希）→ immutable 长缓存；
            # 其余静态文件保持 no-store（HTML 必须每次都是最新的）。
            cache_control = (cache_control_for(file_path)
                             if str(file_path).lower().endswith('.woff2')
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
            self.send_header('Cache-Control', cache_control_for(_fp))
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
            # 与 do_GET 一致：内容寻址字体长缓存，其余 no-store
            cache_control = (cache_control_for(full_path)
                             if full_path.lower().endswith('.woff2')
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

        # Google Calendar 集成已移除: 与 do_GET 一致, 先于鉴权给出 410。
        if path.startswith('/todo/api/google-calendar/'):
            self._send_json({
                'error': 'Google Calendar integration has been removed',
                'detail': '本版本已移除 Google Calendar 同步; 历史任务数据'
                          '(含 task_uid/gcal_event_id)未受影响。',
            }, 410)
            return

        user = self._current_user()
        if user is None:
            self._send_error('Unauthorized', 401)
            return

        # 列表系统: 新建列表 (每个列表可设归属 self/shared)
        if path == '/todo/api/lists':
            name = body_data.get('name', '').strip()
            if not name:
                self._send_error('List name is required')
                return
            d = checked_list_dir(name)
            if d is None:
                self._send_error('Invalid list name', 400)
                return
            # owner: self(默认)->当前用户, 或 shared
            owner = body_data.get('owner', 'self')
            if owner == 'shared':
                owner = 'shared'
            else:
                owner = user
            if os.path.isdir(d):
                self._send_error('List with that name already exists', 409)
                return
            os.makedirs(d, exist_ok=True)
            meta = {'name': name, 'owner': owner, 'created': date.today().isoformat()}
            atomic_write(os.path.join(d, 'meta.json'),
                         json.dumps(meta, ensure_ascii=False))
            self._send_json({'id': name, 'name': name, 'owner': owner, 'created': meta['created']}, 201)
            return

        # 列表系统: 某列表加条目
        m_item = re.match(r'^/todo/api/lists/([^/]+)/items$', path)
        if m_item:
            lid = urllib.parse.unquote(m_item.group(1))
            list_dir = checked_list_dir(lid)
            meta = list_meta(lid) if list_dir else None
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', CONFIG.default_owner)
            if not _list_visible(lo, user):
                self._send_error('Forbidden', 403)
                return
            dedupe = body_data.get('dedupe', False)
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
                            it_path = safe_join(list_dir, it['id'])
                            if it_path:
                                try:
                                    with open(it_path, 'r', encoding='utf-8') as f:
                                        raw = f.read()
                                    fm, body = parse_frontmatter(raw)
                                    fm['status'] = 'todo'
                                    atomic_write(it_path, build_frontmatter(fm) + '\n\n' + body)
                                except Exception:
                                    pass
                        self._send_json({'duplicate': True, 'existing_id': it['id'], 'message': '已在清单中'}, 200)
                        return
            fm = {'status': 'todo', 'created': date.today().isoformat(), 'owner': lo}
            frontmatter = build_frontmatter(fm)
            fpath = os.path.join(list_dir, fname)
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
            if project and _clean_project_name(project) is None:
                self._send_error('Invalid project name', 400)
                return
            # recurring_dates 写入前格式验证 (O3-N2): 非法规则 400, 不落盘。
            # 旧实现零验证 — 非法规则会在完成推进时静默卡死任务 (见 recurring 逻辑注释)。
            recurring_dates = None
            if recurring:
                recurring_dates, rec_err = normalize_recurring_dates(
                    body_data.get('recurring_dates'))
                if rec_err:
                    self._send_error(rec_err, 400)
                    return
            # owner: default = current user; only allow self or shared
            owner = body_data.get('owner', user)
            if owner == 'self':
                owner = user
            if owner not in (user, 'shared'):
                self._send_error('Forbidden: cannot create task for another user', 403)
                return

            folder = 'recurring' if recurring else 'todo'

            # 显示标题进 frontmatter; 文件名 = slug + uid(唯一, 同名任务不再互相覆盖)
            fm = {'title': title, 'status': 'todo', 'color': color,
                  'created': date.today().isoformat(), 'owner': owner}
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
                fm['recurring'] = recurring_dates

            frontmatter = build_frontmatter(fm)
            content_full = frontmatter + '\n\n' + content + '\n'

            # 项目文件夹方案 (2026-08-13): 有 project 的一次性任务存到 todo/<project>/ 子目录
            with DATA_LOCK:
                if project and not recurring:
                    proj_dir = safe_join(TODOLIST_DIR, 'todo', project)
                    if proj_dir is None:
                        self._send_error('Invalid project name', 400)
                        return
                    os.makedirs(proj_dir, exist_ok=True)
                    fpath = _unique_task_path(proj_dir, title)
                    task_id = f'{project}/{os.path.basename(fpath)}'
                else:
                    task_dir = safe_join(TODOLIST_DIR, folder)
                    if task_dir is None:
                        self._send_error('Invalid task path', 400)
                        return
                    # 全新数据目录下 todo/ recurring/ 可能尚不存在 —— 先建目录再写。
                    os.makedirs(task_dir, exist_ok=True)
                    fpath = _unique_task_path(task_dir, title)
                    task_id = os.path.basename(fpath)
                with open(fpath, 'w', encoding='utf-8') as f:
                    f.write(content_full)

            self._send_json({'id': task_id, 'message': 'Task created'}, 201)
            return

        # 购物列表: 新增物品 (购物 = 列表系统内 shopping 列表, 归属可变)
        if path == '/todo/api/shopping':
            ensure_shopping_list()
            # owner 由列表归属决定（lists/shopping meta.json）；默认 shared 保持旧行为
            owner = (list_meta('shopping') or {}).get('owner', 'shared')
            # 查重由调用方显式请求（Web UI 默认不查重，允许同名条目手动区分）
            dedupe = body_data.get('dedupe', False)
            title = body_data.get('title', '').strip()
            if not title:
                self._send_error('Item title is required')
                return
            # 条目 = <uuid>.md，标题/内容 = 文件正文 (2026-08-20: 长文本无上限)
            # 购物别名与列表系统一致: 文件名固定 uuid, title 直接读 body 内容
            fname = f'{uuid.uuid4()}.md'
            # 查重：同 title 的条目已存在 → 不重复添加；
            # 若已购(done)则恢复为待购(todo)，让同一物品永远只有一条
            if dedupe:
                for it in list_shopping():
                    if it['title'] == title and _can_access_shopping(it['owner'], user):
                        if it['status'] == 'done':
                            it_path = safe_join(SHOPPING_DIR, it['id'])
                            if it_path:
                                try:
                                    with open(it_path, 'r', encoding='utf-8') as f:
                                        raw = f.read()
                                    fm, body = parse_frontmatter(raw)
                                    fm['status'] = 'todo'
                                    atomic_write(it_path, build_frontmatter(fm) + '\n\n' + body)
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
                fpath = safe_join(TODOLIST_DIR, folder, fname)
                if fpath and os.path.isfile(fpath):
                    # 读-改-写全程持锁 (DATA_LOCK)
                    with DATA_LOCK:
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
                                except ValueError:
                                    # due 本身不是 YYYY-MM-DD — 无法推进, 响亮失败
                                    # (旧行为: 同样不推进, 但会把任务标记成 done 卡住)
                                    self._send_error(
                                        f'Invalid due date {old_due!r} on recurring task '
                                        f'— 任务保持未完成, 请先修正 due', 400)
                                    return
                                try:
                                    next_date = _next_recurring_date(recurring_dates, old_due_date)
                                except RecurringRuleError as e:
                                    # O3-N2: 规则推不出下一期 (如 2 月里的 "31"、
                                    # 手写坏的旧数据) — 400 且**不落盘**: 任务保持
                                    # 未完成、始终可见。绝不像旧版那样静默吞掉
                                    # ValueError, 把任务写成 status=done 留在
                                    # recurring/ 里 (界面不可见, 数据却在)。
                                    self._send_error(
                                        f'Recurring rule cannot advance: {e} — '
                                        f'任务保持未完成, 请先修正 recurring 规则', 400)
                                    return
                                if next_date:
                                    fm['due'] = next_date.isoformat()
                                    fm['status'] = 'todo'
                                    del fm['completed']

                            atomic_write(fpath, build_frontmatter(fm) + '\n\n' + body + '\n')
                            self._send_json({'message': 'Recurring task updated', 'next_due': fm.get('due', '')})
                        else:
                            # Move to done/ (保留项目子目录: done/日期/项目/文件名.md)
                            today_str = date.today().isoformat()
                            done_dir = os.path.join(TODOLIST_DIR, 'done', today_str)
                            done_path = safe_join(done_dir, fname)
                            if done_path is None:
                                self._send_error('Invalid task path', 400)
                                return
                            # 无损冲突处理: 目标已存在(同日同名归档) → uid 变体, 绝不覆盖
                            done_path, fm = _resolve_move_target(done_path, fm)
                            os.makedirs(os.path.dirname(done_path), exist_ok=True)
                            atomic_write(done_path, build_frontmatter(fm) + '\n\n' + body + '\n')
                            os.remove(fpath)
                            # ID 用**逻辑路径**拼接: safe_join 返回 realpath, 数据目录
                            # 带符号链接时(/tmp→/private/tmp 等) relpath 会算出
                            # '../../..' 这种畸形 id (2026-10-17 P3 修复)。
                            final_rel = os.path.join(os.path.dirname(fname),
                                                     os.path.basename(done_path))
                            archived_id = 'done/' + today_str + '/' + final_rel
                            self._send_json({'message': 'Task completed', 'id': archived_id})
                    return
            self._send_error('Task not found', 404)
            return

        self._send_error('Not found', 404)

    def do_PUT(self):
        path = self._parse_path()
        body_data = self._read_body()
        user = self._current_user()
        if user is None:
            self._send_error('Unauthorized', 401)
            return

        # API: 通知设置 (每用户偏好, 覆盖保存)
        if path == '/todo/api/notification-settings':
            user = self._current_user()
            if user is None:
                self._send_error('Unauthorized', 401)
                return
            settings, err = normalize_settings(body_data,
                                              known_channels=registered_channels())
            if err:
                self._send_error(err, 400)
                return
            store = NotificationSettingsStore(NOTIFICATION_SETTINGS_FILE)
            store.set(user, settings)
            payload = notification_settings_payload(user)
            payload['settings'] = settings
            self._send_json(payload)
            return

        # 列表系统: 修改列表归属或条目
        # 列表归属更改: PUT /todo/api/lists/<id>  {owner?}
        m_listmeta = re.match(r'^/todo/api/lists/([^/]+)$', path)
        if m_listmeta:
            lid = urllib.parse.unquote(m_listmeta.group(1))
            list_dir = checked_list_dir(lid)
            meta = list_meta(lid) if list_dir else None
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', CONFIG.default_owner)
            # 列表所有者本人才能改归属
            if lo != 'shared' and lo != user:
                self._send_error('Forbidden', 403)
                return
            if 'owner' in body_data:
                ov = body_data['owner']
                if ov == 'shared':
                    meta['owner'] = 'shared'
                elif ov in ('self', user):
                    meta['owner'] = user
                else:
                    self._send_error('Forbidden: cannot reassign list', 403)
                    return
                # 列表归属变化 → 所有条目 owner 同步更新
                for it in list_items(lid):
                    it_path = safe_join(list_dir, it['id'])
                    if not it_path:
                        continue
                    try:
                        with open(it_path, 'r', encoding='utf-8') as f:
                            raw = f.read()
                        ifm, ibody = parse_frontmatter(raw)
                        ifm['owner'] = meta['owner']
                        atomic_write(it_path, build_frontmatter(ifm) + '\n\n' + ibody + '\n')
                    except Exception:
                        pass
            atomic_write(os.path.join(list_dir, 'meta.json'),
                         json.dumps(meta, ensure_ascii=False))
            self._send_json({'id': lid, 'name': meta.get('name', lid), 'owner': meta['owner']})
            return

        # 列表条目更新: PUT /todo/api/lists/<id>/items/<item>  {status?|title?}
        m_listitem = re.match(r'^/todo/api/lists/([^/]+)/items/(.+)', path)
        if m_listitem:
            lid = urllib.parse.unquote(m_listitem.group(1))
            fname = urllib.parse.unquote(m_listitem.group(2))
            list_dir = checked_list_dir(lid)
            meta = list_meta(lid) if list_dir else None
            if meta is None:
                self._send_error('List not found', 404)
                return
            if not _list_visible(meta.get('owner', CONFIG.default_owner), user):
                self._send_error('Forbidden', 403)
                return
            fpath = safe_join(list_dir, fname)
            if not fpath or not os.path.isfile(fpath):
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
            atomic_write(fpath, build_frontmatter(fm) + '\n\n' + body)
            self._send_json({'id': fname, 'message': 'Item updated'})
            return

        # 购物列表兼容别名: PUT /todo/api/shopping/<item> = lists/shopping 条目
        m_shop = re.match(r'^/todo/api/shopping/(.+)', path)
        if m_shop:
            ensure_shopping_list()
            fname = urllib.parse.unquote(m_shop.group(1))
            fpath = safe_join(SHOPPING_DIR, fname)
            if not fpath or not os.path.isfile(fpath):
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
            atomic_write(fpath, build_frontmatter(fm) + '\n\n' + body)
            self._send_json({'id': fname, 'message': 'Item updated'})
            return

        # API: 持久项目整体覆盖 (跨设备同步)
        if path == '/todo/api/persistent-projects':
            projects = body_data.get('projects', [])
            saved = save_persistent_projects(user, projects)
            self._send_json({'projects': saved})
            return

        m = re.match(r'^/todo/api/tasks/(.+)', path)
        if m:
            fname = urllib.parse.unquote(m.group(1))
            # 已完成任务 (done/日期/.../文件.md): 支持编辑 + status=todo 恢复
            if fname.startswith('done/'):
                rel = fname[len('done/'):]  # <date>/[project/]<fname.md>
                fpath = safe_join(TODOLIST_DIR, 'done', rel)
                if not fpath or not os.path.isfile(fpath):
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
                    if proj:
                        tdir = safe_join(TODOLIST_DIR, 'todo', proj)
                    else:
                        tdir = safe_join(TODOLIST_DIR, 'todo')
                    if tdir is None:
                        self._send_error('Invalid project name', 400)
                        return
                    # 读-改-写全程持锁; 目标已存在(如两个同名归档先后恢复) → uid 变体
                    with DATA_LOCK:
                        tpath = safe_join(tdir, fname_only)
                        if tpath is None:
                            self._send_error('Invalid task path', 400)
                            return
                        tpath, fm = _resolve_move_target(tpath, fm)
                        os.makedirs(tdir, exist_ok=True)
                        atomic_write(tpath, build_frontmatter(fm) + '\n\n' + body + '\n')
                        os.remove(fpath)
                    # ID 用逻辑路径拼 (safe_join 返回 realpath, 符号链接数据目录下
                    # relpath 会畸形, 见 done 分支的同源修复)。
                    restored_id = (proj + '/' if proj else '') + os.path.basename(tpath)
                    self._send_json({'id': restored_id, 'message': 'Task restored', 'status': 'todo'})
                    return
                # 否则原地编辑已完成任务
                if 'owner' in body_data:
                    new_owner = body_data['owner']
                    if new_owner == 'self':
                        new_owner = user
                    if new_owner not in (user, 'shared'):
                        self._send_error('Forbidden: cannot reassign task to another user', 403)
                        return
                    fm['owner'] = new_owner
                if 'title' in body_data:
                    new_title = body_data['title'].strip()
                    if new_title:
                        fm['title'] = new_title
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
                        if _clean_project_name(v) is None:
                            self._send_error('Invalid project name', 400)
                            return
                        fm['project'] = v
                    else:
                        fm.pop('project', None)
                if 'content' in body_data:
                    body = body_data['content']
                atomic_write(fpath, build_frontmatter(fm) + '\n\n' + body + '\n')
                self._send_json({'id': fname, 'message': 'Task updated'})
                return
            for folder in ('todo', 'recurring'):
                fpath = safe_join(TODOLIST_DIR, folder, fname)
                if fpath and os.path.isfile(fpath):
                    # 读-改-写全程持锁: DATA_LOCK 保证编辑不丢更新。
                    with DATA_LOCK:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            raw = f.read()
                        fm, body = parse_frontmatter(raw)
                        if not _user_can_access(_task_owner(fm), user):
                            self._send_error('Forbidden', 403)
                            return

                        # Update owner: only allow self or shared
                        if 'owner' in body_data:
                            new_owner = body_data['owner']
                            if new_owner == 'self':
                                new_owner = user
                            if new_owner not in (user, 'shared'):
                                self._send_error('Forbidden: cannot reassign task to another user', 403)
                                return
                            fm['owner'] = new_owner

                        # 标题变更 (2026-10-17): 只更新 frontmatter title, **不重命名文件**。
                        # 文件名与显示标题已分离 —— 重命名会让任务 ID 变化、打断前端编辑态,
                        # 且对旧任务(文件名即标题)会丢失标题。文件保持原名, ID 稳定,
                        # gcal_event_id / task_uid 等关联全部保留。
                        if 'title' in body_data:
                            new_title = body_data['title'].strip()
                            if new_title:
                                fm['title'] = new_title

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
                                if _clean_project_name(v) is None:
                                    self._send_error('Invalid project name', 400)
                                    return
                                fm['project'] = v
                            else:
                                fm.pop('project', None)
                        if 'content' in body_data:
                            body = body_data['content']
                        # 项目文件夹方案 (2026-08-13): 仅 project 变化才移动文件
                        # (一次性任务在 todo/ 平铺与 todo/<project>/ 之间迁移)
                        if folder == 'todo':
                            if fm.get('project'):
                                target_dir = safe_join(TODOLIST_DIR, 'todo', fm['project'])
                            else:
                                target_dir = safe_join(TODOLIST_DIR, 'todo')
                        else:
                            target_dir = safe_join(TODOLIST_DIR, folder)
                        if target_dir is None:
                            self._send_error('Invalid project name', 400)
                            return
                        target_fpath = safe_join(target_dir, os.path.basename(fname))
                        if target_fpath is None:
                            self._send_error('Invalid task path', 400)
                            return
                        if target_fpath != fpath:
                            # 无损冲突: 目标项目目录已有同名文件(旧数据) → uid 变体, 不覆盖
                            target_fpath, fm = _resolve_move_target(target_fpath, fm)
                            os.makedirs(target_dir, exist_ok=True)
                            atomic_write(target_fpath, build_frontmatter(fm) + '\n\n' + body + '\n')
                            os.remove(fpath)
                            # ID 用逻辑路径拼 (realpath vs 逻辑路径的符号链接分歧, 同上)
                            new_id = (fm.get('project', '') + '/' if folder == 'todo' and fm.get('project') else '') + os.path.basename(target_fpath)
                            self._send_json({'id': new_id, 'message': 'Task updated'})
                            return
                        atomic_write(fpath, build_frontmatter(fm) + '\n\n' + body + '\n')
                    self._send_json({'id': fname, 'message': 'Task updated'})
                    return
            self._send_error('Task not found', 404)
            return

        self._send_error('Not found', 404)

    def do_DELETE(self):
        path = self._parse_path()
        user = self._current_user()
        if user is None:
            self._send_error('Unauthorized', 401)
            return

        # 列表系统: 删除整个列表
        m_listdel = re.match(r'^/todo/api/lists/([^/]+)$', path)
        if m_listdel:
            lid = urllib.parse.unquote(m_listdel.group(1))
            list_dir = checked_list_dir(lid)
            meta = list_meta(lid) if list_dir else None
            if meta is None:
                self._send_error('List not found', 404)
                return
            lo = meta.get('owner', CONFIG.default_owner)
            # 列表所有者本人可删
            if lo != 'shared' and lo != user:
                self._send_error('Forbidden', 403)
                return
            import shutil
            shutil.rmtree(list_dir, ignore_errors=True)
            self._send_json({'message': 'List deleted'})
            return

        # 列表系统: 删除某列表条目
        m_listitem = re.match(r'^/todo/api/lists/([^/]+)/items/(.+)', path)
        if m_listitem:
            lid = urllib.parse.unquote(m_listitem.group(1))
            fname = urllib.parse.unquote(m_listitem.group(2))
            list_dir = checked_list_dir(lid)
            meta = list_meta(lid) if list_dir else None
            if meta is None:
                self._send_error('List not found', 404)
                return
            if not _list_visible(meta.get('owner', CONFIG.default_owner), user):
                self._send_error('Forbidden', 403)
                return
            fpath = safe_join(list_dir, fname)
            if not fpath or not os.path.isfile(fpath):
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

        # 购物列表: 删除物品 (兼容别名 = lists/shopping 条目)
        m_shop = re.match(r'^/todo/api/shopping/(.+)', path)
        if m_shop:
            ensure_shopping_list()
            fname = urllib.parse.unquote(m_shop.group(1))
            fpath = safe_join(SHOPPING_DIR, fname)
            if not fpath or not os.path.isfile(fpath):
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
                fpath = safe_join(TODOLIST_DIR, folder, fname)
                if fpath and os.path.isfile(fpath):
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


def _cleanup_stale_tmp_files():
    """删除上一次进程崩溃遗留的 .tmp 文件 (atomic_write 的中间产物)。

    崩溃时 tmp 可能残留; 它们不是任务数据(列表只扫 .md), 但会污染
    数据目录。启动时清理一次即可, 正常写入会原子替换。
    """
    if not os.path.isdir(TODOLIST_DIR):
        return
    for root, _dirs, files in os.walk(TODOLIST_DIR):
        for fn in files:
            if fn.endswith('.tmp'):
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass


def main():
    problems = CONFIG.validate()
    if problems:
        for p in problems:
            print(f'[startup error] {p}', flush=True)
        raise SystemExit(2)
    _cleanup_stale_tmp_files()
    print(f'Todolist server starting: {CONFIG.summary()}', flush=True)
    server = http.server.HTTPServer((CONFIG.bind, CONFIG.port), TodolistHandler)
    print(f'API: http://{CONFIG.bind}:{CONFIG.port}/todo/api/tasks', flush=True)
    print(f'UI:  http://{CONFIG.bind}:{CONFIG.port}/todo/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.', flush=True)


if __name__ == '__main__':
    main()
