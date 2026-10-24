"""Font migration regression tests (Fusion Pixel, 2026-10-22).

Guard rails for the Zpix → Fusion Pixel migration:

  * the vendored font files are present, intact (WOFF2 magic + SHA256) and
    still covered by their OFL-1.1 license text;
  * every font file name carries **its own content hash**, so the server may
    cache it immutably without any user being able to keep rendering an
    outdated font;
  * every stylesheet the app loads is versioned by the **content hash** of
    that stylesheet, so an edited CSS file can never be shadowed by a stale
    browser cache;
  * the license/attribution documents describe the font correctly and do
    **not** claim MIT for it;
  * the running code declares the @font-face rules and the font-family
    variables, and no longer references Zpix;
  * the static route serves the fonts with the right MIME type and the right
    cache policy, while path traversal through the new fonts/ directory
    stays blocked.

Stdlib only — the project's core has no third-party dependencies, and these
tests run in the same CI job as everything else. (The *glyph coverage* check
needs fontTools and lives in tests/test_font_coverage.py.)
"""
import hashlib
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / 'vendor' / 'y8y-shared-chrome'
FONTS_DIR = VENDOR / 'fonts'
TOKENS_CSS = VENDOR / 'tokens.css'
INDEX_HTML = REPO / 'src' / 'index.html'
SERVE_PY = REPO / 'src' / 'serve.py'
THIRD_PARTY = REPO / 'THIRD_PARTY_LICENSES.md'
NOTICE = REPO / 'NOTICE'

# The file names carry the sha256 prefix of their own content — see
# fonts/SOURCES.txt for the "how to swap the font" procedure.
FONT_FILES = [
    'fusion-pixel-12px-proportional-zh_hans.037fb51624ec.otf.woff2',
]

# CSS family name -> file it must point at
FONT_FAMILIES = {
    'Fusion Pixel': 'fusion-pixel-12px-proportional-zh_hans.037fb51624ec.otf.woff2',
}

# A content-addressed font file name: <base>.<sha256[:12]>.otf.woff2
CONTENT_HASHED_NAME = re.compile(r'^[a-z0-9._\-]+\.([0-9a-f]{12})\.otf\.woff2$')

# Stylesheets the app pulls in, and where they live.
STYLESHEETS = {
    'tokens.css': TOKENS_CSS,
    'header.css': VENDOR / 'header.css',
    'icons.css': VENDOR / 'icons.css',
    'ui.css': VENDOR / 'ui.css',
}


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _recorded_checksums():
    """SHA256 values recorded in fonts/SOURCES.txt (the provenance record).

    The file lists one block per font: the file name on its own line, then
    indented metadata including the ``SHA256 : <hex>`` line.
    """
    out = {}
    current = None
    for line in (FONTS_DIR / 'SOURCES.txt').read_text(encoding='utf-8').splitlines():
        stripped = line.strip()
        if stripped.endswith('.woff2') and ' ' not in stripped:
            current = stripped
            continue
        m = re.match(r'SHA256\s*:\s*([0-9a-f]{64})', stripped)
        if m and current:
            out[current] = m.group(1)
            current = None
    return out


# ── vendored assets ────────────────────────────────────────────────────

@pytest.mark.parametrize('name', FONT_FILES)
def test_font_file_present_and_is_woff2(name):
    path = FONTS_DIR / name
    assert path.is_file(), f'missing font file: {path}'
    with open(path, 'rb') as f:
        head = f.read(4)
    assert head == b'wOF2', f'{name} is not a WOFF2 file (magic={head!r})'
    assert path.stat().st_size > 100_000, f'{name} looks truncated'


@pytest.mark.parametrize('name', FONT_FILES)
def test_font_checksum_matches_recorded_source(name):
    recorded = _recorded_checksums()
    assert name in recorded, f'{name} has no SHA256 recorded in fonts/SOURCES.txt'
    actual = _sha256(FONTS_DIR / name)
    assert actual == recorded[name], (
        f'{name} checksum drift: file={actual} SOURCES.txt={recorded[name]}')


@pytest.mark.parametrize('name', FONT_FILES)
def test_font_file_name_encodes_its_own_content_hash(name):
    """The name must carry the content hash.

    This is what makes ``immutable`` caching safe: the URL changes whenever
    the bytes change, so no browser can keep serving an outdated font for a
    year. Without it, swapping the file in place would leave every cached
    copy stale — the exact failure this naming scheme exists to prevent.
    """
    m = CONTENT_HASHED_NAME.match(name)
    assert m, f'{name} does not follow the <name>.<sha256[:12]>.otf.woff2 scheme'
    actual = _sha256(FONTS_DIR / name)[:12]
    assert m.group(1) == actual, (
        f'{name} claims hash {m.group(1)} but its content hashes to {actual} — '
        'rename the file to match its content (see fonts/SOURCES.txt)')


def test_no_unversioned_font_file_is_shipped():
    """Every shipped font file must be content-addressed — and there is only one."""
    shipped = sorted(p.name for p in FONTS_DIR.glob('*.woff2'))
    assert shipped == sorted(FONT_FILES), (
        f'unexpected font files in {FONTS_DIR}: {shipped}')


def test_only_one_font_file_is_shipped():
    """The whole UI uses a single face: 12px proportional zh-Hans.

    Adding the monospaced or the 10px variant back would double the
    payload for no gain: the digits are tabular in the proportional face
    too (fontTools: advance width 600/1000 em for 0-9, same as the
    monospaced face), so the calendar and the date fields stay aligned.
    """
    shipped = [p for p in FONTS_DIR.glob('*.woff2')]
    assert len(shipped) == 1, (
        f'expected exactly one webfont, found {[p.name for p in shipped]}')


def test_ofl_license_text_ships_with_the_font():
    ofl = FONTS_DIR / 'OFL.txt'
    assert ofl.is_file(), 'OFL.txt must ship next to the font files'
    text = ofl.read_text(encoding='utf-8')
    assert 'SIL OPEN FONT LICENSE Version 1.1' in text
    assert 'Fusion Pixel Font' in text, 'OFL.txt must carry the font copyright line'
    # The upstream component fonts' licenses travel along too.
    for sub in ('ark-pixel', 'cubic-11', 'galmuri'):
        assert (FONTS_DIR / 'LICENSES' / sub).is_dir(), f'missing LICENSES/{sub}'


def test_sources_file_records_provenance():
    text = (FONTS_DIR / 'SOURCES.txt').read_text(encoding='utf-8')
    assert 'github.com/TakWolf/fusion-pixel-font' in text
    assert '2026.10.16' in text
    assert '未经任何修改' in text or 'No modifications' in text.lower() \
        or '未经任何修改、转换或子集化' in text


def test_sources_file_documents_the_rename_procedure():
    """A future maintainer must be able to swap the font without guessing."""
    text = (FONTS_DIR / 'SOURCES.txt').read_text(encoding='utf-8')
    assert 'sha256' in text, 'SOURCES.txt must explain the content-addressed naming'
    assert 'tokens.css' in text, 'SOURCES.txt must point at the @font-face definitions'


# ── licensing documents ────────────────────────────────────────────────

def test_third_party_licenses_document_the_font():
    text = THIRD_PARTY.read_text(encoding='utf-8')
    assert 'Fusion Pixel' in text
    assert 'SIL Open Font License' in text or 'OFL' in text
    assert 'TakWolf' in text
    for name in FONT_FILES:
        assert name in text, f'{name} must be listed in THIRD_PARTY_LICENSES.md'


def test_font_is_not_claimed_as_mit():
    """The application stays MIT; the font must not be declared MIT."""
    text = THIRD_PARTY.read_text(encoding='utf-8')
    font_section = text.split('## Pixelart icons')[0]
    assert 'Open Font License' in font_section, \
        'the font section must state the font license'
    assert 'MIT License' not in font_section, \
        'the font section of THIRD_PARTY_LICENSES.md must not claim MIT for the font'
    assert 'not covered by' in font_section or 'does not become MIT' in font_section, \
        'the font section must state that MIT does not cover the font'
    notice = NOTICE.read_text(encoding='utf-8')
    assert 'Fusion Pixel' in notice
    assert 'OFL' in notice or 'Open Font License' in notice


def test_notice_still_points_to_zpix_removal_reason():
    text = THIRD_PARTY.read_text(encoding='utf-8')
    assert 'zpix.woff2' in text, \
        'the reason Zpix was removed must stay documented'


# ── CSS / runtime wiring ───────────────────────────────────────────────

def test_font_face_rules_are_declared():
    css = TOKENS_CSS.read_text(encoding='utf-8')
    for family, filename in FONT_FAMILIES.items():
        assert f"font-family: '{family}'" in css, f'missing @font-face for {family}'
        assert filename in css, f'{family} does not point at {filename}'
        # the src() must be relative to tokens.css -> /assets/fonts/...
        assert f'url("fonts/{filename}")' in css


def test_every_font_url_in_css_is_content_addressed():
    """No @font-face may point at a file whose name is not content-addressed."""
    css = TOKENS_CSS.read_text(encoding='utf-8')
    urls = re.findall(r'url\(["\']?fonts/([^"\')]+)["\']?\)', css)
    assert urls, 'no font url found in tokens.css'
    for name in urls:
        assert CONTENT_HASHED_NAME.match(name), (
            f'font url {name} is not content-addressed — a cached copy would '
            'outlive the file it was downloaded from')


def test_font_family_variables_are_defined_and_used():
    css = TOKENS_CSS.read_text(encoding='utf-8')
    for var in ('--font-ui',):
        assert f'{var}:' in css, f'{var} is not defined in tokens.css'
    html = INDEX_HTML.read_text(encoding='utf-8')
    assert 'var(--font-ui)' in html, 'the app must use the shared font variable'
    assert "font-family: 'Zpix'" not in html
    assert "font-family: 'Zpix'" not in (VENDOR / 'header.css').read_text(encoding='utf-8')


def test_only_one_font_family_is_declared():
    """One face, one variable.

    The monospaced and 10px variants were dropped (the digits are tabular
    in the proportional face too, so nothing needs a second file). A
    second @font-face would double the payload for no gain.
    """
    css = TOKENS_CSS.read_text(encoding='utf-8')
    families = re.findall(r"@font-face\s*{[^}]*?font-family:\s*'([^']+)'", css, re.S)
    assert families == ['Fusion Pixel'], f'unexpected @font-face families: {families}'
    for gone in ('--font-mono', '--font-ui-small'):
        assert gone not in css, f'{gone} should be gone — there is only one face now'
    html = INDEX_HTML.read_text(encoding='utf-8')
    for gone in ('--font-mono', '--font-ui-small'):
        assert gone not in html, f'{gone} should be gone — there is only one face now'


def test_no_runtime_zpix_reference():
    """Zpix may still be *mentioned* in comments (migration history),
    but no declaration may ask the browser for it."""
    for path in (INDEX_HTML, TOKENS_CSS, VENDOR / 'header.css',
                 VENDOR / 'ui.css', VENDOR / 'icons.css'):
        text = path.read_text(encoding='utf-8')
        assert "font-family:'Zpix'" not in text.replace(' ', '')
        assert "font-family: 'Zpix'" not in text


def test_font_preload_points_at_an_existing_file():
    html = INDEX_HTML.read_text(encoding='utf-8')
    m = re.search(r'<link rel="preload" href="([^"]+\.woff2)" as="font"', html)
    assert m, 'the main webfont should be preloaded'
    name = m.group(1).rsplit('/', 1)[-1]
    assert (FONTS_DIR / name).is_file(), f'preload target missing: {name}'


def test_preload_targets_the_default_face():
    """The preloaded file must be the one the default body text uses.

    Preloading a file the page does not use would waste a 680 KB download
    and, worse, could hide a mismatch between preload and @font-face.
    """
    html = INDEX_HTML.read_text(encoding='utf-8')
    m = re.search(r'<link rel="preload" href="[^"]*/([^"/]+\.woff2)" as="font"', html)
    assert m, 'the main webfont should be preloaded'
    assert m.group(1) == FONT_FAMILIES['Fusion Pixel'], (
        f'preload targets {m.group(1)} but the default face is '
        f'{FONT_FAMILIES["Fusion Pixel"]}')


# ── cache busting ──────────────────────────────────────────────────────

@pytest.mark.parametrize('name,path', sorted(STYLESHEETS.items()))
def test_stylesheet_version_query_matches_content_hash(name, path):
    """``?v=`` must be the content hash of the file it points at.

    A hand-written version number silently goes stale the day somebody
    edits the CSS without bumping it; deriving it from the content makes
    that impossible.
    """
    html = INDEX_HTML.read_text(encoding='utf-8')
    m = re.search(r'href="assets/' + re.escape(name) + r'\?v=([0-9a-f]+)"', html)
    assert m, f'assets/{name} is not referenced with a ?v= version'
    expected = _sha256(path)[:8]
    assert m.group(1) == expected, (
        f'assets/{name}?v={m.group(1)} but the file content hashes to {expected} — '
        'the browser would keep serving the stale stylesheet')


def test_every_referenced_stylesheet_is_versioned():
    """No stylesheet may be loaded without a version query."""
    html = INDEX_HTML.read_text(encoding='utf-8')
    refs = re.findall(r'<link rel="stylesheet" href="assets/([^"?]+)(?:\?v=([^"]*))?"', html)
    assert refs, 'no stylesheet link found'
    for name, version in refs:
        assert version, f'assets/{name} is loaded without a ?v= version query'
        assert re.fullmatch(r'[0-9a-f]{8}', version), (
            f'assets/{name}?v={version} is not a content hash')


# ── static serving ─────────────────────────────────────────────────────

def test_fonts_directory_is_allowlisted_with_woff2_mime():
    sys_path = str(VENDOR)
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import serve_asset
    assert 'fonts' in serve_asset.ALLOWED_SUBDIRS, \
        'the fonts/ directory must be explicitly allowlisted'
    assert serve_asset.CONTENT_TYPES.get('.woff2') == 'font/woff2'


def test_content_addressed_font_gets_the_immutable_cache():
    sys_path = str(VENDOR)
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import serve_asset
    name = FONT_FILES[0]
    assert 'immutable' in serve_asset.cache_control_for(FONTS_DIR / name)
    assert 'max-age=31536000' in serve_asset.cache_control_for(FONTS_DIR / name)


def test_font_without_content_hash_does_not_get_immutable_cache():
    """A stable-name font must NOT be cached for a year.

    Its bytes could change under a URL that browsers have already cached
    for a year, which is exactly the "users keep the old font forever"
    trap. Such a file falls back to the ordinary 1-day policy.
    """
    sys_path = str(VENDOR)
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import serve_asset
    plain = serve_asset.cache_control_for(FONTS_DIR / 'fusion-pixel-12px-proportional.otf.woff2')
    assert 'immutable' not in plain, (
        'a font file name without a content hash must not be cached immutably')
    assert 'max-age=86400' in plain


def test_serve_py_uses_the_font_cache_policy():
    text = SERVE_PY.read_text(encoding='utf-8')
    assert 'cache_control_for' in text, \
        'serve.py must use the shared cache policy for fonts'


@pytest.mark.parametrize('name', FONT_FILES)
def test_font_is_served_with_correct_type_and_cache(server, name):
    _serve, _httpd, _client, _tmp = server
    base = _base_of(server)
    req = urllib.request.Request(base + '/todo/assets/fonts/' + name)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=10) as resp:
        body = resp.read()
        ctype = resp.headers.get('Content-Type')
        cache = resp.headers.get('Cache-Control')
    assert resp.status == 200
    assert ctype == 'font/woff2', f'wrong MIME type: {ctype}'
    assert 'immutable' in (cache or ''), f'fonts must be cached hard: {cache}'
    assert body[:4] == b'wOF2'
    assert len(body) == (FONTS_DIR / name).stat().st_size


def test_font_route_blocks_path_traversal(server):
    _serve, _httpd, _client, _tmp = server
    base = _base_of(server)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ('/todo/assets/fonts/../../../src/serve.py',
                 '/todo/assets/fonts/..%2f..%2fserve.py',
                 '/todo/assets/fonts/%2e%2e%2f%2e%2e%2fserve.py'):
        try:
            with opener.open(urllib.request.Request(base + path), timeout=10) as resp:
                body = resp.read()
                assert resp.status != 200 or b'DefaultHTTPRequestHandler' not in body, \
                    f'traversal leaked through {path}'
        except urllib.error.HTTPError as e:
            assert e.code == 404, f'{path} -> {e.code}'


def test_tokens_css_is_served_and_declares_the_faces(server):
    _serve, _httpd, _client, _tmp = server
    base = _base_of(server)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(base + '/todo/assets/tokens.css'),
                     timeout=10) as resp:
        css = resp.read().decode('utf-8')
    assert "@font-face" in css
    for family in FONT_FAMILIES:
        assert family in css


def test_assets_are_served_ignoring_the_version_query(server):
    """``?v=`` must not change which file is served (it is a cache key)."""
    _serve, _httpd, _client, _tmp = server
    base = _base_of(server)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    plain = opener.open(urllib.request.Request(
        base + '/todo/assets/tokens.css'), timeout=10).read()
    versioned = opener.open(urllib.request.Request(
        base + '/todo/assets/tokens.css?v=1da8bc7f'), timeout=10).read()
    assert plain == versioned


def _base_of(server):
    """The base URL of the running test server (fixture tuple)."""
    _serve, httpd, _client, _tmp = server
    host, port = httpd.server_address[0], httpd.server_address[1]
    return f'http://{host}:{port}'
