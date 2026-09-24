"""Unit tests for scripts/flash.py (stdlib unittest only).

Run with:  python3 -m unittest discover -s tests/flash -v

Everything here is off-line: the PowerShell samples are captured output, the
filesystem is a tempdir, and the flashing flow runs against a fake Host.
No keyboard, no bootloader drive and no powershell.exe are needed.
"""

from __future__ import annotations

import io
import os
import signal
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import flash  # noqa: E402

# --------------------------------------------------------------------------
# captured sample text
# --------------------------------------------------------------------------

# Real output of
#   powershell.exe -NoProfile -NonInteractive -Command "[Console]::OutputEncoding=
#   [System.Text.Encoding]::UTF8; Get-CimInstance Win32_LogicalDisk |
#   Select-Object DeviceID,DriveType,VolumeName | ConvertTo-Csv -NoTypeInformation"
# on the development machine, with an "E:" removable UF2 drive added.
LOGICAL_DISK_CSV = '''"DeviceID","DriveType","VolumeName"
"C:","3","Windows"
"D:","3","ボリューム"
"E:","2","CORNIX"
'''

# Same host, with no removable drive attached - this is what --list sees when
# no keyboard is in its bootloader.
LOGICAL_DISK_CSV_NONE = '''"DeviceID","DriveType","VolumeName"
"C:","3","Windows"
"D:","3","ボリューム"
'''

# Get-Volume | Select-Object DriveLetter,FileSystemLabel,DriveType | ConvertTo-Csv
# The first row is the unlettered recovery partition Windows always reports.
GET_VOLUME_CSV = '''"DriveLetter","FileSystemLabel","DriveType"
,"Windows RE tools","Fixed"
"C","Windows","Fixed"
"D","ボリューム","Fixed"
"E","CORNIX","Removable"
'''

# PowerShell prints *nothing at all* (not even a header) for an empty result.
GET_VOLUME_CSV_EMPTY = "\n"

# INFO_UF2.TXT as written by the Adafruit nRF52840 UF2 bootloader.
INFO_UF2_TEXT = (
    "UF2 Bootloader 0.6.0 lib/nrfx (v2.0.0) lib/tinyusb (0.10.1-293-gaf8e5a90) "
    "lib/uf2 (remotes/origin/configupdate-9-gadbb8c7)\r\n"
    "Model: Cornix\r\n"
    "Board-ID: nRF52840-cornix-v1\r\n"
    "SoftDevice: S140 version 6.1.1\r\n"
    "Date: Jun 30 2021\r\n"
)

FAIL_TXT = "file contains an address outside of the allowed range\r\n"


def make_uf2(directory: str, name: str = "cornix_left.uf2", blocks: int = 2) -> str:
    """Write a file that starts with the UF2 magic, like a real firmware image."""
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write((flash.UF2_MAGIC + b"\x00" * 508) * blocks)
    return path


class InfoUf2ParseTest(unittest.TestCase):
    def test_keys_and_banner(self) -> None:
        info = flash.parse_info_uf2(INFO_UF2_TEXT)
        self.assertEqual(info["Model"], "Cornix")
        self.assertEqual(info["Board-ID"], "nRF52840-cornix-v1")
        self.assertEqual(info["SoftDevice"], "S140 version 6.1.1")
        self.assertEqual(info["Date"], "Jun 30 2021")
        # The banner has no "Key:" prefix, so it is filed under Bootloader.
        self.assertTrue(info["Bootloader"].startswith("UF2 Bootloader 0.6.0"))

    def test_explicit_bootloader_line_wins_over_the_banner(self) -> None:
        info = flash.parse_info_uf2("some banner\nBootloader: 0.9.2\nBoard-ID: x\n")
        self.assertEqual(info["Bootloader"], "0.9.2")

    def test_blank_lines_and_crlf_and_prose_are_ignored(self) -> None:
        info = flash.parse_info_uf2(
            "UF2 Bootloader 0.6.0\r\n\r\nModel: Cornix\r\n"
            "this line: has a space before the colon\r\n"
        )
        self.assertEqual(sorted(info), ["Bootloader", "Model"])

    def test_empty_file(self) -> None:
        self.assertEqual(flash.parse_info_uf2(""), {})

    def test_info_get_is_case_insensitive(self) -> None:
        info = flash.parse_info_uf2(INFO_UF2_TEXT)
        self.assertEqual(flash.info_get(info, "board-id"), "nRF52840-cornix-v1")
        self.assertEqual(flash.info_get(info, "BOARD-ID"), "nRF52840-cornix-v1")
        self.assertIsNone(flash.info_get(info, "nope"))

    def test_describe_info_order_and_omissions(self) -> None:
        lines = flash.describe_info(flash.parse_info_uf2(INFO_UF2_TEXT))
        self.assertEqual(lines[0], "Model: Cornix")
        self.assertEqual(lines[1], "Board-ID: nRF52840-cornix-v1")
        self.assertTrue(lines[2].startswith("Bootloader: UF2 Bootloader 0.6.0"))
        self.assertEqual(flash.describe_info({}), [])


class PowerShellParseTest(unittest.TestCase):
    def test_csv_rows(self) -> None:
        rows = flash.parse_powershell_csv(LOGICAL_DISK_CSV)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["DeviceID"], "E:")
        self.assertEqual(rows[1]["VolumeName"], "ボリューム")  # UTF-8 round trip

    def test_csv_empty_and_bom(self) -> None:
        self.assertEqual(flash.parse_powershell_csv(""), [])
        self.assertEqual(flash.parse_powershell_csv(GET_VOLUME_CSV_EMPTY), [])
        rows = flash.parse_powershell_csv("\ufeff" + LOGICAL_DISK_CSV)
        self.assertEqual(rows[0]["DeviceID"], "C:")

    def test_logical_disks_keeps_only_drive_type_2(self) -> None:
        self.assertEqual(flash.parse_logical_disks(LOGICAL_DISK_CSV), [("E:", "CORNIX")])
        self.assertEqual(flash.parse_logical_disks(LOGICAL_DISK_CSV_NONE), [])

    def test_logical_disks_normalises_the_drive_letter(self) -> None:
        csv_text = '"DeviceID","DriveType","VolumeName"\n"e","2",""\n'
        self.assertEqual(flash.parse_logical_disks(csv_text), [("E:", "")])

    def test_get_volume_labels_skip_the_unlettered_partition(self) -> None:
        volumes = flash.parse_get_volume(GET_VOLUME_CSV)
        self.assertEqual({drive: label for drive, (label, _) in volumes.items()},
                         {"C:": "Windows", "D:": "ボリューム", "E:": "CORNIX"})

    def test_get_volume_removable(self) -> None:
        def removable(csv_text: str) -> list[tuple[str, str]]:
            return [(drive, label)
                    for drive, (label, is_removable) in flash.parse_get_volume(csv_text).items()
                    if is_removable]

        self.assertEqual(removable(GET_VOLUME_CSV), [("E:", "CORNIX")])
        self.assertEqual(removable(GET_VOLUME_CSV_EMPTY), [])


class PathConversionTest(unittest.TestCase):
    def test_drive_mount_point(self) -> None:
        self.assertEqual(flash.drive_mount_point("E:"), "/mnt/e")
        self.assertEqual(flash.drive_mount_point("e"), "/mnt/e")

    def test_windows_root_and_join(self) -> None:
        self.assertEqual(flash.windows_root("e:"), "E:\\")
        self.assertEqual(flash.windows_join("E:", "INFO_UF2.TXT"), "E:\\INFO_UF2.TXT")

    def test_powershell_quoting_escapes_single_quotes(self) -> None:
        self.assertEqual(flash._ps_quote(r"E:\a b"), r"'E:\a b'")
        self.assertEqual(flash._ps_quote("it's"), "'it''s'")

    def test_info_globs_per_platform(self) -> None:
        self.assertEqual(flash.uf2_info_globs("macos"), ["/Volumes/*/INFO_UF2.TXT"])
        linux = flash.uf2_info_globs("linux")
        self.assertIn("/media/*/*/INFO_UF2.TXT", linux)
        self.assertIn("/run/media/*/*/INFO_UF2.TXT", linux)
        self.assertEqual(flash.uf2_info_globs("wsl"), linux)

    def test_uf2_magic(self) -> None:
        self.assertTrue(flash.looks_like_uf2(b"UF2\n\x00\x00"))
        self.assertFalse(flash.looks_like_uf2(b"\x7fELF"))
        self.assertFalse(flash.looks_like_uf2(b""))


def vol(label: str, path: str | None = None, drive: str | None = None) -> flash.Volume:
    return flash.Volume(label=label, path=path, drive=drive,
                        info=flash.parse_info_uf2(INFO_UF2_TEXT))


class VolumeSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.left = vol("CORNIX", path="/mnt/e", drive="E:")
        self.right = vol("CORNIXR", path="/media/me/CORNIXR")

    def test_single_volume_is_chosen(self) -> None:
        chosen, problem = flash.select_volume([self.left])
        self.assertIs(chosen, self.left)
        self.assertIsNone(problem)

    def test_no_volume(self) -> None:
        chosen, problem = flash.select_volume([])
        self.assertIsNone(chosen)
        self.assertIn("no UF2 volume", problem)

    def test_two_volumes_refuse_without_volume_option(self) -> None:
        chosen, problem = flash.select_volume([self.left, self.right])
        self.assertIsNone(chosen)
        self.assertIn("2 UF2 volumes", problem)
        self.assertIn("--volume", problem)
        self.assertIn("CORNIXR", problem)

    def test_volume_option_by_drive_letter_label_and_path(self) -> None:
        for wanted in ("E:", "e", "e:", "/mnt/e", "/mnt/e/", "cornix", "CORNIX"):
            with self.subTest(wanted=wanted):
                chosen, problem = flash.select_volume([self.left, self.right], wanted)
                self.assertIsNone(problem)
                self.assertIs(chosen, self.left)
        for wanted in ("CORNIXR", "/media/me/CORNIXR", "cornixr"):
            with self.subTest(wanted=wanted):
                chosen, problem = flash.select_volume([self.left, self.right], wanted)
                self.assertIsNone(problem)
                self.assertIs(chosen, self.right)

    def test_volume_option_that_matches_nothing(self) -> None:
        chosen, problem = flash.select_volume([self.left], "Z:")
        self.assertIsNone(chosen)
        self.assertIn("no UF2 volume matches", problem)
        self.assertIn("E:", problem)

    def test_volume_option_that_matches_two(self) -> None:
        twin = vol("CORNIX", path="/media/me/CORNIX")
        chosen, problem = flash.select_volume([self.left, twin], "CORNIX")
        self.assertIsNone(chosen)
        self.assertIn("ambiguous", problem)

    def test_empty_volume_option_is_treated_as_unset(self) -> None:
        self.assertFalse(flash.volume_matches(self.left, ""))

    def test_describe_mentions_whether_wsl_can_see_the_drive(self) -> None:
        self.assertIn("mounted at /mnt/e", self.left.describe())
        self.assertIn("not mounted in WSL", vol("CORNIX", drive="E:").describe())
        self.assertEqual(vol("CORNIX", drive="E:").key, "E:")
        self.assertEqual(self.right.key, "/media/me/CORNIXR")


class LabelSanityTest(unittest.TestCase):
    def test_matching_label(self) -> None:
        self.assertIsNone(flash.label_mismatch("firmware/keymap/cornix_left.uf2", "left"))
        self.assertIsNone(flash.label_mismatch("cornix_left.uf2", None))

    def test_swapped_label(self) -> None:
        message = flash.label_mismatch("firmware/keymap/cornix_right.uf2", "left")
        self.assertIsNotNone(message)
        self.assertIn("right", message)

    def test_unrelated_name_is_not_flagged(self) -> None:
        self.assertIsNone(flash.label_mismatch("build/zmk.uf2", "left"))


class PosixDiscoveryTest(unittest.TestCase):
    def test_finds_info_uf2_and_skips_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "media", "me", "CORNIX")
            os.makedirs(root)
            Path(root, flash.INFO_FILE).write_text(INFO_UF2_TEXT, encoding="utf-8")
            other = os.path.join(tmp, "media", "me", "USBSTICK")
            os.makedirs(other)  # no INFO_UF2.TXT -> not a bootloader drive
            host = flash.Host("linux")
            with mock.patch.object(flash, "uf2_info_globs", return_value=[
                os.path.join(tmp, "media/*/*", flash.INFO_FILE),
                os.path.join(tmp, "media/*/*", flash.INFO_FILE),  # same dir twice
            ]):
                volumes = host.find_volumes()
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0].label, "CORNIX")
        self.assertEqual(volumes[0].info["Board-ID"], "nRF52840-cornix-v1")
        self.assertIsNone(volumes[0].drive)


class WslDiscoveryTest(unittest.TestCase):
    """_find_volumes_wsl with powershell.exe replaced by the captured samples."""

    def _fake_powershell(self, info_text: str | None):
        def run(script: str, timeout: float = 30.0):
            if "Win32_LogicalDisk" in script:
                return 0, LOGICAL_DISK_CSV, ""
            if "Get-Volume" in script:
                return 0, GET_VOLUME_CSV, ""
            if "INFO_UF2.TXT" in script:
                return (0, info_text, "") if info_text else (0, "", "")
            if "FAIL.TXT" in script:
                return 0, FAIL_TXT, ""
            raise AssertionError(f"unexpected PowerShell script: {script}")
        return run

    def test_unmounted_drive_is_read_through_powershell(self) -> None:
        host = flash.Host("wsl")
        with mock.patch.object(flash, "run_powershell", self._fake_powershell(INFO_UF2_TEXT)), \
                mock.patch.object(os.path, "isdir", return_value=False):
            volumes = host.find_volumes()
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0].drive, "E:")
        self.assertEqual(volumes[0].label, "CORNIX")
        self.assertIsNone(volumes[0].path)
        self.assertEqual(volumes[0].info["Model"], "Cornix")

    def test_removable_drive_without_info_uf2_is_not_a_bootloader(self) -> None:
        host = flash.Host("wsl")
        with mock.patch.object(flash, "run_powershell", self._fake_powershell(None)), \
                mock.patch.object(os.path, "isdir", return_value=False):
            self.assertEqual(host.find_volumes(), [])

    def test_mounted_drive_is_read_from_mnt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, flash.INFO_FILE).write_text(INFO_UF2_TEXT, encoding="utf-8")
            host = flash.Host("wsl")
            with mock.patch.object(flash, "run_powershell", self._fake_powershell(None)), \
                    mock.patch.object(flash, "drive_mount_point", return_value=tmp):
                volumes = host.find_volumes()
        self.assertEqual(len(volumes), 1)
        self.assertEqual(volumes[0].path, tmp)
        self.assertEqual(volumes[0].drive, "E:")

    def test_no_removable_drive(self) -> None:
        def run(script: str, timeout: float = 30.0):
            if "Win32_LogicalDisk" in script:
                return 0, LOGICAL_DISK_CSV_NONE, ""
            return 0, GET_VOLUME_CSV_EMPTY, ""
        host = flash.Host("wsl")
        with mock.patch.object(flash, "run_powershell", run):
            self.assertEqual(host.find_volumes(), [])

    def test_get_volume_finds_a_drive_win32_logicaldisk_missed(self) -> None:
        def run(script: str, timeout: float = 30.0):
            if "Win32_LogicalDisk" in script:
                return 0, LOGICAL_DISK_CSV_NONE, ""
            if "Get-Volume" in script:
                return 0, GET_VOLUME_CSV, ""
            return 0, INFO_UF2_TEXT, ""
        host = flash.Host("wsl")
        with mock.patch.object(flash, "run_powershell", run), \
                mock.patch.object(os.path, "isdir", return_value=False):
            volumes = host.find_volumes()
        self.assertEqual([v.drive for v in volumes], ["E:"])


class FakeHost(flash.Host):
    """A Host whose drive comes and goes on command."""

    def __init__(self, volumes: list[flash.Volume], *, present_after_copy: bool = False,
                 disconnect: bool = False, fail_txt: str | None = None) -> None:
        super().__init__("linux")
        self._volumes = volumes
        self.present_after_copy = present_after_copy
        self.disconnect = disconnect
        self.fail_txt = fail_txt
        self.copied: list[tuple[str, str]] = []

    def find_volumes(self) -> list[flash.Volume]:
        return list(self._volumes)

    def volume_present(self, volume: flash.Volume) -> bool:
        return self.present_after_copy if self.copied else True

    def read_fail_txt(self, volume: flash.Volume) -> str | None:
        return self.fail_txt

    def copy(self, source: str, volume: flash.Volume) -> tuple[bool, str]:
        self.copied.append((source, volume.key))
        if self.disconnect:
            return True, "write interrupted (Input/output error)"
        return False, ""


def run_main(host: flash.Host, argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(flash, "detect_platform", return_value=host.system), \
            mock.patch.object(flash, "Host", return_value=host), \
            redirect_stdout(out), redirect_stderr(err):
        status = flash.main(argv)
    return status, out.getvalue(), err.getvalue()


class FlashFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.uf2 = make_uf2(self.tmp.name)
        self.volume = vol("CORNIX", path=os.path.join(self.tmp.name, "mnt"))

    def test_success(self) -> None:
        host = FakeHost([self.volume])
        status, out, _ = run_main(host, [self.uf2, "--label", "left", "--settle", "0"])
        self.assertEqual(status, 0)
        self.assertEqual(len(host.copied), 1)
        self.assertIn("Board-ID: nRF52840-cornix-v1", out)
        self.assertIn("flashing the left half", out)
        self.assertIn("ok:", out)

    def test_disconnect_during_copy_is_success(self) -> None:
        host = FakeHost([self.volume], disconnect=True)
        status, out, _ = run_main(host, [self.uf2, "--settle", "0"])
        self.assertEqual(status, 0)
        self.assertIn("normal when the bootloader reboots mid-copy", out)
        self.assertIn("ok:", out)

    def test_volume_still_present_prints_fail_txt(self) -> None:
        host = FakeHost([self.volume], present_after_copy=True, fail_txt=FAIL_TXT)
        status, _out, err = run_main(host, [self.uf2, "--settle", "0"])
        self.assertEqual(status, 1)
        self.assertIn("may have failed", err)
        self.assertIn("outside of the allowed range", err)

    def test_volume_still_present_without_fail_txt(self) -> None:
        host = FakeHost([self.volume], present_after_copy=True)
        status, _out, err = run_main(host, [self.uf2, "--settle", "0"])
        self.assertEqual(status, 1)
        self.assertIn("no FAIL.TXT", err)

    def test_two_volumes_exit_4(self) -> None:
        host = FakeHost([self.volume, vol("CORNIX2", path="/media/me/CORNIX2")])
        status, _out, err = run_main(host, [self.uf2])
        self.assertEqual(status, 4)
        self.assertIn("refusing to guess", err)
        self.assertEqual(host.copied, [])

    def test_two_volumes_with_volume_option(self) -> None:
        host = FakeHost([self.volume, vol("CORNIX2", path="/media/me/CORNIX2")])
        status, _out, _err = run_main(host, [self.uf2, "--volume", "CORNIX2", "--settle", "0"])
        self.assertEqual(status, 0)
        self.assertEqual(host.copied[0][1], "/media/me/CORNIX2")

    def test_no_volume_exits_3(self) -> None:
        host = FakeHost([])
        status, _out, err = run_main(host, [self.uf2, "--timeout", "0"])
        self.assertEqual(status, 3)
        self.assertIn("no UF2 volume appeared", err)
        self.assertEqual(host.copied, [])

    def test_dry_run_writes_nothing(self) -> None:
        host = FakeHost([self.volume])
        status, out, _err = run_main(host, [self.uf2, "--dry-run"])
        self.assertEqual(status, 0)
        self.assertEqual(host.copied, [])
        self.assertIn("dry run: would copy", out)

    def test_unmatched_volume_option_exits_4(self) -> None:
        host = FakeHost([self.volume])
        status, _out, err = run_main(host, [self.uf2, "--volume", "Z:"])
        self.assertEqual(status, 4)
        self.assertIn("no UF2 volume matches", err)
        self.assertEqual(host.copied, [])

    def test_missing_file_exits_2(self) -> None:
        host = FakeHost([self.volume])
        status, _out, err = run_main(host, [os.path.join(self.tmp.name, "nope.uf2")])
        self.assertEqual(status, 2)
        self.assertIn("no such file", err)

    def test_non_uf2_file_exits_2(self) -> None:
        path = os.path.join(self.tmp.name, "not.uf2")
        Path(path).write_bytes(b"\x7fELF not a uf2")
        host = FakeHost([self.volume])
        status, _out, err = run_main(host, [path])
        self.assertEqual(status, 2)
        self.assertIn("UF2 magic", err)
        self.assertEqual(host.copied, [])

    def test_label_mismatch_warns_but_proceeds(self) -> None:
        right = make_uf2(self.tmp.name, "cornix_right.uf2")
        host = FakeHost([self.volume])
        status, _out, err = run_main(host, [right, "--label", "left", "--settle", "0"])
        self.assertEqual(status, 0)
        self.assertIn("looks like the right firmware", err)

    def test_list(self) -> None:
        host = FakeHost([self.volume])
        status, out, _err = run_main(host, ["--list"])
        self.assertEqual(status, 0)
        self.assertIn("UF2 volumes: 1", out)
        self.assertIn("Model: Cornix", out)

    def test_list_with_nothing_attached(self) -> None:
        status, out, _err = run_main(FakeHost([]), ["--list"])
        self.assertEqual(status, 0)
        self.assertIn("UF2 volumes: (none)", out)

    def test_wait_only_does_not_copy(self) -> None:
        host = FakeHost([self.volume])
        status, out, _err = run_main(host, ["--wait-only"])
        self.assertEqual(status, 0)
        self.assertEqual(host.copied, [])
        self.assertIn("Board-ID", out)

    def test_wait_only_timeout(self) -> None:
        status, _out, err = run_main(FakeHost([]), ["--wait-only", "--timeout", "0"])
        self.assertEqual(status, 3)
        self.assertIn("no UF2 volume appeared", err)

    def test_file_is_required_for_a_flash(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            run_main(FakeHost([self.volume]), [])
        self.assertEqual(caught.exception.code, 2)


# --------------------------------------------------------------------------
# --enter: the "1200 bps touch" that puts a half into its bootloader
# --------------------------------------------------------------------------
class EnterArgumentTest(unittest.TestCase):
    def test_baud_table_matches_the_firmware(self) -> None:
        # src/remote_boot.c: RATE_LOCAL_BOOTLOADER / RATE_PERIPHERAL_BOOTLOADER
        # / RATE_RESET.  If these ever drift apart the device silently ignores
        # the rate, so pin them here.
        self.assertEqual(flash.ENTER_BAUDS["left"], 1200)
        self.assertEqual(flash.ENTER_BAUDS["right"], 2400)
        self.assertEqual(flash.ENTER_BAUDS["reset-left"], 4800)
        self.assertNotIn("dongle", flash.ENTER_BAUDS)

    def test_parser_accepts_enter_targets(self) -> None:
        parser = flash.build_parser()
        for target in ("left", "right", "reset-left"):
            self.assertEqual(parser.parse_args(["--enter", target]).enter, target)
        args = parser.parse_args(["--enter", "left", "fw.uf2", "--com", "COM7"])
        self.assertEqual((args.enter, args.file, args.com), ("left", "fw.uf2", "COM7"))
        self.assertTrue(args.release)
        self.assertFalse(parser.parse_args(["--enter", "left", "--no-release"]).release)

    def test_parser_rejects_unknown_target(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            flash.build_parser().parse_args(["--enter", "middle"])

    def test_sub_action_spelling(self) -> None:
        self.assertEqual(flash.normalize_argv(["enter", "right"]), ["--enter", "right"])
        self.assertEqual(flash.normalize_argv(["enter", "left", "fw.uf2", "--com", "COM7"]),
                         ["--enter", "left", "fw.uf2", "--com", "COM7"])
        # Not a target: left for argparse to complain about.
        self.assertEqual(flash.normalize_argv(["enter"]), ["--enter"])
        self.assertEqual(flash.normalize_argv(["enter", "--help"]), ["--enter", "--help"])
        # Untouched when the first word is not the sub-action.
        self.assertEqual(flash.normalize_argv(["fw.uf2", "--label", "left"]),
                         ["fw.uf2", "--label", "left"])
        self.assertEqual(flash.normalize_argv([]), [])

    def test_is_com_name(self) -> None:
        self.assertTrue(flash.is_com_name("COM7"))
        self.assertTrue(flash.is_com_name("com12"))
        self.assertFalse(flash.is_com_name("/dev/ttyACM0"))
        self.assertFalse(flash.is_com_name("COMX"))
        self.assertFalse(flash.is_com_name(""))


class PowerShellTouchTest(unittest.TestCase):
    def test_script_contents(self) -> None:
        script = flash.powershell_touch_script("COM7", 1200, 0.3)
        self.assertIn("New-Object System.IO.Ports.SerialPort 'COM7',1200,'None',8,'One'", script)
        self.assertIn("$p.DtrEnable = $true", script)
        self.assertIn("$p.Open()", script)
        self.assertIn("Start-Sleep -Milliseconds 300", script)
        # The close must survive a failure in between or the COM port stays
        # locked on the Windows side and capture.py cannot have it back.
        self.assertIn("finally { $p.Close(); $p.Dispose() }", script)

    def test_dwell_is_milliseconds(self) -> None:
        self.assertIn("Start-Sleep -Milliseconds 1500",
                      flash.powershell_touch_script("COM3", 2400, 1.5))
        self.assertIn("Start-Sleep -Milliseconds 0",
                      flash.powershell_touch_script("COM3", 2400, 0))

    def test_quoting_is_powershell_safe(self) -> None:
        self.assertIn("'CO''M'", flash.powershell_touch_script("CO'M", 4800, 0.1))

    def test_touch_port_runs_powershell_for_a_com_name(self) -> None:
        host = flash.Host("wsl")
        with mock.patch.object(flash, "run_powershell",
                               return_value=(0, "touched COM7 @ 1200", "")) as run:
            ok, note = host.touch_port("COM7", 1200, 0.3)
        self.assertTrue(ok)
        self.assertEqual(note, "touched COM7 @ 1200")
        script = run.call_args[0][0]
        self.assertIn("'COM7',1200", script)

    def test_touch_port_reports_access_denied(self) -> None:
        host = flash.Host("wsl")
        with mock.patch.object(flash, "run_powershell",
                               return_value=(1, "", "Access to the port 'COM7' is denied.")):
            ok, note = host.touch_port("COM7", 1200, 0.0)
        self.assertFalse(ok)
        self.assertIn("denied", note)
        self.assertIn("capture.py", note)

    def test_touch_port_uses_termios_for_a_device_path(self) -> None:
        host = flash.Host("wsl")  # usbipd case: a COM-less port name on WSL
        with mock.patch.object(host, "_touch_termios",
                               return_value=(True, "ok")) as termios_touch, \
                mock.patch.object(flash, "run_powershell") as run:
            host.touch_port("/dev/ttyACM0", 1200, 0.0)
        termios_touch.assert_called_once()
        run.assert_not_called()


class EnterFlowTest(unittest.TestCase):
    """--enter end to end, against a Host whose touch_port/find_console_port are fakes."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.uf2 = make_uf2(self.tmp.name)
        self.volume = vol("CORNIX", path=os.path.join(self.tmp.name, "mnt"))

    def host(self, **kwargs) -> flash.Host:
        host = FakeHost([self.volume], **kwargs)
        host.touched: list[tuple[str, int, float]] = []
        host.find_console_port = lambda com=None, port=None: (com or port or "COM7", None)

        def touch(port, baud, dwell=flash.DEFAULT_TOUCH_DWELL):
            host.touched.append((port, baud, dwell))
            return True, "touched"

        host.touch_port = touch
        return host

    def test_enter_left_touches_1200_and_waits(self) -> None:
        host = self.host()
        status, out, _ = run_main(host, ["--enter", "left", "--no-release"])
        self.assertEqual(status, 0)
        self.assertEqual(host.touched, [("COM7", 1200, flash.DEFAULT_TOUCH_DWELL)])
        self.assertIn("1200 baud", out)
        self.assertEqual(host.copied, [])  # no file argument: wait-only

    def test_enter_right_uses_2400(self) -> None:
        host = self.host()
        status, out, _ = run_main(host, ["enter", "right", "--no-release"])
        self.assertEqual(status, 0)
        self.assertEqual(host.touched[0][1], 2400)
        self.assertIn("over the split link", out)

    def test_enter_reset_left_does_not_wait_for_a_volume(self) -> None:
        host = self.host()
        status, out, _ = run_main(host, ["--enter", "reset-left", "--no-release"])
        self.assertEqual(status, 0)
        self.assertEqual(host.touched[0][1], 4800)
        self.assertIn("reset requested", out)

    def test_enter_then_flash(self) -> None:
        host = self.host()
        status, out, _ = run_main(
            host, ["--enter", "left", self.uf2, "--no-release", "--settle", "0"])
        self.assertEqual(status, 0)
        self.assertEqual(len(host.copied), 1)
        self.assertIn("flashing the left half", out)

    def test_enter_reports_a_missing_port(self) -> None:
        host = FakeHost([self.volume])
        host.find_console_port = lambda com=None, port=None: (None, "no Windows COM port found")
        status, _out, err = run_main(host, ["--enter", "left", "--no-release"])
        self.assertEqual(status, 5)
        self.assertIn("no Windows COM port found", err)

    def test_enter_reports_a_failed_touch(self) -> None:
        host = self.host()
        host.touch_port = lambda port, baud, dwell=0.3: (False, "PowerShell exited 1: denied")
        status, _out, err = run_main(host, ["--enter", "left", "--no-release"])
        self.assertEqual(status, 5)
        self.assertIn("denied", err)

    def test_dry_run_prints_the_powershell_command_and_sends_nothing(self) -> None:
        host = self.host()
        status, out, _ = run_main(host, ["--enter", "left", "--no-release", "--dry-run"])
        self.assertEqual(status, 0)
        self.assertEqual(host.touched, [])
        self.assertIn("SerialPort 'COM7',1200", out)
        self.assertIn("nothing was sent", out)

    def test_dry_run_describes_the_termios_path_for_a_device(self) -> None:
        host = self.host()
        status, out, _ = run_main(
            host, ["--enter", "left", "--no-release", "--dry-run", "--port", "/dev/ttyACM0"])
        self.assertEqual(status, 0)
        self.assertIn("B1200", out)


class CaptureReleaseTest(unittest.TestCase):
    """flash.py asks a running capture.py to let go of the port (SIGUSR1)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def pidfile(self, label: str, text: str) -> str:
        path = os.path.join(self.tmp.name, f"capture-{label}.pid")
        with open(path, "w", encoding="ascii") as handle:
            handle.write(text)
        return path

    def test_finds_and_signals_every_capture(self) -> None:
        self.pidfile("left", "4242\n")
        self.pidfile("right", "4243")
        sent: list[tuple[int, int]] = []
        signalled = flash.release_captures(
            self.tmp.name, pause=0, kill=lambda pid, sig: sent.append((pid, sig)),
            verbose=False)
        self.assertEqual(sorted(pid for pid, _ in sent), [4242, 4243])
        self.assertTrue(all(sig == signal.SIGUSR1 for _, sig in sent))
        self.assertEqual(len(signalled), 2)

    def test_ignores_garbage_and_stale_pidfiles(self) -> None:
        self.pidfile("left", "not-a-pid")
        self.pidfile("right", "")
        self.pidfile("dongle", "4242")

        def kill(pid, sig):
            raise ProcessLookupError

        signalled = flash.release_captures(self.tmp.name, pause=0, kill=kill, verbose=False)
        self.assertEqual(signalled, [])
        # A stale pidfile is left alone: it may belong to someone else.
        self.assertTrue(os.path.exists(os.path.join(self.tmp.name, "capture-dongle.pid")))

    def test_no_pidfiles_is_not_an_error(self) -> None:
        self.assertEqual(flash.release_captures(self.tmp.name, pause=0, verbose=False), [])

    def test_read_pid(self) -> None:
        # flash.py reads the pidfiles with capture.py's own reader.
        self.assertEqual(flash.capture.read_pidfile(self.pidfile("left", " 17 \n")), 17)
        self.assertIsNone(flash.capture.read_pidfile(self.pidfile("right", "-3")))
        self.assertIsNone(flash.capture.read_pidfile(os.path.join(self.tmp.name, "nope.pid")))

    def test_enter_releases_by_default(self) -> None:
        self.pidfile("left", "4242")
        host = FakeHost([vol("CORNIX", path=os.path.join(self.tmp.name, "mnt"))])
        host.find_console_port = lambda com=None, port=None: ("COM7", None)
        host.touch_port = lambda port, baud, dwell=0.3: (True, "")
        with mock.patch.object(flash.os, "kill") as kill:
            status, _out, _err = run_main(
                host, ["--enter", "reset-left", "--log-dir", self.tmp.name,
                       "--release-pause", "0"])
        self.assertEqual(status, 0)
        kill.assert_called_once_with(4242, signal.SIGUSR1)

    def test_no_release_skips_the_signal(self) -> None:
        self.pidfile("left", "4242")
        host = FakeHost([vol("CORNIX", path=os.path.join(self.tmp.name, "mnt"))])
        host.find_console_port = lambda com=None, port=None: ("COM7", None)
        host.touch_port = lambda port, baud, dwell=0.3: (True, "")
        with mock.patch.object(flash.os, "kill") as kill:
            run_main(host, ["--enter", "reset-left", "--log-dir", self.tmp.name,
                            "--no-release"])
        kill.assert_not_called()


class PlatformTest(unittest.TestCase):
    def test_detect(self) -> None:
        with mock.patch.object(flash.platform, "system", return_value="Darwin"):
            self.assertEqual(flash.detect_platform(), "macos")
        # WSL detection is capture.py's (/proc/version + WSL_DISTRO_NAME).
        with mock.patch.object(flash.platform, "system", return_value="Linux"), \
                mock.patch.object(flash.capture, "is_wsl", return_value=True):
            self.assertEqual(flash.detect_platform(), "wsl")
        with mock.patch.object(flash.platform, "system", return_value="Linux"), \
                mock.patch.object(flash.capture, "is_wsl", return_value=False):
            self.assertEqual(flash.detect_platform(), "linux")

    def test_unsupported_platform_exits_2(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(flash, "detect_platform", return_value="windows"), \
                mock.patch.object(flash.platform, "system", return_value="Windows"), \
                redirect_stdout(out), redirect_stderr(err):
            status = flash.main(["--list"])
        self.assertEqual(status, 2)
        self.assertIn("unsupported platform", err.getvalue())


if __name__ == "__main__":
    unittest.main()
