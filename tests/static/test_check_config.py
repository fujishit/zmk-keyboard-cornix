"""Unit tests for scripts/check_config.py (stdlib unittest only).

Run with:  python3 -m unittest discover -s tests/static -v
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_config as cc  # noqa: E402


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


BOARD_YML = """
boards:
  - name: cornix_left
    vendor: jzf
    socs:
      - name: nrf52840
        variants:
          - name: zmk
  - name: cornix_right
    vendor: jzf
    socs:
      - name: nrf52840
        variants:
          - name: zmk
"""

GOOD_DEFCONFIG_CENTRAL = """
CONFIG_NVS=y
CONFIG_SETTINGS_NVS=y
CONFIG_ZMK_SPLIT=y
CONFIG_ZMK_SPLIT_ROLE_CENTRAL=y
"""

GOOD_DEFCONFIG_PERIPHERAL = """
CONFIG_NVS=y
CONFIG_SETTINGS_NVS=y
CONFIG_ZMK_SPLIT=y
"""


def layouts_dtsi(map_rows: list[str], key_count: int, rows: int = 2, columns: int = 4) -> str:
    keys = "\n".join(f"            , <&key_physical_attrs 100 100 {i * 100} 0 0 0 0>" for i in range(1, key_count))
    positions = " ".join(str(i) for i in range(key_count))
    return f"""
/ {{
    default_transform: keymap_transform_0 {{
        compatible = "zmk,matrix-transform";
        columns = <{columns}>;
        rows = <{rows}>;
        map = <
{chr(10).join(map_rows)}
        >;
    }};

    layout_0: layout_0 {{
        compatible = "zmk,physical-layout";
        display-name = "test";
        transform = <&default_transform>;
        keys
            = <&key_physical_attrs 100 100 0 0 0 0 0>
{keys}
            ;
    }};

    position_map {{
        compatible = "zmk,physical-layout-position-map";
        layout_0 {{
            physical-layout = <&layout_0>;
            positions = <{positions}>;
        }};
    }};
}};
"""


def keymap_dts(layers: dict[str, str], extra: str = "") -> str:
    body = "\n".join(
        f"""
        {name} {{
            bindings = <
{bindings}
            >;
        }};"""
        for name, bindings in layers.items()
    )
    return f"""
#include <behaviors.dtsi>
#include <dt-bindings/zmk/keys.h>

/ {{
{extra}
    keymap {{
        compatible = "zmk,keymap";
{body}
    }};
}};
"""


def bindings(count: int) -> str:
    return " ".join("&kp A" for _ in range(count))


class RealRepositoryTests(unittest.TestCase):
    """Every checker must pass against the committed repository."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = cc.run_all_checks(REPO_ROOT)

    def test_all_checks_registered(self) -> None:
        self.assertEqual(
            set(self.results),
            {
                "build-matrix",
                "snippets",
                "west.yml",
                "kconfig",
                "layouts",
                "keymaps",
                "json",
                "board-metadata",
                "shields",
            },
        )

    def test_no_hard_errors(self) -> None:
        for name, result in self.results.items():
            with self.subTest(check=name):
                self.assertEqual(result["errors"], [])

    def test_main_exit_code_is_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cc.main(["--root", str(REPO_ROOT), "--quiet"]), 0)

    def test_real_layout_has_fifty_keys(self) -> None:
        _transforms, layouts, errors = cc.parse_layouts((REPO_ROOT / cc.LAYOUTS_DTSI).read_text())
        self.assertEqual(errors, [])
        self.assertEqual(layouts["layout_50"]["count"], 50)

    def test_real_keymaps_are_not_skipped(self) -> None:
        for relative in cc.KEYMAP_TARGETS:
            path = REPO_ROOT / relative
            layers, info = cc.parse_keymap_layers(path.read_text(), path.parent)
            with self.subTest(keymap=relative):
                self.assertIsNone(info["skipped"])
                self.assertGreaterEqual(len(layers), 3)


class YamlParserTests(unittest.TestCase):
    def test_nested_structures(self) -> None:
        data = cc.parse_yaml(
            """
            # comment
            ---
            include:
              - board: nice_nano//zmk
                shield: a b   # trailing comment
                artifact-name: x
              - board: cornix_left//zmk
            flow: [a, b]
            quoted: "1"
            number: 3
            empty:
            url: https://example.com/path
            """
        )
        self.assertEqual(data["include"][0], {"board": "nice_nano//zmk", "shield": "a b", "artifact-name": "x"})
        self.assertEqual(data["include"][1], {"board": "cornix_left//zmk"})
        self.assertEqual(data["flow"], ["a", "b"])
        self.assertEqual(data["quoted"], "1")
        self.assertEqual(data["number"], 3)
        self.assertIsNone(data["empty"])
        self.assertEqual(data["url"], "https://example.com/path")

    def test_sequence_at_same_indent_as_key(self) -> None:
        data = cc.parse_yaml("items:\n- a\n- b\nother: c\n")
        self.assertEqual(data, {"items": ["a", "b"], "other": "c"})

    def test_rejects_tabs_and_duplicate_keys(self) -> None:
        with self.assertRaises(cc.ParseError):
            cc.parse_yaml("a:\n\tb: 1\n")
        with self.assertRaises(cc.ParseError):
            cc.parse_yaml("a: 1\na: 2\n")


class BuildYamlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.board_yml = write(self.root / "board.yml", BOARD_YML)
        self.shields = self.root / "shields"
        for name in ("cornix_dongle_adapter", "cornix_dongle_eyelash", "cornix_indicator"):
            (self.shields / name).mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_check(self, text: str) -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        errors = cc.check_build_yaml(write(self.root / "build.yaml", text), self.board_yml, self.shields, warnings)
        return errors, warnings

    def test_valid_matrix(self) -> None:
        errors, _ = self.run_check(
            """
            include:
              - board: nice_nano//zmk
                shield: cornix_dongle_adapter cornix_dongle_eyelash dongle_display
                snippet: studio-rpc-usb-uart nrf52840-nosd
                artifact-name: dongle
              - board: cornix_left//zmk
                artifact-name: left
              - board: cornix_right//zmk
                shield: settings_reset
                artifact-name: reset
            """
        )
        self.assertEqual(errors, [])

    def test_unqualified_boards(self) -> None:
        errors, _ = self.run_check(
            """
            include:
              - board: nice_nano
                shield: settings_reset
                artifact-name: a
              - board: cornix_left
                artifact-name: b
            """
        )
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("qualified form" in e for e in errors))
        self.assertIn("nice_nano//zmk", errors[0])

    def test_unknown_board_shield_snippet(self) -> None:
        errors, _ = self.run_check(
            """
            include:
              - board: cornix_middle//zmk
                shield: cornix_screen
                snippet: not-a-snippet
                artifact-name: a
            """
        )
        self.assertTrue(any("cornix_middle" in e and "not defined" in e for e in errors))
        self.assertTrue(any("cornix_screen" in e and "allowlist" in e for e in errors))
        self.assertTrue(any("not-a-snippet" in e for e in errors))

    def test_duplicate_artifact_name(self) -> None:
        errors, _ = self.run_check(
            """
            include:
              - board: cornix_left//zmk
                artifact-name: same
              - board: cornix_right//zmk
                artifact-name: same
            """
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("already used", errors[0])

    def test_dongle_rules(self) -> None:
        errors, _ = self.run_check(
            """
            include:
              - board: cornix_right//zmk
                shield: cornix_dongle_adapter
                artifact-name: a
              - board: nice_nano//zmk
                shield: cornix_dongle_eyelash
                artifact-name: b
              - board: cornix_left//zmk
                shield: settings_reset cornix_indicator
                artifact-name: c
              - board: nice_nano//zmk
                shield: cornix_indicator
                artifact-name: d
            """
        )
        self.assertTrue(any("cornix_dongle_adapter" in e and "must not be built for a Cornix half" in e for e in errors))
        self.assertTrue(any("cornix_dongle_eyelash requires cornix_dongle_adapter" in e for e in errors))
        self.assertTrue(any("settings_reset must be the only shield" in e for e in errors))
        self.assertTrue(any("cornix_indicator only applies" in e for e in errors))

    def test_parse_failure_is_reported(self) -> None:
        errors, _ = self.run_check("include:\n\t- board: x\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot parse", errors[0])

    def test_local_snippets_are_accepted(self) -> None:
        snippets = self.root / "snippets"
        write(snippets / "cornix-debug-log/snippet.yml", "name: cornix-debug-log\n")
        (snippets / "no-yaml").mkdir()
        text = """
            include:
              - board: cornix_left//zmk
                snippet: zmk-usb-logging cornix-debug-log
                artifact-name: left_debug
              - board: cornix_right//zmk
                snippet: no-yaml
                artifact-name: right_debug
            """
        build = write(self.root / "build-debug.yaml", text)
        errors = cc.check_build_yaml(build, self.board_yml, self.shields, [], snippets_dir=snippets)
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown snippet 'no-yaml'", errors[0])
        # Without a snippets directory the local snippet is unknown.
        errors = cc.check_build_yaml(build, self.board_yml, self.shields, [])
        self.assertTrue(any("unknown snippet 'cornix-debug-log'" in e for e in errors))


class BuildMatrixAndSnippetTests(unittest.TestCase):
    """check_build_matrices / check_snippets over a synthetic repository root."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write(self.root / cc.BOARD_DIR / "board.yml", BOARD_YML)
        (self.root / cc.SHIELDS_DIR / "cornix_dongle_adapter").mkdir(parents=True)
        self.good_matrix = """
            include:
              - board: cornix_left//zmk
                artifact-name: left
        """

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_optional_matrix_is_skipped(self) -> None:
        write(self.root / "build.yaml", self.good_matrix)
        warnings: list[str] = []
        self.assertEqual(cc.check_build_matrices(self.root, warnings), [])
        self.assertIn("build-debug.yaml: skipped: file does not exist", warnings)

    def test_missing_required_matrix_is_an_error(self) -> None:
        errors = cc.check_build_matrices(self.root, [])
        self.assertEqual(errors, ["build.yaml: required build matrix file is missing"])

    def test_artifact_names_unique_per_file(self) -> None:
        write(self.root / "build.yaml", self.good_matrix)
        write(
            self.root / "build-debug.yaml",
            """
            include:
              - board: cornix_left//zmk
                snippet: zmk-usb-logging cornix-debug-log
                artifact-name: left
              - board: cornix_right//zmk
                snippet: cornix-debug-log
                artifact-name: left
            """,
        )
        write(self.root / "snippets/cornix-debug-log/snippet.yml", "name: cornix-debug-log\n")
        errors = cc.check_build_matrices(self.root, [])
        # "left" in both files is fine; "left" twice in build-debug.yaml is not.
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith("build-debug.yaml: include[1]"))
        self.assertIn("artifact-name 'left' already used", errors[0])

    def test_snippets_absent_is_skipped(self) -> None:
        warnings: list[str] = []
        self.assertEqual(cc.check_snippets(self.root, warnings), [])
        self.assertEqual(warnings, ["snippets: skipped: directory does not exist"])

    def test_snippet_names(self) -> None:
        write(self.root / "snippets/cornix-debug-log/snippet.yml", 'name: "cornix-debug-log"\nappend:\n  EXTRA_CONF_FILE: debug.conf\n')
        write(self.root / "snippets/misnamed/snippet.yml", "name: other\n")
        (self.root / "snippets/empty").mkdir()
        errors = cc.check_snippets(self.root, [])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("snippets/empty: missing snippet.yml" in e for e in errors))
        self.assertTrue(any("snippets/misnamed/snippet.yml: name 'other' must equal directory name 'misnamed'" in e for e in errors))


class WestManifestTests(unittest.TestCase):
    def run_check(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            return cc.check_west_manifest(write(Path(tmp) / "west.yml", text), [])

    def test_valid_manifest(self) -> None:
        errors = self.run_check(
            """
            manifest:
              remotes:
                - name: zmkfirmware
                  url-base: https://github.com/zmkfirmware
              projects:
                - name: zmk
                  remote: zmkfirmware
                  revision: main
                  import:
                    file: app/west.yml
              self:
                path: config
            """
        )
        self.assertEqual(errors, [])

    def test_detects_problems(self) -> None:
        errors = self.run_check(
            """
            manifest:
              remotes:
                - name: zmkfirmware
                  url-base: https://github.com/zmkfirmware
              projects:
                - name: zmk
                  remote: zmkfirmware
                  revision: main
                - name: helper
                  remote: nobody
                  revision: main
              self:
                path: cfg
            """
        )
        self.assertTrue(any("must import app/west.yml" in e for e in errors))
        self.assertTrue(any("undeclared remote 'nobody'" in e for e in errors))
        self.assertTrue(any("self.path must be 'config'" in e for e in errors))


class KconfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def conf(self, name: str, text: str, board: bool = True) -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        errors = cc.check_conf_file(write(self.root / name, text), board, warnings)
        return errors, warnings

    def test_good_defconfigs(self) -> None:
        self.assertEqual(self.conf("cornix_left_defconfig", GOOD_DEFCONFIG_CENTRAL)[0], [])
        self.assertEqual(self.conf("cornix_right_nrf52840_zmk_defconfig", GOOD_DEFCONFIG_PERIPHERAL)[0], [])
        self.assertEqual(self.conf("cornix_ph_left_defconfig", GOOD_DEFCONFIG_PERIPHERAL)[0], [])

    def test_settings_none_is_forbidden(self) -> None:
        errors, _ = self.conf("cornix_left_defconfig", GOOD_DEFCONFIG_CENTRAL + "CONFIG_SETTINGS_NONE=y\n")
        self.assertEqual(len(errors), 1)
        self.assertIn("CONFIG_SETTINGS_NONE=y is forbidden", errors[0])
        errors, _ = self.conf("some_shield.conf", "CONFIG_SETTINGS_NONE=y\n", board=False)
        self.assertEqual(len(errors), 1)

    def test_missing_nvs(self) -> None:
        errors, _ = self.conf("cornix_left_defconfig", "CONFIG_ZMK_SPLIT=y\nCONFIG_ZMK_SPLIT_ROLE_CENTRAL=y\n")
        self.assertTrue(any("CONFIG_NVS=y" in e for e in errors))
        self.assertTrue(any("CONFIG_SETTINGS_NVS=y" in e for e in errors))

    def test_split_roles(self) -> None:
        errors, _ = self.conf("cornix_left_defconfig", GOOD_DEFCONFIG_PERIPHERAL)
        self.assertTrue(any("central half must set" in e for e in errors))
        errors, _ = self.conf("cornix_right_defconfig", GOOD_DEFCONFIG_CENTRAL)
        self.assertTrue(any("peripheral half must not set" in e for e in errors))
        errors, _ = self.conf("cornix_ph_left_defconfig", "CONFIG_NVS=y\nCONFIG_SETTINGS_NVS=y\n")
        self.assertTrue(any("CONFIG_ZMK_SPLIT=y" in e for e in errors))

    def test_duplicate_keys(self) -> None:
        errors, warnings = self.conf(
            "cornix_left_defconfig",
            GOOD_DEFCONFIG_CENTRAL + "CONFIG_ZMK_USB=y\nCONFIG_ZMK_USB=n\nCONFIG_NVS=y\n# CONFIG_ZMK_SPLIT is not set\n",
        )
        self.assertTrue(any("CONFIG_ZMK_USB redefined as 'n'" in e for e in errors))
        self.assertTrue(any("CONFIG_ZMK_SPLIT redefined as 'n'" in e for e in errors))
        self.assertTrue(any("CONFIG_NVS=y duplicates" in w for w in warnings))

    def test_shield_conf_expectations(self) -> None:
        errors, _ = self.conf("cornix_dongle_adapter.conf", "CONFIG_ZMK_SPLIT=y\n", board=False)
        self.assertTrue(any("CONFIG_ZMK_SPLIT_ROLE_CENTRAL=y" in e for e in errors))

    def test_directory_scan(self) -> None:
        write(self.root / cc.BOARD_DIR / "cornix_left_defconfig", GOOD_DEFCONFIG_CENTRAL)
        write(self.root / "boards/shields/x/x.conf", "CONFIG_SETTINGS_NONE=y\n")
        write(self.root / "config/cornix.conf", "CONFIG_ZMK_SLEEP=y\n")
        errors = cc.check_kconfig_fragments(self.root, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("boards/shields/x/x.conf", errors[0])


class LayoutTests(unittest.TestCase):
    GOOD_MAP = ["RC(0,0) RC(0,1) RC(0,2) RC(0,3)", "RC(1,0) RC(1,1) RC(1,2) RC(1,3)"]

    def run_check(self, text: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            return cc.check_layouts_dtsi(write(Path(tmp) / "layouts.dtsi", text), [])

    def test_consistent_layout(self) -> None:
        self.assertEqual(self.run_check(layouts_dtsi(self.GOOD_MAP, 8)), [])

    def test_rc_out_of_range(self) -> None:
        errors = self.run_check(layouts_dtsi(["RC(0,0) RC(0,1) RC(0,2) RC(0,3)", "RC(1,0) RC(1,1) RC(1,2) RC(2, 4)"], 8))
        self.assertEqual(len(errors), 1)
        self.assertIn("RC(2,4) is outside rows=2 columns=4", errors[0])

    def test_key_count_mismatch(self) -> None:
        errors = self.run_check(layouts_dtsi(self.GOOD_MAP, 7))
        self.assertEqual(len(errors), 1)
        self.assertIn("has 7 keys but transform default_transform maps 8 positions", errors[0])

    def test_position_map_mismatch(self) -> None:
        text = layouts_dtsi(self.GOOD_MAP, 8).replace("positions = <0 1 2 3 4 5 6 7>", "positions = <0 1 2 3 4 5 6 6>")
        errors = self.run_check(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("position map layout_0 must list 0..7 exactly once", errors[0])
        text = layouts_dtsi(self.GOOD_MAP, 8).replace("positions = <0 1 2 3 4 5 6 7>", "positions = <0 1 2 3 4 5 6>")
        errors = self.run_check(text)
        self.assertTrue(any("has 7 positions, layout layout_0 has 8 keys" in e for e in errors))

    def test_duplicate_rc(self) -> None:
        errors = self.run_check(layouts_dtsi(["RC(0,0) RC(0,1) RC(0,2) RC(0,3)", "RC(1,0) RC(1,1) RC(1,2) RC(0,0)"], 8))
        self.assertEqual(len(errors), 1)
        self.assertIn("maps RC(0,0) twice", errors[0])

    def test_unknown_transform_reference(self) -> None:
        text = layouts_dtsi(self.GOOD_MAP, 8).replace("transform = <&default_transform>", "transform = <&missing>")
        errors = self.run_check(text)
        self.assertTrue(any("unknown transform &missing" in e for e in errors))

    def test_comments_are_ignored(self) -> None:
        text = layouts_dtsi(self.GOOD_MAP, 8).replace(
            "map = <", "// RC(9,9) in a comment\n        /* RC(8,8) */ map = <"
        )
        self.assertEqual(self.run_check(text), [])


class KeymapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_check(self, text: str, expected: int | None = 50, name: str = "test.keymap") -> tuple[list[str], list[str]]:
        warnings: list[str] = []
        errors = cc.check_keymap(write(self.root / name, text), expected, warnings)
        return errors, warnings

    def test_valid_keymap(self) -> None:
        errors, warnings = self.run_check(keymap_dts({"default_layer": bindings(50), "lower_layer": bindings(50)}))
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_layer_with_49_bindings(self) -> None:
        errors, _ = self.run_check(keymap_dts({"default_layer": bindings(50), "lower_layer": bindings(49)}))
        self.assertTrue(any("different binding counts" in e for e in errors))
        self.assertTrue(any("'lower_layer' has 49 bindings, layout expects 50" in e for e in errors))

    def test_parameters_do_not_count(self) -> None:
        text = keymap_dts(
            {
                "default_layer": "&kp LS(A) &bt BT_SEL 0 &lt 1 SPACE &trans &none",
                "second": "&trans &trans &trans &trans &trans",
            }
        )
        errors, _ = self.run_check(text, expected=5)
        self.assertEqual(errors, [])

    def test_define_expansion(self) -> None:
        text = "#define HOME &kp HOME\n#define NAV 1\n" + keymap_dts(
            {"default_layer": "HOME &mo NAV &kp A", "nav_layer": "&trans &trans &trans"}
        )
        errors, warnings = self.run_check(text, expected=3)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_layer_index_out_of_range(self) -> None:
        text = keymap_dts({"default_layer": "&mo 5 &kp A", "second": "&trans &trans"})
        errors, _ = self.run_check(text, expected=2)
        self.assertTrue(any("references layer 5 but only 2 layers exist" in e for e in errors))

    def test_key_position_out_of_range(self) -> None:
        extra = """
    combos {
        compatible = "zmk,combos";
        combo_esc {
            bindings = <&kp ESC>;
            key-positions = <0 50>;
        };
    };
"""
        text = keymap_dts({"default_layer": bindings(50)}, extra=extra)
        errors, _ = self.run_check(text)
        self.assertEqual(len(errors), 1)
        self.assertIn("combo_esc key-positions references position 50 >= 50", errors[0])

    def test_combo_bindings_do_not_count_as_layers(self) -> None:
        extra = """
    behaviors {
        hm: homerow_mods {
            compatible = "zmk,behavior-hold-tap";
            bindings = <&kp>, <&kp>;
            hold-trigger-key-positions = <0 1>;
        };
    };
"""
        text = keymap_dts({"default_layer": bindings(4)}, extra=extra)
        errors, _ = self.run_check(text, expected=4)
        self.assertEqual(errors, [])

    def test_local_include_defines(self) -> None:
        write(self.root / "includes/pos.h", "#define LT0 5\n#define RP2 60\n#define HM_TAPPING_TERM 250\n")
        extra = """
    behaviors {
        hm: homerow_mods {
            compatible = "zmk,behavior-hold-tap";
            bindings = <&kp>, <&kp>;
            hold-trigger-key-positions = <LT0>;
        };
    };
"""
        text = '#include "includes/pos.h"\n' + keymap_dts({"default_layer": bindings(50)}, extra=extra)
        errors, warnings = self.run_check(text)
        self.assertEqual(errors, [])
        self.assertTrue(any("position macro RP2=60" in w for w in warnings))
        # Timing constants are not key positions and must not be reported.
        self.assertFalse(any("HM_TAPPING_TERM" in w for w in warnings))

    def test_zmk_layer_macro_form(self) -> None:
        text = """
#include "zmk-helpers/helper.h"
ZMK_LAYER(base, &kp A &kp B &mo 1)
ZMK_LAYER(nav, &trans &trans &trans, &inc_dec_kp C_VOL_UP C_VOL_DN)
"""
        errors, warnings = self.run_check(text, expected=3)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        errors, _ = self.run_check(text.replace("&mo 1)", "&mo 1 &kp C)"), expected=3)
        self.assertTrue(any("has 4 bindings, layout expects 3" in e for e in errors))

    def test_function_like_macro_is_skipped(self) -> None:
        text = "#define HRM(k) &hm LCTRL k\n" + keymap_dts({"default_layer": "HRM(A) &kp B"})
        errors, warnings = self.run_check(text, expected=2)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("skipped: layer 'default_layer' uses function-like macro(s) ['HRM']", warnings[0])

    def test_no_keymap_is_skipped(self) -> None:
        errors, warnings = self.run_check("/ { };\n", expected=2)
        self.assertEqual(errors, [])
        self.assertIn("skipped: no zmk,keymap node", warnings[0])

    def test_check_keymaps_uses_targets(self) -> None:
        write(self.root / cc.LAYOUTS_DTSI, layouts_dtsi(LayoutTests.GOOD_MAP, 8))
        write(self.root / "boards/jzf/cornix/cornix.keymap", keymap_dts({"a": bindings(8)}))
        write(self.root / "config/cornix.keymap", keymap_dts({"a": bindings(7)}))
        write(self.root / "config/cornix42.keymap", keymap_dts({"a": bindings(42)}))
        write(
            self.root / "config/cornix42.json",
            '{"layouts": {"default_layout": {"layout": [' + ", ".join("{}" for _ in range(42)) + "]}}}",
        )
        original = cc.KEYMAP_TARGETS
        cc.KEYMAP_TARGETS = {
            "boards/jzf/cornix/cornix.keymap": ("dtsi", "layout_0"),
            "config/cornix.keymap": ("dtsi", "layout_0"),
            "config/cornix42.keymap": ("json", "config/cornix42.json", "default_layout"),
        }
        try:
            errors = cc.check_keymaps(self.root, [])
        finally:
            cc.KEYMAP_TARGETS = original
        self.assertEqual(len(errors), 1)
        self.assertIn("config/cornix.keymap: layer 'a' has 7 bindings, layout expects 8", errors[0])


class MetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_json_files(self) -> None:
        write(self.root / cc.BOARD_DIR / "metadata/good.json", '{"layouts": {"L_2": {"layout": [{}, {}]}}}')
        write(self.root / cc.BOARD_DIR / "metadata/commented.json", '// note\n{"a": 1}')
        write(self.root / "config/broken.json", '{"a": ')
        write(self.root / "config/misnamed.json", '{"layouts": {"LAYOUT_42": {"layout": [{}]}}}')
        warnings: list[str] = []
        errors = cc.check_json_files(self.root, warnings)
        self.assertEqual(len(errors), 1)
        self.assertIn("config/broken.json: invalid JSON", errors[0])
        self.assertTrue(any("commented.json: metadata-json-comments" in w for w in warnings))
        self.assertTrue(any("misnamed.json: metadata-layout-name: layout 'LAYOUT_42' has 1 keys" in w for w in warnings))

    def make_board(self, siblings: str = "  - cornix_left\n  - cornix_right\n") -> None:
        board_dir = self.root / cc.BOARD_DIR
        write(board_dir / "board.yml", BOARD_YML)
        write(board_dir / "cornix.zmk.yml", "file_format: \"1\"\nid: cornix\ntype: board\nsiblings:\n" + siblings)
        for name in ("cornix_left", "cornix_right"):
            write(board_dir / f"Kconfig.{name}", f"config BOARD_{name.upper()}\n")
            write(board_dir / f"{name}_defconfig", GOOD_DEFCONFIG_CENTRAL)
            write(board_dir / f"{name}.dts", "/dts-v1/;\n")

    def test_board_metadata_ok(self) -> None:
        self.make_board()
        self.assertEqual(cc.check_board_metadata(self.root, []), [])

    def test_board_metadata_bad_sibling_and_missing_file(self) -> None:
        self.make_board(siblings="  - cornix_left\n  - cornix_ghost\n")
        (self.root / cc.BOARD_DIR / "cornix_right.dts").unlink()
        warnings: list[str] = []
        errors = cc.check_board_metadata(self.root, warnings)
        self.assertTrue(any("sibling 'cornix_ghost' is not defined" in e for e in errors))
        self.assertTrue(any("missing cornix_right.dts" in e for e in errors))
        self.assertTrue(any("'cornix_right' from board.yml is not listed" in w for w in warnings))

    def make_shield(self, name: str, meta_id: str | None = None, overlay: bool = True, contains: str | None = None) -> None:
        shield_dir = self.root / cc.SHIELDS_DIR / name
        write(
            shield_dir / "Kconfig.shield",
            f"config SHIELD_{name.upper()}\n\tdef_bool $(shields_list_contains,{contains or name})\n",
        )
        write(shield_dir / "Kconfig.defconfig", "")
        if overlay:
            write(shield_dir / f"{name}.overlay", "/ { };\n")
        write(shield_dir / "shield.yml", f"file_format: \"1\"\nid: {meta_id or name}\ntype: shield\n")

    def test_shields_ok(self) -> None:
        self.make_shield("cornix_thing")
        self.assertEqual(cc.check_shield_dirs(self.root / cc.SHIELDS_DIR, []), [])

    def test_shield_problems(self) -> None:
        self.make_shield("cornix_a", meta_id="cornix_b", overlay=False)
        self.make_shield("cornix_c", contains="cornix_x")
        self.make_shield("cornix_d", contains=" cornix_d")
        warnings: list[str] = []
        errors = cc.check_shield_dirs(self.root / cc.SHIELDS_DIR, warnings)
        self.assertTrue(any("cornix_a: missing cornix_a.overlay" in e for e in errors))
        self.assertTrue(any("id 'cornix_b' must equal directory name 'cornix_a'" in e for e in errors))
        self.assertTrue(any("shields_list_contains argument 'cornix_x' != 'cornix_c'" in e for e in errors))
        self.assertTrue(any("cornix_d: shield-kconfig-style" in w for w in warnings))


class CliTests(unittest.TestCase):
    def test_failure_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cc.main(["--root", tmp, "--quiet"]), 1)
            self.assertIn("Config validation failed", err.getvalue())
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cc.main(["--root", tmp, "--json"]), 1)
            payload = json.loads(out.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(set(payload["checks"]), set(name for name, _ in cc.CHECKS))

    def test_success_output(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cc.main(["--root", str(REPO_ROOT)]), 0)
        self.assertIn("Config validation passed", out.getvalue())
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
