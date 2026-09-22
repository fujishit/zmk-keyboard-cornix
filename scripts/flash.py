#!/usr/bin/env python3
"""Copy a UF2 firmware file onto a board that is in its UF2 bootloader.

Python 3 standard library only, like the other scripts in this repository.
Works on Linux, macOS and WSL2; on WSL2 the Windows side is driven through
``powershell.exe`` because WSL does not auto-mount a USB drive that is plugged
in (or re-appears after a reset) once the distribution is already running.

What it does, in order:

1. find every mounted volume whose root contains ``INFO_UF2.TXT`` (that file is
   what makes a drive a UF2 bootloader drive, not its name);
2. if there is none, poll every 0.5 s until one shows up or ``--timeout``
   expires, printing how to get the board into the bootloader;
3. print the volume label and the ``Model``/``Board-ID``/``Bootloader`` lines
   of ``INFO_UF2.TXT`` so you can confirm *which* half you are about to
   overwrite - the two halves of a split look identical otherwise;
4. refuse to guess when more than one UF2 volume is present (pass ``--volume``);
5. copy the file, then wait for the volume to disappear, which is how the
   nRF52840 UF2 bootloader says "written, rebooting into the new firmware".

Usage::

    python3 scripts/flash.py firmware/keymap/cornix_left.uf2 --label left
    python3 scripts/flash.py --list
    python3 scripts/flash.py --wait-only
    python3 scripts/flash.py firmware/keymap/cornix_right.uf2 --dry-run
    python3 scripts/flash.py --enter left
    python3 scripts/flash.py enter right          # same thing, sub-action spelling
    python3 scripts/flash.py --enter left firmware/led/cornix_left_debug.uf2

With ``--enter`` no one has to touch the keyboard: the left half's USB CDC
console is opened at a magic baud rate (the "1200 bps touch"), which the
firmware turns into a bootloader entry.  It needs a firmware built with
``CONFIG_CORNIX_REMOTE_BOOT=y`` - the ``cornix-remote-boot`` snippet, which
``cornix-debug-log`` pulls in.  See the "Remote bootloader entry" section of
scripts/FLASHING.md.

Exit status:
    0  the file was copied and the bootloader volume went away
    1  the copy failed, or the volume was still there afterwards
    2  usage error (bad arguments, missing/invalid file)
    3  no UF2 volume appeared before --timeout
    4  could not decide which volume to write: several are present and
       --volume was not given, or --volume matched none/several of them
    5  --enter could not touch the console port (no port found, or the
       host-side command failed)

See scripts/FLASHING.md for the double-tap-reset / ``&bootloader`` key /
settings-reset procedures and the WSL2 notes.
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field

try:  # POSIX only; on WSL2 the port is touched through powershell.exe instead.
    import fcntl
    import struct
    import termios
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = struct = termios = None  # type: ignore[assignment]

INFO_FILE = "INFO_UF2.TXT"
FAIL_FILE = "FAIL.TXT"
LABELS = ("left", "right", "dongle")

#: First word of every UF2 block ("UF2\n" little endian).
UF2_MAGIC = b"UF2\n"

#: Seconds to wait for the bootloader volume to disappear after the copy.
DEFAULT_SETTLE = 30.0

#: INFO_UF2.TXT keys worth showing, in the order the bootloader writes them.
INFO_KEYS = ("Model", "Board-ID", "Bootloader", "Date", "SoftDevice")

#: --enter target -> the dwDTERate the firmware reacts to.  Must stay in step
#: with src/remote_boot.c (RATE_LOCAL_BOOTLOADER / RATE_PERIPHERAL_BOOTLOADER /
#: RATE_RESET) - the device ignores every other rate, which is what keeps a
#: terminal program from rebooting the keyboard by accident.
ENTER_BAUDS = {
    "left": 1200,     # this half (the one with the console) -> UF2 bootloader
    "right": 2400,    # forwarded to split peripheral 0 -> its UF2 bootloader
    "reset-left": 4800,  # sys_reboot(SYS_REBOOT_COLD) of this half
}
ENTER_TARGETS = tuple(ENTER_BAUDS)

#: --enter targets that end in a bootloader (so a file argument can be flashed).
ENTER_FLASHABLE = {"left": "left", "right": "right"}

#: Seconds the port is held open at the magic rate before it is closed again.
#: Long enough for the device to see SetLineCoding and short enough that the
#: reboot (CONFIG_CORNIX_REMOTE_BOOT_DELAY_MS, 100 ms) happens after the close.
DEFAULT_TOUCH_DWELL = 0.3

#: Where capture.py advertises its pid (see scripts/log/capture.py).
CAPTURE_PIDFILE_GLOB = "capture-*.pid"
DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")

EPILOG = """
getting the board into the bootloader:
  double-tap the reset button (two taps within ~0.5 s), or press the
  `&bootloader` key on the Fn3 layer: hold the left inner thumb key and tap
  T for the left half or Y for the right half.  A drive named CORNIX/NICENANO
  appears on the host; that drive is what this script writes to.  Each half
  has its own bootloader, so flash them one at a time.

--enter (no hands needed, needs CONFIG_CORNIX_REMOTE_BOOT=y):
  --enter left        1200 bps on the left half's CDC console -> the left half
                      enters its UF2 bootloader
  --enter right       2400 bps -> the left half forwards a `bootload` behavior
                      to the right half over the split link, which enters its
                      own bootloader.  The two halves must be connected.
  --enter reset-left  4800 bps -> a plain cold reset of the left half (no
                      bootloader, no flashing)
  The port is the left half's ZMK console: auto-detected (USB VID 1D50), or
  named with --com COMn (WSL2) / --port /dev/ttyACM0 (Linux, macOS).

  Windows allows only ONE handle on a COM port, so a running
  scripts/log/capture.py has to let go of it first.  This script asks it to:
  capture.py writes logs/capture-<label>.pid and releases the port for 15 s on
  SIGUSR1.  --no-release skips that (use it when you stopped the capture
  yourself); if no pidfile is found and the port is busy you get "access
  denied" and have to stop the capture.

WSL2:
  WSL does not always mount a removable drive that is plugged in after boot,
  so this script asks Windows instead: `Get-CimInstance Win32_LogicalDisk`
  (DriveType 2 = removable) plus `Get-Volume` for the label.  If the drive is
  visible under /mnt/<letter>/ the copy is a plain shutil.copy; otherwise the
  file is copied by `Copy-Item` inside PowerShell, with the source translated
  by `wslpath -w` (a \\\\wsl.localhost\\... UNC path, which Windows accepts).
  You do NOT need usbipd for flashing - that is only for the USB serial log.

the copy "failing" is usually success:
  The bootloader reboots as soon as the last block is written, so the drive
  can vanish in the middle of the copy and the write returns an I/O error.
  This script treats a disconnect during the copy as success as long as the
  volume is really gone afterwards; a volume that is still there after 30 s
  means the write did not take, and FAIL.TXT on the drive (printed here when
  present) says why.
"""


# --------------------------------------------------------------------------
# pure helpers (no I/O, exercised by tests/flash/test_flash.py)
# --------------------------------------------------------------------------
def parse_info_uf2(text: str) -> dict[str, str]:
    """Parse INFO_UF2.TXT into a dict.

    The nRF52840 (Adafruit) UF2 bootloader writes a free-form banner line
    followed by ``Key: value`` lines, e.g.::

        UF2 Bootloader 0.6.0 lib/nrfx (v2.0.0) lib/tinyusb (0.10.1-293-g...)
        Model: Cornix
        Board-ID: nRF52840-cornix-v1
        SoftDevice: S140 version 6.1.1
        Date: Jun 30 2021

    The banner is returned under the key ``Bootloader`` when no explicit
    ``Bootloader:`` line exists, because that is the line a human needs.
    Keys keep the bootloader's spelling; lookup is case-insensitive through
    :func:`info_get`.
    """
    info: dict[str, str] = {}
    for index, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if sep and key.strip() and " " not in key.strip():
            info[key.strip()] = value.strip()
        elif index == 0:
            info.setdefault("Bootloader", line)
    return info


def info_get(info: dict[str, str], key: str) -> str | None:
    """Case-insensitive lookup in a parsed INFO_UF2.TXT."""
    for name, value in info.items():
        if name.lower() == key.lower():
            return value
    return None


def describe_info(info: dict[str, str]) -> list[str]:
    """The INFO_UF2.TXT lines worth printing, as "Key: value" strings."""
    lines = []
    for key in INFO_KEYS:
        value = info_get(info, key)
        if value:
            lines.append(f"{key}: {value}")
    return lines


def parse_powershell_csv(text: str) -> list[dict[str, str]]:
    """Parse `ConvertTo-Csv -NoTypeInformation` output into a list of rows.

    PowerShell may emit nothing at all for an empty result set (not even a
    header row) and may prefix the output with a UTF-8 BOM or blank lines.
    """
    stripped = text.lstrip("\ufeff").strip()
    if not stripped:
        return []
    rows = list(csv.DictReader(io.StringIO(stripped)))
    return [{(k or ""): (v if v is not None else "") for k, v in row.items()} for row in rows]


def parse_logical_disks(csv_text: str) -> list[tuple[str, str]]:
    """Removable drives from `Get-CimInstance Win32_LogicalDisk` CSV output.

    Returns ``[(drive, label), ...]`` with ``drive`` like ``"E:"``.
    Win32_LogicalDisk DriveType 2 is "Removable Disk"; 3 is a fixed disk,
    4 a network drive, 5 optical.  A UF2 bootloader enumerates as 2.
    """
    found: list[tuple[str, str]] = []
    for row in parse_powershell_csv(csv_text):
        if (row.get("DriveType") or "").strip() != "2":
            continue
        drive = (row.get("DeviceID") or "").strip().upper()
        if len(drive) == 1:
            drive += ":"
        if not drive.endswith(":") or len(drive) != 2:
            continue
        found.append((drive, (row.get("VolumeName") or "").strip()))
    return found


def parse_get_volume(csv_text: str) -> dict[str, str]:
    """Drive letter -> FileSystemLabel from `Get-Volume` CSV output.

    Used to fill in a label Win32_LogicalDisk left empty, and as a fallback
    source of removable drives (`DriveType` is the string "Removable" here).
    """
    labels: dict[str, str] = {}
    for row in parse_powershell_csv(csv_text):
        letter = (row.get("DriveLetter") or "").strip().upper()
        if len(letter) != 1 or not letter.isalpha():
            continue
        labels[letter + ":"] = (row.get("FileSystemLabel") or "").strip()
    return labels


def parse_get_volume_removable(csv_text: str) -> list[tuple[str, str]]:
    """Removable drives from `Get-Volume` CSV output (DriveType "Removable")."""
    found: list[tuple[str, str]] = []
    for row in parse_powershell_csv(csv_text):
        if (row.get("DriveType") or "").strip().lower() != "removable":
            continue
        letter = (row.get("DriveLetter") or "").strip().upper()
        if len(letter) != 1 or not letter.isalpha():
            continue
        found.append((letter + ":", (row.get("FileSystemLabel") or "").strip()))
    return found


def drive_mount_point(drive: str) -> str:
    """Windows drive letter -> the path WSL mounts it at ("E:" -> "/mnt/e")."""
    letter = drive.rstrip(":").lower()
    return "/mnt/" + letter


def windows_root(drive: str) -> str:
    """Windows drive letter -> its root in Windows syntax ("E:" -> "E:\\\\")."""
    return drive.rstrip(":").upper() + ":\\"


def windows_join(drive: str, name: str) -> str:
    """Windows path of `name` at the root of `drive` ("E:", "FAIL.TXT")."""
    return windows_root(drive) + name


def uf2_info_globs(system: str) -> list[str]:
    """Where to look for INFO_UF2.TXT on a POSIX host.

    linux: udisks2 mounts under /run/media/<user>/<label>/, older
    udisks/pmount and most desktop environments under /media/<user>/<label>/,
    and a plain `mount` or an fstab entry often lands in /media/<label>/.
    macos: everything is /Volumes/<label>/.
    """
    if system == "macos":
        return ["/Volumes/*/" + INFO_FILE]
    return [
        "/media/*/*/" + INFO_FILE,
        "/run/media/*/*/" + INFO_FILE,
        "/media/*/" + INFO_FILE,
    ]


def is_com_name(port: str) -> bool:
    """True for a Windows "COM7"-style port name, false for "/dev/ttyACM0"."""
    text = (port or "").strip().upper()
    return text.startswith("COM") and text[3:].isdigit()


def enter_baud(target: str) -> int:
    """--enter target -> magic baud rate.  Raises KeyError on an unknown one."""
    return ENTER_BAUDS[target]


def _ps_serial_port_expr(com: str, baud: int) -> str:
    """The `New-Object System.IO.Ports.SerialPort` call for `com` at `baud`."""
    return (f"New-Object System.IO.Ports.SerialPort "
            f"{_ps_quote(com)},{int(baud)},'None',8,'One'")


def powershell_touch_script(com: str, baud: int, dwell: float = DEFAULT_TOUCH_DWELL) -> str:
    """PowerShell that opens `com` at `baud`, asserts DTR, waits and closes.

    This is the whole "1200 bps touch" on the Windows side.  Nothing is
    written to the port: the device only needs the SET_LINE_CODING control
    request that .NET sends when the port is opened with that BaudRate.
    DtrEnable matters because a CDC-ACM console only starts talking once DTR
    is asserted, and because some hosts skip SET_LINE_CODING otherwise.

    The close is in a `finally` so a failure in between cannot leave the COM
    port locked on the Windows side, which would then also block capture.py.
    """
    ms = max(0, int(round(dwell * 1000)))
    return (
        "$ErrorActionPreference='Stop'; "
        f"$p = {_ps_serial_port_expr(com, baud)}; "
        "$p.DtrEnable = $true; "
        "$p.Open(); "
        "try { "
        f"Start-Sleep -Milliseconds {ms} "
        "} finally { $p.Close(); $p.Dispose() }; "
        f"Write-Output 'touched {com} @ {int(baud)}'"
    )


def termios_touch_speed(baud: int):
    """The termios B<rate> constant for `baud`, or None when unsupported."""
    if termios is None:
        return None
    return getattr(termios, "B%d" % int(baud), None)


def capture_pidfiles(log_dir: str) -> list[str]:
    """Paths of the capture pidfiles in `log_dir`, sorted."""
    return sorted(glob.glob(os.path.join(log_dir, CAPTURE_PIDFILE_GLOB)))


def read_pid(path: str) -> int | None:
    """The pid written in `path`, or None when missing/empty/garbage."""
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


def looks_like_uf2(head: bytes) -> bool:
    """True when `head` (the first bytes of a file) starts a UF2 block."""
    return head[:4] == UF2_MAGIC


@dataclass
class Volume:
    """A UF2 bootloader volume, reachable through a path, a drive letter or both."""

    label: str
    #: POSIX path of the volume root, when the host can see it directly.
    path: str | None = None
    #: Windows drive letter with colon ("E:"), on WSL2.
    drive: str | None = None
    info: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Short stable identifier used in messages and by --volume."""
        return self.drive or self.path or self.label

    def describe(self) -> str:
        where = self.path or ""
        if self.drive and self.path:
            where = f"{self.drive} (mounted at {self.path})"
        elif self.drive:
            where = f"{self.drive} (not mounted in WSL)"
        return f"{self.label or '(no label)'} at {where}"


def volume_matches(volume: Volume, wanted: str) -> bool:
    """Does `--volume <wanted>` name this volume?

    Accepts a drive letter with or without a colon ("e", "E:"), a mount path
    (with or without a trailing slash) or the volume label - all case-folded.
    """
    wanted = wanted.strip()
    if not wanted:
        return False
    folded = wanted.casefold().rstrip("/")
    candidates = set()
    if volume.drive:
        candidates.add(volume.drive.casefold())
        candidates.add(volume.drive.rstrip(":").casefold())
        candidates.add(drive_mount_point(volume.drive).casefold())
    if volume.path:
        candidates.add(volume.path.casefold().rstrip("/"))
        candidates.add(os.path.basename(volume.path.rstrip("/")).casefold())
    if volume.label:
        candidates.add(volume.label.casefold())
    return folded in candidates


def select_volume(volumes: list[Volume], wanted: str | None = None) -> tuple[Volume | None, str | None]:
    """Pick the volume to flash.

    Returns ``(volume, None)`` on success or ``(None, message)`` when the
    caller must not proceed: nothing found, an unmatched/ambiguous --volume,
    or several UF2 volumes with no --volume to disambiguate.
    """
    if wanted:
        hits = [v for v in volumes if volume_matches(v, wanted)]
        if not hits:
            known = ", ".join(v.key for v in volumes) or "(none)"
            return None, f"no UF2 volume matches --volume {wanted!r}; found: {known}"
        if len(hits) > 1:
            known = ", ".join(v.key for v in hits)
            return None, f"--volume {wanted!r} is ambiguous, it matches: {known}"
        return hits[0], None
    if not volumes:
        return None, "no UF2 volume found"
    if len(volumes) > 1:
        listing = "\n".join("  " + v.describe() for v in volumes)
        return None, (
            f"{len(volumes)} UF2 volumes are present; refusing to guess which half to "
            f"overwrite.\n{listing}\n"
            "Flash one half at a time, or name one with --volume (a drive letter, "
            "mount path or label)."
        )
    return volumes[0], None


def label_mismatch(filename: str, label: str | None) -> str | None:
    """Warn when --label and the file name disagree (e.g. --label left ... right.uf2)."""
    if not label:
        return None
    name = os.path.basename(filename).casefold()
    if label.casefold() in name:
        return None
    others = [other for other in LABELS if other != label and other in name]
    if others:
        return (f"--label {label} but the file is named {os.path.basename(filename)!r}, "
                f"which looks like the {others[0]} firmware")
    return None


# --------------------------------------------------------------------------
# platform / I/O
# --------------------------------------------------------------------------
def detect_platform() -> str:
    """Return "wsl", "macos" or "linux"."""
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Linux":
        release = platform.uname().release.lower()
        if "microsoft" in release or os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
            return "wsl"
        return "linux"
    return system.lower()


def run_powershell(script: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a PowerShell snippet on the Windows side of WSL2 and return (rc, out, err)."""
    command = [
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + script,
    ]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "", "powershell.exe not found on PATH (is this really WSL2?)"
    except subprocess.TimeoutExpired:
        return 124, "", f"powershell.exe timed out after {timeout:g}s"
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def wsl_to_windows_path(path: str) -> str | None:
    """`wslpath -w` - the Windows spelling of a WSL path, or None on failure."""
    try:
        proc = subprocess.run(["wslpath", "-w", path], capture_output=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace").strip() or None


def _ps_quote(value: str) -> str:
    """Quote a string for a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


_capture_module = None


def capture_module():
    """Import scripts/log/capture.py once, for port detection and the pidfiles.

    capture.py is the script that already knows how to find the ZMK console
    port on every platform this repository supports; duplicating that here
    would guarantee the two disagree eventually.  Returns None if it cannot be
    imported (it never should be, both files ship together).
    """
    global _capture_module
    if _capture_module is None:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log")
        if log_dir not in sys.path:
            sys.path.insert(0, log_dir)
        try:
            import capture  # noqa: PLC0415 - deliberately lazy
        except ImportError:
            return None
        _capture_module = capture
    return _capture_module


def release_captures(log_dir: str, pause: float = 2.0,
                     kill=None, verbose: bool = True) -> list[str]:
    """Ask every running capture.py to let go of the port; return the pidfiles.

    Windows gives out exactly one handle per COM port, so `--enter` and a
    running `scripts/log/capture.py` cannot both have it: the second open
    fails with "access denied".  Rather than killing the capture (which would
    lose the reconnect loop, the open log file and the line numbering), we ask
    it to step aside: capture.py installs a SIGUSR1 handler that closes the
    port and keeps it closed for --pause-seconds (15 s by default), then its
    existing reconnect loop picks the device back up - which is exactly what it
    already does across the reboot into the bootloader.

    A stale pidfile (the capture was killed without cleaning up) is skipped:
    os.kill raises ProcessLookupError, and the file is left alone because
    removing another program's pidfile is worse than a harmless warning.
    """
    kill = kill or os.kill
    signalled: list[str] = []
    for path in capture_pidfiles(log_dir):
        pid = read_pid(path)
        if pid is None:
            continue
        try:
            kill(pid, signal.SIGUSR1)
        except ProcessLookupError:
            if verbose:
                print(f"note: stale capture pidfile {path} (pid {pid} is gone)")
            continue
        except (OSError, AttributeError) as exc:
            if verbose:
                print(f"note: cannot signal capture pid {pid}: {exc}", file=sys.stderr)
            continue
        signalled.append(path)
        if verbose:
            print(f"asked capture (pid {pid}) to release the port for a moment")
    if signalled and pause > 0:
        time.sleep(pause)
    return signalled


class Host:
    """The bits of the world flash.py touches, so tests can replace them."""

    def __init__(self, system: str, verbose: bool = False) -> None:
        self.system = system
        self.verbose = verbose

    # ---- discovery -------------------------------------------------------
    def find_volumes(self) -> list[Volume]:
        if self.system == "wsl":
            return self._find_volumes_wsl()
        return self._find_volumes_posix()

    def _find_volumes_posix(self) -> list[Volume]:
        volumes: list[Volume] = []
        seen: set[str] = set()
        for pattern in uf2_info_globs(self.system):
            for info_path in sorted(glob.glob(pattern)):
                root = os.path.dirname(info_path)
                real = os.path.realpath(root)
                if real in seen:
                    continue
                seen.add(real)
                try:
                    with open(info_path, encoding="utf-8", errors="replace") as handle:
                        text = handle.read()
                except OSError:
                    continue
                volumes.append(Volume(label=os.path.basename(root), path=root,
                                      info=parse_info_uf2(text)))
        return volumes

    def _removable_drives_wsl(self) -> list[tuple[str, str]]:
        rc, out, err = run_powershell(
            "Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,DriveType,VolumeName "
            "| ConvertTo-Csv -NoTypeInformation"
        )
        drives = parse_logical_disks(out) if rc == 0 else []
        if rc != 0 and self.verbose:
            print(f"note: Win32_LogicalDisk query failed ({rc}): {err.strip()}", file=sys.stderr)

        rc2, out2, err2 = run_powershell(
            "Get-Volume | Select-Object DriveLetter,FileSystemLabel,DriveType "
            "| ConvertTo-Csv -NoTypeInformation"
        )
        if rc2 != 0:
            if self.verbose:
                print(f"note: Get-Volume query failed ({rc2}): {err2.strip()}", file=sys.stderr)
            return drives
        labels = parse_get_volume(out2)
        known = {drive for drive, _ in drives}
        # Get-Volume knows removable media Win32_LogicalDisk can miss, and has
        # the friendlier label; merge both views.
        merged = [(drive, label or labels.get(drive, "")) for drive, label in drives]
        for drive, label in parse_get_volume_removable(out2):
            if drive not in known:
                merged.append((drive, label))
        return merged

    def _find_volumes_wsl(self) -> list[Volume]:
        volumes: list[Volume] = []
        for drive, label in self._removable_drives_wsl():
            mount = drive_mount_point(drive)
            info_path = os.path.join(mount, INFO_FILE)
            text: str | None = None
            path: str | None = None
            if os.path.isdir(mount) and os.path.exists(info_path):
                path = mount
                try:
                    with open(info_path, encoding="utf-8", errors="replace") as handle:
                        text = handle.read()
                except OSError:
                    text = None
            if text is None:
                text = self._read_windows_file(windows_join(drive, INFO_FILE))
                if text is None:
                    continue  # removable, but not a UF2 bootloader drive
                if os.path.isdir(mount):
                    path = mount
            volumes.append(Volume(label=label, path=path, drive=drive,
                                  info=parse_info_uf2(text)))
        return volumes

    def _read_windows_file(self, win_path: str) -> str | None:
        rc, out, _ = run_powershell(
            f"if (Test-Path -LiteralPath {_ps_quote(win_path)}) "
            f"{{ Get-Content -Raw -LiteralPath {_ps_quote(win_path)} }}"
        )
        if rc != 0:
            return None
        return out if out.strip() else None

    # ---- per-volume operations -------------------------------------------
    def volume_present(self, volume: Volume) -> bool:
        if volume.path and os.path.exists(os.path.join(volume.path, INFO_FILE)):
            return True
        if volume.drive:
            return self._read_windows_file(windows_join(volume.drive, INFO_FILE)) is not None
        return False

    def read_fail_txt(self, volume: Volume) -> str | None:
        if volume.path:
            candidate = os.path.join(volume.path, FAIL_FILE)
            try:
                if os.path.exists(candidate):
                    with open(candidate, encoding="utf-8", errors="replace") as handle:
                        return handle.read()
            except OSError:
                pass
        if volume.drive:
            return self._read_windows_file(windows_join(volume.drive, FAIL_FILE))
        return None

    def copy(self, source: str, volume: Volume) -> tuple[bool, str]:
        """Copy `source` to the root of `volume`.  Returns (disconnected, note)."""
        if volume.path and os.path.isdir(volume.path):
            return self._copy_posix(source, volume.path)
        if volume.drive:
            return self._copy_powershell(source, volume.drive)
        raise RuntimeError(f"no way to write to {volume.key}")

    def _copy_posix(self, source: str, root: str) -> tuple[bool, str]:
        destination = os.path.join(root, os.path.basename(source))
        try:
            shutil.copy(source, destination)
            try:
                # Best effort: the bootloader usually reboots before this lands.
                with open(destination, "rb") as handle:
                    os.fsync(handle.fileno())
            except OSError:
                pass
        except OSError as exc:
            return True, f"write interrupted ({exc.strerror or exc})"
        return False, ""

    def _copy_powershell(self, source: str, drive: str) -> tuple[bool, str]:
        win_source = wsl_to_windows_path(os.path.abspath(source))
        if not win_source:
            raise RuntimeError("wslpath -w failed; cannot hand the file to Windows")
        script = (f"Copy-Item -LiteralPath {_ps_quote(win_source)} "
                  f"-Destination {_ps_quote(windows_root(drive))} -Force")
        rc, _out, err = run_powershell(script, timeout=180.0)
        if rc != 0:
            return True, f"Copy-Item exited {rc}: {err.strip() or '(no message)'}"
        return False, ""

    # ---- the console port (--enter) --------------------------------------
    def find_console_port(self, com: str | None = None,
                          port: str | None = None) -> tuple[str | None, str | None]:
        """Locate the ZMK CDC console port.  Returns (port, problem).

        `com` is --com COMn (driven through PowerShell), `port` is --port
        /dev/... (driven through termios, which is also the right answer on
        WSL2 when the device was attached with usbipd).  With neither, the
        auto-detection is capture.py's, so --enter and a running capture agree
        on which of several CDC ports belongs to ZMK (USB VID 1D50 on Windows,
        a by-id name containing "ZMK" on Linux).
        """
        if port:
            return port, None
        if com:
            capture = capture_module()
            return (capture.normalize_com(com) if capture else com), None
        capture = capture_module()
        if capture is None:
            return None, "cannot import scripts/log/capture.py for port auto-detection"
        if self.system == "wsl":
            try:
                ports = capture.list_windows_com_ports()
            except capture.PowerShellError as exc:
                return None, f"cannot list Windows COM ports: {exc}"
            candidates = capture.pick_com_ports(ports)
            if not candidates:
                return None, ("no Windows COM port found; plug the left half in and check "
                              "`python3 scripts/log/capture.py --list`")
            if len(candidates) > 1:
                names = ", ".join(port.com for port in candidates)
                return None, f"several ZMK COM ports ({names}); pass --com"
            return candidates[0].com, None
        by_id, acm = capture.list_candidate_ports()
        candidates = capture.pick_ports(by_id, acm)
        if not candidates:
            return None, "no /dev/serial/by-id or /dev/ttyACM* port found; pass --port"
        if len(candidates) > 1:
            return None, "several serial ports (%s); pass --port" % ", ".join(candidates)
        return candidates[0], None

    def touch_port(self, port: str, baud: int,
                   dwell: float = DEFAULT_TOUCH_DWELL) -> tuple[bool, str]:
        """Open `port` at `baud`, assert DTR, wait `dwell`, close.  (ok, note).

        A "COMn" name is driven through PowerShell (WSL2 without usbipd); a
        device path goes through termios, which is also what --port means on
        WSL2 when usbipd attached the device to Linux.
        """
        if is_com_name(port):
            return self._touch_powershell(port, baud, dwell)
        return self._touch_termios(port, baud, dwell)

    def _touch_powershell(self, com: str, baud: int, dwell: float) -> tuple[bool, str]:
        script = powershell_touch_script(com, baud, dwell)
        rc, out, err = run_powershell(script, timeout=max(30.0, dwell + 30.0))
        if rc != 0:
            message = (err.strip() or out.strip() or "(no message)")
            if "denied" in message.lower():
                message += ("\n       another program holds the port open; stop "
                            "scripts/log/capture.py (or let this script signal it, "
                            "see --no-release) and retry")
            return False, f"PowerShell exited {rc}: {message}"
        return True, out.strip()

    def _touch_termios(self, port: str, baud: int, dwell: float) -> tuple[bool, str]:
        if termios is None:
            return False, "termios is unavailable on this platform"
        speed = termios_touch_speed(baud)
        if speed is None:
            return False, f"termios has no B{baud} constant on this platform"
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            return False, f"cannot open {port}: {exc.strerror or exc}"
        try:
            attrs = termios.tcgetattr(fd)
            # ispeed/ospeed are what become dwDTERate in the CDC SET_LINE_CODING
            # request the kernel sends; the rest of the line coding is left as
            # it is, because the firmware only looks at the rate.
            attrs[4] = speed
            attrs[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            if fcntl is not None and struct is not None:
                try:
                    fcntl.ioctl(fd, termios.TIOCMBIS,
                                struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS))
                except OSError:
                    pass
            time.sleep(dwell)
        except OSError as exc:
            os.close(fd)
            return False, f"cannot set {baud} baud on {port}: {exc.strerror or exc}"
        try:
            os.close(fd)
        except OSError:
            pass
        return True, f"touched {port} @ {baud}"


# --------------------------------------------------------------------------
# the flow
# --------------------------------------------------------------------------
def print_volume(volume: Volume, prefix: str = "found") -> None:
    print(f"{prefix}: {volume.describe()}")
    for line in describe_info(volume.info):
        print(f"       {line}")


def wait_for_volumes(host: Host, timeout: float, interval: float = 0.5) -> list[Volume]:
    """Poll until at least one UF2 volume is present, or `timeout` runs out."""
    volumes = host.find_volumes()
    if volumes:
        return volumes
    print("No UF2 bootloader volume is mounted.")
    print("  Put the board into its bootloader: double-tap the reset button, or")
    print("  hold the left inner thumb key (Fn3) and tap T for the left half /")
    print("  Y for the right half (the `&bootloader` keys).")
    print(f"  Waiting up to {timeout:g}s ...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        volumes = host.find_volumes()
        if volumes:
            return volumes
    return []


def wait_until_gone(host: Host, volume: Volume, settle: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        if not host.volume_present(volume):
            return True
        time.sleep(interval)
    return not host.volume_present(volume)


def do_list(host: Host) -> int:
    volumes = host.find_volumes()
    print(f"platform: {host.system}")
    if not volumes:
        print("UF2 volumes: (none)")
        return 0
    print(f"UF2 volumes: {len(volumes)}")
    for volume in volumes:
        print_volume(volume, prefix="  -")
    return 0


def do_flash(host: Host, args: argparse.Namespace) -> int:
    source = args.file
    if not os.path.isfile(source):
        print(f"error: no such file: {source}", file=sys.stderr)
        return 2
    with open(source, "rb") as handle:
        head = handle.read(4)
    if not looks_like_uf2(head):
        print(f"error: {source} does not start with the UF2 magic {UF2_MAGIC!r}; "
              "this is not a UF2 firmware file", file=sys.stderr)
        return 2
    warning = label_mismatch(source, args.label)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)

    volumes = wait_for_volumes(host, args.timeout)
    if not volumes:
        print(f"error: no UF2 volume appeared within {args.timeout:g}s", file=sys.stderr)
        return 3
    volume, problem = select_volume(volumes, args.volume)
    if volume is None:
        print(f"error: {problem}", file=sys.stderr)
        return 4
    print_volume(volume, prefix="target")
    if args.label:
        print(f"       (flashing the {args.label} half)")

    size = os.path.getsize(source)
    if args.dry_run:
        where = volume.path or windows_root(volume.drive or "")
        print(f"dry run: would copy {source} ({size} bytes) to {where}")
        print("dry run: nothing was written")
        return 0

    print(f"copying {source} ({size} bytes) ...", flush=True)
    try:
        disconnected, note = host.copy(source, volume)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if disconnected:
        # Expected on Windows: the bootloader reboots as the last block lands
        # and the drive disappears under the copy.  Only the volume check
        # below can tell that apart from a real failure.
        print(f"note: {note}")
        print("note: this is normal when the bootloader reboots mid-copy; checking ...")

    print("waiting for the bootloader volume to disappear ...", flush=True)
    if wait_until_gone(host, volume, args.settle):
        print(f"ok: {os.path.basename(source)} written; {volume.key} rebooted into the new firmware")
        return 0

    print(f"error: {volume.key} is still mounted after {args.settle:g}s - the copy may have failed",
          file=sys.stderr)
    fail = host.read_fail_txt(volume)
    if fail:
        print(f"{FAIL_FILE} on the drive says:", file=sys.stderr)
        for line in fail.strip().splitlines():
            print("  " + line, file=sys.stderr)
    else:
        print(f"no {FAIL_FILE} on the drive; re-enter the bootloader and try again",
              file=sys.stderr)
    return 1


def do_enter(host: Host, args: argparse.Namespace) -> int:
    """`--enter left|right|reset-left`: the 1200-bps-touch, then maybe a flash."""
    target = args.enter
    baud = enter_baud(target)

    if args.release:
        release_captures(args.log_dir, pause=args.release_pause)

    port, problem = host.find_console_port(args.com, args.port)
    if port is None:
        print(f"error: {problem}", file=sys.stderr)
        return 5
    what = {
        "left": "the left half into its UF2 bootloader",
        "right": "the right half into its UF2 bootloader (over the split link)",
        "reset-left": "the left half through a plain reset",
    }[target]
    print(f"entering: {port} @ {baud} baud -> {what}")

    if args.dry_run:
        if is_com_name(port):
            print("dry run: would run powershell.exe -Command "
                  f"{powershell_touch_script(port, baud, args.dwell)}")
        else:
            print(f"dry run: would open {port}, set ospeed/ispeed to B{baud}, "
                  f"wait {args.dwell:g}s and close")
        print("dry run: nothing was sent")
        return 0

    ok, note = host.touch_port(port, baud, args.dwell)
    if not ok:
        print(f"error: {note}", file=sys.stderr)
        return 5
    if note and args.verbose:
        print(f"note: {note}")

    if target == "reset-left":
        print("ok: reset requested; the left half reboots into the same firmware")
        if args.file:
            print("note: --enter reset-left does not enter a bootloader, "
                  "so the file argument is ignored", file=sys.stderr)
        return 0

    # From here on it is the ordinary flow: the half is rebooting into its UF2
    # bootloader and the drive is about to appear.
    if not args.file:
        return do_wait_only(host, args)
    if not args.label:
        args.label = ENTER_FLASHABLE[target]
    return do_flash(host, args)


def do_wait_only(host: Host, args: argparse.Namespace) -> int:
    volumes = wait_for_volumes(host, args.timeout)
    if not volumes:
        print(f"error: no UF2 volume appeared within {args.timeout:g}s", file=sys.stderr)
        return 3
    for volume in volumes:
        print_volume(volume)
    if len(volumes) > 1:
        print("note: more than one UF2 volume; a flash would need --volume", file=sys.stderr)
    return 0


def normalize_argv(argv: list[str]) -> list[str]:
    """Accept `flash.py enter left ...` as a spelling of `--enter left ...`.

    A sub-command reads better than a flag for the one action that does not
    write anything ("enter the bootloader"), but flash.py's only positional is
    the .uf2 file, so the word is rewritten here instead of turning the whole
    CLI into subparsers (which would change every existing invocation).
    """
    if argv and argv[0] == "enter":
        if len(argv) > 1 and argv[1] in ENTER_TARGETS:
            return ["--enter", argv[1]] + list(argv[2:])
        return ["--enter"] + list(argv[1:])
    return list(argv)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scripts/flash.py",
        description=__doc__.split("\n\n")[0],
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("file", nargs="?", help="the .uf2 file to copy (omit with --list/--wait-only)")
    p.add_argument("--label", choices=LABELS,
                   help="which half this file is for; only used for messages and a filename sanity check")
    p.add_argument("--volume",
                   help="drive letter, mount path or label of the UF2 volume to write "
                        "(required when several are present)")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="seconds to wait for a UF2 volume to appear (default 120)")
    p.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                   help="seconds to wait for the volume to disappear after the copy (default 30)")
    p.add_argument("--enter", choices=ENTER_TARGETS, metavar="{%s}" % "|".join(ENTER_TARGETS),
                   help="put a half into its bootloader without touching the keyboard, by "
                        "opening the left half's CDC console at a magic baud rate "
                        "(left=1200, right=2400, reset-left=4800); needs a firmware built "
                        "with CONFIG_CORNIX_REMOTE_BOOT=y")
    p.add_argument("--com", metavar="COMn",
                   help="WSL2: the Windows COM port of the left half's ZMK console "
                        "(default: auto-detect by USB VID 1D50, like scripts/log/capture.py)")
    p.add_argument("--port", metavar="DEV",
                   help="Linux/macOS: the serial device of the left half's ZMK console "
                        "(default: auto-detect)")
    p.add_argument("--dwell", type=float, default=DEFAULT_TOUCH_DWELL,
                   help=f"seconds to hold the console port open at the magic baud rate "
                        f"(default {DEFAULT_TOUCH_DWELL:g})")
    p.add_argument("--log-dir", default=os.path.normpath(DEFAULT_LOG_DIR),
                   help="where to look for capture-<label>.pid (default: <repo>/logs)")
    p.add_argument("--no-release", dest="release", action="store_false",
                   help="do not signal a running scripts/log/capture.py to free the console "
                        "port before --enter (it holds the only handle Windows gives out)")
    p.add_argument("--release-pause", type=float, default=2.0,
                   help="seconds to wait after signalling the capture before opening the "
                        "port (default 2)")
    p.add_argument("--list", action="store_true", help="list UF2 volumes and exit")
    p.add_argument("--wait-only", action="store_true",
                   help="wait for a UF2 volume, print what it is, and exit without writing")
    p.add_argument("--dry-run", action="store_true",
                   help="do everything except the copy")
    p.add_argument("--verbose", action="store_true", help="report failing helper commands")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalize_argv(sys.argv[1:] if argv is None else argv))
    host = Host(detect_platform(), verbose=args.verbose)
    if host.system not in ("wsl", "linux", "macos"):
        print(f"error: unsupported platform {platform.system()!r}; "
              "copy the .uf2 to the bootloader drive by hand", file=sys.stderr)
        return 2
    if args.list:
        return do_list(host)
    if args.enter:
        return do_enter(host, args)
    if args.wait_only:
        return do_wait_only(host, args)
    if not args.file:
        parser.error("a .uf2 file is required (or use --enter / --list / --wait-only)")
    return do_flash(host, args)


if __name__ == "__main__":
    sys.exit(main())
