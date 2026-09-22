"""Unit tests for the pure helpers of scripts/log/capture.py (no serial hardware needed).

Covers the POSIX/termios helpers and the WSL2 path that reads a Windows COM
port through powershell.exe: the Win32_PnPEntity parser, WSL detection, port
selection, and the whole line pipeline driven by a fake child process.
"""

import argparse
import datetime
import io
import os
import signal
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "scripts", "log")))

import capture  # noqa: E402


class HelperTests(unittest.TestCase):
    def test_format_host_prefix(self):
        t = datetime.datetime(2026, 9, 15, 12, 34, 56, 789000).timestamp()
        self.assertEqual(capture.format_host_prefix(t), "[host 12:34:56.789]")

    def test_pick_ports_prefers_zmk_by_id(self):
        by_id = ["/dev/serial/by-id/usb-Arduino_x-if00", "/dev/serial/by-id/usb-ZMK_Project_Cornix_1234-if00"]
        acm = ["/dev/ttyACM0", "/dev/ttyACM1"]
        self.assertEqual(capture.pick_ports(by_id, acm), ["/dev/serial/by-id/usb-ZMK_Project_Cornix_1234-if00"])
        self.assertEqual(capture.pick_ports(["/dev/serial/by-id/usb-Arduino_x-if00"], acm),
                         ["/dev/serial/by-id/usb-Arduino_x-if00"])
        self.assertEqual(capture.pick_ports([], acm), acm)
        self.assertEqual(capture.pick_ports([], []), [])

    def test_line_splitter_stamps_first_byte_and_strips_cr(self):
        s = capture.LineSplitter()
        self.assertEqual(s.feed(b"abc", 1.0), [])
        self.assertEqual(s.feed(b"d\r\nef\n", 2.0), [(1.0, "abcd"), (2.0, "ef")])
        self.assertEqual(s.feed(b"partial", 3.0), [])
        self.assertEqual(s.flush(), [(3.0, "partial")])
        self.assertEqual(s.flush(), [])
        self.assertEqual(s.feed(b"", 4.0), [])

    def test_line_splitter_invalid_utf8(self):
        s = capture.LineSplitter()
        out = s.feed(b"\xff\xfe ok\n", 5.0)
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0][1].endswith(" ok"))

    def test_default_log_path(self):
        now = datetime.datetime(2026, 9, 15, 12, 34, 56)
        self.assertEqual(capture.default_log_path("/tmp/logs", "right", now), "/tmp/logs/right-20260915-123456.log")

    def test_parser_help_mentions_wsl(self):
        p = capture.build_parser()
        self.assertIn("usbipd", p.format_help())
        args = p.parse_args(["--label", "right", "--port", "/dev/ttyACM1"])
        self.assertEqual((args.label, args.port, args.baud), ("right", "/dev/ttyACM1", 115200))


# --------------------------------------------------------------------------
# WSL2 / Windows COM support
# --------------------------------------------------------------------------
# Realistic output of PS_LIST_COMMAND on a machine with a ZMK dongle (two CDC
# ports on one USB device), a Bluetooth virtual port and an FTDI adapter.
SAMPLE_PNP = (
    "USB Serial Device (COM5)  USB\\VID_1D50&PID_615E&MI_00\\7&1A2B3C4D&0&0000  "
    "USB\\VID_1D50&PID_615E&MI_00\\7&1A2B3C4D&0&0000\r\n"
    "USB Serial Device (COM6)  USB\\VID_1D50&PID_615E&MI_02\\7&1A2B3C4D&0&0002  "
    "USB\\VID_1D50&PID_615E&MI_02\\7&1A2B3C4D&0&0002\r\n"
    "Standard Serial over Bluetooth link (COM3)  BTHENUM\\{00001101-0000-1000-8000-00805F9B34FB}_"
    "LOCALMFG&0000\\7&Z&000000000000_00000000  BTHENUM\\FOO\r\n"
    "USB Serial Port (COM12)  USB\\VID_0403&PID_6001\\A1B2C3D4  USB\\VID_0403&PID_6001\\A1B2C3D4\r\n"
)


class PnpParserTests(unittest.TestCase):
    def test_parses_name_com_and_ids(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        self.assertEqual([p.com for p in ports], ["COM3", "COM5", "COM6", "COM12"])
        com5 = {p.com: p for p in ports}["COM5"]
        self.assertEqual(com5.name, "USB Serial Device (COM5)")
        self.assertEqual((com5.vid, com5.pid), ("1D50", "615E"))
        self.assertTrue(com5.is_zmk)
        ftdi = {p.com: p for p in ports}["COM12"]
        self.assertEqual((ftdi.vid, ftdi.pid), ("0403", "6001"))
        self.assertFalse(ftdi.is_zmk)
        self.assertFalse({p.com: p for p in ports}["COM3"].is_zmk)

    def test_sorted_numerically_not_lexically(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        self.assertEqual([p.com for p in ports][-1], "COM12")

    def test_ignores_noise_blank_lines_and_ansi(self):
        text = "\n\n\x1b[32mUSB Serial Device (COM5)\x1b[0m  USB\\VID_1D50&PID_615E&MI_00\\X\n" \
               "some unrelated device without a port\n"
        ports = capture.parse_pnp_com_ports(text)
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0].name, "USB Serial Device (COM5)")
        self.assertEqual(ports[0].vid, "1D50")

    def test_empty_and_none(self):
        self.assertEqual(capture.parse_pnp_com_ports(""), [])
        self.assertEqual(capture.parse_pnp_com_ports(None), [])

    def test_format_list_style_ids_on_a_following_line(self):
        text = (
            "Name        : USB Serial Device (COM7)\r\n"
            "DeviceID    : USB\\VID_1D50&PID_615E&MI_00\\7&1A2B3C4D&0&0000\r\n"
            "PNPDeviceID : USB\\VID_1D50&PID_615E&MI_00\\7&1A2B3C4D&0&0000\r\n"
        )
        ports = capture.parse_pnp_com_ports(text)
        self.assertEqual(len(ports), 1)
        self.assertEqual(ports[0].com, "COM7")
        self.assertEqual(ports[0].name, "USB Serial Device (COM7)")
        self.assertEqual(ports[0].vid, "1D50")
        self.assertTrue(ports[0].is_zmk)

    def test_duplicate_com_kept_once(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP + SAMPLE_PNP)
        self.assertEqual(len(ports), 4)

    def test_name_containing_zmk_without_vid(self):
        ports = capture.parse_pnp_com_ports("ZMK Project Cornix (COM9)  SOMETHING\\ELSE\n")
        self.assertTrue(ports[0].is_zmk)
        self.assertIsNone(ports[0].vid)

    def test_pick_com_ports_prefers_zmk(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        self.assertEqual([p.com for p in capture.pick_com_ports(ports)], ["COM5", "COM6"])

    def test_pick_com_ports_without_zmk_returns_all(self):
        ports = capture.parse_pnp_com_ports(
            "USB Serial Port (COM12)  USB\\VID_0403&PID_6001\\A1B2C3D4\n")
        self.assertEqual([p.com for p in capture.pick_com_ports(ports)], ["COM12"])
        self.assertEqual(capture.pick_com_ports([]), [])

    def test_describe_marks_zmk(self):
        ports = {p.com: p for p in capture.parse_pnp_com_ports(SAMPLE_PNP)}
        self.assertIn("COM5", ports["COM5"].describe())
        self.assertIn("<- ZMK", ports["COM5"].describe())
        self.assertNotIn("<- ZMK", ports["COM3"].describe())

    def test_list_windows_com_ports_uses_injected_runner(self):
        ports = capture.list_windows_com_ports(runner=lambda: SAMPLE_PNP)
        self.assertEqual([p.com for p in ports], ["COM3", "COM5", "COM6", "COM12"])


class NormalizeComTests(unittest.TestCase):
    def test_spellings(self):
        for spelling in ("COM5", "com5", "Com5", "5", " COM5 ", "USB Serial Device (COM5)"):
            self.assertEqual(capture.normalize_com(spelling), "COM5", spelling)
        self.assertEqual(capture.normalize_com("COM12"), "COM12")
        self.assertEqual(capture.normalize_com("COM05"), "COM5")


class StripAnsiTests(unittest.TestCase):
    def test_removes_colour_codes_only(self):
        self.assertEqual(capture.strip_ansi("\x1b[1;32m<inf> zmk\x1b[0m: hi"), "<inf> zmk: hi")
        self.assertEqual(capture.strip_ansi("plain"), "plain")
        self.assertEqual(capture.strip_ansi("\x1b[2K\x1b[0m"), "")


class WslDetectionTests(unittest.TestCase):
    def _proc_version(self, text):
        path = os.path.join(self.tmp, "version")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)

    def test_detects_wsl2_proc_version(self):
        path = self._proc_version(
            "Linux version 6.6.87.2-microsoft-standard-WSL2 (root@x) (gcc 11.2.0) #1 SMP\n")
        self.assertTrue(capture.is_wsl(path, environ={}))

    def test_detects_wsl1_capitalised(self):
        path = self._proc_version("Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)\n")
        self.assertTrue(capture.is_wsl(path, environ={}))

    def test_plain_linux_is_not_wsl(self):
        path = self._proc_version("Linux version 6.1.0-18-amd64 (debian-kernel@lists.debian.org)\n")
        self.assertFalse(capture.is_wsl(path, environ={}))

    def test_env_var_alone_is_enough(self):
        path = self._proc_version("Linux version 6.1.0-18-amd64\n")
        self.assertTrue(capture.is_wsl(path, environ={"WSL_DISTRO_NAME": "Debian"}))
        self.assertTrue(capture.is_wsl(path, environ={"WSL_INTEROP": "/run/WSL/1_interop"}))

    def test_missing_proc_version_is_not_fatal(self):
        self.assertFalse(capture.is_wsl(os.path.join(self.tmp, "nope"), environ={}))


class ShouldUseComTests(unittest.TestCase):
    def _args(self, **kw):
        base = dict(com=None, port=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_explicit_com_wins(self):
        self.assertTrue(capture.should_use_com(self._args(com="COM5")))

    def test_explicit_port_keeps_termios(self):
        with mock.patch.object(capture, "is_wsl", return_value=True):
            self.assertFalse(capture.should_use_com(self._args(port="/dev/ttyACM0")))

    def test_not_wsl_keeps_termios(self):
        with mock.patch.object(capture, "is_wsl", return_value=False):
            self.assertFalse(capture.should_use_com(self._args()))

    def test_wsl_without_ttyacm_uses_com(self):
        with mock.patch.object(capture, "is_wsl", return_value=True), \
             mock.patch.object(capture, "list_candidate_ports", return_value=([], [])):
            self.assertTrue(capture.should_use_com(self._args()))

    def test_wsl_with_usbipd_ttyacm_keeps_termios(self):
        with mock.patch.object(capture, "is_wsl", return_value=True), \
             mock.patch.object(capture, "list_candidate_ports", return_value=([], ["/dev/ttyACM0"])):
            self.assertFalse(capture.should_use_com(self._args()))


class PowerShellCommandTests(unittest.TestCase):
    def test_list_command_filters_com_and_forces_utf8(self):
        self.assertIn("Win32_PnPEntity", capture.PS_LIST_COMMAND)
        self.assertIn(r"\(COM\d+\)", capture.PS_LIST_COMMAND)
        self.assertIn("PNPDeviceID", capture.PS_LIST_COMMAND)
        self.assertIn("UTF8", capture.PS_LIST_COMMAND)

    def test_read_command_shape(self):
        with mock.patch.object(capture, "to_windows_path", side_effect=lambda p: r"C:\ps\read_com.ps1"):
            cmd = capture.powershell_read_command("com5", 115200, exe="/mnt/c/.../powershell.exe")
        self.assertEqual(cmd[0], "/mnt/c/.../powershell.exe")
        self.assertIn("-NoProfile", cmd)
        self.assertIn("-ExecutionPolicy", cmd)
        self.assertIn("Bypass", cmd)
        self.assertEqual(cmd[cmd.index("-File") + 1], r"C:\ps\read_com.ps1")
        self.assertEqual(cmd[cmd.index("-Port") + 1], "COM5")
        self.assertEqual(cmd[cmd.index("-Baud") + 1], "115200")

    def test_read_command_without_powershell_raises_powershell_error(self):
        with mock.patch.object(capture.shutil, "which", return_value=None):
            with self.assertRaises(capture.PowerShellError):
                capture.powershell_read_command("COM5", 115200)

    def test_read_command_missing_script_raises(self):
        with self.assertRaises(capture.PowerShellError):
            capture.powershell_read_command("COM5", 115200, exe="/bin/true", script="/no/such.ps1")

    def test_helper_script_exists_and_reports_the_documented_exit_codes(self):
        self.assertTrue(os.path.exists(capture.READ_COM_PS1))
        with open(capture.READ_COM_PS1, encoding="utf-8") as fh:
            ps1 = fh.read()
        self.assertIn("exit %d" % capture.PS_EXIT_OPEN_FAILED, ps1)
        self.assertIn("exit %d" % capture.PS_EXIT_DISCONNECTED, ps1)
        self.assertIn("DtrEnable", ps1)
        self.assertIn("OutputEncoding", ps1)

    def test_helper_script_is_pure_ascii(self):
        # Windows PowerShell 5.1 parses a BOM-less .ps1 in the machine's ANSI
        # code page, so a non-ASCII literal breaks on a localized Windows.
        with open(capture.READ_COM_PS1, "rb") as fh:
            raw = fh.read()
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as e:
            self.fail("read_com.ps1 must stay ASCII: %s" % e)

    def test_run_powershell_list_without_powershell_raises(self):
        with mock.patch.object(capture.shutil, "which", return_value=None):
            with self.assertRaises(capture.PowerShellError):
                capture.run_powershell_list()


class FakeProc:
    """Minimal stand-in for subprocess.Popen with in-memory pipes."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self._done = False

    def wait(self, timeout=None):
        self._done = True
        return self.returncode

    def poll(self):
        return self.returncode if self._done else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class ComPipelineTests(unittest.TestCase):
    """The child's stdout must go through the same pipeline as the termios path."""

    def _capture(self, tmpdir, **kw):
        args = argparse.Namespace(
            com="COM5", port=None, label="dongle", baud=115200, out_dir=tmpdir,
            file=None, retry=0.01, quiet=True, verbose_child=False, **kw)
        cap = capture.Capture(args, use_com=True)
        self.logpath = os.path.join(tmpdir, "out.log")
        cap.log = open(self.logpath, "w", encoding="utf-8")
        return cap

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)

    def _lines(self, cap):
        if not cap.log.closed:
            cap.log.flush()
        with open(self.logpath, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_lines_are_prefixed_crlf_and_ansi_stripped(self):
        cap = self._capture(self.tmp)
        proc = FakeProc(stdout=b"\x1b[1;32m<inf> zmk: boot\x1b[0m\r\n<dbg> zmk: key\r\n",
                        returncode=capture.PS_EXIT_DISCONNECTED)
        rc = cap.pump_com_reader(proc, "COM5")
        cap.log.close()
        self.assertEqual(rc, capture.PS_EXIT_DISCONNECTED)
        lines = self._lines(cap)
        self.assertEqual(len(lines), 2)
        for line, expect in zip(lines, ["<inf> zmk: boot", "<dbg> zmk: key"]):
            self.assertRegex(line, r"^\[host \d{2}:\d{2}:\d{2}\.\d{3}\] ")
            self.assertTrue(line.endswith(expect), line)
            self.assertNotIn("\x1b", line)
            self.assertNotIn("\r", line)

    def test_trailing_partial_line_is_flushed(self):
        cap = self._capture(self.tmp)
        cap.pump_com_reader(FakeProc(stdout=b"complete\nno newline here"), "COM5")
        cap.log.close()
        lines = self._lines(cap)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("no newline here"))

    def test_line_counter_matches(self):
        cap = self._capture(self.tmp)
        cap.pump_com_reader(FakeProc(stdout=b"a\nb\nc\n"), "COM5")
        cap.log.close()
        self.assertEqual(cap.lines, 3)

    def test_open_failure_returns_exit_code_and_writes_nothing(self):
        cap = self._capture(self.tmp)
        proc = FakeProc(stdout=b"", stderr="read_com: cannot open COM5: denied".encode("utf-8"),
                        returncode=capture.PS_EXIT_OPEN_FAILED)
        rc = cap.pump_com_reader(proc, "COM5")
        cap.log.close()
        self.assertEqual(rc, capture.PS_EXIT_OPEN_FAILED)
        self.assertEqual(self._lines(cap), [])

    def test_utf8_survives_a_chunk_split_mid_line(self):
        cap = self._capture(self.tmp)

        class Chunked(FakeProc):
            def __init__(self, chunks):
                FakeProc.__init__(self)
                self._chunks = list(chunks)
                self.stdout = self

            def read(self, n):
                return self._chunks.pop(0) if self._chunks else b""

        cap.pump_com_reader(Chunked([b"<inf> zmk: ", b"half a line", b" and the rest\n"]), "COM5")
        cap.log.close()
        lines = self._lines(cap)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("<inf> zmk: half a line and the rest"))

    def test_keyboard_interrupt_terminates_the_child(self):
        cap = self._capture(self.tmp)

        class Rude(FakeProc):
            def __init__(self):
                FakeProc.__init__(self)
                self.stdout = self

            def read(self, n):
                raise KeyboardInterrupt

        proc = Rude()
        with self.assertRaises(KeyboardInterrupt):
            cap.pump_com_reader(proc, "COM5")
        cap.log.close()
        self.assertTrue(proc.terminated)

    def test_read_until_disconnect_spawns_powershell_with_the_right_argv(self):
        cap = self._capture(self.tmp)
        proc = FakeProc(stdout=b"<inf> zmk: hello\n", returncode=capture.PS_EXIT_DISCONNECTED)
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            seen["kw"] = kw
            return proc

        with mock.patch.object(capture, "powershell_read_command",
                               return_value=["powershell.exe", "-Port", "COM5"]), \
             mock.patch.object(capture.subprocess, "Popen", side_effect=fake_popen):
            rc = cap.read_com_until_disconnect("COM5")
        cap.log.close()
        self.assertEqual(rc, capture.PS_EXIT_DISCONNECTED)
        self.assertEqual(seen["cmd"][0], "powershell.exe")
        self.assertIs(seen["kw"]["stdout"], capture.subprocess.PIPE)
        self.assertTrue(self._lines(cap)[0].endswith("<inf> zmk: hello"))


class ComPortSelectionTests(unittest.TestCase):
    def _cap(self, com=None):
        args = argparse.Namespace(com=com, port=None, label="dongle", baud=115200,
                                  out_dir="/tmp", file=None, retry=0.01, quiet=True,
                                  verbose_child=False)
        return capture.Capture(args, use_com=True)

    def test_single_zmk_port_is_auto_selected(self):
        text = "USB Serial Device (COM5)  USB\\VID_1D50&PID_615E&MI_00\\X\n" \
               "USB Serial Port (COM12)  USB\\VID_0403&PID_6001\\Y\n"
        with mock.patch.object(capture, "list_windows_com_ports",
                               return_value=capture.parse_pnp_com_ports(text)):
            self.assertEqual(self._cap().resolve_com_port(), "COM5")

    def test_two_zmk_ports_require_explicit_com(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        with mock.patch.object(capture, "list_windows_com_ports", return_value=ports):
            with self.assertRaises(SystemExit) as ctx:
                self._cap().resolve_com_port()
            self.assertEqual(ctx.exception.code, 2)
            # ... and picking one explicitly works.
            self.assertEqual(self._cap("com6").resolve_com_port(), "COM6")

    def test_absent_explicit_port_waits_instead_of_opening(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        with mock.patch.object(capture, "list_windows_com_ports", return_value=ports):
            self.assertIsNone(self._cap("COM99").resolve_com_port())

    def test_no_ports_at_all_waits(self):
        with mock.patch.object(capture, "list_windows_com_ports", return_value=[]):
            self.assertIsNone(self._cap().resolve_com_port())

    def test_enumeration_failure_still_tries_an_explicit_port(self):
        with mock.patch.object(capture, "list_windows_com_ports",
                               side_effect=capture.PowerShellError("no powershell.exe")):
            self.assertEqual(self._cap("COM5").resolve_com_port(), "COM5")
            self.assertIsNone(self._cap().resolve_com_port())


class CheckComAvailableTests(unittest.TestCase):
    def test_missing_port_exits_2_without_a_traceback(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        with mock.patch.object(capture, "list_windows_com_ports", return_value=ports):
            self.assertEqual(capture.check_com_available("COM99"), 2)
            self.assertEqual(capture.check_com_available("COM5"), 0)

    def test_powershell_unavailable_is_not_an_error(self):
        with mock.patch.object(capture, "list_windows_com_ports",
                               side_effect=capture.PowerShellError("x")):
            self.assertEqual(capture.check_com_available("COM5"), 0)


class ChildHelperTests(unittest.TestCase):
    def test_read_child_stderr_splits_and_strips(self):
        proc = FakeProc(stderr=b"read_com: opened COM5\n\nread_com: COM5 lost\n")
        self.assertEqual(capture.read_child_stderr(proc),
                         ["read_com: opened COM5", "read_com: COM5 lost"])

    def test_read_child_stderr_tolerates_a_closed_pipe(self):
        proc = FakeProc()
        proc.stderr.close()
        self.assertEqual(capture.read_child_stderr(proc), [])
        proc2 = FakeProc()
        proc2.stderr = None
        self.assertEqual(capture.read_child_stderr(proc2), [])

    def test_terminate_child_is_best_effort(self):
        proc = FakeProc()
        capture.terminate_child(proc)
        self.assertTrue(proc.terminated)


class ComCliTests(unittest.TestCase):
    def test_com_flag_parsed(self):
        args = capture.build_parser().parse_args(["--com", "COM5", "--label", "dongle"])
        self.assertEqual((args.com, args.label), ("COM5", "dongle"))

    def test_help_documents_both_wsl_routes(self):
        text = capture.build_parser().format_help()
        self.assertIn("usbipd", text)          # still documented as the alternative
        self.assertIn("powershell.exe", text)  # ... and the new default
        self.assertIn("--com", text)

    def test_list_prints_no_com_ports_when_windows_has_none(self):
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with mock.patch.object(capture, "is_wsl", return_value=True), \
             mock.patch.object(capture, "list_candidate_ports", return_value=([], [])), \
             mock.patch.object(capture, "list_windows_com_ports", return_value=[]), \
             redirect_stdout(buf):
            rc = capture.main(["--list"])
        self.assertEqual(rc, 0)
        self.assertIn("(no COM ports)", buf.getvalue())

    def test_list_prints_the_ports_and_the_suggestion(self):
        from contextlib import redirect_stdout
        buf = io.StringIO()
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        with mock.patch.object(capture, "is_wsl", return_value=True), \
             mock.patch.object(capture, "list_candidate_ports", return_value=([], [])), \
             mock.patch.object(capture, "list_windows_com_ports", return_value=ports), \
             redirect_stdout(buf):
            capture.main(["--list"])
        out = buf.getvalue()
        self.assertIn("COM5", out)
        self.assertIn("COM6", out)
        self.assertIn("pass --com", out)   # two ZMK ports: ambiguous on purpose

    def test_list_does_not_touch_powershell_off_wsl(self):
        from contextlib import redirect_stdout
        with mock.patch.object(capture, "is_wsl", return_value=False), \
             mock.patch.object(capture, "list_candidate_ports", return_value=([], [])), \
             mock.patch.object(capture, "list_windows_com_ports",
                               side_effect=AssertionError("must not run")), \
             redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(capture.main(["--list"]), 0)

    def test_main_exits_2_on_a_com_port_that_does_not_exist(self):
        ports = capture.parse_pnp_com_ports(SAMPLE_PNP)
        with mock.patch.object(capture, "list_windows_com_ports", return_value=ports), \
             mock.patch.object(capture.Capture, "run",
                               side_effect=AssertionError("must not start capturing")):
            self.assertEqual(capture.main(["--com", "COM99"]), 2)


class PidfileTests(unittest.TestCase):
    """capture.py advertises its pid so scripts/flash.py --enter can find it."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)

    def test_pidfile_path_is_per_label(self):
        self.assertEqual(capture.pidfile_path("/logs", "left"), "/logs/capture-left.pid")
        self.assertNotEqual(capture.pidfile_path("/logs", "left"),
                            capture.pidfile_path("/logs", "right"))

    def test_write_read_remove_round_trip(self):
        path = capture.pidfile_path(self.tmp, "left")
        self.assertEqual(capture.write_pidfile(path, 4242), path)
        self.assertEqual(capture.read_pidfile(path), 4242)
        capture.remove_pidfile(path)
        self.assertFalse(os.path.exists(path))
        capture.remove_pidfile(path)  # removing twice must not raise

    def test_write_pidfile_defaults_to_our_own_pid(self):
        path = capture.pidfile_path(self.tmp, "right")
        capture.write_pidfile(path)
        self.assertEqual(capture.read_pidfile(path), os.getpid())

    def test_read_pidfile_rejects_garbage(self):
        path = os.path.join(self.tmp, "capture-x.pid")
        for text in ("", "   ", "nope", "0", "-1"):
            with open(path, "w", encoding="ascii") as fh:
                fh.write(text)
            self.assertIsNone(capture.read_pidfile(path), text)
        self.assertIsNone(capture.read_pidfile(os.path.join(self.tmp, "missing.pid")))


class PauseTests(unittest.TestCase):
    """SIGUSR1 makes the capture let go of the port without ending the capture."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self._tmpdir.name
        self.addCleanup(self._tmpdir.cleanup)
        self.logpath = os.path.join(self.tmp, "out.log")

    def _cap(self, **kw):
        args = argparse.Namespace(
            com="COM5", port=None, label="left", baud=115200, out_dir=self.tmp,
            file=None, retry=0.01, quiet=True, verbose_child=False,
            pidfile=None, no_pidfile=False, pause_seconds=0.05, **kw)
        cap = capture.Capture(args, use_com=True)
        cap.log = open(self.logpath, "w", encoding="utf-8")
        self.addCleanup(lambda: cap.log.closed or cap.log.close())
        return cap

    def _lines(self):
        with open(self.logpath, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_pause_for_terminates_the_powershell_child(self):
        cap = self._cap()
        child = FakeProc(b"")
        cap._child = child
        cap.pause_for(5.0)
        self.assertTrue(child.terminated)
        self.assertGreater(cap._pause_until, time.time())

    def test_pause_for_closes_the_termios_fd(self):
        cap = self._cap()
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(write_fd))
        cap._fd = read_fd
        cap.pause_for(5.0)
        self.assertIsNone(cap._fd)
        with self.assertRaises(OSError):
            os.fstat(read_fd)  # really closed

    def test_pause_for_survives_an_already_dead_child(self):
        cap = self._cap()
        child = FakeProc(b"")
        child._done = True
        cap._child = child
        cap.pause_for(0.01)
        self.assertFalse(child.terminated)

    def test_wait_out_pause_blocks_then_notes_both_ends(self):
        cap = self._cap()
        self.assertFalse(cap.wait_out_pause())  # nothing pending
        cap.pause_for(0.05)
        started = time.time()
        self.assertTrue(cap.wait_out_pause())
        self.assertGreaterEqual(time.time() - started, 0.04)
        cap.log.flush()
        text = "\n".join(self._lines())
        self.assertIn("paused", text)
        self.assertIn("pause over", text)

    def test_a_second_signal_extends_but_never_shortens_the_pause(self):
        cap = self._cap()
        cap.pause_for(10.0)
        deadline = cap._pause_until
        cap.pause_for(0.01)  # shorter: must not bring the deadline forward
        self.assertEqual(cap._pause_until, deadline)
        cap.pause_for(20.0)
        self.assertGreater(cap._pause_until, deadline)

    @unittest.skipIf(capture.PAUSE_SIGNAL is None, "no SIGUSR1 on this platform")
    def test_the_installed_handler_pauses_the_capture(self):
        cap = self._cap()
        previous = signal.getsignal(capture.PAUSE_SIGNAL)
        self.addCleanup(signal.signal, capture.PAUSE_SIGNAL, previous)
        self.assertTrue(cap.install_pause_handler())
        os.kill(os.getpid(), capture.PAUSE_SIGNAL)
        self.assertGreater(cap._pause_until, time.time())

    def test_read_until_disconnect_returns_when_the_fd_is_closed(self):
        """release_port() closing the fd must read as "device gone", not a crash."""
        cap = self._cap()
        read_fd, write_fd = os.pipe()
        self.addCleanup(lambda: os.close(write_fd))
        cap._fd = read_fd

        def closer():
            time.sleep(0.05)
            cap.pause_for(0.01)

        thread = threading.Thread(target=closer)
        thread.start()
        cap.read_until_disconnect(read_fd)  # must return, not hang or raise
        thread.join()


if __name__ == "__main__":
    unittest.main()
