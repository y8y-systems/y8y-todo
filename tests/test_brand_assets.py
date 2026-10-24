"""品牌资源一致性测试 —— 保证 logo 主文件与 favicon 兜底位图不会各走各的。

背景 (为什么需要这个测试):
  品牌资源分两层:
    · 主文件 `vendor/y8y-shared-chrome/brand/y8y-logo.svg` —— 19×19 像素网格的
      矢量主文件, **唯一事实来源**。header / README / favicon 都直接用它,
      浏览器按矢量渲染 (任何尺寸、任何 DPR 都锐利);
    · `favicon-48.png` —— **仅**给不认 SVG favicon 的老浏览器兜底
      (Safari 26 之前完全不渲染 SVG favicon —— Can I Use / WebKit WWDC25)。

  以前品牌图是 4 张各自导出的 PNG, 换 logo 时漏改一张就会出现
  「header 是新猫、favicon 还是旧猫」。现在位图只有一张, 且由
  `scripts/regen-brand-assets.py` 从主文件生成 —— 这个测试就是那道门。

检查项:
  1. favicon 兜底 PNG 与主文件**逐像素一致**
  2. 它的边长符合命名约定（48）
  3. 主文件是自包含的像素网格：不引用外部资源、形状渲染为 crispEdges
  4. 兜底 PNG 只有「纯色 + 全透明」两种状态 —— 证明缩放没有混色（硬边）
  5. 引用品牌图时带 `?v=` 内容哈希（换 logo 后浏览器缓存不残留旧图）
  6. header / README / favicon 引用的都是主文件本身
  7. 仓库里不存在指向已删除品牌文件的悬空引用，也没有多餘的位图衍生版

纯标准库（与整个项目一致）：PNG 解码用 zlib + struct，SVG 解析用 xml.etree。
"""
import hashlib
import importlib.util
import os
import re
from pathlib import Path
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "vendor/y8y-shared-chrome" / "brand"
INDEX_HTML = ROOT / "src" / "index.html"
README = ROOT / "README.md"
REGEN = ROOT / "scripts" / "regen-brand-assets.py"

MASTER_NAME = "y8y-logo.svg"

# 唯一的位图兜底：文件名 → 边长
EXPECTED_SIZES = {
    "favicon-48.png": 48,
}

# 品牌调色板（主文件里用到的 3 个颜色）
PALETTE = {
    (245, 245, 229, 255),   # #F5F5E5 米白（脸/底板）
    (0, 0, 0, 255),         # #000000 描边/字
    (255, 119, 0, 255),     # #FF7700 橙色（标志 O）
}
TRANSPARENT = (0, 0, 0, 0)

TEXT_SUFFIXES = {".md", ".html", ".py", ".sh", ".yml", ".yaml", ".txt",
                 ".css", ".js", ".json", ".ini", ".cfg", ".service", ".example"}
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".pytest-tmp", ".venv",
             "node_modules", "_verify"}


def _load_regen_module():
    """加载 scripts/regen-brand-assets.py（文件名带连字符, 只能按路径导入）。"""
    spec = importlib.util.spec_from_file_location("regen_brand_assets", REGEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def regen():
    return _load_regen_module()


@pytest.fixture(scope="module")
def master_text():
    return (BRAND_DIR / MASTER_NAME).read_text(encoding="utf-8")


def test_master_exists():
    assert (BRAND_DIR / MASTER_NAME).is_file(), "品牌主文件缺失"
    for name in EXPECTED_SIZES:
        assert (BRAND_DIR / name).is_file(), f"品牌兜底位图缺失: {name}"


def test_fallback_png_matches_master_pixel_for_pixel(regen, master_text):
    """favicon 兜底 PNG 必须与主文件逐像素一致 —— 「换 logo 不会漏改」的关键断言。"""
    built = regen.build_renditions(master_text)
    assert set(built) == set(EXPECTED_SIZES)
    for name, data in built.items():
        path = BRAND_DIR / name
        assert path.is_file(), f"{name} 不存在"
        try:
            committed = regen.decode_png(path.read_bytes())
        except ValueError as exc:
            pytest.fail(f"{name} 不是可解析的 RGBA PNG: {exc}")
        expected = regen.decode_png(data)
        assert committed[0] == expected[0], f"{name} 边长与主文件生成结果不一致"
        if committed != expected:
            diff = [(y, x) for y in range(committed[0])
                    for x in range(committed[0])
                    if committed[1][y][x] != expected[1][y][x]]
            pytest.fail(
                f"{name} 与主文件不同步：{len(diff)} 个像素不同，"
                f"首个差异在 {diff[0]}（仓库里的这张图不是当前主文件生成的）")


def test_fallback_size_matches_name(regen):
    for name, size in EXPECTED_SIZES.items():
        got, _ = regen.decode_png((BRAND_DIR / name).read_bytes())
        assert got == size, f"{name} 边长应为 {size}，实际 {got}"


def test_no_stale_bitmap_renditions():
    """位图只剩 favicon 兜底一张 —— 其余品牌图一律用矢量主文件。

    多余的 PNG 衍生版 = 又多一份可能漂移的事实来源。
    """
    pngs = sorted(p.name for p in BRAND_DIR.glob("*.png"))
    assert pngs == sorted(EXPECTED_SIZES), (
        f"品牌目录里的位图应该是 {sorted(EXPECTED_SIZES)}，实际 {pngs} —— "
        f"header/README 直接用 SVG，不该再有位图衍生版")


def test_master_is_self_contained_pixel_grid(master_text):
    """主文件必须自包含：不能引用外部图片/字体，形状渲染要 crispEdges。"""
    root = ElementTree.fromstring(master_text)
    assert root.get("shape-rendering") == "crispEdges", \
        "主文件必须声明 shape-rendering=crispEdges（像素画要硬边）"
    view_box = root.get("viewBox")
    assert view_box is not None and re.fullmatch(r"0 0 (\d+) \1", view_box), \
        f"viewBox 必须是正方形整数网格: {view_box!r}"
    n = int(view_box.split()[2])
    assert n == 19, f"主文件网格应为 19×19，实际 {n}"
    # 不允许任何外部引用（<image> / href / xlink:href）
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        assert tag != "image", "主文件不得引用外部位图"
        for attr in ("href", "{http://www.w3.org/1999/xlink}href"):
            assert el.get(attr) is None, f"主文件不得带外部引用: {attr}"


def test_fallback_has_hard_edges_only(regen):
    """每个像素要么是纯调色板色, 要么全透明 —— 不允许半透明混色。"""
    for name in EXPECTED_SIZES:
        _, grid = regen.decode_png((BRAND_DIR / name).read_bytes())
        for y, row in enumerate(grid):
            for x, px in enumerate(row):
                assert px in PALETTE or px == TRANSPARENT, (
                    f"{name} ({x},{y}) 出现非调色板颜色 {px} —— "
                    f"缩放混色了，像素画的硬边没了")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_app_references_svg_with_content_hash():
    """index.html 的 header logo 必须指向矢量主文件，并带 ?v=<内容哈希前 8 位>。

    没有 ?v= 时, 换 logo 后浏览器/边缘缓存可能继续显示旧图一天
    （共享资产走的是 max-age=86400）。URL 跟着内容变, 缓存就不可能留旧图。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r'src="assets/brand/' + re.escape(MASTER_NAME) + r'\?v=([0-9a-f]{8})"',
                  html)
    assert m, f"index.html 的 header logo 没有指向 {MASTER_NAME}（应直接用矢量主文件）"
    expected = _sha256(BRAND_DIR / MASTER_NAME)[:8]
    assert m.group(1) == expected, (
        f"{MASTER_NAME}?v={m.group(1)}，但文件内容哈希是 {expected} —— "
        f"改了图忘了更新 ?v=（旧 logo 会被缓存住）")


def test_favicon_declares_svg_primary_and_png_fallback():
    """favicon 两条声明都要在：SVG 为主, PNG 兜底（Safari 26 之前不认 SVG）。

    少了 PNG → Safari 25 及更早的用户标签页空白；
    少了 SVG → 高 DPI 屏上退回位图，白白浪费矢量主文件。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    svg = re.search(r'<link rel="icon" type="image/svg\+xml"[^>]*href="assets/brand/'
                    + re.escape(MASTER_NAME) + r'\?v=([0-9a-f]{8})"', html)
    assert svg, "favicon 缺少 SVG 声明（矢量主文件应作为首选）"
    png = re.search(r'<link rel="icon" type="image/png"[^>]*href="assets/brand/'
                    r'favicon-48\.png\?v=([0-9a-f]{8})"', html)
    assert png, "favicon 缺少 PNG 兜底声明（Safari 26 之前不渲染 SVG favicon）"
    assert svg.group(1) == _sha256(BRAND_DIR / MASTER_NAME)[:8], "SVG favicon 的 ?v= 与文件内容不符"
    assert png.group(1) == _sha256(BRAND_DIR / "favicon-48.png")[:8], "PNG favicon 的 ?v= 与文件内容不符"


def test_readme_uses_svg_master():
    readme = README.read_text(encoding="utf-8")
    assert "vendor/y8y-shared-chrome/brand/" + MASTER_NAME in readme, \
        f"README 顶部应直接引用矢量主文件 {MASTER_NAME}"


def test_no_dangling_brand_asset_references():
    """仓库里提到的每个品牌资源路径都必须真实存在（防「旧 logo 遗留」）。"""
    missing = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for name in re.findall(r'vendor/y8y-shared-chrome/brand/([A-Za-z0-9_.-]+)',
                                   text):
                if "*" in name:
                    continue
                if not (BRAND_DIR / name).exists():
                    missing.append(f"{path.relative_to(ROOT)} -> brand/{name}")
    assert not missing, "悬空的品牌资源引用:\n  " + "\n  ".join(missing)
