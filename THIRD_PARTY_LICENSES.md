# Third-Party Assets and Licenses

## Pixel font — Fusion Pixel Font — `vendor/y8y-shared-chrome/fonts/`

The UI font shipped in this repository is **Fusion Pixel Font** (缝合像素字体)
by TakWolf, obtained from the official release
[2026.10.16](https://github.com/TakWolf/fusion-pixel-font/releases/tag/2026.10.16)
and licensed under the **SIL Open Font License, Version 1.1 (OFL-1.1)**.

Upstream: <https://github.com/TakWolf/fusion-pixel-font>
Copyright (c) 2022, TakWolf (<https://takwolf.com>).

The full license text ships **with the font files**, as OFL requires:

- `vendor/y8y-shared-chrome/fonts/OFL.txt` — the OFL-1.1 text for this font
- `vendor/y8y-shared-chrome/fonts/LICENSES/` — the upstream component fonts'
  own licenses (Ark Pixel Font, Cubic 11, Galmuri), kept for completeness
- `vendor/y8y-shared-chrome/fonts/SOURCES.txt` — exact version, download
  URLs and SHA256 checksums of every shipped file

Files shipped (the `zh-Hans` regional glyph variant, OpenType-flavored WOFF2):

| File | Family / role | SHA256 |
| --- | --- | --- |
| `fusion-pixel-12px-proportional-zh_hans.037fb51624ec.otf.woff2` | Fusion Pixel 12px Prop zh-Hans — the single webfont the whole UI uses | `037fb51624ec39500a6be28f7eac4aa6a8c9366205b2344dd2b4f182879519ce` |

**No modifications, conversions or subsetting were applied.** The file is
byte-for-byte the one from the upstream release, so the upstream font names
are kept and OFL-1.1 §3's Reserved Font Name ("Fusion Pixel Font") is
untouched. *If the font is ever subsetted or modified, it must be
renamed to something that does not contain the Reserved Font Name, and the
change must be recorded in `fonts/SOURCES.txt`.*

The OFL permits use, study, modification and redistribution, including
embedding in applications and commercial use, provided the font is not sold
on its own and the license and copyright notice travel with it — which they
do here. The application itself remains MIT-licensed; the font does not
become MIT-licensed and is not covered by this project's MIT license.

### Why one file, one size, one width mode

Upstream offers every "size × width mode" in seven regional glyph variants
(`latin`, `zh-Hans`, `zh-Hant`, `zh-HK`, `zh-TW`, `ja`, `ko`). Verified with
fontTools, all seven share an **identical character map** (37,689 characters
at 12 px) covering Latin, Simplified and Traditional Chinese,
Japanese kana and kanji, and Hangul — and identical Latin/digit advance
widths. The only difference between variants is the glyph *shape* of some
shared Han characters. The `zh-Hans` variant alone therefore renders Chinese,
English and Japanese completely; the other six would add ~670 KB each per
face for regional Han-character shapes that this Chinese-primary UI does not
need.

The same reasoning applies to the *size* and *width mode*: upstream also
ships a 12 px monospaced face and a 10 px face, but fontTools confirms the
proportional and monospaced 12 px faces have **identical Han advance widths
and identical digit advance widths** (0-9 are 600/1000 em in both, i.e.
tabular digits). The only areas that needed strict alignment — the calendar
day numbers and the date/time fields — therefore stay aligned on the
proportional face, and a second ~650 KB file would buy nothing. The 10 px
face existed to give the 10/11 px small labels a crisp 1:1 grid; dropping it
means those labels are rendered by the 12 px face scaled down (slightly
softer) in exchange for 61 % less font payload and two fewer requests.

## Pixelart icons — `vendor/y8y-shared-chrome/icons/ui/*.svg`

The UI icons shipped in this repository are derived from
[Pixelarticons](https://github.com/halfmage/pixelarticons) v2.4.2,
Copyright (c) 2019 Gerrit Halfmann, licensed under the **MIT License**.

The full upstream license text is preserved at
`vendor/y8y-shared-chrome/icons/PIXELARTICONS-LICENSE.txt`.

Mapping used by this project (upstream name → vendored name):

| Vendored (`icons/ui/<name>.svg`) | Upstream (`svg/<name>.svg`) |
| --- | --- |
| add.svg | plus.svg |
| list.svg | bulletlist.svg |
| calendar.svg | calendar.svg |
| cart.svg | shopping-cart.svg |
| reload.svg | reload.svg |
| settings.svg | settings-cog.svg |
| bookmark.svg | bookmark.svg |
| expand.svg | chevron-down.svg |
| pager.svg | chevron-right.svg |
| check.svg | check.svg |
| trash.svg | trash.svg |
| edit.svg | pencil.svg |
| copy.svg | copy.svg |
| close.svg | close.svg |
| sun.svg | sun.svg |
| moon.svg | moon.svg |
| recurring.svg | repeat.svg |
| menu.svg | menu.svg |
| eye.svg | eye.svg |

To refresh the icons, download the files listed above from
`https://github.com/halfmage/pixelarticons` (branch `master`) into
`vendor/y8y-shared-chrome/icons/ui/` and keep the license file in place.

## Official brand logo — `vendor/y8y-shared-chrome/brand/y8y-logo.svg`

The official y8y logo (pixel-cat mark holding a TODO sign) is an in-house
brand asset owned by the project. It is **not** MIT-licensed: it ships under
the separate y8y Brand Asset License —
see [`vendor/y8y-shared-chrome/brand/LICENSE`](vendor/y8y-shared-chrome/brand/LICENSE)
and [`BRANDING.md`](BRANDING.md) (repository root) for the usage rules.

`y8y-logo.svg` is the owner's original export, shipped byte-identical: a
19×19 pixel grid (`shape-rendering="crispEdges"`). **It is the only
artwork** — the app header, the README and the favicon all load that
vector file directly, and the browser renders it as vector art, so it stays
sharp at any size and on any device-pixel ratio (a pre-rendered bitmap
would have to be re-rasterized per size and would blur on high-DPI
screens).

The single bitmap in that directory, `favicon-48.png`, exists only as a
**fallback for browsers that do not render SVG favicons** — Safari before
version 26 ignores them entirely (Can I Use / WebKit WWDC25), and those
users would otherwise see a blank tab. It is generated from the master by
`scripts/regen-brand-assets.py` (standard library only — no
ImageMagick / Pillow / Inkscape needed), which rasterizes the master and
rescales it with a **rounded-boundary nearest-neighbour** filter: every
output pixel comes from exactly one source pixel, so the pixel artwork
keeps hard edges and every source cell stays square.
`scripts/regen-brand-assets.py --check` (and `tests/test_brand_assets.py`)
fail the build if the committed fallback drifts away from the master.

A former in-house text-mark logo SVG (previously MIT-licensed) was retired
when the official logo was adopted and is not part of this release.

## Not redistributed (deliberate omissions)

The legacy deployment previously bundled these assets, which are **not**
redistributed here because their redistribution license could not be
confirmed:

- `zpix.woff2` — pixel font, no LICENSE/OFL file available. It was removed
  during the open-source preparation pass (2026-10) and the UI then fell back
  to the system monospace stack. As of 2026-10-22 the UI uses **Fusion Pixel**
  instead (OFL-1.1, see above) — Zpix is no longer referenced anywhere in the
  running code.
- The previous in-house icon PNGs — replaced by the MIT-licensed
  Pixelarticons SVGs.
