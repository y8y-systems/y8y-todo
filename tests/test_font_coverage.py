"""Real glyph-coverage checks for the bundled Fusion Pixel fonts.

The browser-side test (``tests/browser_font_verify.py``) can only ask the
renderer what it *thinks* it rendered. This module answers the question at
the source of truth: **the font's own cmap table**, parsed with fontTools.

What is checked
---------------
1. *App-owned* characters — every character the application itself can
   produce (static markup text plus the string literals in the app's own
   JavaScript) — must exist in the bundled fonts. This is the part the
   project controls, so it is held to 100 %.
2. *User-supplied* characters (task titles, descriptions, list and project
   names) are **not** the project's to guarantee: users type anything.
   What is guaranteed instead is stated explicitly here — the common
   Chinese repertoire (GB2312 level 1, 3755 hanzi), Latin, digits and the
   punctuation the UI uses are covered by **every** tier, so ordinary user
   text never falls back to a smooth system font.

Two characters are deliberately *not* in the fonts: U+2713 CHECK MARK and
U+2717 BALLOT X. The app converts them into the bundled pixel SVG icons
before they ever reach the DOM (see ``renderToastText`` in src/index.html),
so they are declared here as "rendered as an icon" rather than as coverage
holes. The exact set is asserted, so a *new* gap cannot slip in unnoticed.
"""
import io
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FONTS_DIR = REPO / 'vendor' / 'y8y-shared-chrome' / 'fonts'
INDEX_HTML = REPO / 'src' / 'index.html'


def _brotli_available():
    """WOFF2 is a Brotli container — decoding one needs a Brotli decoder."""
    import importlib
    for name in ('brotli', 'brotlicffi'):
        try:
            importlib.import_module(name)
            return True
        except ImportError:
            continue
    return False


if not _brotli_available():
    _msg = ('reading a WOFF2 file needs a Brotli decoder — '
            'pip install "fonttools[woff]"')
    if os.environ.get('Y8Y_REQUIRE_FONT_TOOLS') == '1':
        raise RuntimeError(_msg)
    pytest.skip(_msg, allow_module_level=True)

# fontTools is a *test-only* dependency (the app itself stays stdlib-only).
# CI installs it; locally it is optional unless Y8Y_REQUIRE_FONT_TOOLS=1.
try:
    from fontTools.ttLib import TTFont
except ImportError:                                    # pragma: no cover
    if os.environ.get('Y8Y_REQUIRE_FONT_TOOLS') == '1':
        raise
    pytest.skip('fontTools is not installed (pip install "fonttools[woff]")',
                allow_module_level=True)

# The three faces, by CSS family.
FACES = {
    'Fusion Pixel': 'fusion-pixel-12px-proportional-zh_hans.037fb51624ec.otf.woff2',
}

# Characters the app never renders as text — it swaps them for pixel icons.
RENDERED_AS_ICON = {'✓', '✗'}


def _codepoints(path):
    """Every Unicode codepoint the font's cmap maps to a glyph."""
    font = TTFont(io.BytesIO(Path(path).read_bytes()))
    try:
        points = set()
        for table in font['cmap'].tables:
            if table.isUnicode():
                points |= set(table.cmap.keys())
        return points
    finally:
        font.close()


@pytest.fixture(scope='module')
def face_codepoints():
    return {family: _codepoints(FONTS_DIR / name) for family, name in FACES.items()}


@pytest.fixture(scope='module')
def covered_union(face_codepoints):
    union = set()
    for points in face_codepoints.values():
        union |= points
    return union


def _app_owned_corpus():
    """Characters the application itself can put on screen.

    Two sources:
      * the static markup (tags stripped, script/style removed);
      * the string literals inside the app's own <script> blocks.
    """
    html = INDEX_HTML.read_text(encoding='utf-8')
    without_code = re.sub(r'(?is)<script.*?</script>', ' ', html)
    without_code = re.sub(r'(?is)<style.*?</style>', ' ', without_code)
    markup_text = re.sub(r'(?s)<[^>]+>', ' ', without_code)

    scripts = ' '.join(re.findall(r'(?is)<script[^>]*>(.*?)</script>', html))
    literals = re.findall(r"'([^'\\\n]*)'|\"([^\"\\\n]*)\"", scripts)
    literal_text = ' '.join(a or b for a, b in literals)

    corpus = set()
    for source in (markup_text, literal_text):
        for ch in source:
            if not ch.isspace():
                corpus.add(ch)
    return corpus


def _gb2312_level1():
    """The 3755 most common simplified hanzi, straight from the stdlib codec."""
    chars = []
    for hi in range(0xB0, 0xD8):
        for lo in range(0xA1, 0xFF):
            try:
                chars.append(bytes([hi, lo]).decode('gb2312'))
            except UnicodeDecodeError:
                continue
    return set(chars)


# ── app-owned text ─────────────────────────────────────────────────────

def test_app_owned_characters_are_fully_covered(covered_union):
    """Every character the app itself renders exists in the bundled fonts."""
    corpus = _app_owned_corpus()
    assert corpus, 'failed to extract the app-owned character corpus'
    missing = sorted(ch for ch in corpus
                     if ord(ch) not in covered_union and ch not in RENDERED_AS_ICON)
    assert not missing, 'app-owned characters missing from the bundled fonts: ' + ', '.join(
        f'{c!r} (U+{ord(c):04X})' for c in missing)


def test_the_only_uncovered_app_characters_are_the_icon_swaps(covered_union):
    """The "rendered as an icon" list must match reality exactly.

    If upstream ever ships a font that *does* contain ✓/✗, this test asks
    for the list to be revisited (the icons may then be redundant); if the
    app starts using another character the fonts lack, it fails loudly.
    """
    corpus = _app_owned_corpus()
    uncovered = {ch for ch in corpus if ord(ch) not in covered_union}
    assert uncovered == RENDERED_AS_ICON, (
        'the set of app characters the fonts do not cover changed: '
        f'{sorted(uncovered)} (expected {sorted(RENDERED_AS_ICON)})')


def test_icon_swapped_characters_are_really_absent_from_the_fonts(covered_union):
    """If the fonts had them, the icon swap would be pointless (but harmless)."""
    for ch in RENDERED_AS_ICON:
        assert ord(ch) not in covered_union, (
            f'{ch!r} is now covered by the fonts — the icon swap can be reviewed')


# ── user-supplied text: the guarantee we actually make ────────────────

@pytest.mark.parametrize('family', sorted(FACES))
def test_every_tier_covers_latin_digits_and_ascii(family, face_codepoints):
    points = face_codepoints[family]
    missing = [c for c in range(0x20, 0x7F) if c not in points]
    assert not missing, f'{family} misses printable ASCII: {missing}'


@pytest.mark.parametrize('family', sorted(FACES))
def test_every_tier_covers_common_chinese(family, face_codepoints):
    """Ordinary user input (task titles, project names) must not fall back.

    GB2312 level 1 is the 3755 most common simplified hanzi — the realistic
    input space of this app. Anything outside it (rare names, traditional
    characters, CJK extension blocks) may fall back to the system stack;
    that is a documented limit, not a bug.
    """
    points = face_codepoints[family]
    common = _gb2312_level1()
    missing = sorted(ch for ch in common if ord(ch) not in points)
    assert not missing, (
        f'{family} misses {len(missing)} common hanzi, e.g. '
        f'{"".join(missing[:20])} — ordinary Chinese user text would fall back '
        'to a system font')


@pytest.mark.parametrize('family', sorted(FACES))
def test_every_tier_covers_the_punctuation_the_ui_uses(family, face_codepoints):
    points = face_codepoints[family]
    needed = '，。、；：？！（）【】《》“”‘’—…·～￥'
    missing = [ch for ch in needed if ord(ch) not in points]
    assert not missing, f'{family} misses CJK punctuation: {missing}'


def test_the_app_small_labels_are_covered(covered_union):
    """The small labels ("剩2天" / "今天到期" …) must exist in the font."""
    for label in ('剩', '天', '今', '到', '期', '明', '2'):
        assert ord(label) in covered_union, f'the font misses {label!r}'


# ── the fonts are what we think they are ──────────────────────────────

def test_the_face_has_the_expected_coverage():
    """Guard against silently shipping a different file."""
    sizes = {
        'Fusion Pixel': 37689,
    }
    for family, name in FACES.items():
        points = _codepoints(FONTS_DIR / name)
        assert len(points) == sizes[family], (
            f'{family} covers {len(points)} codepoints, expected {sizes[family]} '
            '— the vendored font changed, re-check the coverage guarantees')


def test_no_font_contains_a_bold_face():
    """The faces are weight 400 only; a bold face must not be added silently.

    A real bold face would be fine, but it has to be deliberate: the
    synthetic-bold question is answered in docs/font-migration/README.md.
    """
    for family, name in FACES.items():
        font = TTFont(io.BytesIO((FONTS_DIR / name).read_bytes()))
        try:
            assert font['OS/2'].usWeightClass == 400, (
                f'{family} is not weight 400 (usWeightClass='
                f'{font["OS/2"].usWeightClass})')
            subfamilies = {str(r.toUnicode()) for r in font['name'].names
                           if r.nameID == 2}
            assert subfamilies <= {'Regular'}, (
                f'{family} declares the subfamily {subfamilies}')
        finally:
            font.close()
