"""y8y-chrome shared brand assets — local serving.

This module serves the vendored shared UI shell that lives inside this
repository (vendor/y8y-shared-chrome/). URLs of the form
'/<prefix>/assets/<name>' resolve to files in that directory.

Security: only same-directory files are exposed; sub-paths are restricted
to an explicit allowlist (icons/, brand/ and fonts/). Path traversal, hidden
files and directory requests are rejected.
"""
import re
from pathlib import Path

# The directory this file lives in IS the shared chrome root. Keeping the
# resolution relative to __file__ means the repo works from any checkout
# location — no deployment-specific paths.
SHARED_CHROME_DIR = Path(__file__).resolve().parent

# Allowed sub-directories exposed under /assets/<subdir>/<file>.
#   icons/ = UI icons (Pixelarticons SVGs, MIT — see PIXELARTICONS-LICENSE.txt)
#   brand/ = in-house logo/favicon assets (SVG master + PNG renditions)
#   fonts/ = Fusion Pixel webfonts (SIL OFL 1.1 — see fonts/OFL.txt)
ALLOWED_SUBDIRS = ('icons', 'brand', 'fonts')

CONTENT_TYPES = {
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.png': 'image/png',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.svg': 'image/svg+xml',
    '.json': 'application/json; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8',
    '.woff2': 'font/woff2',
}

# Shared brand assets: 1 day, not immutable — otherwise a stale version
# gets locked in browsers/edge caches and file changes are invisible.
CACHE_HEADERS = {'Cache-Control': 'public, max-age=86400'}

# Fonts are immutable **only when the file name is content-addressed**:
# `<name>.<sha256[:12]>.otf.woff2`. Then "same URL ⇒ same bytes" holds for
# good, so the browser may keep it for a year — and a future font update
# lands under a *new* URL, so no user can keep rendering the old file.
#
# A font whose name carries no content hash would break that promise (the
# bytes behind a stable URL would change while the cache still holds the
# old ones), so it falls back to the ordinary 1-day policy instead.
FONT_SUFFIXES = ('.woff2',)
FONT_HASH_RE = re.compile(r'\.[0-9a-f]{12}\.otf\.woff2$', re.IGNORECASE)
FONT_CACHE_CONTROL = 'public, max-age=31536000, immutable'


def is_content_addressed_font(fp) -> bool:
    """True when the font file name carries its own content hash."""
    return str(fp).lower().endswith(FONT_SUFFIXES) and bool(FONT_HASH_RE.search(str(fp)))


def cache_control_for(fp) -> str:
    """Cache-Control for a shared asset.

    Content-addressed fonts get an immutable one-year cache (the name
    changes whenever the bytes do, so a stale copy is impossible).
    Everything else keeps the 1-day policy of CACHE_HEADERS — a stale
    CSS/JS must not be locked in by an edge cache.
    """
    if is_content_addressed_font(fp):
        return FONT_CACHE_CONTROL
    return CACHE_HEADERS['Cache-Control']


def resolve_shared_asset(request_path: str):
    """Resolve a request path to a vendored shared file.

    request_path looks like '/assets/tokens.css' (a '/todo/assets/...'
    or '/book/static/assets/...' prefix is also accepted).
    Returns (Path, content_type) or None when no shared asset matches.
    """
    marker = '/assets/'
    idx = request_path.find(marker)
    if idx < 0:
        return None
    name = request_path[idx + len(marker):]
    name = name.split('?', 1)[0].split('#', 1)[0]   # strip cache-buster/fragment
    # Reject empty names, traversal (..), hidden files (leading dot),
    # and directory requests (trailing slash).
    if not name or '..' in name or name.startswith('.') or name.endswith('/'):
        return None
    # Only allow top-level files or files inside allowlisted sub-directories.
    parts = name.split('/')
    if len(parts) > 1 and parts[0] not in ALLOWED_SUBDIRS:
        return None
    fp = SHARED_CHROME_DIR / name
    # is_file() only swallows ENOENT/ENOTDIR; other OSErrors (e.g. EACCES)
    # would take down the handler thread, so degrade to "no match".
    try:
        _is_file = fp.is_file()
    except OSError:
        _is_file = False
    if not _is_file:
        return None
    ct = CONTENT_TYPES.get(fp.suffix.lower(), 'application/octet-stream')
    return fp, ct
