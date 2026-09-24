#!/usr/bin/env python3
r"""Edit ``config/cornix.keymap`` in place, by ZMK key position and layer name.

``config/cornix.keymap`` is the source of truth for the user keymap: it is no
longer regenerated from the Vial export (``scripts/vial2zmk.py`` was the
one-time importer).  This script is the supported way to change it, so that
key positions, layer indices and the ASCII grid comments stay consistent.

Key positions are the ZMK positions of the Cornix ``layout_50`` layout,
row-major, 0..49 - exactly the numbering printed by ``remap.py show`` and
recorded in ``config/keymap-notes.md``::

     0 TAB    1 Q     2 W     3 E     4 R     5 T    |                 |  6 Y     7 U     8 I     9 O    10 P    11 BSPC
    12 CAPS  13 A    14 S    15 D    16 F    17 G    |                 | 18 H    19 J    20 K    21 L    22 \    23 ENTER
    24 SHIFT 25 Z    26 X    27 C    28 V    29 B    | 30 MUTE 31 MCLK | 32 N    33 M    34 ,    35 .    36 UP   37 /
    38 CTRL  39 CMD  40 OPT  41 CMD  42 Num  43 SPC  |                 | 44 SPC  45 Fn3  46 Fn2  47 LEFT 48 DOWN 49 RIGHT

Examples::

    scripts/remap.py show                       # every layer
    scripts/remap.py show --layer Base
    scripts/remap.py set --layer Fn3 5 '&none' 6 '&none'
    scripts/remap.py set --layer Win 38 '&trans'
    scripts/remap.py swap --layer Base 41 42
    scripts/remap.py copy --from-layer Base --to-layer Win 39 40
    scripts/remap.py clear --layer Win 38 39      # -> &trans
    scripts/remap.py layer list
    scripts/remap.py layer add Gaming --after Win
    scripts/remap.py layer rename Conn Media
    scripts/remap.py --dry-run set --layer Base 0 '&kp ESC'

Behaviour tuning nodes (such as the keymap's ``&lt`` override) and the
``sensor-bindings`` lines are hand-written and never touched by this script.

Only the ``bindings = < ... >;`` block of a touched layer and the ASCII grid
comment directly above it are regenerated; everything else in the file is
copied byte for byte.  The one exception is column alignment: the bindings
columns are padded to a width shared by all layers, so when an edit makes a
column wider or narrower every layer's bindings block is re-padded (the
bindings themselves are untouched).  ``--dry-run`` shows the diff first.

After every write the keymap checks from ``scripts/check_config.py`` are run
against the result; if they fail, the original file is restored and nothing
is changed.  Exit status: 0 ok, 1 validation failure, 2 usage error.

Only the Python standard library is used, like the other scripts here.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_config  # noqa: E402  (the keymap checks run after every write)

DEFAULT_KEYMAP = REPO_ROOT / "config/cornix.keymap"

#: Cornix layout_50: 50 key positions, printed as 12 / 12 / 14 / 12.
KEY_COUNT = 50
SLOTS = 14
ROW_SLOTS: tuple[tuple[int, ...], ...] = (
    (0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13),
    (0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13),
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13),
    (0, 1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 13),
)
#: The two keys of the centre pair (row 2, slots 6 and 7).
CENTRE_POSITIONS = (30, 31)

INDENT = " " * 12  # indentation of `display-name` / `bindings` inside a layer
NODE_INDENT = " " * 8  # indentation of the `layer_N {` nodes themselves

#: ZMK behaviours that may appear as `&name ...` in a bindings list.
BUILTIN_BEHAVIORS = frozenset(
    {
        "kp", "mo", "to", "tog", "sl", "lt", "mt", "kt", "sk", "none", "trans",
        "bt", "out", "bootloader", "sys_reset", "mkp", "mmv", "msc",
        "caps_word", "key_repeat", "ext_power", "rgb_ug", "bl",
        "studio_unlock", "soft_off", "gresc", "inc_dec_kp",
    }
)

#: Behaviours whose first parameter is a layer index (shared with check_config).
LAYER_BEHAVIORS = check_config.LAYER_BEHAVIORS
_LAYER_BINDING_RE = re.compile(r"&(" + "|".join(sorted(LAYER_BEHAVIORS)) + r")\s+(\d+)")


class RemapError(Exception):
    """A validation error; reported on stderr, exit status 1."""


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

#: `&kp X` -> short label for the position grid printed by `show`.
SHOW_KEYS = {
    "LSHFT": "SHIFT", "RSHFT": "RSHIFT", "LCTRL": "CTRL", "RCTRL": "RCTRL",
    "LGUI": "CMD", "RGUI": "RCMD", "LALT": "OPT", "RALT": "ROPT",
    "SPACE": "SPC", "RET": "ENTER", "ENTER": "ENTER", "C_MUTE": "MUTE",
    "BSLH": "\\", "FSLH": "/", "COMMA": ",", "DOT": ".",
    "C_VOL_UP": "VOL+", "C_VOL_DN": "VOL-",
}

#: `&kp X` -> label for the ASCII grid comment inside the keymap (the QMK-ish
#: spelling the imported file uses, so regenerated comments keep its style).
COMMENT_OUT = {"OUT_USB": "OUTUSB", "OUT_BLE": "OUTBLE", "OUT_TOG": "OUTTOG"}
COMMENT_MKP = {"LCLK": "BTN1", "RCLK": "BTN2", "MCLK": "BTN3"}


def _parts(binding: str) -> list[str]:
    return binding.strip().lstrip("&").split()


def show_label(binding: str, layer_names: Sequence[str] = ()) -> str:
    """Short label for the numbered position grid (`remap.py show`)."""
    parts = _parts(binding)
    if not parts:
        return "?"
    name, args = parts[0], parts[1:]

    def layer(index: str) -> str:
        number = int(index)
        if 0 <= number < len(layer_names) and layer_names[number]:
            return layer_names[number]
        return f"L{number}"

    if name == "trans":
        return "_"
    if name == "none":
        return "-"
    if name == "kp" and args:
        return SHOW_KEYS.get(args[0], args[0])
    if name == "mo" and args:
        return layer(args[0])
    if name in ("tog", "to", "sl") and args:
        return {"tog": "TG", "to": "TO", "sl": "SL"}[name] + args[0]
    if name == "lt" and len(args) >= 2:
        return f"{layer(args[0])}/{SHOW_KEYS.get(args[1], args[1])}"
    if name == "mt" and len(args) >= 2:
        return f"{args[0]}/{SHOW_KEYS.get(args[1], args[1])}"
    if name == "sk" and args:
        return f"SK{args[0]}"
    if name == "bt" and args:
        return "BT" + (args[1] if args[0] == "BT_SEL" and len(args) > 1 else args[0][3:])
    if name == "out" and args:
        return {"OUT_USB": "USB", "OUT_BLE": "BLE", "OUT_TOG": "OTOG"}.get(args[0], args[0])
    if name in ("mkp", "msc", "mmv") and args:
        return args[0]
    if name == "bootloader":
        return "BOOT"
    if name == "sys_reset":
        return "RESET"
    return name.upper()


def comment_label(binding: str) -> str:
    """Label for the ASCII grid comment above a layer's bindings."""
    parts = _parts(binding)
    if not parts:
        return ""
    name, args = parts[0], parts[1:]
    if name in ("trans", "none"):
        return ""
    if name == "kp" and args:
        return args[0][2:] if args[0].startswith("C_") and args[0] != "C_MUTE" else (
            "MUTE" if args[0] == "C_MUTE" else args[0]
        )
    if name in ("mo", "tog", "to", "sl", "df") and args:
        return {"mo": "MO", "tog": "TG", "to": "TO", "sl": "OSL", "df": "DF"}[name] + f"({args[0]})"
    if name == "lt" and len(args) >= 2:
        return f"LT({args[0]},{args[1]})"
    if name == "mt" and len(args) >= 2:
        return f"MT({args[0]},{args[1]})"
    if name == "sk" and args:
        return f"OSM({args[0]})"
    if name == "kt" and args:
        return f"KT({args[0]})"
    if name == "mkp" and args:
        return COMMENT_MKP.get(args[0], args[0])
    if name in ("mmv", "msc") and args:
        return args[0]
    if name == "out" and args:
        return COMMENT_OUT.get(args[0], args[0])
    if name == "bt" and args:
        return args[0] if args[0] != "BT_SEL" else "BTSEL" + (args[1] if len(args) > 1 else "")
    if name == "bootloader":
        return "BOOT"
    return name.upper()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def mask(text: str) -> str:
    """Blank out comments and string literals, keeping every offset intact."""
    out = list(text)
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = length if end < 0 else end + 2
            for i in range(index, end):
                if out[i] != "\n":
                    out[i] = " "
            index = end
        elif char == "/" and text.startswith("//", index):
            end = text.find("\n", index)
            end = length if end < 0 else end
            for i in range(index, end):
                out[i] = " "
            index = end
        elif char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            for i in range(index + 1, min(end, length)):
                if out[i] != "\n":
                    out[i] = " "
            index = min(end + 1, length)
        else:
            index += 1
    return "".join(out)


def _match_brace(masked: str, open_index: int) -> int:
    """Index just past the `}` matching the `{` at ``open_index``."""
    depth = 0
    for index in range(open_index, len(masked)):
        if masked[index] == "{":
            depth += 1
        elif masked[index] == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    raise RemapError("unbalanced braces in the keymap")


@dataclass
class Layer:
    index: int
    node_name: str
    display_name: str
    bindings: list[str]
    #: char offsets into the file text
    node_end: int
    node_name_span: tuple[int, int]
    display_span: tuple[int, int]
    comment_span: tuple[int, int]  # the `// | ... |` block (may be empty)
    body_span: tuple[int, int]  # between `bindings = <\n` and the `>;` line
    touched: bool = False
    #: set when a layer has to be renumbered (`layer add`)
    new_node_name: str | None = None


@dataclass
class Keymap:
    path: Path
    text: str
    layers: list[Layer]
    behaviors: set[str]

    @property
    def display_names(self) -> list[str]:
        """Layer index -> display name, for `&mo N` labels."""
        return [layer.display_name for layer in self.layers]

    def find(self, wanted: str) -> Layer:
        for layer in self.layers:
            if layer.display_name.lower() == wanted.lower() or layer.node_name == wanted:
                return layer
        if re.fullmatch(r"\d+", wanted.strip()):
            index = int(wanted)
            if 0 <= index < len(self.layers):
                return self.layers[index]
        names = ", ".join(f"{i}:{l.display_name}" for i, l in enumerate(self.layers))
        raise RemapError(f"unknown layer {wanted!r}; the keymap has {names}")


def split_bindings(body: str) -> list[str]:
    return ["&" + item.strip(" \t\n,") for item in body.split("&")[1:]]


def parse(path: Path) -> Keymap:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RemapError(f"cannot read {path}: {exc}") from exc
    return parse_text(text, path)


def parse_text(text: str, path: Path) -> Keymap:
    """Parse keymap ``text`` already read from ``path`` (only named in errors)."""
    masked = mask(text)

    behaviors: set[str] = set()
    for match in re.finditer(r"^\s*(\w+)\s*:\s*\w+\s*\{", masked, re.M):
        behaviors.add(match.group(1))

    # (string contents are blanked in `masked`, so match on the original text
    # and use `masked` only to prove the match is not inside a comment)
    compatible = next(
        (
            match
            for match in re.finditer(r'compatible\s*=\s*"zmk,keymap"\s*;', text)
            if masked[match.start() : match.end()].strip()
        ),
        None,
    )
    if compatible is None:
        raise RemapError(f"{path}: no `compatible = \"zmk,keymap\";` node found")
    depth = 0
    open_index = -1
    for index in range(compatible.start() - 1, -1, -1):
        if masked[index] == "}":
            depth += 1
        elif masked[index] == "{":
            if depth == 0:
                open_index = index
                break
            depth -= 1
    if open_index < 0:
        raise RemapError(f"{path}: cannot find the keymap node")
    keymap_end = _match_brace(masked, open_index)

    layers: list[Layer] = []
    index = open_index + 1
    while index < keymap_end:
        match = re.compile(r"([A-Za-z_][\w-]*)\s*\{").search(masked, index, keymap_end)
        if match is None:
            break
        node_end = _match_brace(masked, match.end() - 1)
        if masked[node_end : node_end + 1] == ";":  # the `};` that closes a node
            node_end += 1
        layers.append(_parse_layer(text, masked, len(layers), match, node_end))
        index = node_end
    if not layers:
        raise RemapError(f"{path}: the keymap node has no layers")
    for layer in layers:
        if len(layer.bindings) != KEY_COUNT:
            raise RemapError(
                f"{path}: layer {layer.display_name!r} has {len(layer.bindings)} bindings, "
                f"expected {KEY_COUNT}"
            )
    return Keymap(path=path, text=text, layers=layers, behaviors=behaviors)


def _parse_layer(text: str, masked: str, index: int, match: re.Match[str], node_end: int) -> Layer:
    node_name = match.group(1)
    start, end = match.start(1), match.end(1)
    body = slice(match.end(), node_end)

    display = re.compile(r'display-name\s*=\s*"([^"]*)"').search(text, body.start, body.stop)
    if display is None:
        raise RemapError(f"layer {node_name}: no display-name property")

    bindings_open = re.compile(r"bindings\s*=\s*<").search(masked, body.start, body.stop)
    if bindings_open is None:
        raise RemapError(f"layer {node_name}: no bindings property")
    close = masked.find(">", bindings_open.end(), body.stop)
    if close < 0:
        raise RemapError(f"layer {node_name}: unterminated bindings property")

    body_start = text.index("\n", bindings_open.end()) + 1
    body_end = text.rindex("\n", body_start, close) + 1
    bindings = split_bindings(text[body_start:body_end])

    # The block of `// ...` comment lines above `bindings = <` (there is a
    # blank line between the two).
    line_start = text.rindex("\n", 0, bindings_open.start()) + 1

    def previous_line_start(offset: int) -> int:
        if offset == 0:
            return 0
        found = text.rfind("\n", 0, offset - 1)
        return 0 if found < 0 else found + 1

    cursor = line_start
    while cursor > 0 and not text[previous_line_start(cursor) : cursor].strip():
        cursor = previous_line_start(cursor)
    comment_end = cursor
    while cursor > 0 and text[previous_line_start(cursor) : cursor].strip().startswith("//"):
        cursor = previous_line_start(cursor)
    comment_start = cursor
    if comment_start == comment_end:  # no comment block yet
        comment_start = comment_end = line_start

    return Layer(
        index=index,
        node_name=node_name,
        display_name=display.group(1),
        bindings=bindings,
        node_end=node_end,
        node_name_span=(start, end),
        display_span=display.span(1),
        comment_span=(comment_start, comment_end),
        body_span=(body_start, body_end),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _rows(cells: Sequence[str]) -> list[list[str]]:
    """Lay a flat 50-cell list out as four printed rows of 14 slots."""
    rows: list[list[str]] = []
    position = 0
    for slots in ROW_SLOTS:
        row = [""] * SLOTS
        for slot in slots:
            row[slot] = cells[position]
            position += 1
        rows.append(row)
    return rows


def slot_widths(all_bindings: Iterable[Sequence[str]]) -> list[int]:
    widths = [0] * SLOTS
    for bindings in all_bindings:
        for row in _rows(bindings):
            for slot, cell in enumerate(row):
                widths[slot] = max(widths[slot], len(cell))
    return widths


def render_bindings(bindings: Sequence[str], widths: Sequence[int]) -> str:
    lines = []
    for row in _rows(bindings):
        line = "  ".join(cell.ljust(widths[slot]) for slot, cell in enumerate(row))
        lines.append(line.rstrip())
    return "".join(line + "\n" for line in lines)


def render_comment(bindings: Sequence[str], indent: str = INDENT) -> str:
    return render_labels([comment_label(binding) for binding in bindings], indent)


def render_labels(labels: Sequence[str], indent: str = INDENT) -> str:
    """The `// | ... |` grid comment for one label per key position."""
    widths = slot_widths([labels])
    lines = []
    for row in _rows(labels):
        cells = " | ".join(cell.ljust(widths[slot]) for slot, cell in enumerate(row))
        lines.append(f"{indent}// | {cells} |")
    return "".join(line + "\n" for line in lines)


def render_grid(bindings: Sequence[str], layer_names: Sequence[str] = ()) -> str:
    """The numbered position grid, in the style of config/keymap-notes.md."""
    labels = [show_label(binding, layer_names) for binding in bindings]
    cells = [f"{position:2d} {label}" for position, label in enumerate(labels)]
    rows = _rows(cells)
    left_widths = [max(max(len(row[slot]) for row in rows), 7) for slot in range(6)]
    right_widths = [max(max(len(row[slot]) for row in rows), 7) for slot in range(8, SLOTS)]
    centre = " ".join(cells[position] for position in CENTRE_POSITIONS)
    lines = []
    for index, row in enumerate(rows):
        left = " ".join(row[slot].ljust(left_widths[slot]) for slot in range(6))
        middle = centre if row[6] or row[7] else ""
        right = " ".join(row[slot].ljust(right_widths[slot - 8]) for slot in range(8, SLOTS))
        lines.append(f"{left} | {middle.ljust(len(centre))} | {right}".rstrip())
    return "".join(line + "\n" for line in lines)


def grid_of(keymap: Keymap, layer: Layer) -> str:
    return render_grid(layer.bindings, keymap.display_names)


def new_layer_node(node_name: str, display_name: str, widths: Sequence[int]) -> str:
    bindings = ["&trans"] * KEY_COUNT
    return (
        f"{NODE_INDENT}{node_name} {{\n"
        f'{INDENT}display-name = "{display_name}";\n'
        "\n"
        + render_comment(bindings)
        + "\n"
        f"{INDENT}bindings = <\n"
        + render_bindings(bindings, widths)
        + f"{INDENT}>;\n"
        f"{NODE_INDENT}}};"
    )


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def validate_position(value: str) -> int:
    if not re.fullmatch(r"\d+", value.strip()):
        raise RemapError(f"{value!r} is not a key position (0-{KEY_COUNT - 1})")
    position = int(value)
    if not 0 <= position < KEY_COUNT:
        raise RemapError(f"key position {position} is out of range (0-{KEY_COUNT - 1})")
    return position


def validate_binding(binding: str, known: set[str], force: bool) -> str:
    binding = " ".join(binding.split())
    if not binding.startswith("&"):
        raise RemapError(f"binding {binding!r} must start with '&' (e.g. '&kp MINUS')")
    if binding.count("&") > 1 or any(char in binding for char in "<>;,"):
        raise RemapError(f"binding {binding!r} must be a single binding, without <>, ; or ,")
    name = binding[1:].split()[0] if binding[1:].split() else ""
    if not name:
        raise RemapError("empty binding")
    if name not in known and not force:
        raise RemapError(
            f"unknown behavior '&{name}'; ZMK built-ins are "
            f"{', '.join(sorted(BUILTIN_BEHAVIORS))} plus the labels defined in the "
            "keymap's behaviors node.  Use --force to write it anyway."
        )
    return binding


def set_bindings(keymap: Keymap, layer: Layer, changes: dict[int, str]) -> None:
    for position, binding in changes.items():
        layer.bindings[position] = binding
    layer.touched = True


def shift_layer_ref(binding: str, index: int) -> str:
    """``binding`` with its layer reference (if any) at or above ``index`` moved up by one."""

    def replace(match: re.Match[str]) -> str:
        number = int(match.group(2))
        return f"&{match.group(1)} {number + 1 if number >= index else number}"

    return _LAYER_BINDING_RE.sub(replace, binding)


def shift_layer_refs(keymap: Keymap, index: int) -> None:
    """Renumber the `&mo/&tog/&to/&sl/&lt/...` references at or above ``index``."""
    for layer in keymap.layers:
        for position, binding in enumerate(layer.bindings):
            new = shift_layer_ref(binding, index)
            if new != binding:
                layer.bindings[position] = new
                layer.touched = True


def render_file(
    keymap: Keymap,
    inserted: list[tuple[int, str]] | None = None,
    extra: Sequence[Sequence[str]] = (),
) -> str:
    """Rebuild the file text.

    Only a touched layer's comment and bindings block are regenerated.  The
    bindings columns share one set of widths across all layers, so when an
    edit makes a column wider or narrower every layer's bindings block is
    re-padded as well (the bindings themselves stay as they are).
    """
    all_bindings = [layer.bindings for layer in keymap.layers] + [list(item) for item in extra]
    widths = slot_widths(all_bindings)
    stored = [split_bindings(keymap.text[slice(*layer.body_span)]) for layer in keymap.layers]
    realigned = widths != slot_widths(stored)

    edits: list[tuple[int, int, str]] = []
    for layer in keymap.layers:
        if layer.touched:
            comment = render_comment(layer.bindings)
            if layer.comment_span[0] == layer.comment_span[1]:
                comment += "\n"  # no comment block yet: keep the blank line
            edits.append((*layer.comment_span, comment))
        if layer.touched or realigned:
            edits.append((*layer.body_span, render_bindings(layer.bindings, widths)))
        if layer.new_node_name is not None and layer.new_node_name != layer.node_name:
            edits.append((*layer.node_name_span, layer.new_node_name))
    for offset, text in inserted or []:
        edits.append((offset, offset, text))

    text = keymap.text
    for start, end, replacement in sorted(edits, key=lambda item: item[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


# ---------------------------------------------------------------------------
# Checking and writing
# ---------------------------------------------------------------------------


def run_checks(path: Path) -> list[str]:
    warnings: list[str] = []
    errors = check_config.check_keymap(path, KEY_COUNT, warnings, path.as_posix())
    errors.extend(f"{path.as_posix()}: {warning}" for warning in warnings if "skipped" in warning)
    return errors


def write(keymap: Keymap, text: str, dry_run: bool, out=sys.stdout) -> int:
    if text == keymap.text:
        print("nothing to change", file=out)
        return 0
    if dry_run:
        diff = difflib.unified_diff(
            keymap.text.splitlines(keepends=True),
            text.splitlines(keepends=True),
            fromfile=f"a/{keymap.path.name}",
            tofile=f"b/{keymap.path.name}",
        )
        out.writelines(diff)
        return 0
    original = keymap.text
    keymap.path.write_text(text, encoding="utf-8")
    errors = run_checks(keymap.path)
    if errors:
        keymap.path.write_text(original, encoding="utf-8")
        print("error: the edited keymap does not pass check_config.py:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        print(f"{keymap.path} was restored unchanged", file=sys.stderr)
        return 1
    print(f"wrote {keymap.path}", file=out)
    return 0


def report(keymap: Keymap, text: str, dry_run: bool, out=sys.stdout) -> int:
    status = write(keymap, text, dry_run, out)
    if status == 0 and not dry_run:
        for layer in keymap.layers:
            if layer.touched:
                print(f"\n{layer.index} {layer.display_name}:", file=out)
                out.write(grid_of(keymap, layer))
    return status


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_show(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    layers = [keymap.find(args.layer)] if args.layer else keymap.layers
    for position, layer in enumerate(layers):
        if position:
            print(file=out)
        print(f"{layer.index} {layer.display_name} ({layer.node_name}):", file=out)
        out.write(grid_of(keymap, layer))
    return 0


def _pairs(values: Sequence[str]) -> list[tuple[str, str]]:
    if not values or len(values) % 2:
        raise RemapError("expected POS BINDING pairs, e.g. `set --layer Base 0 '&kp ESC'`")
    return [(values[i], values[i + 1]) for i in range(0, len(values), 2)]


def cmd_set(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    layer = keymap.find(args.layer)
    known = set(BUILTIN_BEHAVIORS) | keymap.behaviors
    changes: dict[int, str] = {}
    for raw_position, raw_binding in _pairs(args.pairs):
        changes[validate_position(raw_position)] = validate_binding(raw_binding, known, args.force)
    set_bindings(keymap, layer, changes)
    return report(keymap, render_file(keymap), args.dry_run, out)


def cmd_swap(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    layer = keymap.find(args.layer)
    first, second = validate_position(args.pos1), validate_position(args.pos2)
    bindings = layer.bindings
    set_bindings(keymap, layer, {first: bindings[second], second: bindings[first]})
    return report(keymap, render_file(keymap), args.dry_run, out)


def cmd_copy(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    source = keymap.find(args.from_layer)
    target = keymap.find(args.to_layer)
    if source is target:
        raise RemapError("--from-layer and --to-layer are the same layer")
    if not args.positions:
        raise RemapError("copy needs at least one key position")
    changes = {}
    for raw in args.positions:
        position = validate_position(raw)
        changes[position] = source.bindings[position]
    set_bindings(keymap, target, changes)
    return report(keymap, render_file(keymap), args.dry_run, out)


def cmd_clear(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    layer = keymap.find(args.layer)
    if not args.positions:
        raise RemapError("clear needs at least one key position")
    set_bindings(keymap, layer, {validate_position(raw): "&trans" for raw in args.positions})
    return report(keymap, render_file(keymap), args.dry_run, out)


def cmd_layer_list(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    for layer in keymap.layers:
        print(f"{layer.index} {layer.display_name} ({layer.node_name})", file=out)
    return 0


def cmd_layer_rename(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    layer = keymap.find(args.layer)
    name = args.name.strip()
    if not name or '"' in name:
        raise RemapError(f"{args.name!r} is not a usable display name")
    if any(other.display_name.lower() == name.lower() for other in keymap.layers):
        raise RemapError(f"a layer named {name!r} already exists")
    start, end = layer.display_span
    text = keymap.text[:start] + name + keymap.text[end:]
    return write(keymap, text, args.dry_run, out)


def cmd_layer_add(args: argparse.Namespace, keymap: Keymap, out=sys.stdout) -> int:
    name = args.name.strip()
    if not name or '"' in name:
        raise RemapError(f"{args.name!r} is not a usable display name")
    if any(layer.display_name.lower() == name.lower() for layer in keymap.layers):
        raise RemapError(f"a layer named {name!r} already exists")
    if args.after is None:
        index = len(keymap.layers)
        offset = keymap.layers[-1].node_end
    else:
        previous = keymap.find(args.after)
        index = previous.index + 1
        offset = previous.node_end

    shift_layer_refs(keymap, index)
    fresh = ["&trans"] * KEY_COUNT
    for layer in keymap.layers[index:]:
        layer.index += 1
    for layer in keymap.layers:
        layer.new_node_name = f"layer_{layer.index}"
    widths = slot_widths([layer.bindings for layer in keymap.layers] + [fresh])
    node = "\n\n" + new_layer_node(f"layer_{index}", name, widths)
    text = render_file(keymap, inserted=[(offset, node)], extra=[fresh])
    status = write(keymap, text, args.dry_run, out)
    if status == 0 and not args.dry_run:
        print(f"\n{index} {name}:", file=out)
        out.write(render_grid(fresh))
    return status


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remap.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--keymap", type=Path, default=DEFAULT_KEYMAP,
        help="keymap file to edit (default: config/cornix.keymap)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the diff instead of writing")
    parser.add_argument(
        "--force", action="store_true",
        help="accept a binding whose behavior is not a known ZMK built-in",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="print the numbered position grid")
    show.add_argument("--layer", help="layer display name, node name or index (default: every layer)")
    show.set_defaults(func=cmd_show)

    setter = sub.add_parser("set", help="replace bindings: POS BINDING [POS BINDING ...]")
    setter.add_argument("--layer", required=True)
    setter.add_argument("pairs", nargs="*", metavar="POS BINDING")
    setter.set_defaults(func=cmd_set)

    swap = sub.add_parser("swap", help="swap two key positions on one layer")
    swap.add_argument("--layer", required=True)
    swap.add_argument("pos1")
    swap.add_argument("pos2")
    swap.set_defaults(func=cmd_swap)

    copy = sub.add_parser("copy", help="copy key positions from one layer to another")
    copy.add_argument("--from-layer", required=True, dest="from_layer")
    copy.add_argument("--to-layer", required=True, dest="to_layer")
    copy.add_argument("positions", nargs="*", metavar="POS")
    copy.set_defaults(func=cmd_copy)

    clear = sub.add_parser("clear", help="set key positions to &trans")
    clear.add_argument("--layer", required=True)
    clear.add_argument("positions", nargs="*", metavar="POS")
    clear.set_defaults(func=cmd_clear)

    layer = sub.add_parser("layer", help="add, rename or list layers")
    layer_sub = layer.add_subparsers(dest="layer_command", required=True)

    add = layer_sub.add_parser("add", help="add a &trans-filled layer and renumber &mo/&tog/...")
    add.add_argument("name")
    add.add_argument("--after", help="insert after this layer (default: last)")
    add.set_defaults(func=cmd_layer_add)

    rename = layer_sub.add_parser("rename", help="change a layer's display-name")
    rename.add_argument("layer")
    rename.add_argument("name")
    rename.set_defaults(func=cmd_layer_rename)

    listing = layer_sub.add_parser("list", help="list the layers")
    listing.set_defaults(func=cmd_layer_list)

    return parser


def main(argv: Sequence[str] | None = None, out=sys.stdout) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        keymap = parse(args.keymap)
        return args.func(args, keymap, out)
    except RemapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
