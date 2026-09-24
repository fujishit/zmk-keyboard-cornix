#!/usr/bin/env python3
"""Render ``config/cornix.keymap`` into a single self-contained cheat sheet.

    python3 scripts/cheatsheet.py            # -> config/cheatsheet.html
    python3 scripts/cheatsheet.py -o /tmp/sheet.html

The output is one HTML file with no external resources at all: the CSS is
inline, every key diagram is an inline ``<svg>`` drawn from the *real* key
positions of ``layout_50`` (``boards/jzf/cornix/cornix-layouts.dtsi``), so the
column stagger, the two centre keys and the rotated thumb cluster come out the
way the board actually is.  It works at phone width and on a desktop and
follows ``prefers-color-scheme``.

The keymap itself is not parsed here: ``scripts/remap.py`` already owns that
(``remap.parse_text()``, ``remap._parts()``), and this script only turns its
bindings into human labels.  The footer carries the SHA-256 of the keymap file
it was generated from, so a stale sheet is easy to spot.

Only the Python standard library is used, like the other scripts here.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import remap  # noqa: E402  (the keymap parser; never re-implemented here)

DEFAULT_KEYMAP = REPO_ROOT / "config/cornix.keymap"
DEFAULT_LAYOUT = REPO_ROOT / "boards/jzf/cornix/cornix-layouts.dtsi"
DEFAULT_OUTPUT = REPO_ROOT / "config/cheatsheet.html"

#: Layers that get a diagram, by display name, in sheet order.
DRAWN_LAYERS = ("Base", "Win", "Num", "Fn2", "Fn3", "Conn")
#: Layers deliberately left undrawn (they are entirely `&trans` / `&none`).
SKIPPED_LAYERS = ("MouseSlow", "MouseFast")

LAYER_BLURB = {
    "Base": "既定レイヤー（macOS 向け）。何も押していないときはこれ。",
    "Win": "OS 検出が Windows/Linux を見つけると自動で重なる差分レイヤー。"
    "色の薄いキーは Base のまま（<code>&amp;trans</code>）。",
    "Num": "42（左親指の内側）を押している間。数字と記号。",
    "Fn2": "46（右親指の内側）を押している間。F キー・ナビゲーション。",
    "Fn3": "45（右親指）を押している間。マウス。",
    "Conn": "<kbd>45</kbd> と <kbd>46</kbd>（右親指の Fn キー 2 つ）を<strong>両方</strong>押している間だけ"
    "自動で重なる条件レイヤー（<code>conditional_layers</code>）。接続切り替え専用で、"
    "左端の列が USB / BLE、その隣の列が BT プロファイル 0 / 1 / 2。"
    "それ以外のキーは下の層のまま。",
}


# ---------------------------------------------------------------------------
# Physical layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhysKey:
    """One ``&key_physical_attrs`` entry, in 1/100 of a key unit."""

    w: int
    h: int
    x: int
    y: int
    r: int  # rotation, 1/100 degree
    rx: int
    ry: int

    def corners(self) -> list[tuple[float, float]]:
        pts = [
            (self.x, self.y),
            (self.x + self.w, self.y),
            (self.x + self.w, self.y + self.h),
            (self.x, self.y + self.h),
        ]
        if not self.r:
            return pts
        rad = math.radians(self.r / 100.0)
        cos, sin = math.cos(rad), math.sin(rad)
        out = []
        for px, py in pts:
            dx, dy = px - self.rx, py - self.ry
            out.append((self.rx + dx * cos - dy * sin, self.ry + dx * sin + dy * cos))
        return out


_NUM = r"\(?\s*(-?\d+)\s*\)?"
_ATTRS_RE = re.compile(r"&key_physical_attrs\s+" + r"\s+".join([_NUM] * 7))



def grace_seconds() -> str:
    """CONFIG_CORNIX_BLE_PAUSE_ON_USB_BLE_GRACE_MS from config/cornix_left.conf, in seconds."""
    conf = Path(__file__).resolve().parents[1] / "config" / "cornix_left.conf"
    try:
        m = re.search(r"^CONFIG_CORNIX_BLE_PAUSE_ON_USB_BLE_GRACE_MS=(\d+)", conf.read_text(), re.M)
        if m:
            return str(int(m.group(1)) // 1000)
    except OSError:
        pass
    return "20"

def parse_layout(path: Path, node: str = "layout_50") -> list[PhysKey]:
    """Read the ``key_physical_attrs`` list of one ``zmk,physical-layout``."""
    text = path.read_text(encoding="utf-8")
    masked = remap.mask(text)
    # `mask` blanks comments but keeps every offset, so slicing is safe.
    start = masked.find(f"{node}:")
    if start < 0:
        raise SystemExit(f"{path}: no `{node}:` node")
    keys_at = masked.find("keys", start)
    end = masked.find(";", keys_at)
    body = text[keys_at:end]
    keys = [PhysKey(*(int(v) for v in m.groups())) for m in _ATTRS_RE.finditer(body)]
    if len(keys) != remap.KEY_COUNT:
        raise SystemExit(f"{path}: {node} has {len(keys)} keys, expected {remap.KEY_COUNT}")
    return keys


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

MODS = {"LSHFT", "RSHFT", "LCTRL", "RCTRL", "LGUI", "RGUI", "LALT", "RALT"}

KP_LABEL = {
    "TAB": "Tab", "CAPS": "Caps", "ESC": "Esc", "DEL": "Del", "BSPC": "⌫",
    "RET": "⏎", "ENTER": "⏎", "SPACE": "Space",
    "LSHFT": "⇧", "RSHFT": "⇧", "LCTRL": "Ctrl", "RCTRL": "Ctrl",
    "LGUI": "Cmd", "RGUI": "Cmd", "LALT": "Opt/Alt", "RALT": "Opt/Alt",
    "UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→",
    "HOME": "Home", "END": "End", "PG_UP": "PgUp", "PG_DN": "PgDn",
    "PSCRN": "PrtSc", "INS": "Ins",
    "C_MUTE": "ミュート", "C_VOL_UP": "音量+", "C_VOL_DN": "音量-",
    "MINUS": "-", "EQUAL": "=", "SEMI": ";", "SQT": "'", "GRAVE": "`",
    "BSLH": "\\", "FSLH": "/", "COMMA": ",", "DOT": ".",
    "LBKT": "[", "RBKT": "]", "LBRC": "{", "RBRC": "}",
}

MKP_LABEL = {"LCLK": "左クリック", "RCLK": "右クリック", "MCLK": "中クリック"}
MMV_LABEL = {
    "MOVE_UP": "マウス↑", "MOVE_DOWN": "マウス↓",
    "MOVE_LEFT": "マウス←", "MOVE_RIGHT": "マウス→",
}
OUT_LABEL = {"OUT_USB": "USB へ", "OUT_BLE": "BLE へ", "OUT_TOG": "出力切替"}
#: Layers whose `&mo` key reads better with a word than with its node name.
LAYER_LABEL = {"MouseSlow": "低速", "MouseFast": "高速"}


def kp_label(code: str) -> str:
    if code in KP_LABEL:
        return KP_LABEL[code]
    if re.fullmatch(r"N[0-9]", code):
        return code[1]
    return code


def describe(binding: str, layer_names: Sequence[str]) -> tuple[str, str]:
    """``(kind, label)`` for one binding.  ``kind`` drives the colour."""
    parts = remap._parts(binding)
    if not parts:
        return "none", "–"
    name, args = parts[0], parts[1:]

    def layer_name(index: str) -> str:
        number = int(index)
        if 0 <= number < len(layer_names) and layer_names[number]:
            display = layer_names[number]
            return LAYER_LABEL.get(display, display)
        return f"L{number}"

    if name == "none":
        return "none", "–"
    if name == "trans":
        return "none", "–"
    if name == "kp" and args:
        code = args[0]
        if code in MODS:
            return "mod", kp_label(code)
        if code.startswith("C_"):
            return "media", kp_label(code)
        return "key", kp_label(code)
    if name == "mo" and args:
        return "layer", layer_name(args[0])
    if name == "tog" and args:
        return "layer", f"{layer_name(args[0])}層"
    if name in ("to", "sl") and args:
        return "layer", f"{layer_name(args[0])}固定"
    if name == "lt" and len(args) >= 2:
        return "layer", f"{layer_name(args[0])}/{kp_label(args[1])}"
    if name == "mt" and len(args) >= 2:
        return "mod", f"{kp_label(args[0])}/{kp_label(args[1])}"
    if name == "mkp" and args:
        return "mouse", MKP_LABEL.get(args[0], args[0])
    if name == "mmv" and args:
        return "mouse", MMV_LABEL.get(args[0], args[0])
    if name == "msc" and args:
        return "mouse", args[0]
    if name == "out" and args:
        return "conn", OUT_LABEL.get(args[0], args[0])
    if name == "bt" and args:
        if args[0] == "BT_SEL":
            return "conn", "BT" + (args[1] if len(args) > 1 else "")
        if args[0] == "BT_CLR":
            return "danger", "BT_CLR"
        return "conn", args[0]
    if name in ("bootloader", "sys_reset"):
        return "danger", name.upper()
    if name == "app_tab":
        return "misc", "Tab (Alt+Tab)"
    return "misc", remap.show_label(binding, layer_names)


# ---------------------------------------------------------------------------
# Cells: one per key position per drawn layer
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    position: int
    kind: str
    label: str
    binding: str


def cells_for(keymap: "remap.Keymap", layer: "remap.Layer") -> list[Cell]:
    names = keymap.display_names
    base = keymap.layers[0]
    win = next((l for l in keymap.layers if l.display_name == "Win"), None)
    out: list[Cell] = []
    for pos in range(remap.KEY_COUNT):
        binding = layer.bindings[pos]
        if binding.strip() == "&trans" and layer.index != 0:
            kind, label = describe(base.bindings[pos], names)
            out.append(Cell(pos, "inherit" if kind != "none" else "none", label, binding))
            continue
        kind, label = describe(binding, names)
        if (
            layer.index == 0
            and win is not None
            and binding.startswith("&kp ")
            and win.bindings[pos].startswith("&kp ")
        ):
            # The one place where the same physical key differs per OS.
            other = describe(win.bindings[pos], names)[1]
            if other != label:
                label = f"{label}/{other}"
        out.append(Cell(pos, kind, label, binding))
    return out


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------

INSET = 4  # gap between neighbouring key caps, in layout units
PAD = 14
FONT_1 = 24.0
FONT_2 = 20.0
FONT_MIN = 9.0
FONT_NUM = 13.0

def _char_width(ch: str) -> float:
    """Approximate advance of one character, in em (CJK is full width)."""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 1.0
    code = ord(ch)
    if 0x2190 <= code <= 0x21FF or 0x2300 <= code <= 0x23FF:  # arrows, ⌫ ⏎
        return 0.8
    return 0.56


def _width(text: str) -> float:
    return sum(_char_width(ch) for ch in text)


def _split(label: str) -> list[str]:
    """Break a label in two at the most central ``/`` or space, if any."""
    best: tuple[float, str, str] | None = None
    for index, ch in enumerate(label):
        if ch not in "/ ":
            continue
        left = label[: index + 1] if ch == "/" else label[:index]
        right = label[index + 1:]
        if not left.strip() or not right.strip():
            continue
        score = abs(_width(left) - _width(right))
        if best is None or score < best[0]:
            best = (score, left.strip(), right.strip())
    return [best[1], best[2]] if best else [label]


def fit(label: str, box: float) -> list[tuple[str, float]]:
    """``[(line, font-size)]`` for a label that has to fit ``box`` units."""
    if not label:
        return []
    if _width(label) * FONT_1 <= box:
        return [(label, FONT_1)]
    lines = _split(label)
    if len(lines) == 1:
        size = max(FONT_MIN, box / _width(label))
        return [(label, round(min(FONT_1, size), 1))]
    size = min(FONT_2, min(box / _width(line) for line in lines))
    return [(line, round(max(FONT_MIN, size), 1)) for line in lines]


def _num(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def svg_for(keys: Sequence[PhysKey], cells: Sequence[Cell], layer_name: str) -> str:
    xs: list[float] = []
    ys: list[float] = []
    for key in keys:
        for px, py in key.corners():
            xs.append(px)
            ys.append(py)
    min_x, max_x = min(xs) - PAD, max(xs) + PAD
    min_y, max_y = min(ys) - PAD, max(ys) + PAD

    parts = [
        '<svg class="kb" xmlns="http://www.w3.org/2000/svg" viewBox="'
        f"{_num(min_x)} {_num(min_y)} {_num(max_x - min_x)} {_num(max_y - min_y)}"
        f'" role="img" aria-label="{esc(layer_name)} レイヤーのキー配列">'
    ]
    for key, cell in zip(keys, cells):
        x = key.x + INSET / 2
        y = key.y + INSET / 2
        w = key.w - INSET
        h = key.h - INSET
        open_g = f'<g class="key k-{cell.kind}"'
        if key.r:
            open_g += f' transform="rotate({_num(key.r / 100.0)} {key.rx} {key.ry})"'
        open_g += ">"
        parts.append(open_g)
        parts.append(f"<title>{esc(cell.binding)}</title>")
        parts.append(
            f'<rect class="cap" x="{_num(x)}" y="{_num(y)}" width="{_num(w)}"'
            f' height="{_num(h)}" rx="9"/>'
        )
        parts.append(
            f'<text class="kn" x="{_num(x + 7)}" y="{_num(y + 18)}">{cell.position}</text>'
        )
        lines = fit(cell.label, w - 12)
        cx = _num(x + w / 2)
        if len(lines) == 1:
            text, size = lines[0]
            parts.append(
                f'<text class="kl" x="{cx}" y="{_num(y + h / 2 + 12)}"'
                f' font-size="{_num(size)}">{esc(text)}</text>'
            )
        elif len(lines) == 2:
            for offset, (text, size) in zip((-4, 20), lines):
                parts.append(
                    f'<text class="kl" x="{cx}" y="{_num(y + h / 2 + offset)}"'
                    f' font-size="{_num(size)}">{esc(text)}</text>'
                )
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


def esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


#: The dark palette, applied both for `prefers-color-scheme: dark` (unless the
#: page opts out with `data-theme="light"`) and for an explicit `data-theme="dark"`.
DARK_VARS = """\
  --bg:#14161a;--fg:#e7e9ee;--muted:#98a0ad;--card:#1b1e24;--line:#2c313a;
  --accent:#8fb0ff;--warn:#ff9a7a;
  --k-bg:#242830;--k-bd:#363c47;--k-fg:#e7e9ee;
  --mod-bg:#1d2a45;--mod-bd:#33507f;--mod-fg:#bcd2ff;
  --layer-bg:#16301f;--layer-bd:#2c5b3b;--layer-fg:#a9e6bd;
  --mouse-bg:#332413;--mouse-bd:#5f4521;--mouse-fg:#f0c48a;
  --conn-bg:#122e33;--conn-bd:#265760;--conn-fg:#9fdde7;
  --danger-bg:#3a1719;--danger-bd:#6d2a2c;--danger-fg:#ffb3b3;
  --misc-bg:#281a3a;--misc-bd:#4a3066;--misc-fg:#d7bdf5;
  --none-bg:#1a1d22;--none-bd:#262b33;--none-fg:#4f5764;
  --inherit-bg:#181b20;--inherit-bd:#2a2f38;--inherit-fg:#6b7482;
"""

CSS = f"""
:root{{
  --bg:#fbfbfd;--fg:#1a1c20;--muted:#6b7280;--card:#ffffff;--line:#e2e5ea;
  --accent:#2f5bd7;--warn:#a3320f;
  --k-bg:#f3f4f7;--k-bd:#d5d9e0;--k-fg:#1a1c20;
  --mod-bg:#e7eefc;--mod-bd:#b3c9f2;--mod-fg:#123073;
  --layer-bg:#e7f6ea;--layer-bd:#a9ddb6;--layer-fg:#11522b;
  --mouse-bg:#fdf1e2;--mouse-bd:#eecb9c;--mouse-fg:#6d3c06;
  --conn-bg:#e2f3f6;--conn-bd:#a2d5de;--conn-fg:#0b4a53;
  --danger-bg:#fde9e9;--danger-bd:#efb2b2;--danger-fg:#7c1414;
  --misc-bg:#f2ebfb;--misc-bd:#d0badf;--misc-fg:#45177a;
  --none-bg:#f7f7f9;--none-bd:#e7e9ee;--none-fg:#b2b7c0;
  --inherit-bg:#fdfdfe;--inherit-bd:#dfe3ea;--inherit-fg:#9aa1ad;
}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{
{DARK_VARS}}}}}
:root[data-theme="dark"]{{
{DARK_VARS}}}
*{{box-sizing:border-box}}
html{{-webkit-text-size-adjust:100%}}
body{{margin:0;background:var(--bg);color:var(--fg);
  font-family:system-ui,-apple-system,"Hiragino Kaku Gothic ProN","Noto Sans JP",
  "Yu Gothic UI",Meiryo,sans-serif;line-height:1.7;font-size:15px}}
.wrap{{max-width:1180px;margin:0 auto;padding:24px 16px 56px}}
h1{{font-size:1.5rem;margin:0 0 4px;letter-spacing:.01em}}
h2{{font-size:1.15rem;margin:0 0 10px}}
h3{{font-size:.95rem;margin:0 0 8px;color:var(--muted);font-weight:600;
  letter-spacing:.06em;text-transform:uppercase}}
p{{margin:0 0 10px}}
.sub{{color:var(--muted);margin:0 0 20px;font-size:.9rem}}
.cards{{display:grid;gap:14px;grid-template-columns:1fr;margin-bottom:26px}}
@media (min-width:820px){{.cards{{grid-template-columns:1fr 1fr}}
  .cards .wide{{grid-column:1 / -1}}}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:14px 16px}}
dl.kv{{margin:0;display:grid;grid-template-columns:auto 1fr;gap:4px 12px;
  align-items:baseline}}
dl.kv dt{{font-weight:600;white-space:nowrap}}
dl.kv dd{{margin:0;color:var(--fg)}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);
  vertical-align:top}}
th{{color:var(--muted);font-weight:600;white-space:nowrap}}
tbody tr:last-child td{{border-bottom:0}}
code,kbd{{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:.86em}}
kbd{{background:var(--k-bg);border:1px solid var(--k-bd);border-radius:5px;
  padding:1px 6px;white-space:nowrap}}
.danger{{color:var(--warn);font-weight:600}}
section.layer{{margin:0 0 26px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:14px 16px}}
section.layer .blurb{{color:var(--muted);font-size:.88rem;margin:0 0 10px}}
.scroll{{overflow-x:auto;-webkit-overflow-scrolling:touch;padding-bottom:4px}}
svg.kb{{display:block;width:100%;min-width:780px;height:auto}}
.hint{{color:var(--muted);font-size:.78rem;margin:2px 0 0}}
@media (min-width:860px){{.hint{{display:none}}}}
.key .cap{{fill:var(--k-bg);stroke:var(--k-bd);stroke-width:2}}
.key .kl{{fill:var(--k-fg);text-anchor:middle;font-weight:600}}
.key .kn{{fill:var(--muted);font-size:13px;opacity:.65}}
.k-mod .cap{{fill:var(--mod-bg);stroke:var(--mod-bd)}} .k-mod .kl{{fill:var(--mod-fg)}}
.k-layer .cap{{fill:var(--layer-bg);stroke:var(--layer-bd)}} .k-layer .kl{{fill:var(--layer-fg)}}
.k-mouse .cap{{fill:var(--mouse-bg);stroke:var(--mouse-bd)}} .k-mouse .kl{{fill:var(--mouse-fg)}}
.k-conn .cap{{fill:var(--conn-bg);stroke:var(--conn-bd)}} .k-conn .kl{{fill:var(--conn-fg)}}
.k-media .cap{{fill:var(--conn-bg);stroke:var(--conn-bd)}} .k-media .kl{{fill:var(--conn-fg)}}
.k-danger .cap{{fill:var(--danger-bg);stroke:var(--danger-bd);stroke-width:3}}
.k-danger .kl{{fill:var(--danger-fg)}}
.k-misc .cap{{fill:var(--misc-bg);stroke:var(--misc-bd)}} .k-misc .kl{{fill:var(--misc-fg)}}
.k-none .cap{{fill:var(--none-bg);stroke:var(--none-bd)}} .k-none .kl{{fill:var(--none-fg)}}
.k-inherit .cap{{fill:var(--inherit-bg);stroke:var(--inherit-bd);stroke-dasharray:5 4}}
.k-inherit .kl{{fill:var(--inherit-fg);font-weight:400}}
ul.legend{{list-style:none;display:flex;flex-wrap:wrap;gap:6px 10px;margin:0;padding:0;
  font-size:.8rem}}
ul.legend li{{display:flex;align-items:center;gap:5px;color:var(--muted)}}
ul.legend i{{width:14px;height:14px;border-radius:4px;display:inline-block;
  border:1px solid var(--line)}}
footer{{margin-top:10px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--line);
  padding-top:12px;word-break:break-all}}
"""

LEGEND = (
    ("k-bg", "文字・記号"),
    ("mod-bg", "修飾キー"),
    ("layer-bg", "レイヤーキー"),
    ("mouse-bg", "マウス"),
    ("conn-bg", "接続・メディア"),
    ("misc-bg", "専用ビヘイビア"),
    ("inherit-bg", "下の層のまま"),
)

LED_ROWS = [
    ("接続 LED", "USB 出力を選択中", "シアン点灯（ケーブル接続中はつきっぱなし）"),
    ("接続 LED", "BT プロファイル接続", "プロファイル色（0 緑 / 1 赤 / 2 青）1.5 秒、USB 給電中は継続"),
    ("接続 LED", "相手を探している（広告中）", "プロファイル色でゆっくり明滅、電池駆動なら 5 秒で消灯"),
    ("接続 LED", "接続が切れた", "プロファイル色で 0.5 秒点滅、電池駆動なら 3 秒で消灯"),
    ("接続 LED", "右半分 / ドングル側の分割リンク", "青：接続で 1.5 秒点灯、切断で 3 秒点滅"),
    ("電池 LED", "残量レポート（60 秒ごと）", "緑 &gt;80% / 黄 20-80% / 赤 ≤20% を 2 秒"),
    ("電池 LED", "充電中 / 充電完了", "緑の明滅 / 99% 以上で緑点灯"),
]


def _dl(rows: Iterable[tuple[str, str]]) -> str:
    out = ['<dl class="kv">']
    for term, value in rows:
        out.append(f"<dt>{term}</dt><dd>{value}</dd>")
    out.append("</dl>")
    return "".join(out)


def build_html(keymap: "remap.Keymap", keys: Sequence[PhysKey], digest: str,
               stamp: str) -> str:
    by_name = {layer.display_name: layer for layer in keymap.layers}

    head = [
        "<!DOCTYPE html>",
        '<html lang="ja">',
        "<head>",
        '<meta charset="utf-8"/>',
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>',
        "<title>Cornix Cheat Sheet</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        '<div class="wrap">',
        "<h1>Cornix チートシート</h1>",
        '<p class="sub">config/cornix.keymap から自動生成。キーの数字は ZMK のキー位置'
        "（0-49）で、<code>scripts/remap.py</code> で使う番号と同じ。"
        "キーにカーソルを合わせると元のバインディングが出る。</p>",
    ]

    cards = ['<div class="cards">']

    cards.append("<div class=\"card\"><h3>レイヤーの出し方</h3>" + _dl([
        ("<kbd>42</kbd> 押しながら", "Num（数字・記号）"),
        ("<kbd>46</kbd> 押しながら", "Fn2（F キー・ナビ）"),
        ("<kbd>45</kbd> 押しながら", "Fn3（マウス）"),
        ("<kbd>45</kbd>+<kbd>46</kbd> 両方押しながら", "Conn（接続切り替え。両方押すと自動で出る）"),
        ("Win レイヤー", "OS 検出が Windows/Linux を見つけると自動。"
                         "手動は <kbd>45</kbd>+<kbd>0</kbd>（<code>&amp;tog 1</code>）"),
        ("エンコーダ（左）", "音量アップ / ダウン"),
        ("エンコーダ（右）", "スクロール（1 ノッチ 3 ホイール単位）"),
    ]) + "<p class=\"sub\" style=\"margin:10px 0 0\">左親指は "
        "<kbd>41</kbd> Cmd/Ctrl・<kbd>42</kbd> Num・<kbd>43</kbd> Space、"
        "右親指は <kbd>44</kbd> Space・<kbd>45</kbd> Fn3・<kbd>46</kbd> Fn2。</p></div>")

    cards.append("<div class=\"card\"><h3>Mac と Windows の違い</h3>" + _dl([
        ("<kbd>41</kbd>（左親指）", "macOS では Cmd、Windows では Ctrl"),
        ("<kbd>39</kbd>", "macOS では Cmd、Windows では Windows キー（LGUI のまま）"),
        ("<kbd>38</kbd>", "どちらも Ctrl（変わらない）"),
        ("<kbd>41</kbd>+<kbd>Tab</kbd>", "macOS は Cmd+Tab、Windows は Alt+Tab。"
                                         "<kbd>41</kbd> を押している間はスイッチャーが開いたまま"
                                         "（<code>app_tab</code> が Alt を本物の修飾キーとして保持）"),
        ("Ctrl+Space", "IME 切り替え。macOS は既定、Windows は IME 側の設定で割り当てる"),
    ]) + "</div>")

    conn_rows = [
        ("<kbd>45</kbd>+<kbd>46</kbd> を両方押しながら…",
         "Conn レイヤー。覚え方：<strong>左端の列</strong>が上から USB → Bluetooth、"
         "<strong>その隣の列</strong>が上からプロファイル 0 → 1 → 2"),
        ("…<kbd>0</kbd>（Tab の位置）", "USB 出力に切り替え（<code>&amp;out OUT_USB</code>）"),
        ("…<kbd>12</kbd>（左 Ctrl の位置）", "BLE 出力に切り替え（<code>&amp;out OUT_BLE</code>）。"
                                          "USB 接続中は <strong>" + grace_seconds() + " 秒</strong>の"
                                          "猶予ウィンドウの間だけ広告が許される"),
        ("…<kbd>1</kbd> / <kbd>13</kbd> / <kbd>25</kbd>（Q / A / Z の位置）",
         "BT プロファイル 0 / 1 / 2 を選択"),
        ("<kbd>45</kbd>+<kbd>0</kbd>", "Win レイヤーの手動トグル（OS 検出が外れたとき。Fn3 単独）"),
    ]
    led = ['<table><thead><tr><th>LED</th><th>状態</th><th>見え方</th></tr></thead><tbody>']
    for which, state, look in LED_ROWS:
        led.append(f"<tr><td>{which}</td><td>{state}</td><td>{look}</td></tr>")
    led.append("</tbody></table>")
    cards.append(
        '<div class="card wide"><h3>接続まわり</h3>' + _dl(conn_rows)
        + '<p class="sub" style="margin:12px 0 6px">LED の意味（'
        "<code>cornix_indicator</code> シールドを載せた場合。"
        "左半分は LED0 が接続・LED1 が電池、右半分は逆）</p>"
        + "".join(led)
        + "</div>"
    )

    cards.append("<div class=\"card wide\"><h3>マウス（Fn3 = <kbd>45</kbd> を押しながら）</h3>"
                 + _dl([
                     ("<kbd>I</kbd> / <kbd>J</kbd> / <kbd>K</kbd> / <kbd>L</kbd>",
                      "カーソル移動（上 / 左 / 下 / 右、位置 8 / 19 / 20 / 21）"),
                     ("<kbd>W</kbd> / <kbd>E</kbd>",
                      "押している間だけ速度 1/2 倍 / 2 倍（位置 2 / 3）"),
                     ("<kbd>41</kbd> / <kbd>42</kbd> / <kbd>43</kbd>",
                      "左クリック / 右クリック / 中クリック（左親指 3 つ）"),
                     ("<kbd>31</kbd>（中央）", "中クリック（全レイヤー共通）"),
                 ]) + "</div>")
    cards.append("</div>")

    legend = ['<ul class="legend">']
    for var, text in LEGEND:
        legend.append(f'<li><i style="background:var(--{var})"></i>{text}</li>')
    legend.append("</ul>")

    body = ["".join(cards), '<div class="card" style="margin-bottom:22px">'
            "<h3>色の凡例</h3>" + "".join(legend) + "</div>"]

    for name in DRAWN_LAYERS:
        layer = by_name.get(name)
        if layer is None:
            continue
        cells = cells_for(keymap, layer)
        body.append(
            f'<section class="layer" data-layer="{esc(name)}">'
            f"<h2>{esc(name)} <span class=\"sub\" style=\"font-weight:400\">"
            f"(layer {layer.index})</span></h2>"
            f'<p class="blurb">{LAYER_BLURB.get(name, "")}</p>'
            f'<div class="scroll">{svg_for(keys, cells, name)}</div>'
            '<p class="hint">← 横にスクロールできます →</p>'
            "</section>"
        )

    skipped = "、".join(SKIPPED_LAYERS)
    body.append(
        '<div class="card" style="margin-bottom:22px"><h3>図にしていないレイヤー</h3>'
        f"<p>{esc(skipped)} は図にしていません。"
        "<strong>MouseSlow</strong>(6) と <strong>MouseFast</strong>(7) は全部 "
        "<code>&amp;trans</code> で、キーは 1 つも持ちません — Fn3 の "
        "<kbd>W</kbd> / <kbd>E</kbd> が立てる<em>フラグ</em>で、"
        "<code>&amp;mmv_input_listener</code> のスケーラを 1/2 倍・2 倍に切り替えるためだけに"
        "存在します。</p></div>"
    )

    body.append(
        f'<footer data-keymap-sha256="{digest}">'
        f"config/cornix.keymap: {esc(stamp)} ・ SHA-256: <code>{digest}</code>"
        "<br/>ハッシュが今の keymap と違えばこのシートは古い。"
        "<code>python3 scripts/cheatsheet.py</code> で作り直す。</footer>"
    )

    return "\n".join(head + body + ["</div>", "</body>", "</html>", ""])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def generate(keymap_path: Path, layout_path: Path) -> str:
    raw = keymap_path.read_bytes()  # read once: parsed here, hashed for the footer
    keymap = remap.parse_text(raw.decode("utf-8"), keymap_path)
    keys = parse_layout(layout_path)
    digest = hashlib.sha256(raw).hexdigest()
    return build_html(keymap, keys, digest, source_stamp(keymap_path))


def source_stamp(keymap_path: Path) -> str:
    """A stamp that depends only on the keymap, not on when the script ran.

    The output is committed to the repo and regenerated by CI, so two runs on
    the same keymap must produce byte-identical HTML.  The stamp is the
    keymap's last git commit (short hash and date), with a marker when the
    working copy differs from it; outside a git checkout it falls back to the
    file's modification time in UTC.  SOURCE_DATE_EPOCH overrides both.
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    try:
        run = lambda *a: subprocess.run(  # noqa: E731
            ["git", *a], cwd=keymap_path.parent, capture_output=True, text=True, check=True
        ).stdout.strip()
        head = run("log", "-1", "--format=%h %cs", "--", keymap_path.name)
        dirty = run("status", "--porcelain", "--", keymap_path.name)
    except (OSError, subprocess.CalledProcessError):
        head, dirty = "", ""
    if head:
        return f"commit {head}" + (" + 未コミットの変更" if dirty else "")
    mtime = datetime.fromtimestamp(keymap_path.stat().st_mtime, timezone.utc)
    return mtime.strftime("%Y-%m-%d %H:%M UTC")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keymap", type=Path, default=DEFAULT_KEYMAP)
    parser.add_argument("--layout", type=Path, default=DEFAULT_LAYOUT)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    html_text = generate(args.keymap, args.layout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_text, encoding="utf-8")
    size = len(html_text.encode("utf-8"))
    print(f"wrote {args.output} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except remap.RemapError as exc:  # pragma: no cover - parse failures
        print(f"cheatsheet: {exc}", file=sys.stderr)
        raise SystemExit(1)
