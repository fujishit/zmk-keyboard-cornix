#!/usr/bin/env python3
"""Capture ZMK USB CDC-ACM debug logs with host-side timestamps.

Python 3 standard library only (no pyserial): the port is opened with os.open
and put into raw 115200 8N1 mode with termios. Every received line is written
to logs/<label>-<YYYYmmdd-HHMMSS>.log as

    [host HH:MM:SS.mmm] <line as sent by the device>

and echoed to stdout. The host prefix is the wall-clock time at which the
first byte of that line arrived, so two captures running at the same time on
the same host (left + right half) can be correlated by
scripts/log/analyze_latency.py.

ZMK devices reset (settings_reset, re-flash, Zephyr fatal error with
CONFIG_RESET_ON_FATAL_ERROR, ...), which makes the CDC device disappear and
come back. The capture loop notices EIO/ENXIO/EOF, keeps the log file open,
and retries the port until it is back. Ctrl-C ends the capture cleanly.

While it runs, the capture writes logs/capture-<label>.pid and answers SIGUSR1
by closing the port for --pause-seconds without ending the capture, so
scripts/flash.py --enter can borrow the port (Windows hands out only one handle
per COM port) to put a half into its UF2 bootloader.

On WSL2 without usbipd-win the USB CDC port is not visible to Linux at all
(/dev/ttyS* are *not* the Windows COM ports). In that case the port is read
through Windows instead: scripts/log/read_com.ps1 is started with
powershell.exe and its stdout is fed through the very same line pipeline, so
the log files are identical either way (--com / auto-detection, see --list).

See scripts/log/README.md for the full workflow.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import errno
import glob
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from typing import Callable, NamedTuple

try:  # POSIX only; on Windows use --tio/PuTTY and feed the file to the analyzer.
    import fcntl
    import struct
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    fcntl = struct = termios = tty = None  # type: ignore[assignment]

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_OUT_DIR = os.path.join(REPO_ROOT, "logs")
LABELS = ("left", "right", "dongle", "ph_left", "device")

#: How long the port stays closed after SIGUSR1, unless --pause-seconds says
#: otherwise.  Long enough for scripts/flash.py --enter to open the port, set
#: the magic baud rate, close it and for the device to reboot into its UF2
#: bootloader (which makes the port disappear anyway).
DEFAULT_PAUSE_SECONDS = 15.0

#: Signal that asks a running capture to let go of the port for a while.
#: SIGUSR1 is chosen because nothing else in this repository uses it and,
#: unlike SIGINT/SIGTERM, it does not end the capture: the log file stays open
#: and the existing reconnect loop picks the device back up afterwards.
PAUSE_SIGNAL = getattr(signal, "SIGUSR1", None)

EPILOG = """
port auto-detection:
  /dev/serial/by-id/* entries whose name contains "ZMK" are preferred (the
  USB product string of a ZMK CDC-ACM port is "ZMK Project <keyboard>").
  Without such entries all /dev/serial/by-id/* are candidates, then
  /dev/ttyACM*. With more than one candidate you must pass --port (use --list
  to see them). A dongle built with studio-rpc-usb-uart AND zmk-usb-logging
  exposes two CDC-ACM ports on the same USB device (by-id names differ in the
  trailing "-ifNN" interface number); try the other one if you only see
  silence.

WSL2 (no usbipd needed):
  USB devices are not visible inside WSL2, and /dev/ttyS* are NOT the Windows
  COM ports. When this script runs under WSL and finds no /dev/ttyACM*, it
  falls back to reading the port on the Windows side through powershell.exe:
  ports are enumerated with Get-CimInstance Win32_PnPEntity and the one whose
  USB VID is 1D50 (ZMK) is used. --list shows them, --com COMn picks one
  explicitly (a dongle exposes two CDC ports: Studio RPC and the log; try the
  other COM number if one stays silent). Nothing has to be installed on
  Windows and no administrator shell is involved. Reconnection after a ZMK
  reset is automatic.

WSL2 (alternative: usbipd-win):
  With usbipd-win installed the device can be handed to Linux instead, from an
  *administrator* PowerShell on the Windows side:
      usbipd list
      usbipd bind --busid <BUSID>          # once per device
      usbipd attach --wsl --busid <BUSID>  # after every re-plug / reset
  Then /dev/ttyACM* appears in WSL and the normal (termios) path is used.
  /dev/serial/by-id/ needs udev, which is usually not running on WSL2, so
  expect to fall back to /dev/ttyACM* and to pass --port explicitly when both
  halves are attached. A ZMK reset detaches the device on the Windows side;
  re-run `usbipd attach` and this script reconnects automatically.

sharing the port with scripts/flash.py --enter:
  Windows hands out exactly one handle per COM port, so a capture and the
  remote-bootloader "1200 bps touch" cannot hold it at the same time. This
  script writes <out-dir>/capture-<label>.pid and, on SIGUSR1, closes the port
  and stays away for --pause-seconds (15 s) before its normal reconnect loop
  takes it back - the same loop that already handles the device disappearing
  into its bootloader. flash.py --enter sends that signal by itself; do it by
  hand with `kill -USR1 $(cat logs/capture-left.pid)`. The capture keeps
  running and the log file keeps its numbering; the pause is recorded in the
  log as a "--- capture: paused ... ---" line.

other terminal programs:
  A plain log file written by PuTTY (Session > Logging > "Printable output"),
  `tio --log`, minicom -C, or `screen -L` can be fed to the analyzer as well;
  without host timestamps the analyzer only computes per-device latencies.
  With --tio this script simply runs `tio --timestamp` and the resulting
  "[HH:MM:SS.mmm]" prefix is also understood by the analyzer.
"""


# --------------------------------------------------------------------------
# helpers (kept free of I/O so tests/log/test_capture.py can exercise them)
# --------------------------------------------------------------------------
def format_host_prefix(t: float | None = None) -> str:
    """Return "[host HH:MM:SS.mmm]" for wall-clock time t (seconds)."""
    if t is None:
        t = time.time()
    dt = _dt.datetime.fromtimestamp(t)
    return "[host %02d:%02d:%02d.%03d]" % (dt.hour, dt.minute, dt.second, dt.microsecond // 1000)


def pick_ports(by_id: list[str], acm: list[str]) -> list[str]:
    """Order candidate ports: ZMK by-id entries, then any by-id, then ttyACM."""
    zmk = [p for p in by_id if "zmk" in os.path.basename(p).lower()]
    if zmk:
        return sorted(zmk)
    if by_id:
        return sorted(by_id)
    return sorted(acm)


def list_candidate_ports() -> tuple[list[str], list[str]]:
    # Linux: udev by-id links, then raw CDC-ACM nodes.
    # macOS: no by-id; ZMK CDC ports appear as /dev/cu.usbmodem* (use cu.*, not
    # tty.*, so open() does not block waiting for carrier).
    acm = sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/cu.usbmodem*"))
    return sorted(glob.glob("/dev/serial/by-id/*")), acm

# --------------------------------------------------------------------------
# WSL2 without usbipd: read the port on the Windows side through PowerShell
# --------------------------------------------------------------------------
PS_EXE = "powershell.exe"
READ_COM_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "read_com.ps1")

# read_com.ps1 exit codes (see its .NOTES block).
PS_EXIT_OPEN_FAILED = 3
PS_EXIT_DISCONNECTED = 4

# One line per port; the separator is only cosmetic, the parser is regex based.
PS_LIST_COMMAND = (
    # Without the UTF-8 console encoding a localized Windows returns the device
    # names in its OEM code page and they arrive here as mojibake.
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_PnPEntity | "
    "Where-Object { $_.Name -match '\\(COM\\d+\\)' } | "
    "Select-Object Name, DeviceID, PNPDeviceID | "
    "ForEach-Object { '{0}  {1}  {2}' -f $_.Name, $_.DeviceID, $_.PNPDeviceID }"
)

ZMK_USB_VID = "1D50"  # OpenMoko/ZMK; the USB product string also contains "ZMK"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_COM_RE = re.compile(r"\(COM(\d+)\)")
_VID_RE = re.compile(r"VID_([0-9A-Fa-f]{4})")
_PID_RE = re.compile(r"PID_([0-9A-Fa-f]{4})")
_FIELD_LABEL_RE = re.compile(r"^\s*(?:Name|Caption|Description)\s*:\s*")


class PowerShellError(RuntimeError):
    """powershell.exe is missing or the enumeration command failed."""


class ComPort(NamedTuple):
    com: str           # "COM5"
    name: str          # "USB Serial Device (COM5)"
    vid: str | None    # "1D50"
    pid: str | None    # "615E"
    hwid: str          # the PNPDeviceID part of the line

    @property
    def is_zmk(self) -> bool:
        return (self.vid or "").upper() == ZMK_USB_VID or "zmk" in self.name.lower()

    def describe(self) -> str:
        ids = "VID_%s&PID_%s" % (self.vid or "????", self.pid or "????") if self.vid else "unknown ids"
        return "%-6s %s [%s]%s" % (self.com, self.name, ids, "  <- ZMK" if self.is_zmk else "")


def strip_ansi(text: str) -> str:
    """Drop ANSI SGR/CSI escapes (the debug snippet disables colour, belt and braces)."""
    return _ANSI_RE.sub("", text)


def normalize_com(name: str) -> str:
    """Accept "5", "com5", "COM5", "/dev/ttyS5"-ish spellings; return "COM5"."""
    text = (name or "").strip().strip('"')
    m = _COM_RE.search(text)
    if m:
        return "COM%d" % int(m.group(1))
    digits = text.lstrip("comCOM").strip()
    if digits.isdigit():
        return "COM%d" % int(digits)
    return text.upper()


def is_wsl(proc_version_path: str = "/proc/version", environ: dict | None = None) -> bool:
    """True inside WSL1/WSL2 (microsoft in /proc/version, or WSL_DISTRO_NAME set)."""
    env = os.environ if environ is None else environ
    if env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP"):
        return True
    try:
        with open(proc_version_path, encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def parse_pnp_com_ports(text: str) -> list[ComPort]:
    """Parse the Win32_PnPEntity listing into ComPort records.

    Tolerates the one-line form produced by PS_LIST_COMMAND, Format-Table
    output, and the Format-List form where the hardware id follows on one of
    the next lines. CR/LF and ANSI escapes are ignored.
    """
    ports: list[ComPort] = []
    seen: set[str] = set()
    pending: int | None = None  # index into ports still waiting for a VID line
    for raw in strip_ansi(text or "").splitlines():
        line = raw.replace("\x00", "").strip()
        if not line:
            continue
        m = _COM_RE.search(line)
        if not m:
            # Format-List: the ids arrive on a later line than the name.
            if pending is not None:
                vid = _VID_RE.search(line)
                if vid:
                    pid = _PID_RE.search(line)
                    prev = ports[pending]
                    ports[pending] = prev._replace(
                        vid=vid.group(1).upper(),
                        pid=pid.group(1).upper() if pid else None,
                        hwid=line,
                    )
                    pending = None
            continue
        com = "COM%d" % int(m.group(1))
        name = _FIELD_LABEL_RE.sub("", line[: m.end()]).strip()
        rest = line[m.end():].strip()
        vid = _VID_RE.search(line)
        pid = _PID_RE.search(line)
        if com in seen:
            pending = None
            continue
        seen.add(com)
        ports.append(ComPort(
            com=com,
            name=name,
            vid=vid.group(1).upper() if vid else None,
            pid=pid.group(1).upper() if pid else None,
            hwid=rest,
        ))
        pending = None if vid else len(ports) - 1
    return sorted(ports, key=lambda p: int(p.com[3:]))


def pick_com_ports(ports: list[ComPort]) -> list[ComPort]:
    """Order candidates: ZMK ports (VID 1D50 or "ZMK" in the name) win outright."""
    zmk = [p for p in ports if p.is_zmk]
    return zmk if zmk else list(ports)


def run_powershell_list(exe: str | None = None) -> str:
    """Run PS_LIST_COMMAND and return its stdout."""
    exe = exe or shutil.which(PS_EXE)
    if not exe:
        raise PowerShellError(
            "%s not found on PATH; WSL interop is required to read a Windows COM port" % PS_EXE)
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", PS_LIST_COMMAND],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except OSError as e:
        raise PowerShellError("cannot run %s: %s" % (exe, e))
    except subprocess.TimeoutExpired:
        raise PowerShellError("%s timed out while listing COM ports" % exe)
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise PowerShellError("%s failed (exit %d)%s"
                              % (exe, proc.returncode, ": " + msg[0] if msg else ""))
    return proc.stdout.decode("utf-8", errors="replace")


def list_windows_com_ports(runner: Callable[[], str] | None = None) -> list[ComPort]:
    return parse_pnp_com_ports((runner or run_powershell_list)())


def powershell_read_command(com: str, baud: int, exe: str | None = None,
                            script: str = READ_COM_PS1) -> list[str]:
    """argv that streams `com` to stdout via read_com.ps1."""
    exe = exe or shutil.which(PS_EXE)
    if not exe:
        raise PowerShellError(
            "%s not found on PATH; WSL interop is required to read a Windows COM port" % PS_EXE)
    if not os.path.exists(script):
        raise PowerShellError("missing helper script %s" % script)
    return [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", to_windows_path(script), "-Port", normalize_com(com), "-Baud", str(baud)]


def to_windows_path(path: str) -> str:
    """Translate a WSL path for powershell.exe (\\\\wsl.localhost\\... is accepted)."""
    wslpath = shutil.which("wslpath")
    if wslpath:
        try:
            out = subprocess.run([wslpath, "-w", path], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=10)
            if out.returncode == 0:
                win = out.stdout.decode("utf-8", errors="replace").strip()
                if win:
                    return win
        except (OSError, subprocess.TimeoutExpired):
            pass
    return path


def should_use_com(args: argparse.Namespace) -> bool:
    """True when the Windows/PowerShell path should be used instead of termios."""
    if getattr(args, "com", None):
        return True
    if getattr(args, "port", None):
        return False
    if not is_wsl():
        return False
    by_id, acm = list_candidate_ports()
    return not (by_id or acm)


class LineSplitter:
    """Accumulate bytes and emit complete lines with the arrival time of their first byte."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._t_start: float | None = None

    def feed(self, data: bytes, t: float) -> list[tuple[float, str]]:
        out: list[tuple[float, str]] = []
        if not data:
            return out
        if self._t_start is None:
            self._t_start = t
        self._buf.extend(data)
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            assert self._t_start is not None
            out.append((self._t_start, line))
            self._t_start = t if self._buf else None
        return out

    def flush(self) -> list[tuple[float, str]]:
        """Return the incomplete trailing line (if any) and reset."""
        if not self._buf:
            return []
        line = bytes(self._buf).decode("utf-8", errors="replace").rstrip("\r")
        t = self._t_start if self._t_start is not None else time.time()
        self._buf.clear()
        self._t_start = None
        return [(t, line)]


def pidfile_path(out_dir: str, label: str) -> str:
    """Where a running capture advertises its pid.

    One file per label so a left and a right capture can run side by side;
    scripts/flash.py globs ``capture-*.pid`` in the same directory.
    """
    return os.path.join(out_dir, "capture-%s.pid" % label)


def write_pidfile(path: str, pid: int | None = None) -> str | None:
    """Write our pid to `path`.  Returns the path, or None if it could not be."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="ascii") as fh:
            fh.write("%d\n" % (os.getpid() if pid is None else pid))
    except OSError as e:
        print("capture: cannot write %s: %s" % (path, e), file=sys.stderr)
        return None
    return path


def remove_pidfile(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def read_pidfile(path: str) -> int | None:
    """The pid in `path`, or None when the file is missing/empty/garbage."""
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def default_log_path(out_dir: str, label: str, now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now()
    return os.path.join(out_dir, "%s-%s.log" % (label, now.strftime("%Y%m%d-%H%M%S")))


# --------------------------------------------------------------------------
# serial port
# --------------------------------------------------------------------------
def open_serial(port: str, baud: int) -> int:
    """Open `port` raw, 8N1, no flow control, at `baud`. Returns the fd."""
    if termios is None:
        raise RuntimeError("termios is not available on this platform; use --tio or PuTTY")
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, "B%d" % baud, None)
        if speed is None:
            raise ValueError("unsupported baud rate %d" % baud)
        attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
        attrs[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
        if hasattr(termios, "CRTSCTS"):
            attrs[2] &= ~termios.CRTSCTS
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        # A CDC-ACM console only transmits once the host asserted DTR.
        try:
            fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))
        except OSError:
            pass
    except Exception:
        os.close(fd)
        raise
    return fd


class Capture:
    def __init__(self, args: argparse.Namespace, use_com: bool | None = None) -> None:
        self.args = args
        self.lines = 0
        self.log = None
        self.use_com = should_use_com(args) if use_com is None else use_com
        self._ps_warned = False
        self._open_failed: str | None = None
        # Pause support (SIGUSR1, see release_port()/wait_out_pause()): the
        # port is handed to another tool - scripts/flash.py --enter - without
        # ending the capture.
        self._pause_until = 0.0
        self._child = None
        self._fd: int | None = None
        self._pidfile: str | None = None

    # -- output ------------------------------------------------------------
    def emit(self, t: float, line: str) -> None:
        text = "%s %s" % (format_host_prefix(t), line)
        self.log.write(text + "\n")
        self.log.flush()
        self.lines += 1
        if not self.args.quiet:
            print(text, flush=True)

    def note(self, msg: str) -> None:
        """Host-side status line; written to the log so gaps are explainable."""
        self.emit(time.time(), "--- capture: %s ---" % msg)

    # -- pausing (SIGUSR1) -------------------------------------------------
    def release_port(self) -> None:
        """Let go of the port right now: kill the reader, close the fd.

        Runs from a signal handler, so it only does things that are safe there:
        no waitpid, no logging through the buffered log file.  The main loop is
        blocked in ``proc.stdout.read()`` or ``select()``; terminating the child
        makes the former return b"" and closing the fd makes the latter fail
        with EBADF, and both are already treated as "device gone".
        """
        proc = self._child
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except OSError:
                pass
        fd = self._fd
        if fd is not None:
            self._fd = None
            try:
                os.close(fd)
            except OSError:
                pass

    def pause_for(self, seconds: float) -> None:
        """Close the port and keep it closed for `seconds` (SIGUSR1 handler)."""
        deadline = time.time() + seconds
        if deadline > self._pause_until:
            self._pause_until = deadline
        self.release_port()

    def install_pause_handler(self) -> bool:
        """Arrange for SIGUSR1 to pause the capture.  False if unavailable."""
        if PAUSE_SIGNAL is None:
            return False

        def handler(signum, frame):  # noqa: ARG001 - signal handler signature
            self.pause_for(getattr(self.args, "pause_seconds", DEFAULT_PAUSE_SECONDS))

        try:
            signal.signal(PAUSE_SIGNAL, handler)
        except (ValueError, OSError):  # not the main thread / not supported
            return False
        return True

    def wait_out_pause(self) -> bool:
        """Block until a pause requested by SIGUSR1 is over.  True if it waited."""
        remaining = self._pause_until - time.time()
        if remaining <= 0:
            return False
        self.note("paused for %.0fs, the port is free (SIGUSR1)" % remaining)
        while True:
            remaining = self._pause_until - time.time()
            if remaining <= 0:
                break
            # Short naps so a second SIGUSR1 that extends the pause is noticed.
            time.sleep(min(0.2, remaining))
        self.note("pause over, reopening the port")
        return True

    # -- port selection ----------------------------------------------------
    def resolve_port(self) -> str | None:
        if self.args.port:
            return self.args.port if os.path.exists(self.args.port) else None
        by_id, acm = list_candidate_ports()
        cands = pick_ports(by_id, acm)
        if not cands:
            return None
        if len(cands) > 1:
            sys.stderr.write("capture: several serial ports found, pass --port:\n")
            for c in cands:
                sys.stderr.write("  %s\n" % c)
            sys.exit(2)
        return cands[0]

    # -- port selection (Windows COM through PowerShell) -------------------
    def resolve_com_port(self) -> str | None:
        """Return the COM port to open, or None to retry later."""
        want = normalize_com(self.args.com) if self.args.com else None
        try:
            ports = list_windows_com_ports()
        except PowerShellError as e:
            if not self._ps_warned:
                print("capture: %s" % e, file=sys.stderr)
                self._ps_warned = True
            # With an explicit --com try anyway: enumeration may fail for
            # reasons that do not stop the port itself from opening.
            return want
        self._ps_warned = False
        if want:
            return want if any(p.com == want for p in ports) else None
        cands = pick_com_ports(ports)
        if not cands:
            return None
        if len(cands) > 1:
            sys.stderr.write("capture: several Windows COM ports found, pass --com:\n")
            for c in cands:
                sys.stderr.write("  %s\n" % c.describe())
            sys.exit(2)
        return cands[0].com

    def read_com_until_disconnect(self, com: str) -> int:
        """Stream `com` through read_com.ps1; returns the child's exit code."""
        try:
            cmd = powershell_read_command(com, self.args.baud)
        except PowerShellError as e:
            print("capture: %s" % e, file=sys.stderr)
            return PS_EXIT_OPEN_FAILED
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        except OSError as e:
            print("capture: cannot start %s: %s" % (PS_EXE, e), file=sys.stderr)
            return PS_EXIT_OPEN_FAILED
        self._child = proc
        try:
            return self.pump_com_reader(proc, com)
        finally:
            self._child = None

    def pump_com_reader(self, proc, com: str) -> int:
        """Feed the child's stdout through the same line pipeline as termios."""
        splitter = LineSplitter()
        try:
            try:
                while True:
                    data = proc.stdout.read(4096)
                    if not data:
                        break  # child exited: device gone, or open failed
                    now = time.time()
                    for t, line in splitter.feed(data, now):
                        self.emit(t, strip_ansi(line))
            finally:
                for t, line in splitter.flush():
                    self.emit(t, strip_ansi(line))
        except KeyboardInterrupt:
            terminate_child(proc)
            raise
        rc = proc.wait()
        messages = read_child_stderr(proc)
        verbose = getattr(self.args, "verbose_child", False)
        if rc == PS_EXIT_OPEN_FAILED:
            # The retry loop hammers the port; explain only when it changes.
            if self._open_failed != com:
                self._open_failed = com
                for msg in messages:
                    print("capture: %s" % msg, file=sys.stderr)
                print("capture: if that says access denied, another program "
                      "(PuTTY, TeraTerm, ZMK Studio, another capture.py) has %s open" % com,
                      file=sys.stderr)
        else:
            self._open_failed = None
            if verbose:
                for msg in messages:
                    print("capture: %s" % msg, file=sys.stderr)
        return rc

    # -- main loop ---------------------------------------------------------
    def run(self) -> int:
        os.makedirs(self.args.out_dir, exist_ok=True)
        path = self.args.file or default_log_path(self.args.out_dir, self.args.label)
        self.log = open(path, "a", encoding="utf-8")
        print("capture: writing %s (Ctrl-C to stop)" % path, file=sys.stderr)
        self.emit(time.time(), "--- capture: start label=%s date=%s ---" % (self.args.label, _dt.date.today()))
        if not getattr(self.args, "no_pidfile", False):
            self._pidfile = write_pidfile(
                getattr(self.args, "pidfile", None)
                or pidfile_path(self.args.out_dir, self.args.label))
            if self._pidfile and self.install_pause_handler():
                # scripts/flash.py --enter reads this file and sends SIGUSR1
                # before it opens the port itself (Windows hands out only one
                # handle per COM port).
                print("capture: pid %d in %s; SIGUSR1 frees the port for %gs"
                      % (os.getpid(), self._pidfile,
                         getattr(self.args, "pause_seconds", DEFAULT_PAUSE_SECONDS)),
                      file=sys.stderr)
        announced_wait = False
        open_failed = False
        try:
            while True:
                if self.wait_out_pause():
                    # The device very likely rebooted while we were away, so
                    # re-resolve the port instead of trusting the old name.
                    announced_wait = False
                    open_failed = False
                if self.use_com:
                    com = self.resolve_com_port()
                    if com is None:
                        if not announced_wait:
                            print("capture: waiting for a Windows COM port to appear...", file=sys.stderr)
                            announced_wait = True
                        time.sleep(self.args.retry)
                        continue
                    announced_wait = False
                    if not open_failed:
                        self.note("opening %s @ %d (via %s)" % (com, self.args.baud, PS_EXE))
                    rc = self.read_com_until_disconnect(com)
                    if rc == PS_EXIT_OPEN_FAILED:
                        if not open_failed:
                            open_failed = True
                            self.note("%s could not be opened, retrying" % com)
                    else:
                        open_failed = False
                        self.note("device disconnected, waiting for it to come back")
                    time.sleep(self.args.retry)
                    continue
                port = self.resolve_port()
                if port is None:
                    if not announced_wait:
                        print("capture: waiting for a serial port to appear...", file=sys.stderr)
                        announced_wait = True
                    time.sleep(self.args.retry)
                    continue
                announced_wait = False
                try:
                    fd = open_serial(port, self.args.baud)
                except OSError as e:
                    if e.errno in (errno.EBUSY, errno.EACCES):
                        print("capture: cannot open %s: %s" % (port, e.strerror), file=sys.stderr)
                        if e.errno == errno.EACCES:
                            print("capture: add yourself to the dialout/uucp group or use sudo", file=sys.stderr)
                        time.sleep(self.args.retry)
                        continue
                    time.sleep(self.args.retry)
                    continue
                self.note("connected %s @ %d" % (port, self.args.baud))
                self.read_until_disconnect(fd)
                self.note("device disconnected, waiting for it to come back")
                time.sleep(self.args.retry)
        except KeyboardInterrupt:
            self.note("stopped by user")
        finally:
            remove_pidfile(self._pidfile)
            self.log.close()
            print("capture: %d lines written to %s" % (self.lines, path), file=sys.stderr)
        return 0

    def read_until_disconnect(self, fd: int) -> None:
        splitter = LineSplitter()
        # Published so the SIGUSR1 handler can close it under us; EBADF below
        # is then just another way of saying "the port is gone".
        self._fd = fd
        try:
            while True:
                try:
                    r, _, _ = select.select([fd], [], [], 1.0)
                except (OSError, ValueError):
                    return  # closed by release_port()
                if not r:
                    continue
                try:
                    data = os.read(fd, 4096)
                except OSError as e:
                    if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        continue
                    if e.errno in (errno.EIO, errno.ENXIO, errno.ENODEV, errno.EBADF):
                        return
                    raise
                if data == b"":
                    return  # EOF: device gone
                now = time.time()
                for t, line in splitter.feed(data, now):
                    self.emit(t, line)
        finally:
            for t, line in splitter.flush():
                self.emit(t, line)
            if self._fd is not None:
                self._fd = None
                try:
                    os.close(fd)
                except OSError:
                    pass


def terminate_child(proc) -> None:
    """Best effort: stop the powershell.exe child (Ctrl-C, end of capture)."""
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass


def read_child_stderr(proc) -> list[str]:
    """Drain the child's stderr after it exited; never raises."""
    try:
        if proc.stderr is None:
            return []
        data = proc.stderr.read() or b""
    except (OSError, ValueError):
        return []
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return [ln.strip() for ln in data.decode("utf-8", errors="replace").splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# tio fallback
# --------------------------------------------------------------------------
def run_tio(args: argparse.Namespace) -> int:
    tio = shutil.which("tio")
    if not tio:
        print("capture: tio not found on PATH", file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    path = args.file or default_log_path(args.out_dir, args.label)
    port = args.port
    if not port:
        by_id, acm = list_candidate_ports()
        cands = pick_ports(by_id, acm)
        if len(cands) != 1:
            print("capture: pass --port (candidates: %s)" % ", ".join(cands) or "none", file=sys.stderr)
            return 2
        port = cands[0]
    cmd = [tio, "--baudrate", str(args.baud), "--timestamp", "--timestamp-format", "24hour",
           "--log", "--log-file", path, port]
    print("capture: running %s" % " ".join(cmd), file=sys.stderr)
    return subprocess.call(cmd)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--port", help="serial device (default: auto-detect, see below)")
    p.add_argument("--com", metavar="COMn",
                   help="WSL2/Windows: read this Windows COM port through powershell.exe "
                        "(no usbipd needed; auto-detected under WSL when no /dev/ttyACM* exists)")
    p.add_argument("--label", default="device", choices=LABELS,
                   help="which device this is; becomes the log file prefix (default: device)")
    p.add_argument("--baud", type=int, default=115200, help="baud rate (default 115200; CDC-ACM ignores it)")
    p.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="directory for log files (default: <repo>/logs)")
    p.add_argument("--file", help="explicit output file instead of <out-dir>/<label>-<timestamp>.log")
    p.add_argument("--retry", type=float, default=0.5, help="seconds between reconnect attempts (default 0.5)")
    p.add_argument("--quiet", action="store_true", help="do not echo lines to stdout")
    p.add_argument("--list", action="store_true", help="list candidate serial ports and exit")
    p.add_argument("--verbose-child", action="store_true",
                   help="echo the status lines of the powershell.exe reader to stderr")
    p.add_argument("--pidfile", help="where to advertise this capture's pid "
                                     "(default: <out-dir>/capture-<label>.pid)")
    p.add_argument("--no-pidfile", action="store_true",
                   help="do not write a pidfile (scripts/flash.py --enter can then not ask "
                        "this capture to release the port)")
    p.add_argument("--pause-seconds", type=float, default=DEFAULT_PAUSE_SECONDS,
                   help="how long SIGUSR1 makes this capture close the port and stay away "
                        "(default %g); scripts/flash.py --enter uses it to open the same "
                        "port, which Windows only hands to one program at a time"
                        % DEFAULT_PAUSE_SECONDS)
    p.add_argument("--tio", action="store_true", help="delegate to `tio --timestamp --log` instead of the built-in reader")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        by_id, acm = list_candidate_ports()
        print("by-id:   " + (", ".join(by_id) or "(none)"))
        print("ttyACM:  " + (", ".join(acm) or "(none)"))
        print("would use: " + (", ".join(pick_ports(by_id, acm)) or "(none)"))
        if is_wsl() or args.com:
            print("")
            try:
                ports = list_windows_com_ports()
            except PowerShellError as e:
                print("Windows COM: unavailable (%s)" % e)
                return 0
            if not ports:
                print("Windows COM: (no COM ports)")
                print("  plug the keyboard in; Device Manager should show "
                      "\"USB Serial Device (COMn)\" under Ports (COM & LPT)")
                return 0
            print("Windows COM:")
            for port in ports:
                print("  " + port.describe())
            chosen = pick_com_ports(ports)
            if len(chosen) == 1:
                print("would use: --com %s" % chosen[0].com)
            else:
                print("would use: pass --com (%s)" % ", ".join(p.com for p in chosen))
        return 0
    if args.tio:
        return run_tio(args)
    use_com = should_use_com(args)
    if use_com and args.com:
        rc = check_com_available(normalize_com(args.com))
        if rc:
            return rc
    return Capture(args, use_com=use_com).run()


def check_com_available(com: str) -> int:
    """Fail early (exit 2) on --com COMn when Windows has no such port."""
    try:
        ports = list_windows_com_ports()
    except PowerShellError as e:
        print("capture: %s" % e, file=sys.stderr)
        print("capture: continuing anyway; %s will be opened directly" % com, file=sys.stderr)
        return 0
    if any(p.com == com for p in ports):
        return 0
    print("capture: %s does not exist on the Windows side" % com, file=sys.stderr)
    if ports:
        print("capture: available ports:", file=sys.stderr)
        for port in ports:
            print("  " + port.describe(), file=sys.stderr)
    else:
        print("capture: no COM ports at all - plug the keyboard in and check "
              "Device Manager > Ports (COM & LPT)", file=sys.stderr)
    print("capture: see `%s --list`" % os.path.basename(__file__), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
