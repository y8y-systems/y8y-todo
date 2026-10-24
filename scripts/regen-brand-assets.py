#!/usr/bin/env python3
"""regen-brand-assets.py — 从品牌主文件生成 favicon 的 PNG 兜底。

## 为什么还有这个脚本

品牌资源只有**一份事实来源**：`vendor/y8y-shared-chrome/brand/y8y-logo.svg`
（19×19 像素网格的矢量主文件）。应用直接加载这个 SVG —— 浏览器按矢量渲染，
任何尺寸、任何 DPR 都锐利，不需要预渲染的位图。

只有 **favicon** 例外：Safari 26 之前完全不渲染 SVG favicon
（Can I Use / WebKit WWDC25），那部分用户只认 PNG。所以额外留一张
`favicon-48.png` 兜底，由本脚本从同一份主文件生成：

    python3 scripts/regen-brand-assets.py           # 重新生成（就地写回）
    python3 scripts/regen-brand-assets.py --check   # 只校验，不写；有漂移就退出 1

`--check` 是 CI 用的门（tests/test_brand_assets.py 也会调它）：
兜底 PNG 与主文件不同步 = 红。

## 纯标准库

运行时零第三方依赖（与整个项目一致）：SVG 解析用 xml.etree，
PNG 编码用 zlib + struct，**不需要** inkscape / ImageMagick / Pillow。

## 缩放规则：像素画必须「硬边」

像素画缩放只允许**最近邻**（nearest neighbour）——插值会把像素块揉成糊边，
像素画的方块感就没了。源网格 19 格、目标 48 格不是整数倍，所以这里用
**边界取整**（rounded-boundary）映射而不是朴素的 `int(x * N / S)`：

    边界 b(x) = round(x * S / N)      （源第 x 格占设备像素 [b(x), b(x+1)) )

取整误差被摊到整张图上，而不是全堆在某一侧；每个设备像素仍然只来自
**一个**源像素，绝不混色 —— 所以边缘永远是硬的，且每个源格仍是正方形。
（浏览器渲染 SVG 时用 `shape-rendering="crispEdges"`，snap 规则与此一致。）
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
import zlib
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "vendor" / "y8y-shared-chrome" / "brand"

SVG_NS = "{http://www.w3.org/2000/svg}"

# 主文件 → 兜底位图清单：(输出文件名, 边长)
# 只有 favicon 需要位图兜底（Safari 26 之前不认 SVG favicon）。
# header / README 直接用 SVG，没有衍生版。
RENDITIONS = (
    ("favicon-48.png", 48),
)

MASTER_NAME = "y8y-logo.svg"

# ── SVG 路径解析 ────────────────────────────────────────────────────────────
# 只支持像素画会用到的指令：M/m L/l H/h V/v Z/z。够用即可，不做通用渲染器。
_TOKEN_RE = re.compile(r"[MmLlHhVvZz]|-?\d*\.?\d+(?:e[-+]?\d+)?")


def _parse_path_d(d: str):
    """把 path 的 d 属性解析成若干闭合多边形（每条子路径一个点列表）。"""
    tokens = _TOKEN_RE.findall(d)
    polys: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = 0.0
    start = (0.0, 0.0)
    i = 0
    cmd = None
    while i < len(tokens):
        tok = tokens[i]
        if re.match(r"[A-Za-z]", tok):
            cmd = tok
            i += 1
            if cmd in "Zz":
                if len(cur) >= 3:
                    polys.append(cur)
                cur = []
                x, y = start
                continue
        if cmd is None:
            raise ValueError(f"path data starts without a command: {d[:40]!r}")
        rel = cmd.islower()
        c = cmd.upper()

        def num():
            nonlocal i
            v = float(tokens[i])
            i += 1
            return v

        if c == "M":
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            if len(cur) >= 3:
                polys.append(cur)
            cur = [(nx, ny)]
            x, y = nx, ny
            start = (x, y)
            cmd = "l" if rel else "L"      # 后续坐标按折线处理
        elif c == "L":
            nx, ny = num(), num()
            if rel:
                nx, ny = x + nx, y + ny
            cur.append((nx, ny))
            x, y = nx, ny
        elif c == "H":
            nx = num()
            if rel:
                nx = x + nx
            cur.append((nx, y))
            x = nx
        elif c == "V":
            ny = num()
            if rel:
                ny = y + ny
            cur.append((x, ny))
            y = ny
        else:
            raise ValueError(f"unsupported path command: {cmd}")
    if len(cur) >= 3:
        polys.append(cur)
    return polys


def _parse_color(value: str):
    """'#rgb' / '#rrggbb' / 'none' → (r, g, b, a) 或 None。"""
    if value is None:
        return None
    value = value.strip()
    if not value or value.lower() == "none":
        return None
    if value.startswith("#"):
        h = value[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            raise ValueError(f"unsupported colour: {value!r}")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
    raise ValueError(f"unsupported colour: {value!r}")


def rasterize_svg(svg_text: str):
    """把 SVG 光栅化成 N×N 的 RGBA 网格（每格一个像素）。

    规则：
      · 只画 <path>，按文档顺序叠放（后画的覆盖先画的）——与 SVG 语义一致。
      · 填充规则 even-odd，在像素中心采样 → 边缘像素归属确定，不产生半透明边。
      · 没有背景色 → 未被覆盖处全透明。
    """
    root = ElementTree.fromstring(svg_text)
    view_box = root.get("viewBox")
    if not view_box:
        raise ValueError("SVG master has no viewBox")
    parts = [float(v) for v in re.split(r"[\s,]+", view_box.strip())]
    if len(parts) != 4:
        raise ValueError(f"bad viewBox: {view_box!r}")
    _, _, n_w, n_h = parts
    if n_w != n_h or n_w != int(n_w):
        raise ValueError(f"viewBox must be a square integer grid: {view_box!r}")
    n = int(n_w)

    grid = [[(0, 0, 0, 0)] * n for _ in range(n)]

    for el in root.iter(SVG_NS + "path"):
        colour = _parse_color(el.get("fill"))
        if colour is None:
            continue
        polys = _parse_path_d(el.get("d", ""))
        for py in range(n):
            cy = py + 0.5
            for px in range(n):
                cx = px + 0.5
                if _inside(polys, cx, cy):
                    grid[py][px] = colour
    return n, grid


def _inside(polys, x: float, y: float) -> bool:
    """even-odd 射线法：水平射线向右，数穿越次数。"""
    inside = False
    for poly in polys:
        count = len(poly)
        for i in range(count):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % count]
            if (y1 > y) != (y2 > y):
                x_at = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                if x < x_at:
                    inside = not inside
    return inside


# ── 缩放（边界取整最近邻）────────────────────────────────────────────────
def _boundary_lut(n: int, size: int) -> list[int]:
    """设备像素 → 源像素 的查表（边界取整，误差均摊）。"""
    lut = []
    idx = 0
    for d in range(size):
        # b(x) = round(x * size / n)，取整用整数运算（半值向上），跨平台一致
        while idx + 1 < n and (2 * (idx + 1) * size + n) // (2 * n) <= d:
            idx += 1
        lut.append(idx)
    return lut


def scale_grid(grid, n: int, size: int):
    """把 n×n 源网格放大到 size×size（硬边最近邻）。"""
    lut = _boundary_lut(n, size)
    return [[grid[lut[y]][lut[x]] for x in range(size)] for y in range(size)]


# ── PNG 编码（stdlib）─────────────────────────────────────────────────────
def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return (struct.pack(">I", len(data)) + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))


def encode_png(grid) -> bytes:
    """RGBA / 8bit / 无隔行 的 PNG 字节流（只 IHDR+IDAT+IEND，无元数据）。"""
    size = len(grid)
    raw = bytearray()
    for row in grid:
        raw.append(0)                      # filter type 0 (None)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def decode_png(data: bytes):
    """解 PNG → (size, grid)。只支持 8bit RGBA（我们自己的输出格式）。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, width = 8, bytearray(), None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, ctype = struct.unpack(">IIBB", payload[:10])
            if depth != 8 or ctype != 6:
                raise ValueError("expected 8-bit RGBA PNG")
            if width != height:
                raise ValueError("expected a square PNG")
        elif tag == b"IDAT":
            idat += payload
        pos += 12 + length
    raw = zlib.decompress(bytes(idat))
    stride = width * 4
    out, prev, i = [], bytearray(stride), 0
    for _ in range(width):
        f = raw[i]; i += 1
        line = bytearray(raw[i:i + stride]); i += stride
        if f == 1:
            for x in range(4, stride):
                line[x] = (line[x] + line[x - 4]) & 0xFF
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif f == 3:
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif f == 4:
            for x in range(stride):
                a = line[x - 4] if x >= 4 else 0
                c = prev[x - 4] if x >= 4 else 0
                b = prev[x]
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 0xFF
        elif f != 0:
            raise ValueError(f"unsupported PNG filter {f}")
        out.append([tuple(line[x * 4:x * 4 + 4]) for x in range(width)])
        prev = line
    return width, out


# ── 主流程 ─────────────────────────────────────────────────────────────────
def build_renditions(svg_text: str):
    """返回 {文件名: PNG 字节}。"""
    n, grid = rasterize_svg(svg_text)
    out = {}
    for name, size in RENDITIONS:
        out[name] = encode_png(scale_grid(grid, n, size))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="只校验衍生版与主文件是否同步（漂移则退出 1），不写文件")
    ap.add_argument("--brand-dir", default=str(BRAND_DIR),
                    help="品牌资源目录（默认 vendor/y8y-shared-chrome/brand）")
    args = ap.parse_args(argv)

    brand_dir = Path(args.brand_dir).resolve()
    master = brand_dir / MASTER_NAME
    if not master.is_file():
        print(f"FAIL: 主文件不存在: {master.name}", file=sys.stderr)
        return 1

    built = build_renditions(master.read_text(encoding="utf-8"))
    drift = []
    for name, data in built.items():
        path = brand_dir / name
        if not path.is_file():
            drift.append(f"{name}: 文件缺失")
            continue
        current = path.read_bytes()
        if current == data:
            continue
        # 字节可能因 zlib 版本不同而不同 → 比像素，而不是比压缩流
        try:
            same_pixels = decode_png(current) == decode_png(data)
        except ValueError as exc:
            same_pixels = False
            drift.append(f"{name}: 无法解析 ({exc})")
            continue
        if not same_pixels:
            drift.append(f"{name}: 像素与主文件不一致")
        elif args.check:
            print(f"NOTE: {name}: 像素一致（压缩流字节不同，忽略）")

    if args.check:
        if drift:
            for line in drift:
                print(f"FAIL: {line}", file=sys.stderr)
            print("BRAND ASSETS: OUT OF SYNC — 跑 scripts/regen-brand-assets.py",
                  file=sys.stderr)
            return 1
        print("BRAND ASSETS: IN SYNC")
        return 0

    for name, data in built.items():
        path = brand_dir / name
        path.write_bytes(data)
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
