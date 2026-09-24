"""Unit tests for scripts/remap.py (stdlib unittest only).

Run with:  python3 -m unittest discover -s tests/remap -v

Every test works on a temporary copy of the real keymap, config/cornix.keymap,
so the tests exercise exactly the file the CLI is meant to edit.
"""

from __future__ import annotations

import io
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_config as cc  # noqa: E402
import remap  # noqa: E402

KEYMAP = REPO_ROOT / "config/cornix.keymap"
NOTES = REPO_ROOT / "config/keymap-notes.md"


def run(argv: list[str], keymap: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stderr(err):
        status = remap.main(["--keymap", str(keymap), *argv], out)
    return status, out.getvalue(), err.getvalue()


def layer_blocks(text: str) -> dict[str, str]:
    """Every `layer_N { ... };` node of a keymap, by node name."""
    blocks: dict[str, str] = {}
    for match in re.finditer(r"^( {8})(\w+) \{\n(.*?)^\1\};$", text, re.S | re.M):
        blocks[match.group(2)] = match.group(3)
    return blocks


class TempKeymapTest(unittest.TestCase):
    """Base class: a writable copy of config/cornix.keymap per test."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keymap = Path(self.tmp.name) / "cornix.keymap"
        shutil.copy(KEYMAP, self.keymap)
        self.original = self.keymap.read_text(encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        return run(list(argv), self.keymap)

    def bindings(self, layer: str) -> list[str]:
        return remap.parse(self.keymap).find(layer).bindings

    def assertUnchanged(self) -> None:
        self.assertEqual(self.keymap.read_text(encoding="utf-8"), self.original)


class ParseTest(TempKeymapTest):
    def test_layers_positions_and_behaviors(self) -> None:
        keymap = remap.parse(self.keymap)
        self.assertEqual(
            [layer.display_name for layer in keymap.layers],
            ["Base", "Win", "Num", "Fn2", "Fn3", "Conn", "MouseSlow", "MouseFast"],
        )
        for layer in keymap.layers:
            with self.subTest(layer=layer.display_name):
                self.assertEqual(len(layer.bindings), remap.KEY_COUNT)
                self.assertEqual(layer.node_name, f"layer_{layer.index}")
        # The label defined in the keymap's own behaviors node is accepted.
        self.assertIn("inc_dec_msc", keymap.behaviors)

    def test_layers_can_be_addressed_by_name_node_name_or_index(self) -> None:
        keymap = remap.parse(self.keymap)
        for wanted in ("Win", "win", "layer_1", "1"):
            with self.subTest(wanted=wanted):
                self.assertEqual(keymap.find(wanted).index, 1)

    def test_comments_do_not_confuse_the_parser(self) -> None:
        # The header comment mentions `bindings`, `display-name` and braces.
        keymap = remap.parse(self.keymap)
        self.assertEqual(keymap.layers[0].bindings[0], "&kp TAB")
        self.assertGreater(keymap.layers[0].body_span[0], self.original.index("layer_0 {"))


class ShowTest(TempKeymapTest):
    def test_base_grid_matches_the_position_map_in_keymap_notes(self) -> None:
        status, out, _ = self.run_cli("show", "--layer", "Base")
        self.assertEqual(status, 0)
        grid = out.splitlines()[1:]  # drop the "0 Base (layer_0):" heading

        block = re.search(r"## Position map.*?```\n(.*?)```", NOTES.read_text(encoding="utf-8"), re.S)
        self.assertIsNotNone(block, "config/keymap-notes.md has no position map block")
        self.assertEqual(grid, block.group(1).splitlines())

    def test_labels(self) -> None:
        _status, out, _ = self.run_cli("show", "--layer", "Base")
        self.assertIn(" 0 TAB", out)
        self.assertIn("42 Num", out)  # &mo 2 -> the layer's display name
        self.assertIn("30 MUTE 31 MCLK", out)  # the centre pair of row 2
        _status, out, _ = self.run_cli("show", "--layer", "Fn3")
        self.assertIn(" 0 TG1", out)  # &tog 1
        self.assertIn("37 -", out)  # &none: BT_CLR left Fn3 for the Conn layer on 2026-09-22
        self.assertNotIn("USB", out)  # ... and so did &out OUT_USB / OUT_BLE
        self.assertIn(" 1 -", out)  # &none
        # The mouse lives on Fn3 since 2026-09-19: IJKL move the pointer,
        # W/E are the two speed layers and the left thumb clicks.
        self.assertIn(" 8 MOVE_UP", out)
        self.assertIn(" 2 MouseSlow", out)  # &mo 6 -> the layer's display name
        self.assertIn(" 3 MouseFast", out)  # &mo 7
        self.assertIn("41 LCLK", out)
        self.assertIn("42 RCLK", out)
        self.assertIn("43 MCLK", out)
        # Conn (layer 5, hold 45 + 46): the connection column and the profiles.
        _status, out, _ = self.run_cli("show", "--layer", "Conn")
        self.assertIn(" 0 USB", out)  # &out OUT_USB
        self.assertIn("12 BLE", out)  # &out OUT_BLE
        self.assertIn("24 _", out)  # BT_CLR removed 2026-09-24
        self.assertIn(" 1 BT0", out)  # &bt BT_SEL 0
        self.assertIn("25 BT2", out)  # &bt BT_SEL 2
        self.assertIn("38 _", out)  # &trans
        _status, out, _ = self.run_cli("show", "--layer", "Win")
        self.assertIn(" 0 APP_TAB", out)  # &app_tab, the Ctrl+Tab -> held-Alt+Tab switcher
        self.assertIn("39 _", out)  # &kp LCTRL
        self.assertIn(" 1 _", out)  # &trans

    def test_show_prints_every_layer_by_default(self) -> None:
        status, out, _ = self.run_cli("show")
        self.assertEqual(status, 0)
        layers = ["Base", "Win", "Num", "Fn2", "Fn3", "Conn", "MouseSlow", "MouseFast"]
        for index, name in enumerate(layers):
            self.assertIn(f"{index} {name} (layer_{index}):", out)

    def test_show_does_not_write(self) -> None:
        self.run_cli("show")
        self.assertUnchanged()


class SetTest(TempKeymapTest):
    def test_set_replaces_only_the_named_positions(self) -> None:
        before = self.bindings("Conn")
        status, out, _ = self.run_cli("set", "--layer", "Conn", "0", "&kp ESC", "13", "&kp A")
        self.assertEqual(status, 0)
        after = self.bindings("Conn")
        self.assertEqual(after[0], "&kp ESC")
        self.assertEqual(after[13], "&kp A")
        self.assertEqual(
            [b for i, b in enumerate(after) if i not in (0, 13)],
            [b for i, b in enumerate(before) if i not in (0, 13)],
        )
        self.assertIn("5 Conn:", out)  # the touched layer's grid is printed

    def test_untouched_layers_stay_byte_identical(self) -> None:
        self.run_cli("set", "--layer", "Conn", "0", "&kp ESC")
        before = layer_blocks(self.original)
        after = layer_blocks(self.keymap.read_text(encoding="utf-8"))
        self.assertEqual(sorted(before), sorted(after))
        for name in before:
            with self.subTest(layer=name):
                if name == "layer_5":  # Conn, the one that was edited
                    self.assertNotEqual(before[name], after[name])
                else:
                    self.assertEqual(before[name], after[name])

    def test_everything_outside_the_layers_is_copied_verbatim(self) -> None:
        self.run_cli("set", "--layer", "Conn", "0", "&kp ESC")
        text = self.keymap.read_text(encoding="utf-8")
        head = self.original[: self.original.index("        layer_0 {")]
        self.assertTrue(text.startswith(head))
        self.assertIn("sensor-bindings = <&inc_dec_kp C_VOL_UP C_VOL_DN>", text)
        self.assertEqual(text.count("sensor-bindings = "), self.original.count("sensor-bindings = "))

    def test_the_grid_comment_of_a_touched_layer_is_regenerated(self) -> None:
        self.run_cli("set", "--layer", "Conn", "0", "&kp ESC")
        block = layer_blocks(self.keymap.read_text(encoding="utf-8"))["layer_5"]
        comment = [line for line in block.splitlines() if line.lstrip().startswith("//")]
        self.assertEqual(len(comment), 4)
        self.assertIn("ESC", comment[0])

    def test_roundtrip_through_check_config(self) -> None:
        self.run_cli("set", "--layer", "Base", "0", "&kp ESC")
        warnings: list[str] = []
        self.assertEqual(cc.check_keymap(self.keymap, remap.KEY_COUNT, warnings), [])
        self.assertEqual([w for w in warnings if "skipped" in w], [])

    def test_dry_run_prints_a_diff_and_writes_nothing(self) -> None:
        status, out, _ = self.run_cli("--dry-run", "set", "--layer", "Base", "0", "&kp ESC")
        self.assertEqual(status, 0)
        self.assertIn("-&kp TAB", out)
        self.assertIn("+&kp ESC", out)
        self.assertUnchanged()

    def test_invalid_position_is_rejected(self) -> None:
        for position in ("50", "-1", "x"):
            with self.subTest(position=position):
                status, _out, err = self.run_cli("set", "--layer", "Base", position, "&kp A")
                self.assertEqual(status, 1)
                self.assertIn("error:", err)
                self.assertUnchanged()

    def test_unknown_layer_is_rejected(self) -> None:
        status, _out, err = self.run_cli("set", "--layer", "Nope", "0", "&kp A")
        self.assertEqual(status, 1)
        self.assertIn("unknown layer", err)
        self.assertUnchanged()

    def test_invalid_binding_is_rejected(self) -> None:
        for binding in ("kp A", "&nope A", "&kp A; &kp B", "&kp A &kp B"):
            with self.subTest(binding=binding):
                status, _out, err = self.run_cli("set", "--layer", "Base", "0", binding)
                self.assertEqual(status, 1)
                self.assertIn("error:", err)
                self.assertUnchanged()

    def test_force_accepts_an_unknown_behavior(self) -> None:
        status, _out, _err = self.run_cli("--force", "set", "--layer", "Conn", "0", "&my_macro")
        self.assertEqual(status, 0)
        self.assertEqual(self.bindings("Conn")[0], "&my_macro")

    def test_a_behavior_defined_in_the_file_is_accepted_without_force(self) -> None:
        status, _out, _err = self.run_cli("set", "--layer", "Conn", "0", "&inc_dec_msc SCRL_UP SCRL_DOWN")
        self.assertEqual(status, 0)

    def test_odd_number_of_arguments_is_rejected(self) -> None:
        status, _out, err = self.run_cli("set", "--layer", "Base", "0")
        self.assertEqual(status, 1)
        self.assertIn("POS BINDING", err)
        self.assertUnchanged()


class SwapCopyClearTest(TempKeymapTest):
    def test_swap_roundtrip(self) -> None:
        before = self.bindings("Base")
        self.assertEqual(self.run_cli("swap", "--layer", "Base", "47", "49")[0], 0)
        swapped = self.bindings("Base")
        self.assertEqual(swapped[47], before[49])
        self.assertEqual(swapped[49], before[47])
        self.assertEqual(self.run_cli("swap", "--layer", "Base", "47", "49")[0], 0)
        self.assertEqual(self.bindings("Base"), before)

    def test_copy_from_another_layer(self) -> None:
        base = self.bindings("Base")
        self.assertEqual(self.run_cli("copy", "--from-layer", "Base", "--to-layer", "Conn", "1", "2")[0], 0)
        fn4 = self.bindings("Conn")
        self.assertEqual([fn4[1], fn4[2]], [base[1], base[2]])
        self.assertEqual(fn4[3], "&trans")  # nothing else moved
        self.assertEqual(self.bindings("Base"), base)  # the source is untouched

    def test_copy_to_the_same_layer_is_rejected(self) -> None:
        status, _out, err = self.run_cli("copy", "--from-layer", "Base", "--to-layer", "Base", "1")
        self.assertEqual(status, 1)
        self.assertIn("same layer", err)
        self.assertUnchanged()

    def test_clear_sets_trans(self) -> None:
        self.assertEqual(self.run_cli("clear", "--layer", "Conn", "0", "1")[0], 0)
        self.assertEqual(self.bindings("Conn")[:2], ["&trans", "&trans"])

    def test_clear_needs_a_position(self) -> None:
        status, _out, err = self.run_cli("clear", "--layer", "Conn")
        self.assertEqual(status, 1)
        self.assertIn("position", err)
        self.assertUnchanged()


class LayerCommandTest(TempKeymapTest):
    def test_list(self) -> None:
        status, out, _ = self.run_cli("layer", "list")
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines()[:2], ["0 Base (layer_0)", "1 Win (layer_1)"])
        self.assertUnchanged()

    def test_add_at_the_end(self) -> None:
        status, out, _ = self.run_cli("layer", "add", "Extra")
        self.assertEqual(status, 0, out)
        keymap = remap.parse(self.keymap)
        self.assertEqual(keymap.layers[-1].display_name, "Extra")
        self.assertEqual(keymap.layers[-1].node_name, f"layer_{len(keymap.layers) - 1}")
        self.assertEqual(set(keymap.layers[-1].bindings), {"&trans"})
        # A layer appended at the end displaces nothing.
        self.assertEqual(keymap.find("Base").bindings, remap.parse(KEYMAP).find("Base").bindings)

    def test_add_in_the_middle_shifts_layer_references(self) -> None:
        base_before = self.bindings("Base")
        status, _out, err = self.run_cli("layer", "add", "Gaming", "--after", "Win")
        self.assertEqual(status, 0, err)
        keymap = remap.parse(self.keymap)
        self.assertEqual(
            [layer.display_name for layer in keymap.layers],
            ["Base", "Win", "Gaming", "Num", "Fn2", "Fn3", "Conn", "MouseSlow", "MouseFast"],
        )
        self.assertEqual([layer.node_name for layer in keymap.layers],
                         [f"layer_{i}" for i in range(9)])
        base = keymap.find("Base").bindings
        # &mo 2 -> &mo 3, &mo 4 -> &mo 5, &mo 3 -> &mo 4; the Win layer (1) is
        # below the insert and Base has no &lt any more.
        for position, before in enumerate(base_before):
            match = re.fullmatch(r"&(mo|lt|tog|to|sl) (\d+)(.*)", before)
            expected = before
            if match and int(match.group(2)) >= 2:
                expected = f"&{match.group(1)} {int(match.group(2)) + 1}{match.group(3)}"
            with self.subTest(position=position):
                self.assertEqual(base[position], expected)
        self.assertEqual(keymap.find("Fn3").bindings[0], "&tog 1")  # below the insert: unchanged

    def test_add_keeps_the_keymap_valid(self) -> None:
        self.run_cli("layer", "add", "Gaming", "--after", "Win")
        warnings: list[str] = []
        self.assertEqual(cc.check_keymap(self.keymap, remap.KEY_COUNT, warnings), [])
        self.assertEqual([w for w in warnings if "skipped" in w], [])

    def test_add_rejects_a_duplicate_name(self) -> None:
        status, _out, err = self.run_cli("layer", "add", "win")
        self.assertEqual(status, 1)
        self.assertIn("already exists", err)
        self.assertUnchanged()

    def test_rename(self) -> None:
        status, _out, err = self.run_cli("layer", "rename", "Conn", "Media")
        self.assertEqual(status, 0, err)
        keymap = remap.parse(self.keymap)
        self.assertEqual(keymap.layers[5].display_name, "Media")
        self.assertEqual(keymap.layers[5].bindings, remap.parse(KEYMAP).layers[5].bindings)
        # Only the display-name line changed.
        diff = [
            (a, b)
            for a, b in zip(self.original.splitlines(), self.keymap.read_text().splitlines())
            if a != b
        ]
        self.assertEqual(diff, [('            display-name = "Conn";', '            display-name = "Media";')])

    def test_rename_rejects_a_duplicate_name(self) -> None:
        status, _out, err = self.run_cli("layer", "rename", "Conn", "Base")
        self.assertEqual(status, 1)
        self.assertIn("already exists", err)
        self.assertUnchanged()


class CheckIntegrationTest(TempKeymapTest):
    def test_a_write_that_fails_the_checks_is_rolled_back(self) -> None:
        # &lt 99 points at a layer that does not exist; check_config catches it.
        status, _out, err = self.run_cli("set", "--layer", "Base", "0", "&lt 99 A")
        self.assertEqual(status, 1)
        self.assertIn("check_config", err)
        self.assertIn("references layer 99", err)
        self.assertUnchanged()

    def test_the_repo_keymap_passes_the_checks(self) -> None:
        warnings: list[str] = []
        self.assertEqual(cc.check_keymap(KEYMAP, remap.KEY_COUNT, warnings), [])
        self.assertEqual([w for w in warnings if "skipped" in w], [])

    def test_layer_behaviors_come_from_check_config(self) -> None:
        """One table, not three: remap and vial2zmk both use check_config's."""
        self.assertIs(remap.LAYER_BEHAVIORS, cc.LAYER_BEHAVIORS)
        for behavior in remap.LAYER_BEHAVIORS:
            with self.subTest(behavior=behavior):
                # A reference at or above the insert point moves up by one,
                # one below it stays put.
                self.assertEqual(remap.shift_layer_ref(f"&{behavior} 3", 2), f"&{behavior} 4")
                self.assertEqual(remap.shift_layer_ref(f"&{behavior} 1", 2), f"&{behavior} 1")


class RenderingTest(TempKeymapTest):
    def test_the_renderer_reproduces_the_bindings_blocks_it_parsed(self) -> None:
        """Rendering an unedited layer gives back the exact text in the file."""
        keymap = remap.parse(self.keymap)
        widths = remap.slot_widths([layer.bindings for layer in keymap.layers])
        for layer in keymap.layers:
            start, end = layer.body_span
            with self.subTest(layer=layer.display_name):
                self.assertEqual(remap.render_bindings(layer.bindings, widths), keymap.text[start:end])

    def test_an_edit_that_changes_nothing_leaves_the_file_alone(self) -> None:
        base = self.bindings("Base")
        status, out, _err = self.run_cli("set", "--layer", "Base", "0", base[0])
        self.assertEqual(status, 0)
        self.assertIn("nothing to change", out)
        self.assertUnchanged()

    def test_alignment_is_refreshed_when_a_column_changes_width(self) -> None:
        # `&mt LGUI SPACE` is wider than anything in its column, so every
        # layer's bindings block is re-padded (bindings themselves unchanged).
        before = remap.parse(self.keymap).layers
        status, _out, err = self.run_cli("set", "--layer", "Conn", "44", "&mt LGUI SPACE")
        self.assertEqual(status, 0, err)
        after = remap.parse(self.keymap).layers
        for index, layer in enumerate(after):
            if index == 5:
                continue
            with self.subTest(layer=layer.display_name):
                self.assertEqual(layer.bindings, before[index].bindings)
        text = self.keymap.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("&"):
                self.assertNotIn("\t", line)
                self.assertEqual(line, line.rstrip())


class KeymapContentTest(unittest.TestCase):
    """The pending keymap changes that were applied with this CLI."""

    def setUp(self) -> None:
        self.bindings = {
            layer.display_name: layer.bindings for layer in remap.parse(KEYMAP).layers
        }

    def test_fn3_has_no_bootloader_keys(self) -> None:
        self.assertEqual(self.bindings["Fn3"][5], "&none")
        self.assertEqual(self.bindings["Fn3"][6], "&none")
        for layer, bindings in self.bindings.items():
            with self.subTest(layer=layer):
                self.assertNotIn("&bootloader", bindings)

    def test_win_layer_app_switch(self) -> None:
        # Ctrl(+Tab) on the Win layer must come out as Alt+Tab (app switching),
        # with the Alt *held* across taps - which is why this is a dedicated
        # behavior (src/behavior_app_switch.c) and not a mod-morph to
        # `&kp LA(TAB)`, whose implicit Alt is released with every Tab.
        self.assertEqual(self.bindings["Win"][0], "&app_tab")
        text = KEYMAP.read_text(encoding="utf-8")
        node = re.search(r"app_tab: app_tab \{(.*?)\};", text, re.S)
        self.assertIsNotNone(node, "the app_tab app-switch node is missing")
        body = node.group(1)
        self.assertIn('compatible = "zmk,behavior-cornix-app-switch";', body)
        self.assertIn("#binding-cells = <0>;", body)
        self.assertIn("hold-mod = <LCTRL>;", body)
        self.assertIn("switch-mod = <LALT>;", body)
        self.assertIn("tap = <TAB>;", body)
        # mod-position must be the key that actually holds the Ctrl on Win.
        self.assertIn("mod-position = <41>;", body)
        self.assertEqual(self.bindings["Win"][41], "&kp LCTRL")
        # The mod-morph it replaced must be gone, node and all (the string
        # still appears in the comment that explains why it was replaced, so
        # match the property, not the compatible on its own).
        self.assertNotIn("win_tab", text)
        self.assertNotIn('compatible = "zmk,behavior-mod-morph"', text)

    def test_win_layer_modifiers(self) -> None:
        win = self.bindings["Win"]
        self.assertEqual(win[38], "&trans")  # physical Ctrl stays LCTRL -> Ctrl+Space
        self.assertEqual(win[39], "&trans")
        self.assertEqual(win[40], "&trans")  # Opt = LALT already
        self.assertEqual(win[41], "&kp LCTRL")  # the thumb rest key

    def test_base_thumb_row(self) -> None:
        base = self.bindings["Base"]
        # Left thumb: 41 Cmd/Ctrl rest key, 42 Num, 43 Space (2026-09-18).
        self.assertEqual(base[38:45], [
            "&kp LCTRL", "&kp LGUI", "&kp LALT", "&kp LGUI", "&mo 2", "&kp SPACE", "&kp SPACE",
        ])
        # Fn3 moved to the right thumb; 43 and 44 are both Space again.
        self.assertEqual(base[45], "&mo 4")
        self.assertEqual(base.count("&kp SPACE"), 2)
        # Conn (layer 5) is a conditional layer (45 + 46), not an &mo on Base.
        self.assertNotIn("&mo 5", base)

    def test_mouse_keys_live_on_fn3(self) -> None:
        # 2026-09-19: the mouse moved off its own layer-tap layer onto Fn3
        # (held with position 45).  WASD-style on IJKL for the right hand;
        # the left hand has all three clicks on the thumbs and the two speed
        # layers on W/E (they were on D/F, and before that on U/O).
        fn3 = self.bindings["Fn3"]
        self.assertEqual(fn3[8], "&mmv MOVE_UP")
        self.assertEqual(fn3[19], "&mmv MOVE_LEFT")
        self.assertEqual(fn3[20], "&mmv MOVE_DOWN")
        self.assertEqual(fn3[21], "&mmv MOVE_RIGHT")
        self.assertEqual(fn3[41], "&mkp LCLK")
        self.assertEqual(fn3[42], "&mkp RCLK")
        self.assertEqual(fn3[43], "&mkp MCLK")
        self.assertEqual(fn3[44], "&trans")  # the right thumb Space stays dead
        self.assertEqual(fn3[2], "&mo 6")  # W -> MouseSlow
        self.assertEqual(fn3[3], "&mo 7")  # E -> MouseFast
        self.assertEqual(fn3[15], "&none")  # D/F are free again
        self.assertEqual(fn3[16], "&none")
        # Only the Win toggle stays on the left column; the connection keys
        # moved to the Conn layer on 2026-09-22 and their old spots are dead.
        self.assertEqual(fn3[0], "&tog 1")
        self.assertEqual(fn3[7], "&none")  # was &out OUT_USB (45+U)
        self.assertEqual(fn3[29], "&none")  # was &out OUT_BLE (45+B)
        self.assertEqual(fn3[37], "&none")  # was &bt BT_CLR
        self.assertEqual(fn3[24], "&trans")  # Shift and Ctrl fall through
        self.assertEqual(fn3[38], "&trans")
        self.assertEqual(fn3[30], "&kp C_MUTE")
        self.assertEqual(fn3[31], "&mkp MCLK")
        # Position 49 is a plain arrow key again.
        self.assertEqual(self.bindings["Base"][49], "&kp RIGHT")
        self.assertEqual(self.bindings["Win"][49], "&trans")

    def test_speed_layers_are_empty_flags(self) -> None:
        # MouseSlow/MouseFast carry no bindings at all: they exist only so the
        # &mmv_input_listener overrides below can key off an active layer, and
        # they must stay transparent or they would shadow the &mmv keys on Fn3.
        for name in ("MouseSlow", "MouseFast"):
            with self.subTest(layer=name):
                self.assertEqual(set(self.bindings[name]), {"&trans"})

    def test_pointer_speed_input_processors(self) -> None:
        text = KEYMAP.read_text(encoding="utf-8")
        # The old &lt override went away with the &lt 6 RIGHT layer-tap.
        self.assertIsNone(re.search(r"^&lt \{", text, re.M))
        self.assertNotIn("&lt ", "".join(b for layer in self.bindings.values() for b in layer))
        self.assertIn("#include <input/processors.dtsi>", text)
        listener = re.search(r"&mmv_input_listener \{(.*?)\n\};", text, re.S)
        self.assertIsNotNone(listener, "the &mmv_input_listener overlay is missing")
        body = listener.group(1)
        slow = re.search(r"slow \{(.*?)\};", body, re.S)
        fast = re.search(r"fast \{(.*?)\};", body, re.S)
        self.assertIsNotNone(slow)
        self.assertIsNotNone(fast)
        self.assertIn("layers = <6>;", slow.group(1))
        # 2026-09-19: softened from 1/3 and 3/1 to 1/2 and 2/1.
        self.assertIn("input-processors = <&zip_xy_scaler 1 2>;", slow.group(1))
        self.assertIn("layers = <7>;", fast.group(1))
        self.assertIn("input-processors = <&zip_xy_scaler 2 1>;", fast.group(1))

    def test_num_layer_programming_keys(self) -> None:
        # 2026-09-19: the brackets and the backtick, which the Base layer has
        # no room for, on the Num layer (hold 42).
        num = self.bindings["Num"]
        self.assertEqual(num[13], "&kp GRAVE")  # A
        self.assertEqual(num[20], "&kp LBKT")  # K
        self.assertEqual(num[21], "&kp RBKT")  # L
        self.assertEqual(num[34], "&kp LBRC")  # ,
        self.assertEqual(num[35], "&kp RBRC")  # .
        # The keys that were already there are untouched.
        self.assertEqual(num[0], "&kp ESC")
        self.assertEqual(num[17], "&kp MINUS")
        self.assertEqual(num[18], "&kp EQUAL")
        self.assertEqual(num[19], "&kp SQT")
        self.assertEqual(num[1:11], [f"&kp N{d}" for d in list(range(1, 10)) + [0]])

    def test_fn2_layer_function_and_navigation_keys(self) -> None:
        # 2026-09-19: F1-F12 on the top row (F12 on the Tab position, so the
        # numbers line up with the Num layer's N1-N0) and the navigation
        # cluster on the right home row (hold 46).
        fn2 = self.bindings["Fn2"]
        self.assertEqual(fn2[0], "&kp F12")
        self.assertEqual(fn2[1:11], [f"&kp F{n}" for n in range(1, 11)])
        self.assertEqual(fn2[13], "&kp F11")
        self.assertEqual(fn2[18], "&kp HOME")
        self.assertEqual(fn2[19], "&kp PG_DN")
        self.assertEqual(fn2[20], "&kp PG_UP")
        self.assertEqual(fn2[21], "&kp END")
        self.assertEqual(fn2[22], "&kp PSCRN")
        self.assertEqual(fn2[36], "&kp INS")
        # The Bluetooth column moved to the Conn layer on 2026-09-22: 12 is
        # dead and 24/38 fall through to Shift/Ctrl.  The centre pair stays.
        self.assertEqual(fn2[12], "&none")
        self.assertEqual(fn2[24], "&trans")
        self.assertEqual(fn2[38], "&trans")
        self.assertEqual(fn2[30], "&kp C_MUTE")
        self.assertEqual(fn2[31], "&mkp MCLK")

    def test_conn_layer_is_the_only_home_of_the_connection_keys(self) -> None:
        # 2026-09-22: hold both right thumb Fn keys (45 = Fn3 = layer 4,
        # 46 = Fn2 = layer 3) and the leftmost column picks the connection,
        # the column next to it the BT profile.  Layer 5 is switched on by a
        # hand-written conditional_layers node, not by any &mo.
        conn = self.bindings["Conn"]
        self.assertEqual(conn[0], "&out OUT_USB")
        self.assertEqual(conn[12], "&out OUT_BLE")
        self.assertEqual(conn[24], "&trans")  # BT_CLR removed 2026-09-24
        self.assertEqual(conn[1], "&bt BT_SEL 0")
        self.assertEqual(conn[13], "&bt BT_SEL 1")
        self.assertEqual(conn[25], "&bt BT_SEL 2")
        rest = [b for pos, b in enumerate(conn) if pos not in (0, 12, 24, 1, 13, 25)]
        self.assertEqual(set(rest), {"&trans"}, "everything else on Conn must fall through")
        for name, layer in self.bindings.items():
            if name != "Conn":
                for binding in layer:
                    self.assertFalse(binding.startswith(("&out ", "&bt ")),
                                     f"{name} still has a connection key: {binding}")
        text = KEYMAP.read_text(encoding="utf-8")
        node = re.search(r"conditional_layers \{(.*?)\n    \};", text, re.S)
        self.assertIsNotNone(node, "the conditional_layers node is missing")
        body = node.group(1)
        self.assertIn('compatible = "zmk,conditional-layers";', body)
        self.assertIn("if-layers = <3 4>;", body)
        self.assertIn("then-layer = <5>;", body)
        self.assertEqual(self.bindings["Base"][45], "&mo 4")
        self.assertEqual(self.bindings["Base"][46], "&mo 3")
        # The chord must work in either order: with one Fn layer already on,
        # the other thumb key has to fall through to Base's &mo, not hit a
        # &none on the layer that is up (found by tests/sim/.../conn-layer).
        self.assertEqual(self.bindings["Fn3"][46], "&trans")
        self.assertEqual(self.bindings["Fn2"][45], "&trans")


if __name__ == "__main__":
    unittest.main()
