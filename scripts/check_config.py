#!/usr/bin/env python3
"""Static, toolchain-free validation of the Cornix ZMK module configuration.

This script only uses the Python standard library (no PyYAML, no pytest) so
that it can run anywhere Python 3 is available.  It cross-checks the files
that most often go wrong when the module is edited by hand:

* ``build.yaml`` and the optional ``build-debug.yaml``
                            - qualified board names, known shields/snippets
                              (ZMK snippets plus local ``snippets/<name>``),
                              per-file unique artifact names, dongle rules.
* ``snippets/*/snippet.yml``- snippet ``name`` matches its directory.
* ``config/west.yml``       - remotes, ``zmk`` import, ``self.path``.
* Kconfig fragments         - settings backend invariants (NVS, no
                              ``CONFIG_SETTINGS_NONE``), split roles,
                              conflicting duplicate keys.
* Device tree / keymaps     - matrix transform vs. physical layout key counts,
                              per-layer binding counts vs. the targeted layout,
                              key position / layer index ranges.
* Metadata                  - JSON validity, ``*.zmk.yml`` / ``shield.yml``
                              ids, mandatory shield files.

Every ``check_*`` function is parameterised on paths or strings and returns a
list of *error* strings.  Non-fatal observations are appended to the optional
``warnings`` list argument.  ``main()`` runs everything and exits with status
1 when any check produced an error.

Usage::

    python3 scripts/check_config.py [--root DIR] [--json] [--quiet]
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Knowledge about the repository / ZMK that the checks rely on
# ---------------------------------------------------------------------------

BOARD_DIR = Path("boards/jzf/cornix")
SHIELDS_DIR = Path("boards/shields")
CONFIG_DIR = Path("config")
LAYOUTS_DTSI = BOARD_DIR / "cornix-layouts.dtsi"

# Shields that are not defined in this repository but are provided by ZMK
# itself or by the west modules listed in config/west.yml.
EXTERNAL_SHIELDS: dict[str, str] = {
    "settings_reset": "ZMK core (app/boards/shields/settings_reset)",
    "dongle_display": "zmk-dongle-display module (englmaxi)",
    "dongle_screen": "zmk-dongle-screen module (janpfischer)",
}

# Snippets accepted in the build matrices.  studio-rpc-usb-uart and
# zmk-usb-logging ship with ZMK; nrf52840-nosd is the no-SoftDevice flash
# layout snippet.  Local snippets (snippets/<name>/snippet.yml, valid because
# zephyr/module.yml declares snippet_root: .) are accepted as well.
KNOWN_SNIPPETS = frozenset({"studio-rpc-usb-uart", "nrf52840-nosd", "zmk-usb-logging"})
SNIPPETS_DIR = Path("snippets")

# Build matrix files: name -> required.  Optional files are skipped (with a
# warning) when they do not exist.  Artifact names are unique per file.
BUILD_MATRIX_FILES: dict[str, bool] = {
    "build.yaml": True,
    "build-debug.yaml": False,
}

# Boards that live outside this repository and are known to work with the
# dongle / settings_reset builds.
EXTERNAL_BOARDS = frozenset({"nice_nano", "nice_nano_v2", "seeeduino_xiao_ble", "xiao_ble"})

# Board name prefixes that must be written with the Zephyr 4.1 "//zmk"
# qualifier.  README: the unqualified nice_nano target may select
# CONFIG_SETTINGS_NONE=y and lose Bluetooth bonds.
QUALIFIED_BOARD_PREFIXES = ("cornix_", "nice_nano")
BOARD_QUALIFIER = "//zmk"

# Split role expected per Cornix board (from the README and the defconfigs).
BOARD_ROLES: dict[str, str] = {
    "cornix_left": "central",
    "cornix_right": "peripheral",
    "cornix_ph_left": "peripheral",
}

# Shield .conf files with mandatory values.
SHIELD_CONF_EXPECTATIONS: dict[str, dict[str, str]] = {
    "cornix_dongle_adapter": {
        "CONFIG_ZMK_SPLIT": "y",
        "CONFIG_ZMK_SPLIT_ROLE_CENTRAL": "y",
    },
}

REQUIRED_DEFCONFIG_VALUES: dict[str, str] = {
    "CONFIG_NVS": "y",
    "CONFIG_SETTINGS_NVS": "y",
}
FORBIDDEN_CONF_VALUES: dict[str, str] = {
    "CONFIG_SETTINGS_NONE": "y",
}

# Which physical layout each keymap in the repository targets.
#   ("dtsi", <label>)          -> label of a zmk,physical-layout node in
#                                 cornix-layouts.dtsi
#   ("json", <path>, <layout>) -> a keymap-editor JSON layout description
KEYMAP_TARGETS: dict[str, tuple[str, ...]] = {
    "boards/jzf/cornix/cornix.keymap": ("dtsi", "layout_50"),
    "config/cornix.keymap": ("dtsi", "layout_50"),
    "config/cornix42.keymap": ("json", "config/cornix42.json", "default_layout"),
}

# Behaviours whose first parameter is a layer index.  Public: scripts/remap.py
# imports this set so the two tools agree on what counts as a layer reference.
LAYER_BEHAVIORS = frozenset({"df", "mo", "to", "tog", "sl", "lt"})


class ParseError(ValueError):
    """Raised by the minimal YAML / DTS parsers on malformed input."""


# ---------------------------------------------------------------------------
# Minimal YAML subset parser (block mappings, block sequences, scalars,
# flow lists such as [a, b]).  Enough for build.yaml, west.yml, board.yml
# and the *.zmk.yml / shield.yml metadata files.
# ---------------------------------------------------------------------------

_YAML_KEY_RE = re.compile(r"^([A-Za-z0-9_.\-/]+?)\s*:(?:\s+(.*))?$")


def _yaml_strip_comment(line: str) -> str:
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip()
    return line.rstrip()


def _yaml_scalar(text: str) -> Any:
    text = text.strip()
    if text == "":
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_yaml_scalar(item) for item in inner.split(",")]
    if text == "{}":
        return {}
    if text in ("null", "~"):
        return None
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


class _YAMLParser:
    def __init__(self, lines: list[list[Any]]) -> None:
        self.lines = lines
        self.index = 0

    def _current(self) -> list[Any] | None:
        if self.index < len(self.lines):
            return self.lines[self.index]
        return None

    @staticmethod
    def _is_sequence_item(text: str) -> bool:
        return text == "-" or text.startswith("- ")

    def parse_node(self, indent: int) -> Any:
        line = self._current()
        if line is None:
            return None
        if line[0] != indent:
            raise ParseError(f"line {line[2]}: unexpected indentation")
        if self._is_sequence_item(line[1]):
            return self.parse_sequence(indent)
        return self.parse_mapping(indent)

    def parse_sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while (line := self._current()) is not None:
            line_indent, text, number = line
            if line_indent < indent or not self._is_sequence_item(text):
                break
            if line_indent > indent:
                raise ParseError(f"line {number}: unexpected indentation in sequence")
            rest = text[1:].lstrip()
            if not rest:
                self.index += 1
                nxt = self._current()
                if nxt is not None and nxt[0] > indent:
                    items.append(self.parse_node(nxt[0]))
                else:
                    items.append(None)
                continue
            if _YAML_KEY_RE.match(rest) and not rest.startswith(("\"", "'", "[")):
                # "- key: value" starts an inline mapping; treat the rest of the
                # line as if it were indented under the dash.
                child_indent = indent + (len(text) - len(rest))
                self.lines[self.index] = [child_indent, rest, number]
                items.append(self.parse_mapping(child_indent))
            else:
                items.append(_yaml_scalar(rest))
                self.index += 1
        return items

    def parse_mapping(self, indent: int) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        while (line := self._current()) is not None:
            line_indent, text, number = line
            if line_indent < indent or self._is_sequence_item(text):
                break
            if line_indent > indent:
                raise ParseError(f"line {number}: unexpected indentation in mapping")
            match = _YAML_KEY_RE.match(text)
            if not match:
                raise ParseError(f"line {number}: expected 'key: value', got {text!r}")
            key, value = match.group(1), match.group(2)
            if key in mapping:
                raise ParseError(f"line {number}: duplicate key {key!r}")
            self.index += 1
            if value is None or value.strip() == "":
                nxt = self._current()
                if nxt is not None and nxt[0] > indent:
                    mapping[key] = self.parse_node(nxt[0])
                elif nxt is not None and nxt[0] == indent and self._is_sequence_item(nxt[1]):
                    mapping[key] = self.parse_sequence(indent)
                else:
                    mapping[key] = None
            else:
                mapping[key] = _yaml_scalar(value)
        return mapping


def parse_yaml(text: str) -> Any:
    """Parse the YAML subset used by this repository.  Raises ParseError."""
    lines: list[list[Any]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ParseError(f"line {number}: tabs are not allowed for indentation")
        stripped = _yaml_strip_comment(raw)
        if not stripped.strip() or stripped.strip() in ("---", "..."):
            continue
        lines.append([len(stripped) - len(stripped.lstrip()), stripped.strip(), number])
    if not lines:
        return None
    parser = _YAMLParser(lines)
    value = parser.parse_node(lines[0][0])
    if parser.index < len(lines):
        number = lines[parser.index][2]
        raise ParseError(f"line {number}: could not parse remainder of document")
    return value


def load_yaml(path: Path) -> Any:
    return parse_yaml(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Minimal device tree helpers
# ---------------------------------------------------------------------------

_PREPROCESSOR_LINE_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:include|define|undef|pragma|if|ifdef|ifndef|elif|else|endif)\b.*$",
    re.MULTILINE,
)
_NODE_START_RE = re.compile(r"(?:([A-Za-z_]\w*)\s*:\s*)?(/|&?[A-Za-z_][\w,.+\-@]*)\s*\{")


#: A double-quoted string, a /* */ comment or a // comment, whichever starts
#: first.  Strings are in the alternation so that a ``display-name = "a//b"``
#: or a brace inside a literal is not mistaken for a comment or a node.
_C_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|/\*.*?\*/|//[^\n]*', re.DOTALL)


def strip_c_comments(text: str) -> str:
    """Remove /* */ and // comments while preserving line structure.

    String literals are matched first and handed back untouched, so a comment
    marker inside one cannot swallow the rest of the line (and, after
    stripping, leave an unbalanced brace behind for the node walker).
    """

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith('"'):
            return token
        if token.startswith("/*"):
            return re.sub(r"[^\n]", " ", token)  # keep the line structure
        return ""

    return _C_TOKEN_RE.sub(_replace, text)


def _match_brace(text: str, start: int) -> int:
    """Return the index just past the brace matching the '{' before start."""
    depth = 1
    index = start
    while index < len(text) and depth:
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        raise ParseError("unbalanced braces in device tree source")
    return index


def iter_dts_nodes(body: str) -> Iterator[tuple[str | None, str, str]]:
    """Yield (label, name, inner_text) for every direct child node of body."""
    position = 0
    while True:
        match = _NODE_START_RE.search(body, position)
        if not match:
            return
        end = _match_brace(body, match.end())
        yield match.group(1), match.group(2), body[match.end() : end - 1]
        position = end


def walk_dts_nodes(body: str) -> Iterator[tuple[str | None, str, str]]:
    """Yield every node (depth-first) in body."""
    for label, name, inner in iter_dts_nodes(body):
        yield label, name, inner
        yield from walk_dts_nodes(inner)


@functools.lru_cache(maxsize=512)
def dts_own_text(inner: str) -> str:
    """Return the node body with all child nodes removed (properties only).

    Memoised: every dts_property() lookup asks for the same node body, so a
    node with n properties would otherwise be re-scanned n times.
    """
    parts: list[str] = []
    position = 0
    while True:
        match = _NODE_START_RE.search(inner, position)
        if not match:
            parts.append(inner[position:])
            return "".join(parts)
        parts.append(inner[position : match.start()])
        position = _match_brace(inner, match.end())


def dts_property(inner: str, name: str) -> str | None:
    """Return the raw value text of property `name` (without the trailing ;)."""
    pattern = re.compile(r"(?<![\w#\-])" + re.escape(name) + r"\s*=\s*(.*?)\s*;", re.DOTALL)
    match = pattern.search(dts_own_text(inner))
    return match.group(1) if match else None


def dts_compatible(inner: str) -> str | None:
    value = dts_property(inner, "compatible")
    if value is None:
        return None
    match = re.search(r'"([^"]*)"', value)
    return match.group(1) if match else None


def dts_int(inner: str, name: str) -> int | None:
    value = dts_property(inner, name)
    if value is None:
        return None
    match = re.fullmatch(r"<\s*(\d+)\s*>", value.strip())
    return int(match.group(1)) if match else None


def dts_phandle(inner: str, name: str) -> str | None:
    value = dts_property(inner, name)
    if value is None:
        return None
    match = re.fullmatch(r"<\s*&([A-Za-z_]\w*)\s*>", value.strip())
    return match.group(1) if match else None


def dts_int_cells(value: str) -> list[int]:
    """Flatten all integers found inside the <...> groups of a property value."""
    numbers: list[int] = []
    for group in re.findall(r"<([^>]*)>", value):
        for token in group.split():
            if re.fullmatch(r"\(?-?\d+\)?", token):
                numbers.append(int(token.strip("()")))
    return numbers


@functools.lru_cache(maxsize=16)
def prepare_dts(text: str) -> str:
    """Strip comments and preprocessor lines so node parsing is reliable.

    Memoised: a single check may prepare the same file's text more than once
    (cornix-layouts.dtsi is walked for transforms and again for position
    maps).  Both argument and result are immutable, so sharing is safe.
    """
    return _PREPROCESSOR_LINE_RE.sub("", strip_c_comments(text))


# ---------------------------------------------------------------------------
# Kconfig fragment helpers
# ---------------------------------------------------------------------------

_CONF_LINE_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
_CONF_UNSET_RE = re.compile(r"^#\s*(CONFIG_[A-Za-z0-9_]+) is not set$")


def parse_conf(text: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Parse a Kconfig fragment.

    Returns (values, conflict_errors, duplicate_warnings).  Values keep the
    last assignment; conflicting duplicates are reported as errors and
    identical duplicates as warnings.
    """
    values: dict[str, str] = {}
    first_line: dict[str, int] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = _CONF_LINE_RE.match(line)
        if match:
            key, value = match.group(1), match.group(2).strip()
        else:
            unset = _CONF_UNSET_RE.match(line)
            if not unset:
                continue
            key, value = unset.group(1), "n"
        if key in values:
            if values[key] != value:
                errors.append(
                    f"line {number}: {key} redefined as {value!r} "
                    f"(line {first_line[key]} sets {values[key]!r})"
                )
            else:
                warnings.append(f"line {number}: {key}={value} duplicates line {first_line[key]}")
        else:
            first_line[key] = number
        values[key] = value
    return values, errors, warnings


# ---------------------------------------------------------------------------
# Check 1: build.yaml
# ---------------------------------------------------------------------------


def board_names_from_data(data: Any, board_yml: Path) -> list[str]:
    """The board names in an already-parsed board.yml (`board_yml` names it)."""
    if not isinstance(data, dict) or not isinstance(data.get("boards"), list):
        raise ParseError(f"{board_yml}: expected a top-level 'boards' list")
    names = []
    for entry in data["boards"]:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ParseError(f"{board_yml}: every board entry needs a 'name'")
        names.append(str(entry["name"]))
    return names


def board_names_from_board_yml(board_yml: Path) -> list[str]:
    return board_names_from_data(load_yaml(board_yml), board_yml)


def local_shield_names(shields_dir: Path) -> set[str]:
    if not shields_dir.is_dir():
        return set()
    return {entry.name for entry in shields_dir.iterdir() if entry.is_dir()}


def local_snippet_names(snippets_dir: Path) -> set[str]:
    """Names of local snippets: directories under snippets/ holding a snippet.yml."""
    if not snippets_dir.is_dir():
        return set()
    return {entry.name for entry in snippets_dir.iterdir() if (entry / "snippet.yml").is_file()}


def split_board_name(board: str) -> tuple[str, str | None]:
    """Split 'name//qualifier' into (name, qualifier)."""
    if "//" in board:
        name, qualifier = board.split("//", 1)
        return name, qualifier
    return board, None


def _tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return str(value).split()


def check_build_yaml(
    build_yaml: Path,
    board_yml: Path,
    shields_dir: Path,
    warnings: list[str],
    snippets_dir: Path | None = None,
) -> list[str]:
    """Validate one build matrix file against the boards/shields/snippets in the repo.

    Artifact names must be unique within the file.  Snippet tokens must be
    one of KNOWN_SNIPPETS or a local snippet found under ``snippets_dir``.
    """
    errors: list[str] = []
    prefix = f"{build_yaml.name}"

    try:
        data = load_yaml(build_yaml)
    except (OSError, ParseError) as exc:
        return [f"{prefix}: cannot parse: {exc}"]
    try:
        local_boards = set(board_names_from_board_yml(board_yml))
    except (OSError, ParseError) as exc:
        return [f"{prefix}: cannot load board list: {exc}"]
    local_shields = local_shield_names(shields_dir)
    known_snippets = set(KNOWN_SNIPPETS)
    if snippets_dir is not None:
        known_snippets |= local_snippet_names(snippets_dir)

    if not isinstance(data, dict):
        return [f"{prefix}: expected a top-level mapping"]

    includes = data.get("include")
    if includes is None:
        includes = []
    if not isinstance(includes, list):
        return [f"{prefix}: 'include' must be a list"]

    def validate_board(where: str, board: str) -> str | None:
        """Return the base board name when it is acceptable."""
        name, qualifier = split_board_name(board)
        if name.startswith(QUALIFIED_BOARD_PREFIXES) and qualifier != BOARD_QUALIFIER.lstrip("/"):
            errors.append(
                f"{where}: board {board!r} must use the Zephyr 4.1 qualified form "
                f"{name}{BOARD_QUALIFIER} (unqualified targets may select CONFIG_SETTINGS_NONE)"
            )
        if name.startswith("cornix_"):
            if name not in local_boards:
                errors.append(f"{where}: board {name!r} is not defined in {board_yml.as_posix()}")
        elif name not in EXTERNAL_BOARDS:
            warnings.append(f"{where}: board {name!r} is not a known local or external board")
            if qualifier is None:
                warnings.append(f"{where}: board {board!r} is not qualified with {BOARD_QUALIFIER}")
        return name

    def validate_shields(where: str, shields: list[str]) -> None:
        for shield in shields:
            if shield in local_shields or shield in EXTERNAL_SHIELDS:
                continue
            errors.append(
                f"{where}: shield {shield!r} is neither in {shields_dir.as_posix()}/ nor in the "
                f"external allowlist {sorted(EXTERNAL_SHIELDS)}"
            )

    def validate_snippets(where: str, snippets: list[str]) -> None:
        for snippet in snippets:
            if snippet not in known_snippets:
                errors.append(f"{where}: unknown snippet {snippet!r} (known: {sorted(known_snippets)})")

    artifacts: dict[str, str] = {}
    for index, entry in enumerate(includes):
        where = f"{prefix}: include[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        board = entry.get("board")
        if not isinstance(board, str) or not board:
            errors.append(f"{where}: missing 'board'")
            continue
        where = f"{where} ({board})"
        base = validate_board(where, board) or ""
        shields = _tokens(entry.get("shield"))
        validate_shields(where, shields)
        validate_snippets(where, _tokens(entry.get("snippet")))

        artifact = entry.get("artifact-name")
        if artifact is None:
            warnings.append(f"{where}: no artifact-name")
        else:
            artifact = str(artifact)
            if artifact in artifacts:
                errors.append(f"{where}: artifact-name {artifact!r} already used by {artifacts[artifact]}")
            artifacts[artifact] = where

        # Check 6: dongle and role combination rules.
        if "cornix_dongle_adapter" in shields and base.startswith("cornix_"):
            errors.append(
                f"{where}: cornix_dongle_adapter is the dongle central role and must not be built "
                f"for a Cornix half; use nice_nano{BOARD_QUALIFIER} or another dongle board"
            )
        if "cornix_dongle_eyelash" in shields and "cornix_dongle_adapter" not in shields:
            errors.append(f"{where}: cornix_dongle_eyelash requires cornix_dongle_adapter in the same build")
        if "settings_reset" in shields and len(shields) > 1:
            errors.append(f"{where}: settings_reset must be the only shield in a reset build")
        if "cornix_indicator" in shields and not base.startswith("cornix_"):
            errors.append(f"{where}: cornix_indicator only applies to Cornix boards (got {board!r})")

        for key in entry:
            if key not in {"board", "shield", "snippet", "artifact-name", "cmake-args"}:
                warnings.append(f"{where}: unexpected key {key!r}")

    if not includes:
        errors.append(f"{prefix}: no build targets defined")
    return errors


def check_build_matrices(root: Path, warnings: list[str]) -> list[str]:
    """Run check_build_yaml over every file in BUILD_MATRIX_FILES."""
    errors: list[str] = []
    for name, required in BUILD_MATRIX_FILES.items():
        path = root / name
        if not path.is_file():
            if required:
                errors.append(f"{name}: required build matrix file is missing")
            else:
                warnings.append(f"{name}: skipped: file does not exist")
            continue
        errors.extend(
            check_build_yaml(path, root / BOARD_DIR / "board.yml", root / SHIELDS_DIR, warnings, root / SNIPPETS_DIR)
        )
    return errors


def check_snippets(root: Path, warnings: list[str]) -> list[str]:
    """Every snippets/<name>/snippet.yml must declare name: <name>."""
    errors: list[str] = []
    snippets_dir = root / SNIPPETS_DIR
    if not snippets_dir.is_dir():
        warnings.append(f"{SNIPPETS_DIR.as_posix()}: skipped: directory does not exist")
        return errors
    for entry in sorted(snippets_dir.iterdir()):
        if not entry.is_dir():
            continue
        prefix = f"{SNIPPETS_DIR.as_posix()}/{entry.name}"
        snippet_yml = entry / "snippet.yml"
        if not snippet_yml.is_file():
            errors.append(f"{prefix}: missing snippet.yml")
            continue
        try:
            data = load_yaml(snippet_yml)
        except (OSError, ParseError) as exc:
            errors.append(f"{prefix}/snippet.yml: cannot parse: {exc}")
            continue
        name = data.get("name") if isinstance(data, dict) else None
        if name != entry.name:
            errors.append(f"{prefix}/snippet.yml: name {name!r} must equal directory name {entry.name!r}")
    return errors


# ---------------------------------------------------------------------------
# Check 2: config/west.yml
# ---------------------------------------------------------------------------


def check_west_manifest(west_yml: Path, warnings: list[str]) -> list[str]:
    """Validate the west manifest structure."""
    errors: list[str] = []
    prefix = west_yml.as_posix()
    try:
        data = load_yaml(west_yml)
    except (OSError, ParseError) as exc:
        return [f"{prefix}: cannot parse: {exc}"]
    manifest = data.get("manifest") if isinstance(data, dict) else None
    if not isinstance(manifest, dict):
        return [f"{prefix}: missing top-level 'manifest' mapping"]

    remotes = manifest.get("remotes") or []
    remote_names: set[str] = set()
    for index, remote in enumerate(remotes):
        if not isinstance(remote, dict) or "name" not in remote:
            errors.append(f"{prefix}: remotes[{index}] needs a 'name'")
            continue
        if "url-base" not in remote:
            errors.append(f"{prefix}: remote {remote['name']!r} has no url-base")
        if remote["name"] in remote_names:
            errors.append(f"{prefix}: duplicate remote {remote['name']!r}")
        remote_names.add(str(remote["name"]))

    default_remote = None
    defaults = manifest.get("defaults")
    if isinstance(defaults, dict):
        default_remote = defaults.get("remote")

    projects = manifest.get("projects") or []
    project_names: set[str] = set()
    zmk_seen = False
    for index, project in enumerate(projects):
        if not isinstance(project, dict) or "name" not in project:
            errors.append(f"{prefix}: projects[{index}] needs a 'name'")
            continue
        name = str(project["name"])
        if name in project_names:
            errors.append(f"{prefix}: duplicate project {name!r}")
        project_names.add(name)
        remote = project.get("remote", default_remote)
        if remote is None and "url" not in project:
            errors.append(f"{prefix}: project {name!r} has no remote or url")
        elif remote is not None and remote not in remote_names:
            errors.append(f"{prefix}: project {name!r} uses undeclared remote {remote!r}")
        if "revision" not in project:
            warnings.append(f"{prefix}: project {name!r} has no pinned revision")
        if name == "zmk":
            zmk_seen = True
            imported = project.get("import")
            import_file = imported.get("file") if isinstance(imported, dict) else imported
            if import_file != "app/west.yml":
                errors.append(f"{prefix}: project 'zmk' must import app/west.yml (got {import_file!r})")
    if not zmk_seen:
        errors.append(f"{prefix}: no 'zmk' project in manifest")

    self_section = manifest.get("self")
    self_path = self_section.get("path") if isinstance(self_section, dict) else None
    if self_path != "config":
        errors.append(f"{prefix}: manifest.self.path must be 'config' (got {self_path!r})")
    return errors


# ---------------------------------------------------------------------------
# Check 3: Kconfig fragments
# ---------------------------------------------------------------------------


def defconfig_role(path: Path) -> str | None:
    """Map cornix_left_nrf52840_zmk_defconfig -> 'central' etc."""
    stem = path.name.removesuffix("_defconfig")
    stem = re.sub(r"_nrf52840(_zmk)?$", "", stem)
    return BOARD_ROLES.get(stem)


def check_conf_file(
    path: Path,
    is_board_defconfig: bool,
    warnings: list[str],
    display: str | None = None,
) -> list[str]:
    """Validate a single defconfig / .conf fragment."""
    errors: list[str] = []
    prefix = display or path.as_posix()
    try:
        values, conflicts, duplicates = parse_conf(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"{prefix}: cannot read: {exc}"]
    errors.extend(f"{prefix}: {item}" for item in conflicts)
    warnings.extend(f"{prefix}: {item}" for item in duplicates)

    for key, forbidden in FORBIDDEN_CONF_VALUES.items():
        if values.get(key) == forbidden:
            errors.append(f"{prefix}: {key}={forbidden} is forbidden (settings must use NVS)")

    if is_board_defconfig:
        for key, expected in REQUIRED_DEFCONFIG_VALUES.items():
            if values.get(key) != expected:
                errors.append(f"{prefix}: expected {key}={expected}, got {values.get(key)!r}")
        role = defconfig_role(path)
        if role is None:
            warnings.append(f"{prefix}: unknown board role for defconfig name")
        else:
            if values.get("CONFIG_ZMK_SPLIT") != "y":
                errors.append(f"{prefix}: expected CONFIG_ZMK_SPLIT=y for a split half")
            central = values.get("CONFIG_ZMK_SPLIT_ROLE_CENTRAL")
            if role == "central" and central != "y":
                errors.append(f"{prefix}: central half must set CONFIG_ZMK_SPLIT_ROLE_CENTRAL=y")
            if role == "peripheral" and central == "y":
                errors.append(f"{prefix}: peripheral half must not set CONFIG_ZMK_SPLIT_ROLE_CENTRAL=y")
    else:
        shield = path.stem
        for key, expected in SHIELD_CONF_EXPECTATIONS.get(shield, {}).items():
            if values.get(key) != expected:
                errors.append(f"{prefix}: expected {key}={expected}, got {values.get(key)!r}")
    return errors


def check_kconfig_fragments(root: Path, warnings: list[str]) -> list[str]:
    """Run check_conf_file over every defconfig and .conf in boards/ and config/."""
    errors: list[str] = []
    board_dir = root / BOARD_DIR
    defconfigs = sorted(board_dir.glob("*_defconfig"))
    if not defconfigs:
        errors.append(f"{BOARD_DIR.as_posix()}: no *_defconfig files found")
    for path in defconfigs:
        errors.extend(check_conf_file(path, True, warnings, path.relative_to(root).as_posix()))
    conf_files = sorted((root / "boards").rglob("*.conf")) + sorted((root / CONFIG_DIR).rglob("*.conf"))
    for path in conf_files:
        errors.extend(check_conf_file(path, False, warnings, path.relative_to(root).as_posix()))
    return errors


# ---------------------------------------------------------------------------
# Check 4a: matrix transforms and physical layouts
# ---------------------------------------------------------------------------

_RC_RE = re.compile(r"RC\(\s*(\d+)\s*,\s*(\d+)\s*\)")


def parse_layouts(text: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """Return (transforms, layouts, errors) from a layouts .dtsi text."""
    body = prepare_dts(text)
    transforms: dict[str, dict[str, Any]] = {}
    layouts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for label, name, inner in walk_dts_nodes(body):
        compatible = dts_compatible(inner)
        ident = label or name
        if compatible == "zmk,matrix-transform":
            map_value = dts_property(inner, "map") or ""
            entries = [(int(row), int(col)) for row, col in _RC_RE.findall(map_value)]
            tokens = [t for t in re.sub(r"[<>]", " ", _RC_RE.sub(" ", map_value)).split() if t]
            if tokens:
                errors.append(f"transform {ident}: non-RC tokens in map: {tokens[:5]}")
            transforms[ident] = {
                "rows": dts_int(inner, "rows"),
                "columns": dts_int(inner, "columns"),
                "entries": entries,
                "count": len(entries),
            }
        elif compatible == "zmk,physical-layout":
            keys_value = dts_property(inner, "keys") or ""
            layouts[ident] = {
                "transform": dts_phandle(inner, "transform"),
                "count": len(re.findall(r"&key_physical_attrs\b", keys_value)),
            }
    return transforms, layouts, errors


def check_layouts_dtsi(path: Path, warnings: list[str]) -> list[str]:
    """Cross-check transforms, physical layouts and position maps."""
    errors: list[str] = []
    prefix = path.name
    try:
        text = path.read_text(encoding="utf-8")
        transforms, layouts, parse_errors = parse_layouts(text)
    except (OSError, ParseError) as exc:
        return [f"{prefix}: cannot parse: {exc}"]
    errors.extend(f"{prefix}: {item}" for item in parse_errors)

    for ident, transform in transforms.items():
        rows, columns = transform["rows"], transform["columns"]
        if rows is None or columns is None:
            errors.append(f"{prefix}: transform {ident} needs integer rows and columns")
            continue
        if transform["count"] == 0:
            errors.append(f"{prefix}: transform {ident} has an empty map")
        seen: set[tuple[int, int]] = set()
        for row, col in transform["entries"]:
            if row >= rows or col >= columns:
                errors.append(
                    f"{prefix}: transform {ident} RC({row},{col}) is outside rows={rows} columns={columns}"
                )
            if (row, col) in seen:
                errors.append(f"{prefix}: transform {ident} maps RC({row},{col}) twice")
            seen.add((row, col))

    if not layouts:
        errors.append(f"{prefix}: no zmk,physical-layout nodes found")
    for ident, layout in layouts.items():
        if layout["count"] == 0:
            errors.append(f"{prefix}: physical layout {ident} has no keys")
        transform = layout["transform"]
        if transform is None:
            errors.append(f"{prefix}: physical layout {ident} has no transform")
        elif transform not in transforms:
            errors.append(f"{prefix}: physical layout {ident} references unknown transform &{transform}")
        elif transforms[transform]["count"] != layout["count"]:
            errors.append(
                f"{prefix}: physical layout {ident} has {layout['count']} keys but transform "
                f"{transform} maps {transforms[transform]['count']} positions"
            )

    # Position maps must list every key of the layout exactly once.
    body = prepare_dts(text)
    for _label, _name, inner in walk_dts_nodes(body):
        if dts_compatible(inner) != "zmk,physical-layout-position-map":
            continue
        for child_label, child_name, child in iter_dts_nodes(inner):
            ident = child_label or child_name
            layout_ref = dts_phandle(child, "physical-layout")
            positions = dts_int_cells(dts_property(child, "positions") or "")
            if layout_ref not in layouts:
                errors.append(f"{prefix}: position map {ident} references unknown layout &{layout_ref}")
                continue
            expected = layouts[layout_ref]["count"]
            if len(positions) != expected:
                errors.append(
                    f"{prefix}: position map {ident} has {len(positions)} positions, layout "
                    f"{layout_ref} has {expected} keys"
                )
            if len(set(positions)) != len(positions) or any(p >= expected for p in positions):
                errors.append(f"{prefix}: position map {ident} must list 0..{expected - 1} exactly once")
    return errors


# ---------------------------------------------------------------------------
# Check 4b: keymaps
# ---------------------------------------------------------------------------

_DEFINE_OBJECT_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(?:[ \t]+(.*?))?[ \t]*$", re.MULTILINE)
_DEFINE_FUNCTION_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)\(", re.MULTILINE)
_LOCAL_INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]+"([^"]+)"', re.MULTILINE)
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")


def collect_defines(text: str, base_dir: Path | None) -> tuple[dict[str, str], set[str]]:
    """Collect object-like and function-like #defines from text and local includes."""
    objects: dict[str, str] = {}
    functions: set[str] = set()
    text = strip_c_comments(text)
    if base_dir is not None:
        for include in _LOCAL_INCLUDE_RE.findall(text):
            included = base_dir / include
            if included.is_file():
                sub_objects, sub_functions = collect_defines(included.read_text(encoding="utf-8"), included.parent)
                objects.update(sub_objects)
                functions |= sub_functions
    functions |= set(_DEFINE_FUNCTION_RE.findall(text))
    for match in _DEFINE_OBJECT_RE.finditer(text):
        name = match.group(1)
        if name in functions:
            continue
        objects[name] = (match.group(2) or "").strip()
    return objects, functions


def expand_defines(text: str, objects: dict[str, str], active: frozenset[str] = frozenset()) -> str:
    """Textually expand object-like macros.

    Like the C preprocessor, a macro is not re-expanded inside its own
    expansion (so ``#define HOME &kp HOME`` expands exactly once).
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(0)
        if name in active or name not in objects:
            return name
        return expand_defines(objects[name], objects, active | {name})

    return _IDENT_RE.sub(replace, text)


def count_bindings(value: str) -> int:
    """Count behaviour references (& tokens) in a bindings property value."""
    return len(re.findall(r"&[A-Za-z_]\w*", value))


def _split_top_level_args(text: str) -> list[str]:
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth -= 1
        if char == "," and depth == 0:
            args.append("".join(current))
            current = []
        else:
            current.append(char)
    args.append("".join(current))
    return args


def parse_keymap_layers(text: str, base_dir: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract layers from a keymap.

    Returns (layers, info).  Each layer is {"name", "bindings", "count"}.
    ``info["skipped"]`` carries a reason when the keymap cannot be parsed
    reliably; ``info["layer_refs"]`` and ``info["positions"]`` collect layer
    indices / key positions referenced by the keymap for range checks.
    """
    objects, functions = collect_defines(text, base_dir)
    clean = strip_c_comments(text)
    info: dict[str, Any] = {"skipped": None, "layer_refs": [], "positions": [], "defines": objects}
    layers: list[dict[str, Any]] = []

    def register(bindings: str, name: str) -> None:
        used_functions = sorted(f for f in functions if re.search(r"\b" + re.escape(f) + r"\s*\(", bindings))
        if used_functions:
            info["skipped"] = f"layer {name!r} uses function-like macro(s) {used_functions}"
        expanded = expand_defines(bindings, objects)
        layers.append({"name": name, "bindings": expanded, "count": count_bindings(expanded)})
        for behavior, arg in re.findall(r"&(" + "|".join(sorted(LAYER_BEHAVIORS)) + r")\s+(\S+)", expanded):
            if re.fullmatch(r"\d+", arg):
                info["layer_refs"].append((name, behavior, int(arg)))

    body = prepare_dts(clean)
    keymap_inner = None
    for _label, _name, inner in walk_dts_nodes(body):
        if dts_compatible(inner) == "zmk,keymap":
            keymap_inner = inner
            break
    if keymap_inner is not None:
        for label, name, inner in iter_dts_nodes(keymap_inner):
            bindings = dts_property(inner, "bindings")
            if bindings is None:
                info["skipped"] = f"layer {label or name!r} has no bindings property"
                continue
            register(bindings, label or name)
    else:
        # zmk-helpers form: ZMK_LAYER(name, bindings[, sensors])
        position = 0
        while (match := re.search(r"\bZMK_LAYER\s*\(", clean[position:])) is not None:
            start = position + match.end()
            depth = 1
            index = start
            while index < len(clean) and depth:
                depth += (clean[index] == "(") - (clean[index] == ")")
                index += 1
            args = _split_top_level_args(clean[start : index - 1])
            if len(args) < 2:
                info["skipped"] = "ZMK_LAYER macro without a bindings argument"
            else:
                register(args[1], args[0].strip())
            position = index
        if not layers and info["skipped"] is None:
            info["skipped"] = "no zmk,keymap node and no ZMK_LAYER macros found"

    # Key positions referenced by hold-tap / combo nodes.
    for _label, name, inner in walk_dts_nodes(body):
        for prop in ("hold-trigger-key-positions", "key-positions"):
            value = dts_property(inner, prop)
            if value is not None:
                for number in dts_int_cells(expand_defines(value, objects)):
                    info["positions"].append((name, prop, number))
        if dts_compatible(inner) == "zmk,conditional-layers":
            for _cl, cname, cinner in iter_dts_nodes(inner):
                for prop in ("if-layers", "then-layer"):
                    value = dts_property(cinner, prop)
                    if value is not None:
                        for number in dts_int_cells(expand_defines(value, objects)):
                            info["layer_refs"].append((cname, prop, number))
    return layers, info


def json_layout_key_count(path: Path, layout: str) -> int:
    data = load_json_lenient(path.read_text(encoding="utf-8"))[0]
    return len(data["layouts"][layout]["layout"])


def check_keymap(
    keymap: Path,
    expected_keys: int | None,
    warnings: list[str],
    display: str | None = None,
) -> list[str]:
    """Validate one keymap: equal layer sizes, key count, index ranges."""
    errors: list[str] = []
    prefix = display or keymap.as_posix()
    try:
        layers, info = parse_keymap_layers(keymap.read_text(encoding="utf-8"), keymap.parent)
    except (OSError, ParseError) as exc:
        return [f"{prefix}: cannot parse: {exc}"]
    if info["skipped"]:
        warnings.append(f"{prefix}: skipped: {info['skipped']}")
        return errors
    if not layers:
        return [f"{prefix}: no layers found"]

    counts = {layer["name"]: layer["count"] for layer in layers}
    distinct = sorted(set(counts.values()))
    if len(distinct) > 1:
        errors.append(f"{prefix}: layers have different binding counts: {counts}")
    if expected_keys is not None:
        for name, count in counts.items():
            if count != expected_keys:
                errors.append(f"{prefix}: layer {name!r} has {count} bindings, layout expects {expected_keys}")
        for node, prop, position in info["positions"]:
            if position >= expected_keys:
                errors.append(f"{prefix}: {node} {prop} references position {position} >= {expected_keys}")
        for macro, value in info["defines"].items():
            if re.fullmatch(r"\d+", value) and int(value) >= expected_keys and re.fullmatch(r"[LR][TMBHP]\d", macro):
                warnings.append(f"{prefix}: position macro {macro}={value} is outside the {expected_keys}-key layout")
    layer_count = len(layers)
    for node, prop, index in info["layer_refs"]:
        if index >= layer_count:
            errors.append(f"{prefix}: {node} {prop} references layer {index} but only {layer_count} layers exist")
    return errors


def check_keymaps(root: Path, warnings: list[str]) -> list[str]:
    """Run check_keymap for every keymap listed in KEYMAP_TARGETS."""
    errors: list[str] = []
    layouts_path = root / LAYOUTS_DTSI
    try:
        _transforms, layouts, _ = parse_layouts(layouts_path.read_text(encoding="utf-8"))
    except (OSError, ParseError) as exc:
        return [f"{LAYOUTS_DTSI.as_posix()}: cannot parse: {exc}"]

    for relative, target in KEYMAP_TARGETS.items():
        keymap = root / relative
        if not keymap.is_file():
            errors.append(f"{relative}: keymap listed in KEYMAP_TARGETS is missing")
            continue
        expected: int | None = None
        if target[0] == "dtsi":
            layout = layouts.get(target[1])
            if layout is None:
                errors.append(f"{relative}: physical layout {target[1]!r} not found in {LAYOUTS_DTSI.name}")
            else:
                expected = layout["count"]
        elif target[0] == "json":
            try:
                expected = json_layout_key_count(root / target[1], target[2])
            except (OSError, KeyError, ValueError) as exc:
                errors.append(f"{relative}: cannot read layout {target[2]!r} from {target[1]}: {exc}")
        errors.extend(check_keymap(keymap, expected, warnings, relative))
    return errors


# ---------------------------------------------------------------------------
# Check 5: metadata (JSON, *.zmk.yml, shield directories)
# ---------------------------------------------------------------------------


def load_json_lenient(text: str) -> tuple[Any, bool]:
    """Parse JSON; tolerate full-line // comments.  Returns (data, had_comments)."""
    try:
        return json.loads(text), False
    except json.JSONDecodeError:
        stripped = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
        return json.loads(stripped), True


def check_json_files(root: Path, warnings: list[str]) -> list[str]:
    """Every metadata JSON file must parse (comment lines are a warning)."""
    errors: list[str] = []
    files = sorted((root / BOARD_DIR / "metadata").glob("*.json")) + sorted((root / CONFIG_DIR).glob("*.json"))
    if not files:
        errors.append("no metadata JSON files found")
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            data, commented = load_json_lenient(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{relative}: invalid JSON: {exc}")
            continue
        if commented:
            warnings.append(f"{relative}: metadata-json-comments: parses only after dropping // comment lines")
        layouts = data.get("layouts") if isinstance(data, dict) else None
        if isinstance(layouts, dict):
            for name, layout in layouts.items():
                keys = layout.get("layout") if isinstance(layout, dict) else None
                if not isinstance(keys, list):
                    errors.append(f"{relative}: layout {name!r} has no 'layout' key list")
                    continue
                digits = re.search(r"(\d+)$", name)
                if digits and int(digits.group(1)) != len(keys):
                    warnings.append(
                        f"{relative}: metadata-layout-name: layout {name!r} has {len(keys)} keys"
                    )
    return errors


def check_board_metadata(root: Path, warnings: list[str]) -> list[str]:
    """board.yml, cornix.zmk.yml and per-board files must agree."""
    errors: list[str] = []
    board_dir = root / BOARD_DIR
    board_yml = board_dir / "board.yml"
    try:
        data = load_yaml(board_yml)
        names = board_names_from_data(data, board_yml)
    except (OSError, ParseError) as exc:
        return [f"{board_yml.relative_to(root).as_posix()}: cannot parse: {exc}"]
    for entry in data["boards"]:
        name = str(entry["name"])
        variants = {
            str(variant.get("name"))
            for soc in entry.get("socs") or []
            if isinstance(soc, dict)
            for variant in soc.get("variants") or []
            if isinstance(variant, dict)
        }
        if BOARD_QUALIFIER.lstrip("/") not in variants:
            errors.append(f"board.yml: board {name!r} has no '{BOARD_QUALIFIER.lstrip('/')}' variant")
        for required in (f"Kconfig.{name}", f"{name}_defconfig", f"{name}.dts"):
            if not (board_dir / required).is_file():
                errors.append(f"{BOARD_DIR.as_posix()}: board {name!r} is missing {required}")

    zmk_yml = board_dir / "cornix.zmk.yml"
    try:
        meta = load_yaml(zmk_yml)
    except (OSError, ParseError) as exc:
        return errors + [f"{zmk_yml.relative_to(root).as_posix()}: cannot parse: {exc}"]
    if not isinstance(meta, dict):
        return errors + [f"{zmk_yml.name}: expected a mapping"]
    if meta.get("type") != "board":
        errors.append(f"{zmk_yml.name}: type must be 'board' (got {meta.get('type')!r})")
    siblings = meta.get("siblings") or []
    for sibling in siblings:
        if sibling not in names:
            errors.append(f"{zmk_yml.name}: sibling {sibling!r} is not defined in board.yml")
    for name in names:
        if name not in siblings:
            warnings.append(f"{zmk_yml.name}: board {name!r} from board.yml is not listed as a sibling")
    return errors


REQUIRED_SHIELD_FILES = ("Kconfig.shield", "Kconfig.defconfig")


def check_shield_dirs(shields_dir: Path, warnings: list[str]) -> list[str]:
    """Every shield directory must carry the files ZMK expects."""
    errors: list[str] = []
    names = sorted(local_shield_names(shields_dir))
    if not names:
        return [f"{shields_dir.as_posix()}: no shield directories found"]
    for name in names:
        shield_dir = shields_dir / name
        prefix = f"{shields_dir.name}/{name}"
        for required in REQUIRED_SHIELD_FILES + (f"{name}.overlay",):
            if not (shield_dir / required).is_file():
                errors.append(f"{prefix}: missing {required}")

        kconfig = shield_dir / "Kconfig.shield"
        if kconfig.is_file():
            text = kconfig.read_text(encoding="utf-8")
            symbol = f"SHIELD_{name.upper()}"
            if not re.search(r"^\s*config\s+" + re.escape(symbol) + r"\b", text, re.MULTILINE):
                errors.append(f"{prefix}: Kconfig.shield must define 'config {symbol}'")
            match = re.search(r"shields_list_contains,([^)]*)\)", text)
            if not match:
                errors.append(f"{prefix}: Kconfig.shield must use $(shields_list_contains,{name})")
            elif match.group(1) != name:
                if match.group(1).strip() == name:
                    warnings.append(f"{prefix}: shield-kconfig-style: whitespace inside shields_list_contains argument")
                else:
                    errors.append(f"{prefix}: shields_list_contains argument {match.group(1)!r} != {name!r}")

        metadata_files = [p for p in (shield_dir / "shield.yml", shield_dir / f"{name}.zmk.yml") if p.is_file()]
        if not metadata_files:
            errors.append(f"{prefix}: missing shield.yml or {name}.zmk.yml")
        for meta_path in metadata_files:
            try:
                meta = load_yaml(meta_path)
            except (OSError, ParseError) as exc:
                errors.append(f"{prefix}/{meta_path.name}: cannot parse: {exc}")
                continue
            if not isinstance(meta, dict):
                errors.append(f"{prefix}/{meta_path.name}: expected a mapping")
                continue
            if meta.get("id") != name:
                errors.append(f"{prefix}/{meta_path.name}: id {meta.get('id')!r} must equal directory name {name!r}")
            if meta.get("type") != "shield":
                errors.append(f"{prefix}/{meta_path.name}: type must be 'shield' (got {meta.get('type')!r})")
            for sibling in meta.get("siblings") or []:
                if sibling not in names:
                    warnings.append(
                        f"{prefix}/{meta_path.name}: shield-metadata-siblings: sibling {sibling!r} "
                        f"is not a shield directory"
                    )
    return errors


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

Check = tuple[str, Callable[[Path, list[str]], list[str]]]

CHECKS: list[Check] = [
    ("build-matrix", check_build_matrices),
    ("snippets", check_snippets),
    ("west.yml", lambda root, w: check_west_manifest(root / CONFIG_DIR / "west.yml", w)),
    ("kconfig", check_kconfig_fragments),
    ("layouts", lambda root, w: check_layouts_dtsi(root / LAYOUTS_DTSI, w)),
    ("keymaps", check_keymaps),
    ("json", check_json_files),
    ("board-metadata", check_board_metadata),
    ("shields", lambda root, w: check_shield_dirs(root / SHIELDS_DIR, w)),
]


def run_all_checks(root: Path = ROOT) -> dict[str, dict[str, list[str]]]:
    """Run every registered check against root; return {name: {errors, warnings}}."""
    results: dict[str, dict[str, list[str]]] = {}
    for name, function in CHECKS:
        warnings: list[str] = []
        try:
            errors = function(root, warnings)
        except Exception as exc:  # pragma: no cover - diagnostic boundary
            errors = [f"{name}: checker crashed: {type(exc).__name__}: {exc}"]
        results[name] = {"errors": errors, "warnings": warnings}
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--json", action="store_true", help="print results as JSON")
    parser.add_argument("--quiet", action="store_true", help="only print errors and the summary")
    args = parser.parse_args(argv)

    results = run_all_checks(args.root.resolve())
    error_count = sum(len(r["errors"]) for r in results.values())
    warning_count = sum(len(r["warnings"]) for r in results.values())

    if args.json:
        payload = {
            "ok": error_count == 0,
            "errors": error_count,
            "warnings": warning_count,
            "checks": results,
        }
        print(json.dumps(payload, indent=2))
        return 0 if error_count == 0 else 1

    for name, result in results.items():
        status = "FAIL" if result["errors"] else "ok"
        if not args.quiet or result["errors"]:
            print(f"[{status}] {name}")
        for error in result["errors"]:
            print(f"  ERROR: {error}", file=sys.stderr)
        if not args.quiet:
            for warning in result["warnings"]:
                print(f"  WARNING: {warning}")

    if error_count:
        print(f"Config validation failed with {error_count} error(s), {warning_count} warning(s).", file=sys.stderr)
        return 1
    print(f"Config validation passed: {len(results)} checks, {warning_count} warning(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
