"""y8y-chrome 共享品牌资产服务核心 — 唯一实现 (单一来源)。

所有 origin 用一行调用它, 处理请求 → 读取 /var/lib/todolist-legacy/shared/chrome/<name>。
BaseHTTPRequestHandler 系 (dash/todo/docs/cost/review): 用 resolve_shared_asset()
FastAPI 系 (shop/book): 用 shared_asset_file()

安全: 只放行 SHARED_CHROME_DIR 内的同名文件, 拒绝子路径/穿越。
"""
from pathlib import Path

SHARED_CHROME_DIR = Path('/var/lib/todolist-legacy/shared/chrome')   # canonical (2026-09-11); /srv/todolist-legacy-chrome 保留兼容软链

# 允许经 /assets/<subdir>/<file> 提供的共享子目录 (白名单, 防路径穿越)。
# 顶层单文件依旧全量放行; 子路径仅放行这些白名单目录 (防任意文件读取)。
# icons/ = 1-bit 像素图标库 (2026-09-12, 供 dash 等导航/设置按钮引用)。
ALLOWED_SUBDIRS = ('icons', 'fonts')   # fonts/ = 全站唯一 Zpix 字体源 (2026-09-23 字体统一)

CONTENT_TYPES = {
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.png': 'image/png',
    '.woff2': 'font/woff2',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.json': 'application/json; charset=utf-8',
}

# 共享品牌资产缓存: 1 天、非 immutable —— 否则旧版本被浏览器/CF 永久锁死, 改文件看不到
# (曾 immutable+1yr 导致改 header.css 无效 2026-08-27). 变更时记得 bump link ?v=N.
CACHE_HEADERS = {'Cache-Control': 'public, max-age=86400'}


def resolve_shared_asset(request_path: str):
    """把请求路径解析到共享文件。request_path 形如 '/assets/tokens.css'
       (也接受带前缀 '/dash/assets/tokens.css' 或 '/book/static/assets/tokens.css')。
       返回 (Path, content_type) 或 None(未命中共享资产)。"""
    marker = '/assets/'
    idx = request_path.find(marker)
    if idx < 0:
        return None
    name = request_path[idx + len(marker):]
    name = name.split('?', 1)[0].split('#', 1)[0]   # 剥缓存爆破 query/fragment
    # 拒绝空名 / 路径穿越(..) / 隐藏文件(前导点) / 以斜杠结尾
    if not name or '..' in name or name.startswith('.') or name.endswith('/'):
        return None
    # 只放行: 顶层单文件, 或白名单子目录(如 icons/)下的文件
    parts = name.split('/')
    if len(parts) > 1 and parts[0] not in ALLOWED_SUBDIRS:
        return None
    fp = SHARED_CHROME_DIR / name
    # is_file() 只吞 ENOENT/ENOTDIR; EACCES (如 ProtectHome 沙箱内 /root 不可达,
    # 2026-09-13 控制台 /assets/* 全站崩溃的根因) 会抛 PermissionError 拖垮整个
    # handler 线程。包一层: 任何 OSError 一律降级为"未命中"。
    try:
        _is_file = fp.is_file()
    except OSError:
        _is_file = False
    if not _is_file:
        return None
    ct = CONTENT_TYPES.get(fp.suffix.lower(), 'application/octet-stream')
    return fp, ct


def shared_asset_file(request_path: str):
    """FastAPI/Starlette 用: 返回 FileResponse 或 None。"""
    res = resolve_shared_asset(request_path)
    if not res:
        return None
    fp, ct = res
    from fastapi.responses import FileResponse
    return FileResponse(fp, media_type=ct, headers=dict(CACHE_HEADERS))