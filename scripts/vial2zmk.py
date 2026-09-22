#!/usr/bin/env python3
"""Convert a Vial/VIA keymap export (``*.vil``) into a ZMK ``.keymap`` file.

Only the Python standard library is used, like the other scripts in this
repository.

A ``.vil`` file stores the keymap as ``layout[layer][matrix_row][matrix_col]``
using QMK keycode names, plus ``encoder_layout[layer][encoder]`` as
``[counter-clockwise, clockwise]`` pairs.  ZMK instead stores one flat list of
bindings per layer, ordered by *key position* as defined by the board's matrix
transform.  This script therefore needs two pieces of knowledge:

1. a **matrix table** (``MATRICES``) that maps every ZMK key position to the
   ``(row, column)`` cell of the Vial matrix it comes from, and
2. a **keycode table** (``KEYCODES`` plus the ``_FUNC_*`` handlers) that maps
   QMK/Vial keycodes to ZMK bindings.

Unknown keycodes are never dropped silently: the conversion fails and lists
every keycode it could not translate.

Usage::

    python3 scripts/vial2zmk.py config/vial/cornix-default-keymap.vil \\
        --layers 5 --layer-names Base,Num,Fn2,Fn3,Fn4 \\
        --insert-layer 1:Win \\
        --user-key USER00='&bt BT_SEL 0' -o config/cornix.keymap

``--insert-layer INDEX:NAME`` adds an empty (``&trans``-filled) overlay layer
that is not in the .vil file at all and renumbers every ``MO()``/``TG()``/...
reference at or above ``INDEX`` accordingly, so an OS/host override layer can
be slotted in without hand-editing the generated bindings.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

# ---------------------------------------------------------------------------
# Matrix tables: ZMK key position -> Vial (row, column)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Matrix:
    """How a Vial matrix maps onto the key positions of a ZMK layout."""

    description: str
    rows: int
    columns: int
    #: ZMK key position i comes from Vial cell ``positions[i]``.
    positions: tuple[tuple[int, int], ...]
    #: Number of display slots per printed row (widest row of the layout).
    slots: int
    #: For every printed row, the display slot each of its keys occupies.
    row_slots: tuple[tuple[int, ...], ...]
    encoders: int = 0

    @property
    def key_count(self) -> int:
        return len(self.positions)


def _cells(row: int, columns: Iterable[int]) -> list[tuple[int, int]]:
    return [(row, column) for column in columns]


# Cornix: 50 keys, 3x6 per hand + 2 inner extra keys + 6 thumb/bottom keys.
#
# Vial rows 0-3 are the left half (0 = top letter row, 3 = thumb row) with
# columns 0-5 running outer -> inner and column 6 holding the single inner
# extra key (left: Vial row 2, right: Vial row 5).  Vial rows 4-7 are the
# right half in the same order, but columns 0-5 run OUTER -> INNER, so they
# are reversed when laid out left to right on screen.
#
# The resulting order matches `default_transform` in
# boards/jzf/cornix/cornix-layouts.dtsi:
#
#   pos  0..5  = RC(0,0)..RC(0,5)   pos  6..11 = RC(0,12)..RC(0,7)
#   pos 12..17 = RC(1,0)..RC(1,5)   pos 18..23 = RC(1,12)..RC(1,7)
#   pos 24..29 = RC(2,0)..RC(2,5)   pos 30 = RC(2,6), pos 31 = RC(1,13)
#                                   pos 32..37 = RC(2,12)..RC(2,7)
#   pos 38..43 = RC(3,0)..RC(3,5)   pos 44..49 = RC(3,12)..RC(3,7)
CORNIX = Matrix(
    description="Cornix 50-key split (layout_50 / default_transform)",
    rows=8,
    columns=7,
    positions=tuple(
        _cells(0, range(0, 6)) + _cells(4, range(5, -1, -1))
        + _cells(1, range(0, 6)) + _cells(5, range(5, -1, -1))
        + _cells(2, range(0, 6)) + [(2, 6), (5, 6)] + _cells(6, range(5, -1, -1))
        + _cells(3, range(0, 6)) + _cells(7, range(5, -1, -1))
    ),
    slots=14,
    row_slots=(
        (0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13),
        (0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13),
        (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13),
        (0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13),
    ),
    encoders=2,
)

MATRICES: dict[str, Matrix] = {"cornix": CORNIX}


# ---------------------------------------------------------------------------
# Keycode table: QMK / Vial keycode -> ZMK binding
# ---------------------------------------------------------------------------

KEYCODES: dict[str, str] = {
    "KC_NO": "&none",
    "KC_TRNS": "&trans",
    "KC_TRANSPARENT": "&trans",
    # Letters and digits
    **{f"KC_{letter}": f"&kp {letter}" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{f"KC_{digit}": f"&kp N{digit}" for digit in "1234567890"},
    # Punctuation / whitespace
    "KC_ENTER": "&kp RET",
    "KC_ENT": "&kp RET",
    "KC_ESCAPE": "&kp ESC",
    "KC_ESC": "&kp ESC",
    "KC_BSPACE": "&kp BSPC",
    "KC_BSPC": "&kp BSPC",
    "KC_TAB": "&kp TAB",
    "KC_SPACE": "&kp SPACE",
    "KC_SPC": "&kp SPACE",
    "KC_MINUS": "&kp MINUS",
    "KC_MINS": "&kp MINUS",
    "KC_EQUAL": "&kp EQUAL",
    "KC_EQL": "&kp EQUAL",
    "KC_LBRACKET": "&kp LBKT",
    "KC_LBRC": "&kp LBKT",
    "KC_RBRACKET": "&kp RBKT",
    "KC_RBRC": "&kp RBKT",
    "KC_BSLASH": "&kp BSLH",
    "KC_BSLS": "&kp BSLH",
    "KC_NONUS_HASH": "&kp NUHS",
    "KC_NONUS_BSLASH": "&kp NUBS",
    "KC_SCOLON": "&kp SEMI",
    "KC_SCLN": "&kp SEMI",
    "KC_QUOTE": "&kp SQT",
    "KC_QUOT": "&kp SQT",
    "KC_GRAVE": "&kp GRAVE",
    "KC_GRV": "&kp GRAVE",
    "KC_COMMA": "&kp COMMA",
    "KC_COMM": "&kp COMMA",
    "KC_DOT": "&kp DOT",
    "KC_SLASH": "&kp FSLH",
    "KC_SLSH": "&kp FSLH",
    "KC_CAPSLOCK": "&kp CAPS",
    "KC_CAPS": "&kp CAPS",
    # Navigation / editing
    "KC_PSCREEN": "&kp PSCRN",
    "KC_PSCR": "&kp PSCRN",
    "KC_SCROLLLOCK": "&kp SLCK",
    "KC_SLCK": "&kp SLCK",
    "KC_PAUSE": "&kp PAUSE_BREAK",
    "KC_PAUS": "&kp PAUSE_BREAK",
    "KC_INSERT": "&kp INS",
    "KC_INS": "&kp INS",
    "KC_HOME": "&kp HOME",
    "KC_PGUP": "&kp PG_UP",
    "KC_DELETE": "&kp DEL",
    "KC_DEL": "&kp DEL",
    "KC_END": "&kp END",
    "KC_PGDOWN": "&kp PG_DN",
    "KC_PGDN": "&kp PG_DN",
    "KC_RIGHT": "&kp RIGHT",
    "KC_RGHT": "&kp RIGHT",
    "KC_LEFT": "&kp LEFT",
    "KC_DOWN": "&kp DOWN",
    "KC_UP": "&kp UP",
    "KC_APPLICATION": "&kp K_APP",
    "KC_APP": "&kp K_APP",
    "KC_MENU": "&kp K_APP",
    "KC_POWER": "&kp K_POWER",
    "KC_NUMLOCK": "&kp KP_NUM",
    "KC_NLCK": "&kp KP_NUM",
    # Modifiers
    "KC_LCTRL": "&kp LCTRL",
    "KC_LCTL": "&kp LCTRL",
    "KC_LSHIFT": "&kp LSHFT",
    "KC_LSFT": "&kp LSHFT",
    "KC_LALT": "&kp LALT",
    "KC_LGUI": "&kp LGUI",
    "KC_LCMD": "&kp LGUI",
    "KC_LWIN": "&kp LGUI",
    "KC_RCTRL": "&kp RCTRL",
    "KC_RCTL": "&kp RCTRL",
    "KC_RSHIFT": "&kp RSHFT",
    "KC_RSFT": "&kp RSHFT",
    "KC_RALT": "&kp RALT",
    "KC_RGUI": "&kp RGUI",
    "KC_RCMD": "&kp RGUI",
    "KC_RWIN": "&kp RGUI",
    # Keypad
    "KC_KP_SLASH": "&kp KP_DIVIDE",
    "KC_PSLS": "&kp KP_DIVIDE",
    "KC_KP_ASTERISK": "&kp KP_MULTIPLY",
    "KC_PAST": "&kp KP_MULTIPLY",
    "KC_KP_MINUS": "&kp KP_MINUS",
    "KC_PMNS": "&kp KP_MINUS",
    "KC_KP_PLUS": "&kp KP_PLUS",
    "KC_PPLS": "&kp KP_PLUS",
    "KC_KP_ENTER": "&kp KP_ENTER",
    "KC_PENT": "&kp KP_ENTER",
    "KC_KP_DOT": "&kp KP_DOT",
    "KC_PDOT": "&kp KP_DOT",
    "KC_KP_EQUAL": "&kp KP_EQUAL",
    "KC_PEQL": "&kp KP_EQUAL",
    **{f"KC_KP_{digit}": f"&kp KP_N{digit}" for digit in "1234567890"},
    **{f"KC_P{digit}": f"&kp KP_N{digit}" for digit in "1234567890"},
    # Media / consumer
    "KC_MUTE": "&kp C_MUTE",
    "KC_AUDIO_MUTE": "&kp C_MUTE",
    "KC_VOLU": "&kp C_VOL_UP",
    "KC_AUDIO_VOL_UP": "&kp C_VOL_UP",
    "KC_VOLD": "&kp C_VOL_DN",
    "KC_AUDIO_VOL_DOWN": "&kp C_VOL_DN",
    "KC_MNXT": "&kp C_NEXT",
    "KC_MEDIA_NEXT_TRACK": "&kp C_NEXT",
    "KC_MPRV": "&kp C_PREV",
    "KC_MEDIA_PREV_TRACK": "&kp C_PREV",
    "KC_MSTP": "&kp C_STOP",
    "KC_MEDIA_STOP": "&kp C_STOP",
    "KC_MPLY": "&kp C_PP",
    "KC_MEDIA_PLAY_PAUSE": "&kp C_PP",
    "KC_BRIU": "&kp C_BRI_UP",
    "KC_BRIGHTNESS_UP": "&kp C_BRI_UP",
    "KC_BRID": "&kp C_BRI_DN",
    "KC_BRIGHTNESS_DOWN": "&kp C_BRI_DN",
    # Mouse buttons / wheel / movement (ZMK pointing)
    "KC_BTN1": "&mkp LCLK",
    "KC_BTN2": "&mkp RCLK",
    "KC_BTN3": "&mkp MCLK",
    "KC_BTN4": "&mkp MB4",
    "KC_BTN5": "&mkp MB5",
    "KC_MS_BTN1": "&mkp LCLK",
    "KC_MS_BTN2": "&mkp RCLK",
    "KC_MS_BTN3": "&mkp MCLK",
    "KC_WH_U": "&msc SCRL_UP",
    "KC_WH_D": "&msc SCRL_DOWN",
    "KC_WH_L": "&msc SCRL_LEFT",
    "KC_WH_R": "&msc SCRL_RIGHT",
    "KC_MS_WH_UP": "&msc SCRL_UP",
    "KC_MS_WH_DOWN": "&msc SCRL_DOWN",
    "KC_MS_WH_LEFT": "&msc SCRL_LEFT",
    "KC_MS_WH_RIGHT": "&msc SCRL_RIGHT",
    "KC_MS_U": "&mmv MOVE_UP",
    "KC_MS_D": "&mmv MOVE_DOWN",
    "KC_MS_L": "&mmv MOVE_LEFT",
    "KC_MS_R": "&mmv MOVE_RIGHT",
    "KC_MS_UP": "&mmv MOVE_UP",
    "KC_MS_DOWN": "&mmv MOVE_DOWN",
    "KC_MS_LEFT": "&mmv MOVE_LEFT",
    "KC_MS_RIGHT": "&mmv MOVE_RIGHT",
    # Firmware
    "RESET": "&bootloader",
    "QK_BOOT": "&bootloader",
    "KC_BOOTLOADER": "&bootloader",
}

# F1..F24
KEYCODES.update({f"KC_F{n}": f"&kp F{n}" for n in range(1, 25)})

# QMK modifier wrappers, e.g. LCTL(KC_C) -> &kp LC(C)
MOD_WRAPPERS: dict[str, str] = {
    "LCTL": "LC",
    "LSFT": "LS",
    "LALT": "LA",
    "LGUI": "LG",
    "RCTL": "RC",
    "RSFT": "RS",
    "RALT": "RA",
    "RGUI": "RG",
    "C": "LC",
    "S": "LS",
    "A": "LA",
    "G": "LG",
}

# QMK mod names usable as the first argument of MT()/OSM().
MOD_NAMES: dict[str, str] = {
    "MOD_LCTL": "LCTRL",
    "MOD_LSFT": "LSHFT",
    "MOD_LALT": "LALT",
    "MOD_LGUI": "LGUI",
    "MOD_RCTL": "RCTRL",
    "MOD_RSFT": "RSHFT",
    "MOD_RALT": "RALT",
    "MOD_RGUI": "RGUI",
    "KC_LCTRL": "LCTRL",
    "KC_LSHIFT": "LSHFT",
    "KC_LALT": "LALT",
    "KC_LGUI": "LGUI",
    "KC_RCTRL": "RCTRL",
    "KC_RSHIFT": "RSHFT",
    "KC_RALT": "RALT",
    "KC_RGUI": "RGUI",
}

_CALL_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)\((.*)\)$")
_USER_RE = re.compile(r"^USER(\d+)$")


class ConversionError(Exception):
    """Raised when the .vil file contains something this script cannot map."""


@dataclass
class Converter:
    user_keys: dict[str, str] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)

    def binding(self, keycode: object, where: str) -> str:
        """Translate one Vial keycode into a ZMK binding, recording failures."""
        if keycode == -1 or keycode is None:
            raise ConversionError(f"{where}: no key expected here but the .vil has {keycode!r}")
        if not isinstance(keycode, str):
            self._unknown(keycode, where)
            return "&none"
        code = keycode.strip()
        if code in KEYCODES:
            return KEYCODES[code]
        if code in self.user_keys:
            return self.user_keys[code]
        match = _CALL_RE.match(code)
        if match:
            name, raw_args = match.group(1), match.group(2)
            args = [arg.strip() for arg in _split_args(raw_args)]
            resolved = self._call(name, args)
            if resolved is not None:
                return resolved
        if _USER_RE.match(code):
            self._unknown(
                code,
                f"{where} (custom RMK/QMK key; pass --user-key {code}='&some behavior')",
            )
            return "&none"
        self._unknown(code, where)
        return "&none"

    def _call(self, name: str, args: list[str]) -> str | None:
        if name in ("MO", "TG", "TO", "OSL", "DF") and len(args) == 1 and args[0].isdigit():
            behavior = {"MO": "mo", "TG": "tog", "TO": "to", "OSL": "sl", "DF": "to"}[name]
            return f"&{behavior} {args[0]}"
        if name == "LT" and len(args) == 2 and args[0].isdigit():
            tapped = KEYCODES.get(args[1])
            if tapped and tapped.startswith("&kp "):
                return f"&lt {args[0]} {tapped[4:]}"
            return None
        if name in ("MT", "OSM"):
            mod = MOD_NAMES.get(args[0]) if args else None
            if mod is None:
                return None
            if name == "OSM" and len(args) == 1:
                return f"&sk {mod}"
            if name == "MT" and len(args) == 2:
                tapped = KEYCODES.get(args[1])
                if tapped and tapped.startswith("&kp "):
                    return f"&mt {mod} {tapped[4:]}"
            return None
        if name in MOD_WRAPPERS and len(args) == 1:
            inner = KEYCODES.get(args[0])
            if inner and inner.startswith("&kp "):
                return f"&kp {MOD_WRAPPERS[name]}({inner[4:]})"
        return None

    def _unknown(self, keycode: object, where: str) -> None:
        self.unknown.append(f"{keycode!r} at {where}")


def _split_args(text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(char)
    args.append("".join(current))
    return args


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


@dataclass
class Layer:
    name: str
    display_name: str
    bindings: list[str]
    labels: list[str]
    sensors: list[str]


def _label(keycode: object) -> str:
    if not isinstance(keycode, str):
        return "?"
    if keycode == "KC_NO":
        return ""
    return keycode[3:] if keycode.startswith("KC_") else keycode


def convert(
    data: dict,
    matrix: Matrix,
    converter: Converter,
    layer_limit: int | None = None,
    layer_names: Sequence[str] = (),
) -> list[Layer]:
    layout = data.get("layout")
    if not isinstance(layout, list) or not layout:
        raise ConversionError("the .vil file has no non-empty 'layout'")
    encoder_layout = data.get("encoder_layout") or []
    count = len(layout) if layer_limit is None else min(layer_limit, len(layout))
    layers: list[Layer] = []
    for index in range(count):
        rows = layout[index]
        if len(rows) < matrix.rows or any(len(row) < matrix.columns for row in rows):
            raise ConversionError(
                f"layer {index}: expected a {matrix.rows}x{matrix.columns} matrix, "
                f"got {len(rows)}x{[len(row) for row in rows]}"
            )
        bindings: list[str] = []
        labels: list[str] = []
        for position, (row, column) in enumerate(matrix.positions):
            keycode = rows[row][column]
            bindings.append(converter.binding(keycode, f"layer {index} position {position}"))
            labels.append(_label(keycode))
        sensors = _sensor_bindings(encoder_layout, index, matrix, converter)
        display = layer_names[index] if index < len(layer_names) else ("Base" if index == 0 else f"Layer {index}")
        layers.append(Layer(f"layer_{index}", display, bindings, labels, sensors))
    return layers


def _sensor_bindings(encoder_layout: list, index: int, matrix: Matrix, converter: Converter) -> list[str]:
    """Vial stores [counter-clockwise, clockwise]; ZMK wants clockwise first."""
    if matrix.encoders == 0 or index >= len(encoder_layout):
        return []
    result: list[str] = []
    for encoder, pair in enumerate(encoder_layout[index][: matrix.encoders]):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ConversionError(f"layer {index} encoder {encoder}: expected [ccw, cw], got {pair!r}")
        ccw, cw = pair
        where = f"layer {index} encoder {encoder}"
        cw_binding = converter.binding(cw, where + " clockwise")
        ccw_binding = converter.binding(ccw, where + " counter-clockwise")
        behaviors = {cw_binding.split()[0], ccw_binding.split()[0]}
        if behaviors == {"&kp"}:
            result.append(f"&inc_dec_kp {cw_binding[4:]} {ccw_binding[4:]}")
        elif behaviors == {"&msc"}:
            result.append(f"&inc_dec_msc {cw_binding[5:]} {ccw_binding[5:]}")
        elif behaviors == {"&mmv"}:
            result.append(f"&inc_dec_mmv {cw_binding[5:]} {ccw_binding[5:]}")
        else:
            raise ConversionError(
                f"{where}: cannot build a sensor binding from {cw_binding!r} / {ccw_binding!r}"
            )
    return result


# ---------------------------------------------------------------------------
# Inserting an extra (overlay) layer
# ---------------------------------------------------------------------------

#: ZMK bindings whose first parameter is a layer number.
_LAYER_BINDING_RE = re.compile(r"&(mo|tog|to|sl|lt|df)\s+(\d+)")
#: The same, as the QMK/Vial keycode names printed in the comment grid.
_LAYER_LABEL_RE = re.compile(r"\b(MO|TG|TO|OSL|DF|LT)\((\d+)")


def _shift_layer_refs(text: str, index: int) -> str:
    """Renumber layer references at or above ``index`` by one."""

    def binding(match: re.Match[str]) -> str:
        number = int(match.group(2))
        return f"&{match.group(1)} {number + 1 if number >= index else number}"

    def label(match: re.Match[str]) -> str:
        number = int(match.group(2))
        return f"{match.group(1)}({number + 1 if number >= index else number}"

    return _LAYER_LABEL_RE.sub(label, _LAYER_BINDING_RE.sub(binding, text))


def insert_layer(layers: list[Layer], index: int, name: str, matrix: Matrix) -> list[Layer]:
    """Insert a `&trans`-filled overlay layer and renumber every reference.

    This is how an OS-specific (or any other) override layer is added without
    hand-editing the generated keymap: the new layer is entirely `&trans`, so
    it changes nothing until keys are filled in by hand, and every existing
    `MO()/TG()/TO()/OSL()/LT()` reference to a layer at or above ``index`` is
    moved up by one so the converted Vial layers keep pointing at the same
    keymaps.  Layer node names (`layer_N`) are renumbered too.

    The new layer deliberately gets no `sensor-bindings`: ZMK's
    `zmk_keymap_sensor_event()` skips a layer that has no binding for a sensor
    and keeps looking at lower layers, so the encoders keep working exactly as
    they do on the layers below.
    """
    if not 1 <= index <= len(layers):
        raise ConversionError(
            f"--insert-layer: index {index} is out of range (1..{len(layers)}); "
            "layer 0 is the base layer and cannot be displaced"
        )
    for layer in layers:
        layer.bindings = [_shift_layer_refs(binding, index) for binding in layer.bindings]
        layer.labels = [_shift_layer_refs(label, index) for label in layer.labels]
        layer.sensors = [_shift_layer_refs(sensor, index) for sensor in layer.sensors]
    layers.insert(
        index,
        Layer(
            name=f"layer_{index}",
            display_name=name,
            bindings=["&trans"] * matrix.key_count,
            labels=[""] * matrix.key_count,
            sensors=[],
        ),
    )
    for position, layer in enumerate(layers):
        layer.name = f"layer_{position}"
    return layers


# ---------------------------------------------------------------------------
# Emitting the keymap
# ---------------------------------------------------------------------------

SCROLL_DEFINE = """\
/*
 * Right-encoder scroll amount, in HID wheel units per detent: one `&msc`
 * tick emits `SCRL_VAL * 16 / 1000` units, so 188 gives 3 (`188 * 16 / 1000
 * = 3.008`).  Has to come before <dt-bindings/zmk/pointing.h>, which derives
 * SCRL_UP/SCRL_DOWN from it behind an `#ifndef`.  The full derivation, and
 * the matching `tap-ms = <24>` on inc_dec_msc below, are in
 * config/keymap-notes.md (2026-09-19).
 */
#define ZMK_POINTING_DEFAULT_SCRL_VAL 188
"""

SCROLL_BEHAVIOR = """\
    behaviors {
        /*
         * ZMK ships `inc_dec_kp` only; the mouse-scroll equivalent is the
         * same sensor-rotate behaviour wired to `&msc`.
         *
         * `&msc SCRL_*` is a *speed*, not a one-shot tick, and it does not
         * accelerate, so the whole amount lands on the first 16 ms trigger
         * tick and `tap-ms` only has to bracket that one tick:
         * `16 < tap-ms < 32`, hence 24.  With
         * ZMK_POINTING_DEFAULT_SCRL_VAL = 188 above that is 3 wheel units
         * per detent, 24 ms apart (config/keymap-notes.md, 2026-09-19).
         */
        inc_dec_msc: sensor_rotate_scroll {
            compatible = "zmk,behavior-sensor-rotate-var";
            #sensor-binding-cells = <2>;
            bindings = <&msc>, <&msc>;
            tap-ms = <24>;
        };
    };
"""

MOVE_BEHAVIOR = """\
    behaviors {
        /* Same caveat as inc_dec_msc: &mmv MOVE_* is a speed, not a step. */
        inc_dec_mmv: sensor_rotate_move {
            compatible = "zmk,behavior-sensor-rotate-var";
            #sensor-binding-cells = <2>;
            bindings = <&mmv>, <&mmv>;
            tap-ms = <150>;
        };
    };
"""


def _grid(
    cells: Sequence[str],
    matrix: Matrix,
    widths: Sequence[int],
    indent: str,
    separator: str = "  ",
    trim: bool = True,
) -> list[str]:
    """Lay out one layer's cells as printed rows, padded to a common width."""
    lines: list[str] = []
    position = 0
    for slots in matrix.row_slots:
        row = [""] * matrix.slots
        for slot in slots:
            row[slot] = cells[position]
            position += 1
        line = indent + separator.join(item.ljust(widths[slot]) for slot, item in enumerate(row))
        lines.append(line.rstrip() if trim else line)
    return lines


def _slot_widths(layers: Sequence[Layer], matrix: Matrix, pick: Callable[[Layer], Sequence[str]]) -> list[int]:
    widths = [0] * matrix.slots
    for layer in layers:
        cells = pick(layer)
        position = 0
        for slots in matrix.row_slots:
            for slot in slots:
                widths[slot] = max(widths[slot], len(cells[position]))
                position += 1
    return widths


def emit(layers: Sequence[Layer], matrix: Matrix, header: str = "") -> str:
    binding_widths = _slot_widths(layers, matrix, lambda layer: layer.bindings)
    text: list[str] = []
    if header:
        text.append(header.rstrip("\n"))
        text.append("")

    joined = " ".join(binding for layer in layers for binding in layer.bindings)
    joined += " " + " ".join(sensor for layer in layers for sensor in layer.sensors)
    includes = ["#include <behaviors.dtsi>", "#include <dt-bindings/zmk/keys.h>"]
    if "&bt " in joined:
        includes.append("#include <dt-bindings/zmk/bt.h>")
    if "&out " in joined:
        includes.append("#include <dt-bindings/zmk/outputs.h>")
    if "&ext_power " in joined:
        includes.append("#include <dt-bindings/zmk/ext_power.h>")
    needs_scroll = "&inc_dec_msc" in joined
    needs_move = "&inc_dec_mmv" in joined
    if re.search(r"&(mkp|msc|mmv|inc_dec_msc|inc_dec_mmv)\b", joined):
        if needs_scroll:
            includes.append("")
            includes.append(SCROLL_DEFINE.rstrip("\n"))
            includes.append("")
        includes.append("#include <dt-bindings/zmk/pointing.h>")
    text.extend(includes)
    text.append("")
    text.append("/ {")
    if needs_scroll:
        text.append(SCROLL_BEHAVIOR.rstrip("\n"))
        text.append("")
    if needs_move:
        text.append(MOVE_BEHAVIOR.rstrip("\n"))
        text.append("")
    text.append("    keymap {")
    text.append('        compatible = "zmk,keymap";')
    for layer in layers:
        text.append("")
        text.append(f"        {layer.name} {{")
        text.append(f'            display-name = "{layer.display_name}";')
        text.append("")
        label_widths = _slot_widths([layer], matrix, lambda one: one.labels)
        for line in _grid(layer.labels, matrix, label_widths, "            // | ", " | ", trim=False):
            text.append(line + " |")
        text.append("")
        text.append("            bindings = <")
        text.extend(_grid(layer.bindings, matrix, binding_widths, ""))
        text.append("            >;")
        if layer.sensors:
            text.append("")
            text.append("            sensor-bindings = " + ", ".join(f"<{s}>" for s in layer.sensors) + ";")
        text.append("        };")
    text.append("    };")
    text.append("};")
    return "\n".join(text) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_header(source: Path, argv: Sequence[str]) -> str:
    command = "python3 scripts/vial2zmk.py " + " ".join(shlex.quote(arg) for arg in argv)
    return (
        "/*\n"
        f" * Generated from {source.as_posix()} by scripts/vial2zmk.py.\n"
        " *\n"
        f" * Command: {command}\n"
        " *\n"
        " * Vial stores encoder actions as [counter-clockwise, clockwise]; ZMK's\n"
        " * inc_dec_* behaviours take the clockwise action first, so the pairs are\n"
        " * swapped here.  If a knob feels reversed on hardware, swap the two\n"
        " * arguments of that sensor-binding.\n"
        " */"
    )


def parse_insert_layer(value: str) -> tuple[int, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError(f"--insert-layer expects INDEX:NAME, got {value!r}")
    index, name = value.split(":", 1)
    index, name = index.strip(), name.strip()
    if not index.isdigit():
        raise argparse.ArgumentTypeError(f"--insert-layer: {index!r} is not a layer number")
    if not name:
        raise argparse.ArgumentTypeError(f"--insert-layer {index}: the display name is empty")
    return int(index), name


def parse_user_key(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"--user-key expects NAME='&binding', got {value!r}")
    name, binding = value.split("=", 1)
    binding = binding.strip()
    if not binding.startswith("&"):
        raise argparse.ArgumentTypeError(f"--user-key {name}: binding must start with '&', got {binding!r}")
    return name.strip(), binding


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("vil", type=Path, help="Vial keymap export (.vil)")
    parser.add_argument("-o", "--output", type=Path, help="write the keymap here (default: stdout)")
    parser.add_argument("--matrix", default="cornix", choices=sorted(MATRICES), help="matrix table to use")
    parser.add_argument("--layers", type=int, help="convert only the first N layers")
    parser.add_argument("--layer-names", default="", help="comma-separated display names, layer 0 first")
    parser.add_argument(
        "--user-key",
        action="append",
        default=[],
        metavar="NAME=BINDING",
        type=parse_user_key,
        help="map a custom keycode (e.g. USER00) to a ZMK binding; repeatable",
    )
    parser.add_argument(
        "--insert-layer",
        action="append",
        default=[],
        metavar="INDEX:NAME",
        type=parse_insert_layer,
        help=(
            "insert an empty (&trans-filled) overlay layer at INDEX and shift every "
            "MO/TG/TO/OSL/LT reference at or above INDEX up by one; repeatable, "
            "applied left to right (e.g. --insert-layer 1:Win)"
        ),
    )
    parser.add_argument("--no-header", action="store_true", help="omit the generated-from header comment")
    args = parser.parse_args(argv)

    matrix = MATRICES[args.matrix]
    try:
        data = json.loads(args.vil.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {args.vil}: {exc}", file=sys.stderr)
        return 2

    converter = Converter(user_keys=dict(args.user_key))
    names = [name.strip() for name in args.layer_names.split(",") if name.strip()]
    try:
        layers = convert(data, matrix, converter, args.layers, names)
        for index, name in args.insert_layer:
            layers = insert_layer(layers, index, name, matrix)
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if converter.unknown:
        print("error: unknown keycodes (nothing was written):", file=sys.stderr)
        for item in dict.fromkeys(converter.unknown):
            print(f"  {item}", file=sys.stderr)
        return 1

    header = "" if args.no_header else build_header(args.vil, argv)
    text = emit(layers, matrix, header)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({len(layers)} layers, {matrix.key_count} keys per layer)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
