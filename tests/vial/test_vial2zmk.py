"""Unit tests for scripts/vial2zmk.py (stdlib unittest only).

Run with:  python3 -m unittest discover -s tests/vial -v

The fixture is the real stock keymap exported from Vial,
config/vial/cornix-default-keymap.vil.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_config as cc  # noqa: E402
import vial2zmk as v2z  # noqa: E402

FIXTURE = REPO_ROOT / "config/vial/cornix-default-keymap.vil"

# The stock layer 0, row by row in ZMK key-position order (12 / 12 / 14 / 12).
LAYER0_ROWS = [
    ["&kp TAB", "&kp Q", "&kp W", "&kp E", "&kp R", "&kp T",
     "&kp Y", "&kp U", "&kp I", "&kp O", "&kp P", "&kp BSPC"],
    ["&kp CAPS", "&kp A", "&kp S", "&kp D", "&kp F", "&kp G",
     "&kp H", "&kp J", "&kp K", "&kp L", "&kp BSLH", "&kp RET"],
    ["&kp LSHFT", "&kp Z", "&kp X", "&kp C", "&kp V", "&kp B",
     "&kp C_MUTE", "&mkp MCLK",
     "&kp N", "&kp M", "&kp COMMA", "&kp DOT", "&kp UP", "&kp FSLH"],
    ["&kp LCTRL", "&kp LGUI", "&kp LALT", "&mo 1", "&mo 3", "&kp SPACE",
     "&kp SPACE", "&mo 4", "&mo 2", "&kp LEFT", "&kp DOWN", "&kp RIGHT"],
]

USER_KEYS = {
    "USER00": "&bt BT_SEL 0",
    "USER01": "&bt BT_SEL 1",
    "USER02": "&bt BT_SEL 2",
}


def load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def convert(layer_limit: int | None = None, user_keys: dict[str, str] | None = None) -> list[v2z.Layer]:
    converter = v2z.Converter(user_keys=dict(USER_KEYS if user_keys is None else user_keys))
    layers = v2z.convert(load(), v2z.CORNIX, converter, layer_limit, ["Base", "Num", "Fn2", "Fn3", "Fn4"])
    if converter.unknown:
        raise AssertionError(f"unexpected unknown keycodes: {converter.unknown}")
    return layers


def convert_with_win() -> list[v2z.Layer]:
    """The repo keymap's layer list: the five Vial layers plus the Win overlay."""
    return v2z.insert_layer(convert(5), 1, "Win", v2z.CORNIX)


def split_bindings(text: str) -> list[str]:
    """Split a devicetree `bindings` property value into individual bindings."""
    return ["&" + item.strip(" \t\n<>;,") for item in text.split("&")[1:]]


def rows(layer: v2z.Layer) -> list[list[str]]:
    result: list[list[str]] = []
    position = 0
    for slots in v2z.CORNIX.row_slots:
        result.append(layer.bindings[position : position + len(slots)])
        position += len(slots)
    return result


class MatrixTableTest(unittest.TestCase):
    def test_cornix_covers_every_position_exactly_once(self) -> None:
        self.assertEqual(v2z.CORNIX.key_count, 50)
        self.assertEqual(len(set(v2z.CORNIX.positions)), 50)
        self.assertEqual(sum(len(slots) for slots in v2z.CORNIX.row_slots), 50)
        for row, column in v2z.CORNIX.positions:
            self.assertLess(row, v2z.CORNIX.rows)
            self.assertLess(column, v2z.CORNIX.columns)

    def test_centre_keys_come_from_column_six(self) -> None:
        # position 30 = RC(2,6) (left inner extra), position 31 = RC(1,13)
        # (right inner extra) in boards/jzf/cornix/cornix-layouts.dtsi.
        self.assertEqual(v2z.CORNIX.positions[30], (2, 6))
        self.assertEqual(v2z.CORNIX.positions[31], (5, 6))


class ConvertTest(unittest.TestCase):
    def test_fixture_shape(self) -> None:
        data = load()
        self.assertEqual(len(data["layout"]), 10)
        self.assertEqual(len(data["encoder_layout"]), 10)

    def test_layer0_rows(self) -> None:
        layers = convert()
        self.assertEqual(rows(layers[0]), LAYER0_ROWS)

    def test_every_layer_has_fifty_bindings(self) -> None:
        for layer in convert():
            with self.subTest(layer=layer.name):
                self.assertEqual(len(layer.bindings), 50)

    def test_layer1_number_row_and_symbols(self) -> None:
        layer = convert()[1]
        top, home, _bottom, _thumb = rows(layer)
        self.assertEqual(
            top,
            ["&kp ESC", "&kp N1", "&kp N2", "&kp N3", "&kp N4", "&kp N5",
             "&kp N6", "&kp N7", "&kp N8", "&kp N9", "&kp N0", "&kp DEL"],
        )
        self.assertEqual(home[4:8], ["&kp SEMI", "&kp MINUS", "&kp EQUAL", "&kp SQT"])

    def test_kc_no_becomes_none(self) -> None:
        layer = convert()[4]
        # Layer 4 is empty apart from the two inner keys.
        self.assertEqual(layer.bindings[30], "&kp C_MUTE")
        self.assertEqual(layer.bindings[31], "&mkp MCLK")
        others = [b for i, b in enumerate(layer.bindings) if i not in (30, 31)]
        self.assertEqual(set(others), {"&none"})

    def test_user_keys_are_substituted(self) -> None:
        layer = convert()[2]
        self.assertEqual(layer.bindings[12], "&bt BT_SEL 0")
        self.assertEqual(layer.bindings[24], "&bt BT_SEL 1")
        self.assertEqual(layer.bindings[38], "&bt BT_SEL 2")

    def test_layer_limit(self) -> None:
        self.assertEqual(len(convert(5)), 5)
        self.assertEqual([layer.name for layer in convert(5)], [f"layer_{i}" for i in range(5)])

    def test_encoder_order_is_clockwise_first(self) -> None:
        # Vial: encoder 0 = [KC_VOLD, KC_VOLU], encoder 1 = [KC_WH_U, KC_WH_D],
        # stored as [counter-clockwise, clockwise].  ZMK's inc_dec_* take the
        # clockwise action first, so the pairs are swapped.
        for layer in convert():
            with self.subTest(layer=layer.name):
                self.assertEqual(
                    layer.sensors,
                    ["&inc_dec_kp C_VOL_UP C_VOL_DN", "&inc_dec_msc SCRL_DOWN SCRL_UP"],
                )


class InsertLayerTest(unittest.TestCase):
    """--insert-layer INDEX:NAME adds a &trans overlay and renumbers references."""

    def test_layer_list_and_display_names(self) -> None:
        layers = convert_with_win()
        self.assertEqual([layer.name for layer in layers], [f"layer_{i}" for i in range(6)])
        self.assertEqual(
            [layer.display_name for layer in layers],
            ["Base", "Win", "Num", "Fn2", "Fn3", "Fn4"],
        )

    def test_inserted_layer_is_all_trans_with_no_sensor_bindings(self) -> None:
        win = convert_with_win()[1]
        self.assertEqual(len(win.bindings), 50)
        self.assertEqual(set(win.bindings), {"&trans"})
        self.assertEqual(win.sensors, [])
        self.assertEqual(set(win.labels), {""})

    def test_layer_references_at_or_above_the_index_move_up(self) -> None:
        # Vial MO(1)..MO(4) -> &mo 2..&mo 5; nothing else on the thumb row moves.
        thumb = rows(convert_with_win()[0])[3]
        self.assertEqual(
            thumb,
            ["&kp LCTRL", "&kp LGUI", "&kp LALT", "&mo 2", "&mo 4", "&kp SPACE",
             "&kp SPACE", "&mo 5", "&mo 3", "&kp LEFT", "&kp DOWN", "&kp RIGHT"],
        )

    def test_comment_labels_are_renumbered_too(self) -> None:
        labels = convert_with_win()[0].labels
        self.assertEqual([labels[41], labels[42], labels[45], labels[46]],
                         ["MO(2)", "MO(4)", "MO(5)", "MO(3)"])

    def test_references_below_the_index_are_untouched(self) -> None:
        layers = v2z.insert_layer(convert(5), 3, "Extra", v2z.CORNIX)
        thumb = rows(layers[0])[3]
        # MO(1) and MO(2) stay; MO(3) and MO(4) move up.
        self.assertEqual([thumb[3], thumb[8], thumb[4], thumb[7]],
                         ["&mo 1", "&mo 2", "&mo 4", "&mo 5"])

    def test_repeated_insertion_is_applied_in_order(self) -> None:
        layers = v2z.insert_layer(convert(5), 1, "Win", v2z.CORNIX)
        layers = v2z.insert_layer(layers, 2, "Linux", v2z.CORNIX)
        self.assertEqual(
            [layer.display_name for layer in layers],
            ["Base", "Win", "Linux", "Num", "Fn2", "Fn3", "Fn4"],
        )
        thumb = rows(layers[0])[3]
        self.assertEqual([thumb[3], thumb[4], thumb[7], thumb[8]],
                         ["&mo 3", "&mo 5", "&mo 6", "&mo 4"])

    def test_index_zero_and_out_of_range_are_rejected(self) -> None:
        for index in (0, 6, 99):
            with self.subTest(index=index), self.assertRaises(v2z.ConversionError):
                v2z.insert_layer(convert(5), index, "X", v2z.CORNIX)

    def test_cli_argument_parsing(self) -> None:
        self.assertEqual(v2z.parse_insert_layer("1:Win"), (1, "Win"))
        self.assertEqual(v2z.parse_insert_layer(" 2 : Gaming "), (2, "Gaming"))
        for bad in ("Win", "x:Win", "1:"):
            with self.subTest(value=bad), self.assertRaises(argparse.ArgumentTypeError):
                v2z.parse_insert_layer(bad)

    def test_cli_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.keymap"
            status = v2z.main([
                str(FIXTURE), "--layers", "5",
                "--layer-names", "Base,Num,Fn2,Fn3,Fn4",
                "--insert-layer", "1:Win",
                "--user-key", "USER00=&bt BT_SEL 0",
                "--user-key", "USER01=&bt BT_SEL 1",
                "--user-key", "USER02=&bt BT_SEL 2",
                "-o", str(out),
            ])
            text = out.read_text(encoding="utf-8")
            warnings: list[str] = []
            errors = cc.check_keymap(out, 50, warnings)
        self.assertEqual(status, 0)
        self.assertEqual(errors, [])
        self.assertIn('display-name = "Win";', text)
        self.assertIn("--insert-layer 1:Win", text)  # recorded in the header
        self.assertIn("&mo 2", text)
        self.assertNotIn("&mo 1", text)


class UnknownKeycodeTest(unittest.TestCase):
    def test_user_keys_without_mapping_are_reported(self) -> None:
        converter = v2z.Converter()
        v2z.convert(load(), v2z.CORNIX, converter, 5)
        self.assertTrue(converter.unknown)
        self.assertTrue(all("USER0" in item for item in converter.unknown))
        self.assertIn("--user-key USER00=", " ".join(converter.unknown))

    def test_unknown_keycode_is_not_dropped(self) -> None:
        data = load()
        data["layout"][0][0][0] = "KC_TOTALLY_MADE_UP"
        converter = v2z.Converter(user_keys=dict(USER_KEYS))
        layers = v2z.convert(data, v2z.CORNIX, converter, 1)
        self.assertEqual(len(layers[0].bindings), 50)
        self.assertEqual(len(converter.unknown), 1)
        self.assertIn("KC_TOTALLY_MADE_UP", converter.unknown[0])

    def test_cli_fails_on_unknown_keycode(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(stderr):
            out = Path(tmp) / "out.keymap"
            status = v2z.main([str(FIXTURE), "--layers", "5", "-o", str(out)])
        self.assertIn("unknown keycodes", stderr.getvalue())
        self.assertEqual(status, 1)
        self.assertFalse(out.exists())

    def test_missing_layout_is_an_error(self) -> None:
        with self.assertRaises(v2z.ConversionError):
            v2z.convert({}, v2z.CORNIX, v2z.Converter())


class KeycodeTableTest(unittest.TestCase):
    def test_layer_and_hold_tap_functions(self) -> None:
        converter = v2z.Converter()
        self.assertEqual(converter.binding("MO(2)", "x"), "&mo 2")
        self.assertEqual(converter.binding("TG(3)", "x"), "&tog 3")
        self.assertEqual(converter.binding("TO(0)", "x"), "&to 0")
        self.assertEqual(converter.binding("OSL(1)", "x"), "&sl 1")
        self.assertEqual(converter.binding("LT(2,KC_SPACE)", "x"), "&lt 2 SPACE")
        self.assertEqual(converter.binding("MT(MOD_LCTL,KC_A)", "x"), "&mt LCTRL A")
        self.assertEqual(converter.binding("OSM(MOD_LSFT)", "x"), "&sk LSHFT")
        self.assertEqual(converter.binding("LCTL(KC_C)", "x"), "&kp LC(C)")
        self.assertEqual(converter.unknown, [])

    def test_pointing_keycodes(self) -> None:
        converter = v2z.Converter()
        self.assertEqual(converter.binding("KC_BTN1", "x"), "&mkp LCLK")
        self.assertEqual(converter.binding("KC_BTN2", "x"), "&mkp RCLK")
        self.assertEqual(converter.binding("KC_BTN3", "x"), "&mkp MCLK")
        self.assertEqual(converter.binding("KC_WH_U", "x"), "&msc SCRL_UP")
        self.assertEqual(converter.binding("KC_MS_LEFT", "x"), "&mmv MOVE_LEFT")
        self.assertEqual(converter.unknown, [])

    def test_trans_and_none(self) -> None:
        converter = v2z.Converter()
        self.assertEqual(converter.binding("KC_TRNS", "x"), "&trans")
        self.assertEqual(converter.binding("KC_NO", "x"), "&none")
        self.assertEqual(converter.unknown, [])


class EmitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.layers = convert(5)
        self.text = v2z.emit(self.layers, v2z.CORNIX, v2z.build_header(Path("config/vial/x.vil"), ["x.vil"]))

    def test_includes_and_structure(self) -> None:
        self.assertIn('compatible = "zmk,keymap";', self.text)
        self.assertIn("#include <behaviors.dtsi>", self.text)
        self.assertIn("#include <dt-bindings/zmk/keys.h>", self.text)
        self.assertIn("#include <dt-bindings/zmk/bt.h>", self.text)
        self.assertIn("#include <dt-bindings/zmk/pointing.h>", self.text)
        self.assertIn('inc_dec_msc: sensor_rotate_scroll', self.text)
        # 2026-09-19 scroll tuning, mirrored from config/cornix.keymap: the
        # define has to precede the pointing.h include (which derives SCRL_*
        # from it behind an #ifndef) and tap-ms brackets a single 16 ms tick.
        self.assertIn("#define ZMK_POINTING_DEFAULT_SCRL_VAL 188", self.text)
        self.assertIn("tap-ms = <24>;", self.text)
        self.assertNotIn("tap-ms = <150>;", self.text)
        self.assertLess(
            self.text.index("#define ZMK_POINTING_DEFAULT_SCRL_VAL 188"),
            self.text.index("#include <dt-bindings/zmk/pointing.h>"),
        )
        for index, name in enumerate(["Base", "Num", "Fn2", "Fn3", "Fn4"]):
            self.assertIn(f"layer_{index} {{", self.text)
            self.assertIn(f'display-name = "{name}";', self.text)
        self.assertEqual(self.text.count("sensor-bindings = "), 5)

    def test_generated_keymap_passes_the_static_checker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cornix.keymap"
            path.write_text(self.text, encoding="utf-8")
            warnings: list[str] = []
            errors = cc.check_keymap(path, 50, warnings)
        self.assertEqual(errors, [])
        self.assertEqual([w for w in warnings if "skipped" in w], [])

    def test_repo_keymap_is_valid_and_starts_from_the_imported_layers(self) -> None:
        """config/cornix.keymap is no longer compared against this converter.

        The keymap became the source of truth on 2026-09-17: it is edited in
        place with scripts/remap.py (bootloader keys removed, thumb row
        rearranged, Mouse layer added, ...), so it can no longer equal the
        converter's output plus a fixed list of hand edits.  vial2zmk.py is
        the one-time importer and is still tested on its own above.

        What is still worth pinning down here is that the documented import
        command is reproducible and that the file in the repository is a valid
        50-key ZMK keymap whose first six layers are still the imported ones,
        in the imported order.  Its actual bindings are covered by
        tests/remap/test_remap.py.
        """
        repo_path = REPO_ROOT / "config/cornix.keymap"
        repo = repo_path.read_text(encoding="utf-8")
        warnings: list[str] = []
        self.assertEqual(cc.check_keymap(repo_path, 50, warnings), [])
        self.assertEqual([w for w in warnings if "skipped" in w], [])

        repo_layers, info = cc.parse_keymap_layers(repo, (REPO_ROOT / "config").resolve())
        self.assertIsNone(info["skipped"])
        for layer in repo_layers:
            with self.subTest(layer=layer["name"]):
                self.assertEqual(layer["count"], 50)

        imported = [layer.display_name for layer in convert_with_win()]
        self.assertEqual(imported, ["Base", "Win", "Num", "Fn2", "Fn3", "Fn4"])
        display_names = re.findall(r'display-name = "([^"]+)"', repo)
        # 2026-09-22: the empty imported "Fn4" (layer 5) was renamed "Conn"
        # and became the conditional connection layer (hold 45 + 46); the
        # order of the imported layers is what still has to hold.
        renamed = {"Fn4": "Conn"}
        self.assertEqual(display_names[: len(imported)], [renamed.get(n, n) for n in imported])
        self.assertGreaterEqual(len(repo_layers), len(imported))
        self.assertEqual(len(display_names), len(repo_layers))


if __name__ == "__main__":
    unittest.main()
