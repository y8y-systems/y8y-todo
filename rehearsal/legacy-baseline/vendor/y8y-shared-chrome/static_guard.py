"""y8y 静态服务共享守卫 —— 「哪个路径可以对外公开」的**全站唯一权威判断**。

物理源: /var/lib/todolist-legacy/shared/chrome/static_guard.py
各站经 sys.path 指向本目录后 import（与已有 serve_asset.py 同一模式）。

## 为什么有这个文件（2026-09-24）

全站 10 个应用里，**3 个把整个项目目录暴露给了 web**（dash / todo / chrome 的
`/.git/config`、`/serve.py`、`/backup/*` 都能直接下载）。根因不是某一行写错，
而是**「什么该公开」这件事没有单一来源**：
  · 4 个站自己手写了白名单（各写各的，互不相干）
  · 1 个站漏写了白名单，回落到「整个目录都是静态根」
  · 1 个站干脆用 `python -m http.server --directory <代码目录>`
新站要建的时候，没地方可查「这规矩是什么」→ 又一次靠记性。

本模块把这件事收成**一处**，并且采取**默认拒绝**：
  调用方必须显式声明自己愿意公开什么；声明之外一律不放行。
（对比「黑名单」写法 —— 那要求每次新建目录都记得去补，必然漏。）

## 使用

    import sys; sys.path.insert(0, '/var/lib/todolist-legacy/shared/chrome')
    from static_guard import resolve_public_file

    res = resolve_public_file(WEB_ROOT, self.path,
                              allow_prefixes=('/assets/',),
                              allow_files=('/index.html', '/'))
    if res is None:
        self.send_error(404); return
    fp, ct = res

## 三道闸（缺一不可）

1. **防穿越**：解析出的真实路径必须仍在 web_root 内（含符号链接解析后）。
2. **敏感清单**：即使调用方白名单写宽了，`.git/` `*.py` `backup/` 这类硬拦。
3. **默认拒绝**：不在 allow_prefixes / allow_files 里的一律不放行。
"""
import os
import posixpath
import urllib.parse
from pathlib import Path

# ── 闸 2：敏感清单（兜底，不依赖调用方写对）─────────────────────────────────
# 即使某站的白名单写成 allow_prefixes=('/',)，这些也永远不放行。
SENSITIVE_DIRS = {
    '.git', '.github', '.ssh', '.secrets', '.env',
    'backup', '_backup', '_snapshots', '_shots', '_verify',
    '__pycache__', 'node_modules', 'venv', '.venv', 'systemd',
    'uploads_private',
}
SENSITIVE_SUFFIXES = (
    '.py', '.pyc', '.pyo', '.pyd',            # 服务端源码
    '.bak', '.orig', '.swp', '.tmp',          # 编辑器/备份残留
    '.log', '.sh', '.bash', '.yml', '.yaml',  # 运维脚本与日志
    '.db', '.sqlite', '.sqlite3', '.db-wal', '.db-shm',
    '.env', '.key', '.pem', '.crt', '.p12',   # 凭据
    '.tar', '.gz', '.tgz', '.zip', '.7z',     # 归档（常含全部历史）
)
SENSITIVE_NAMES = {
    '.gitignore', '.gitattributes', '.dockerignore',
    'internal_tokens.json', 'id_rsa', 'id_ed25519',
}

# ── MIME（stdlib http.server 的表对字体不完整，这里补齐）────────────────────
CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.mjs': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.ico': 'image/x-icon',
    '.woff': 'font/woff', '.woff2': 'font/woff2',
    '.ttf': 'font/ttf', '.otf': 'font/otf',
    '.mp4': 'video/mp4', '.webm': 'video/webm',
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
    '.pdf': 'application/pdf',
    '.csv': 'text/csv; charset=utf-8',
    '.md': 'text/markdown; charset=utf-8',
}


def content_type_for(fp) -> str:
    return CONTENT_TYPES.get(os.path.splitext(str(fp))[1].lower(),
                             'application/octet-stream')


def block_reason(path: str):
    """闸 2：命中敏感清单则返回原因字符串，否则 None。

    path 应为已剥离 query/前缀的**纯路径**（形如 '/backup/x.bak'）。
    """
    # 逐段检查目录名 —— 任何一层是敏感目录就拦
    parts = [p for p in posixpath.normpath(path).split('/') if p and p != '.']
    for p in parts:
        if p in SENSITIVE_DIRS:
            return f'敏感目录: {p}/'
    if not parts:
        return None
    name = parts[-1]
    if name in SENSITIVE_NAMES:
        return f'敏感文件: {name}'
    low = name.lower()
    for suf in SENSITIVE_SUFFIXES:
        if low.endswith(suf):
            return f'敏感后缀: {suf}'
    return None


def is_public(path: str, allow_prefixes=(), allow_files=()) -> bool:
    """闸 2 + 闸 3：默认拒绝。allow_prefixes 形如 ('/assets/',)，allow_files 形如 ('/index.html',)。

    '/' 视为目录请求，由调用方决定是否映射到 index.html 后传入。
    """
    if block_reason(path):
        return False
    norm = posixpath.normpath(path)
    for f in allow_files:
        if norm == posixpath.normpath(f):
            return True
    for pre in allow_prefixes:
        if norm.startswith(posixpath.normpath(pre).rstrip('/') + '/'):
            return True
    return False


def resolve_public_file(web_root: str, request_path: str,
                        allow_prefixes=(), allow_files=(), default_index='index.html'):
    """三道闸全过，则返回 (绝对路径 Path, content_type)；否则 None。

    request_path 可以带 query（?v=N）和任意前缀（如 '/todo/index.html'）——
    调用方负责先剥掉自己的路径前缀；本函数只做 query 剥离与安全检查。

    行为：
      · 目录请求（以 / 结尾或解析后是目录）→ 拼 default_index
      · 路径穿越（..、URL 编码的 %2e%2e）→ 拒（闸 1）
      · 敏感路径 → 拒（闸 2）
      · 不在白名单 → 拒（闸 3）
    """
    raw = request_path.split('?', 1)[0].split('#', 1)[0]
    try:
        # 解码一次，让 %2e%2e 之类在闸 1 里现形
        raw = urllib.parse.unquote(raw)
    except Exception:
        return None
    # 归一化：去掉多余斜杠与 . 段（normpath 会把 .. 也折叠掉，所以之后再查 ..）
    if '..' in raw.split('/'):
        return None
    norm = posixpath.normpath(raw)
    if not norm.startswith('/'):
        norm = '/' + norm
    if norm.endswith('/') or norm == '/':
        norm = norm.rstrip('/') + '/' + default_index
        if norm.startswith('//'):
            norm = norm[1:]

    if not is_public(norm, allow_prefixes, allow_files):
        return None

    # 闸 1：拼真实路径 + 解析符号链接后必须仍在 web_root 内
    root = os.path.realpath(web_root)
    fp = os.path.realpath(os.path.join(root, norm.lstrip('/')))
    if fp != root and not fp.startswith(root + os.sep):
        return None
    if not os.path.isfile(fp):
        return None
    # 返回 pathlib.Path —— 与 serve_asset.resolve_shared_asset 的返回类型保持一致，
    # 调用方可以直接 fp.read_bytes()。（2026-09-24 曾因返回 str 导致 dash 首页 500。）
    return Path(fp), content_type_for(fp)
