"""Unit tests for scripts/cheatsheet.py (stdlib unittest only).

Run with:  python3 -m unittest discover -s tests/cheatsheet -v

Every case generates the sheet from the *real* config/cornix.keymap into a
temporary file - the checked-in config/cheatsheet.html is never touched.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import cheatsheet as cs  # noqa: E402

KEYMAP = REPO_ROOT / "config/cornix.keymap"
LAYOUT = REPO_ROOT / "boards/jzf/cornix/cornix-layouts.dtsi"

#: Elements that never have a closing tag (HTML void elements + SVG shapes we
#: always self-close).
VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
     "meta", "param", "source", "track", "wbr"}
)


class Collector(HTMLParser):
    """Parses the sheet and records enough structure to assert on it."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        #: layer display name -> number of `<g class="key ...">` groups
        self.keys_per_layer: dict[str, int] = {}
        #: layer display name -> [key label text]
        self.labels: dict[str, list[str]] = {}
        self.layer_order: list[str] = []
        self.footer_attrs: dict[str, str] = {}
        self.title = ""
        self._layer: str | None = None
        self._depth_of_layer = 0
        self._text_class: str | None = None
        self._in_title = False

    # -- helpers ----------------------------------------------------------
    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        classes = a.get("class", "").split()
        if tag == "title" and not self.stack.count("g"):
            self._in_title = True
        if tag == "section" and "layer" in classes:
            self._layer = a.get("data-layer", "?")
            self._depth_of_layer = len(self.stack)
            self.layer_order.append(self._layer)
            self.keys_per_layer.setdefault(self._layer, 0)
            self.labels.setdefault(self._layer, [])
        if tag == "g" and "key" in classes and self._layer:
            self.keys_per_layer[self._layer] += 1
        if tag == "text" and "kl" in classes:
            self._text_class = self._layer
        if tag == "footer":
            self.footer_attrs = a

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs)
        if tag == "title":
            self._in_title = False

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"</{tag}> with nothing open")
            return
        if self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.stack[-1]}>")
            return
        self.stack.pop()
        if tag == "title":
            self._in_title = False
        if tag == "text":
            self._text_class = None
        if tag == "section" and self._layer and len(self.stack) == self._depth_of_layer:
            self._layer = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._text_class is not None and data.strip():
            self.labels[self._text_class].append(data.strip())


def render() -> tuple[str, Collector]:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "cheatsheet.html"
        with contextlib.redirect_stdout(io.StringIO()):
            self_rc = cs.main(["--keymap", str(KEYMAP), "--layout", str(LAYOUT),
                               "-o", str(out)])
        assert self_rc == 0, f"generator exited {self_rc}"
        text = out.read_text(encoding="utf-8")
    parser = Collector()
    parser.feed(text)
    parser.close()
    return text, parser


class CheatSheetTest(unittest.TestCase):
    html: str
    doc: Collector

    @classmethod
    def setUpClass(cls) -> None:
        cls.html, cls.doc = render()

    # -- structure --------------------------------------------------------
    def test_parses_and_is_balanced(self):
        self.assertEqual([], self.doc.errors)
        self.assertEqual([], self.doc.stack, "unclosed tags left over")

    def test_title(self):
        self.assertEqual("Cornix Cheat Sheet", self.doc.title.strip())

    def test_six_layer_sections(self):
        self.assertEqual(list(cs.DRAWN_LAYERS), self.doc.layer_order)
        self.assertEqual(6, len(self.doc.layer_order))
        self.assertIn("Conn", self.doc.layer_order)

    def test_fifty_keys_per_layer(self):
        for name in cs.DRAWN_LAYERS:
            self.assertEqual(50, self.doc.keys_per_layer[name],
                             f"{name} does not have 50 keys")

    def test_every_position_number_is_drawn(self):
        # the position numbers live in their own <text class="kn"> elements
        for name in cs.DRAWN_LAYERS:
            for position in range(50):
                self.assertIn(f'>{position}</text>', self.html,
                              f"position {position} missing")

    # -- content ----------------------------------------------------------
    def test_known_labels(self):
        base = self.doc.labels["Base"]
        for wanted in ("Q", "⇧", "Ctrl", "Num", "Fn3", "Space", "↑"):
            self.assertIn(wanted, base, f"Base is missing {wanted!r}")
        # position 41 is the one key that differs per OS
        self.assertTrue(any(l.startswith("Cmd/") for l in base) or "Cmd/" in base,
                        "Base has no Cmd/Ctrl split label")
        self.assertIn("[", self.doc.labels["Num"])
        self.assertIn("{", self.doc.labels["Num"])
        self.assertIn("F1", self.doc.labels["Fn2"])
        self.assertIn("Home", self.doc.labels["Fn2"])
        self.assertIn("左クリック", self.doc.labels["Fn3"])
        self.assertIn("Win層", self.doc.labels["Fn3"])
        # 2026-09-22: the connection keys live on Conn (hold 45 + 46) only.
        for wanted in ("USB へ", "BLE へ", "BT0", "BT1", "BT2"):
            self.assertIn(wanted, self.doc.labels["Conn"], f"Conn is missing {wanted!r}")
            self.assertNotIn(wanted, self.doc.labels["Fn2"], f"Fn2 still shows {wanted!r}")
            self.assertNotIn(wanted, self.doc.labels["Fn3"], f"Fn3 still shows {wanted!r}")

    def test_conn_chord_is_explained(self):
        # The legend must say the chord is both thumb Fn keys held together,
        # and the old single-key chords must be gone from the sheet.
        self.assertIn("<kbd>45</kbd>+<kbd>46</kbd>", self.html)
        self.assertIn("conditional_layers", self.html)
        for stale in ("<kbd>45</kbd>+<kbd>24</kbd>", "<kbd>45</kbd>+<kbd>38</kbd>",
                      "<kbd>45</kbd>+<kbd>11</kbd>", "<kbd>46</kbd>+<kbd>12</kbd>",
                      "<kbd>45</kbd>+<kbd>7</kbd>", "<kbd>45</kbd>+<kbd>29</kbd>", "Fn4"):
            self.assertNotIn(stale, self.html, f"stale chord {stale!r} still on the sheet")

    def test_app_switch_documented(self):
        self.assertIn("Alt+Tab", self.html)
        self.assertIn("Cmd+Tab", self.html)
        self.assertIn("Alt+Tab", " ".join(self.doc.labels["Win"]),
                      "the Win layer diagram does not label position 0")

    def test_mentions_the_skipped_layers(self):
        for name in cs.SKIPPED_LAYERS:
            self.assertIn(name, self.html)

    def test_self_contained(self):
        for forbidden in ("http://", "https://", "<script", "src=", "@import"):
            self.assertNotIn(forbidden, self.html.replace(
                'xmlns="http://www.w3.org/2000/svg"', ""),
                f"{forbidden!r} would make the sheet non self-contained")

    def test_dark_mode_and_viewport(self):
        self.assertIn("prefers-color-scheme", self.html)
        self.assertIn("width=device-width", self.html)

    def test_size_budget(self):
        self.assertLess(len(self.html.encode("utf-8")), 150_000)

    # -- staleness --------------------------------------------------------
    def test_footer_hash_matches_the_keymap(self):
        digest = hashlib.sha256(KEYMAP.read_bytes()).hexdigest()
        self.assertEqual(digest, self.doc.footer_attrs.get("data-keymap-sha256"))
        self.assertIn(digest, self.html)


class LayoutTest(unittest.TestCase):
    def test_layout_50_is_read_from_the_dtsi(self):
        keys = cs.parse_layout(LAYOUT)
        self.assertEqual(50, len(keys))
        self.assertEqual((100, 100, 0, 50), (keys[0].w, keys[0].h, keys[0].x, keys[0].y))
        # column stagger: the middle columns sit higher than the outer ones
        self.assertLess(keys[3].y, keys[0].y)
        # the thumb cluster is the only rotated part
        rotated = [i for i, k in enumerate(keys) if k.r]
        self.assertEqual([42, 43, 44, 45], rotated)


if __name__ == "__main__":
    unittest.main()
