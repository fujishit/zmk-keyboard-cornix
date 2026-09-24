#!/usr/bin/env python3
"""Bluetooth connection / pairing diagnostics for ZMK debug logs (stdlib only).

Reads one or more log files (capture.py output, tio, PuTTY, ...) and reports:
  * firmware identity: boot banners, "Welcome to ZMK!", BT identity address,
    HW platform / HCI version lines
  * reboots: boot banners + backwards jumps of the device clock
  * Zephyr fatal errors / asserts / stack overflows
  * connection timeline: connected / disconnected (HCI reason decoded),
    failed-to-connect, security changed / failed (bt_security_err decoded)
  * pairing: ZMK auth callbacks and Zephyr bt_smp messages (SMP reason decoded)
  * bonds / keys / settings: profile addresses loaded from settings, bt_keys
    messages, settings keys loaded ("set-value OK" needs SETTINGS at DBG),
    settings failures
  * hints: rule-based interpretation of the above

All message strings were checked against ZMK main and the Zephyr revision it
pins (v4.1.0+zmk-fixes); the source of each pattern is noted next to it.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict

from analyze_latency import (  # noqa: E402  (same directory)
    Line, Lines, add_log_arguments, doc_parser, emit_json, parse_file, paths_from_args,
)

# --------------------------------------------------------------------------
# decoders (Bluetooth Core spec / Zephyr enums)
# --------------------------------------------------------------------------
HCI_REASONS = {
    0x05: "authentication failure",
    0x06: "PIN or key missing",
    0x08: "connection timeout (supervision timeout: peer out of range, reset, or lost sync)",
    0x0E: "connection rejected due to security reasons",
    0x13: "remote user terminated connection",
    0x14: "remote device terminated: low resources",
    0x15: "remote device terminated: power off",
    0x16: "connection terminated by local host",
    0x1F: "unspecified error",
    0x22: "LMP/LL response timeout",
    0x28: "instant passed",
    0x3B: "unacceptable connection parameters",
    0x3D: "connection terminated due to MIC failure (encryption key mismatch)",
    0x3E: "connection failed to be established / synchronization timeout",
}
# zephyr/include/zephyr/bluetooth/conn.h enum bt_security_err
SECURITY_ERRS = {
    0: "success",
    1: "authentication failure",
    2: "PIN or key missing (one side lost the bond the other side still has)",
    3: "OOB data not available",
    4: "authentication requirements not met",
    5: "pairing not supported",
    6: "pairing not allowed",
    7: "invalid parameters",
    8: "key rejected",
    9: "unspecified",
}
# SMP Pairing Failed reason codes (Core spec Vol 3 Part H 3.5.5)
SMP_REASONS = {
    0x01: "passkey entry failed", 0x02: "OOB not available", 0x03: "authentication requirements",
    0x04: "confirm value failed", 0x05: "pairing not supported", 0x06: "encryption key size",
    0x07: "command not supported", 0x08: "unspecified reason", 0x09: "repeated attempts",
    0x0A: "invalid parameters", 0x0B: "DHKey check failed", 0x0C: "numeric comparison failed",
    0x0D: "BR/EDR pairing in progress", 0x0E: "cross-transport key derivation not allowed",
    0x0F: "key rejected",
}

ADDR = r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}(?: \([a-z\-]+\))?)"

# (category, name, regex, converter). Applied with re.search on Line.text.
PATTERNS: list[tuple[str, str, re.Pattern, object]] = [
    # --- boot / identity -----------------------------------------------------
    # zephyr/kernel/banner.c: "*** <CONFIG_BOOT_BANNER_STRING> <version> ***"
    ("boot", "banner", re.compile(r"\*\*\* .*?(Booting Zephyr OS build|Zephyr OS build)\s*(\S*)"),
     lambda m: {"version": m.group(2).rstrip("*").strip()}),
    # zmk/app/src/main.c
    ("boot", "welcome", re.compile(r"Welcome to ZMK!"), lambda m: {}),
    # zephyr/subsys/bluetooth/host/hci_core.c bt_dev_show_info() (INF)
    ("boot", "identity", re.compile(r"Identity(?:\[\d+\])?: " + ADDR), lambda m: {"addr": m.group(1)}),
    ("boot", "hw", re.compile(r"HW Platform: (.*)"), lambda m: {"info": m.group(1)}),
    ("boot", "hci_version", re.compile(r"HCI: version (.*)"), lambda m: {"info": m.group(1)}),
    # zmk/app/src/endpoints.c (INF)
    ("boot", "endpoint", re.compile(r"Endpoint changed: (.*)"), lambda m: {"endpoint": m.group(1)}),
    # --- fatal ---------------------------------------------------------------
    # zephyr/kernel/fatal.c: ">>> ZEPHYR FATAL ERROR %d: %s on CPU %d", "Halting system"
    ("fatal", "fatal", re.compile(r"ZEPHYR FATAL ERROR (\d+): (.*?)(?: on CPU \d+)?$"),
     lambda m: {"code": int(m.group(1)), "reason": m.group(2)}),
    ("fatal", "halt", re.compile(r"Halting system"), lambda m: {}),
    # zephyr/include/zephyr/sys/__assert.h
    ("fatal", "assert", re.compile(r"ASSERTION FAIL.*"), lambda m: {"text": m.group(0)}),
    # zephyr/arch/arm/core/cortex_m/fault.c
    ("fatal", "fault", re.compile(r"\*\*\*\*\* (MPU|BUS|USAGE|HARD) FAULT \*\*\*\*\*|Stack overflow|Data Access Violation|Faulting instruction address"),
     lambda m: {"text": m.group(0)}),
    # --- connections (ZMK) ---------------------------------------------------
    # zmk/app/src/ble.c connected(): "Connected %s" (host link, central side)
    # zmk/app/src/split/bluetooth/central.c split_central_connected(): "Connected: %s" (split link)
    ("conn", "connected", re.compile(r"\bConnected(:)? " + ADDR), lambda m: {"addr": m.group(2), "split": bool(m.group(1))}),
    # zmk/app/src/ble.c disconnected() and zmk/app/src/split/bluetooth/peripheral.c disconnected():
    #   "Disconnected from %s (reason 0x%02x)"
    ("conn", "disconnected", re.compile(r"Disconnected from " + ADDR + r" \(reason 0x([0-9A-Fa-f]+)\)"),
     lambda m: {"addr": m.group(1), "reason": int(m.group(2), 16), "split": False}),
    # zmk/app/src/split/bluetooth/central.c split_central_disconnected(): "Disconnected: %s (reason %d)"
    ("conn", "disconnected", re.compile(r"Disconnected: " + ADDR + r" \(reason (\d+)\)"),
     lambda m: {"addr": m.group(1), "reason": int(m.group(2)), "split": True}),
    # zmk/app/src/ble.c connected() (WRN) / split central.c split_central_connected() (ERR)
    ("conn", "connect_failed", re.compile(r"Failed to connect to " + ADDR + r" \((\d+)\)"),
     lambda m: {"addr": m.group(1), "reason": int(m.group(2))}),
    # zmk/app/src/ble.c + split/bluetooth/peripheral.c security_changed()
    ("conn", "security_changed", re.compile(r"Security changed: " + ADDR + r" level (\d+)"),
     lambda m: {"addr": m.group(1), "level": int(m.group(2))}),
    ("conn", "security_failed", re.compile(r"Security failed: " + ADDR + r" level (\d+) err (-?\d+)"),
     lambda m: {"addr": m.group(1), "level": int(m.group(2)), "err": int(m.group(3))}),
    # zephyr/subsys/bluetooth/host/conn.c
    ("conn", "rf_noise", re.compile(r"failed to establish\. RF noise\?"), lambda m: {}),
    ("conn", "fatal_disconnect", re.compile(r"Fatal error \((-?\d+)\)\. Disconnecting"), lambda m: {"err": int(m.group(1))}),
    # zmk/app/src/ble.c: "Active profile connected" / "Active profile disconnected" / "profile %d"
    ("conn", "active_profile", re.compile(r"Active profile (connected|disconnected)"), lambda m: {"state": m.group(1)}),
    ("conn", "advertising", re.compile(r"advertising from (\d+) to (\d+)"), lambda m: {"from": int(m.group(1)), "to": int(m.group(2))}),
    # --- pairing -------------------------------------------------------------
    # zmk/app/src/ble.c auth callbacks
    ("pairing", "passkey", re.compile(r"Passkey (for|entry requested for) " + ADDR), lambda m: {"addr": m.group(2)}),
    ("pairing", "cancelled", re.compile(r"Pairing cancelled: " + ADDR), lambda m: {"addr": m.group(1)}),
    ("pairing", "rejected_taken_profile", re.compile(r"Rejecting pairing request to taken profile (\d+)"),
     lambda m: {"profile": int(m.group(1))}),
    ("pairing", "complete_profile_not_open", re.compile(r"Pairing completed but current profile is not open: " + ADDR),
     lambda m: {"addr": m.group(1)}),
    ("pairing", "accept", re.compile(r"role (\d+), open\? (yes|no)"), lambda m: {"role": int(m.group(1)), "open": m.group(2) == "yes"}),
    # zephyr/subsys/bluetooth/host/smp.c
    ("pairing", "smp_failed", re.compile(r"pairing failed \(peer reason 0x([0-9A-Fa-f]+)\)"),
     lambda m: {"reason": int(m.group(1), 16)}),
    ("pairing", "smp_refused_old_bond", re.compile(r"Refusing new pairing\. The old bond (must be unpaired first|has more trust)"),
     lambda m: {"why": m.group(1)}),
    ("pairing", "smp_timeout", re.compile(r"SMP Timeout"), lambda m: {}),
    ("pairing", "smp_repairing", re.compile(r"(New|Unsupported) auth requirements: 0x([0-9A-Fa-f]+), repairing"),
     lambda m: {"auth": int(m.group(2), 16)}),
    ("pairing", "smp_justworks_refused", re.compile(r"JustWorks failed, authenticated keys present"), lambda m: {}),
    ("pairing", "smp_no_keys", re.compile(r"(Unable to get keys for|No keys space for) " + ADDR),
     lambda m: {"addr": m.group(2), "what": m.group(1)}),
    # --- bonds / keys / settings --------------------------------------------
    # zmk/app/src/ble.c ble_profiles_handle_set(): "Loaded %s address for profile %d"
    ("bonds", "profile_loaded", re.compile(r"Loaded " + ADDR + r" address for profile (\d+)"),
     lambda m: {"addr": m.group(1), "profile": int(m.group(2))}),
    ("bonds", "setting_ble", re.compile(r"Setting BLE value (\S+)"), lambda m: {"key": m.group(1)}),
    ("bonds", "profile_addr_set", re.compile(r"Setting profile addr for (\S+) to " + ADDR),
     lambda m: {"key": m.group(1), "addr": m.group(2)}),
    ("bonds", "peripheral_slot", re.compile(r"(Found existing peripheral address in slot|Storing peripheral " + ADDR + r" in slot) (\d+)"),
     lambda m: {"slot": int(m.group(3)), "addr": m.group(2)}),
    ("bonds", "bonds_cleared", re.compile(r"Clearing all existing BLE bond information"), lambda m: {}),
    ("bonds", "zmk_settings_error", re.compile(r"Failed to handle (profile address|active profile|peripheral address) from settings \(err (-?\d+)\)"),
     lambda m: {"what": m.group(1), "err": int(m.group(2))}),
    # zephyr/subsys/bluetooth/host/keys.c
    ("bonds", "keys_stored", re.compile(r"Stored keys for " + ADDR), lambda m: {"addr": m.group(1)}),
    ("bonds", "keys_restored", re.compile(r"Successfully restored keys for " + ADDR), lambda m: {"addr": m.group(1)}),
    ("bonds", "keys_cleared", re.compile(r"Cleared keys for " + ADDR), lambda m: {"addr": m.group(1)}),
    ("bonds", "keys_error", re.compile(r"Failed to save keys \(err (-?\d+)\)|Unable to find deleted keys for|Keys for .* have no aging counter|Invalid key length|unable to create keys for|Failed to allocate keys for"),
     lambda m: {"text": m.group(0)}),
    # zephyr/subsys/bluetooth/host/settings.c
    ("bonds", "bt_settings_error", re.compile(r"Failed to read (ID address|device name|IRK) from storage|Unable to setup an identity address|settings_subsys_init failed \(err (-?\d+)\)|Ignoring identities stored in flash|Invalid length (ID address|IRK) in storage"),
     lambda m: {"text": m.group(0)}),
    # zephyr/subsys/settings/src/settings.c settings_call_set_handler()
    ("bonds", "settings_key_loaded", re.compile(r"set-value OK\. key: (\S+)"), lambda m: {"key": m.group(1)}),
    ("bonds", "settings_key_failed", re.compile(r"set-value failure\. key: (\S+) error\((-?\d+)\)"),
     lambda m: {"key": m.group(1), "err": int(m.group(2))}),
]


class Finding:
    __slots__ = ("cat", "name", "line", "fields")

    def __init__(self, cat: str, name: str, line: Line, fields: dict) -> None:
        self.cat, self.name, self.line, self.fields = cat, name, line, fields

    def as_dict(self) -> dict:
        d = {"category": self.cat, "event": self.name, "line": self.line.no, "t_dev": self.line.t_dev,
             "level": self.line.level, "module": self.line.module, "text": self.line.text}
        d.update(self.fields)
        return d


def extract(lines: list[Line]) -> list[Finding]:
    out: list[Finding] = []
    for ln in lines:
        for cat, name, rx, conv in PATTERNS:
            m = rx.search(ln.text)
            if m:
                out.append(Finding(cat, name, ln, conv(m)))
                break
    return out


def count_clock_resets(lines: Lines) -> int:
    """Backwards jumps of the device clock, i.e. one less than the number of
    monotonic segments parse_file() already counted."""
    return lines.segments - 1


def decode(f: Finding) -> str:
    n, fl = f.name, f.fields
    if n == "disconnected":
        return "%s reason 0x%02x: %s" % ("split link" if fl["split"] else "host link", fl["reason"],
                                        HCI_REASONS.get(fl["reason"], "?"))
    if n == "connect_failed":
        return "status 0x%02x: %s" % (fl["reason"], HCI_REASONS.get(fl["reason"], "?"))
    if n == "security_failed":
        return "err %d: %s" % (fl["err"], SECURITY_ERRS.get(fl["err"], "?"))
    if n == "security_changed":
        return "level %d" % fl["level"]
    if n == "smp_failed":
        return "peer reason 0x%02x: %s" % (fl["reason"], SMP_REASONS.get(fl["reason"], "?"))
    if n == "connected":
        return "split link" if fl["split"] else "host link"
    return ""


def hints(findings: list[Finding], reboots: int) -> list[str]:
    names = Counter(f.name for f in findings)
    out: list[str] = []
    if reboots > 1:
        out.append("%d boots in this capture: the device resets. Deferred logging usually loses the fatal "
                   "error line itself (CONFIG_RESET_ON_FATAL_ERROR); count boots, then look at what "
                   "happened right before each one." % reboots)
    if names["fatal"] or names["assert"] or names["fault"]:
        out.append("Zephyr fatal error / fault / assertion present: this is a firmware crash, not a BLE problem.")
    sec_key_missing = [f for f in findings if f.name == "security_failed" and f.fields["err"] == 2]
    mic = [f for f in findings if f.name == "disconnected" and f.fields["reason"] in (0x3D, 0x05, 0x06)]
    if sec_key_missing or mic:
        out.append("Encryption fails right after connecting (security err 2 / reason 0x3d, 0x05, 0x06): the host still "
                   "has a bond that the keyboard no longer has (settings lost, layout change, settings_reset) "
                   "or vice versa. Forget the keyboard on the host AND clear the profile on the keyboard "
                   "(BT_CLR / settings_reset), then pair again. See README 'Bluetooth pairing failure'.")
    if names["smp_refused_old_bond"]:
        out.append("bt_smp refused a new pairing because the keyboard still holds an old bond for that host "
                   "address: clear it on the keyboard (BT_CLR on the active profile) before pairing again.")
    if names["smp_failed"]:
        reasons = Counter(f.fields["reason"] for f in findings if f.name == "smp_failed")
        out.append("SMP pairing failed from the peer: " + ", ".join(
            "0x%02x %s (%dx)" % (r, SMP_REASONS.get(r, "?"), c) for r, c in reasons.items()))
    timeouts = [f for f in findings if f.name == "disconnected" and f.fields["reason"] == 0x08]
    if timeouts:
        out.append("%d supervision timeout(s) (0x08): the peer stopped answering - the other side reset, went out "
                   "of range, or radio interference. If it is the split link, compare with the peripheral log "
                   "at the same host time." % len(timeouts))
    if names["connect_failed"] or names["rf_noise"]:
        out.append("Connection attempts that never established (0x3e / 'RF noise?'): the peer advertises but the "
                   "link cannot be set up - typically the other device resets during connection or the "
                   "bond keys mismatch at the link layer.")
    if names["bonds_cleared"]:
        out.append("'Clearing all existing BLE bond information': bonds were wiped at boot "
                   "(CONFIG_ZMK_BLE_CLEAR_BONDS_ON_START or settings_reset firmware). Every host must re-pair.")
    if names["settings_key_failed"] or names["keys_error"] or names["bt_settings_error"] or names["zmk_settings_error"]:
        out.append("Settings / key storage errors: check the build's .config has CONFIG_NVS=y and "
                   "CONFIG_SETTINGS_NVS=y and NOT CONFIG_SETTINGS_NONE=y, and that the storage partition "
                   "did not move between firmware versions (nrf52840-nosd layout).")
    if names["settings_key_loaded"] and not any(f.fields["key"].startswith("bt/keys") for f in findings if f.name == "settings_key_loaded"):
        out.append("Settings were loaded at boot but no 'bt/keys/...' entry was among them: the keyboard has no "
                   "stored bond, so any host that still remembers it will fail encryption.")
    if not names["identity"] and not names["welcome"]:
        out.append("No boot lines captured: start the capture before resetting the keyboard, or raise "
                   "CONFIG_LOG_PROCESS_THREAD_STARTUP_DELAY_MS, to see identity/settings load messages.")
    return out


def analyze(paths: list[tuple[str, str]]) -> dict:
    result: dict = {"files": []}
    for label, path in paths:
        lines = parse_file(path)
        findings = extract(lines)
        clock_resets = count_clock_resets(lines)
        banners = [f for f in findings if f.name == "banner"]
        welcomes = [f for f in findings if f.name == "welcome"]
        reboots = max(len(banners), len(welcomes), clock_resets + (1 if (banners or welcomes) else 0))
        by_addr: dict[str, Counter] = defaultdict(Counter)
        for f in findings:
            if f.cat == "conn" and "addr" in f.fields:
                key = f.name
                if f.name == "disconnected":
                    key = "disconnected 0x%02x" % f.fields["reason"]
                elif f.name == "security_failed":
                    key = "security_failed err %d" % f.fields["err"]
                by_addr[f.fields["addr"]][key] += 1
        settings_keys = [f.fields["key"] for f in findings if f.name == "settings_key_loaded"]
        bt_key_entries = sorted({k for k in settings_keys if k.startswith("bt/keys")})
        profiles = {f.fields["profile"]: f.fields["addr"] for f in findings if f.name == "profile_loaded"}
        restored = sorted({f.fields["addr"] for f in findings if f.name == "keys_restored"})
        result["files"].append({
            "path": path, "label": label, "lines": len(lines),
            "boot": {
                "banners": [f.as_dict() for f in banners],
                "welcome_count": len(welcomes),
                "clock_resets": clock_resets,
                "reboots": reboots,
                "identity": [f.fields["addr"] for f in findings if f.name == "identity"],
                "info": [f.line.text for f in findings if f.name in ("hw", "hci_version", "endpoint")],
            },
            "fatal": [f.as_dict() for f in findings if f.cat == "fatal"],
            "connections": [dict(f.as_dict(), decoded=decode(f)) for f in findings if f.cat == "conn"],
            "per_address": {a: dict(c) for a, c in by_addr.items()},
            "pairing": [dict(f.as_dict(), decoded=decode(f)) for f in findings if f.cat == "pairing"],
            "bonds": {
                "profiles_from_settings": profiles,
                "keys_restored": restored,
                "bond_count_from_settings_keys": len(bt_key_entries),
                "settings_bt_key_entries": bt_key_entries,
                "settings_keys_loaded": len(settings_keys),
                "events": [f.as_dict() for f in findings if f.cat == "bonds" and f.name != "settings_key_loaded"],
            },
            "hints": hints(findings, reboots),
            "finding_count": len(findings),
        })
    return result


def _t(x: float | None) -> str:
    return "%.3f" % x if x is not None else "   -   "


def report_text(result: dict) -> str:
    out: list[str] = []
    for f in result["files"]:
        b = f["boot"]
        out.append("file: %s  label=%s  lines=%d  findings=%d" % (f["path"], f["label"], f["lines"], f["finding_count"]))
        out.append("  boot: reboots=%d (banners=%d, 'Welcome to ZMK!'=%d, device clock resets=%d)" % (
            b["reboots"], len(b["banners"]), b["welcome_count"], b["clock_resets"]))
        for bn in b["banners"]:
            out.append("    line %d t=%s %s" % (bn["line"], _t(bn["t_dev"]), bn["text"]))
        if b["identity"]:
            out.append("  BT identity: " + ", ".join(b["identity"]))
        for i in b["info"]:
            out.append("  " + i)
        out.append("  fatal errors: %d" % len(f["fatal"]))
        for x in f["fatal"]:
            out.append("    line %d t=%s <%s> %s" % (x["line"], _t(x["t_dev"]), x["level"], x["text"]))
        out.append("  connection timeline:")
        if not f["connections"]:
            out.append("    (no connect/disconnect/security lines; they need CONFIG_ZMK_LOG_LEVEL_DBG)")
        for c in f["connections"]:
            out.append("    line %6d t=%s %-18s %s %s" % (c["line"], _t(c["t_dev"]), c["event"], c.get("addr", ""), c["decoded"]))
        if f["per_address"]:
            out.append("  per address:")
            for a, cnt in f["per_address"].items():
                out.append("    %s: %s" % (a, ", ".join("%s=%d" % kv for kv in sorted(cnt.items()))))
        out.append("  pairing:")
        if not f["pairing"]:
            out.append("    (none)")
        for p in f["pairing"]:
            out.append("    line %6d t=%s <%s> %s: %s %s" % (p["line"], _t(p["t_dev"]), p["level"], p["module"], p["text"], p["decoded"]))
        bo = f["bonds"]
        out.append("  bonds / settings:")
        out.append("    ZMK profiles loaded from settings: %s" % (
            ", ".join("%d=%s" % kv for kv in sorted(bo["profiles_from_settings"].items())) or "(none logged)"))
        out.append("    bt/keys entries seen while loading settings: %d %s" % (
            bo["bond_count_from_settings_keys"], bo["settings_bt_key_entries"]))
        if bo["keys_restored"]:
            out.append("    bt_keys restored for: " + ", ".join(bo["keys_restored"]))
        out.append("    settings keys loaded: %d" % bo["settings_keys_loaded"])
        for e in bo["events"]:
            out.append("    line %6d t=%s <%s> %s: %s" % (e["line"], _t(e["t_dev"]), e["level"], e["module"], e["text"]))
        out.append("  hints:")
        if not f["hints"]:
            out.append("    (nothing suspicious found)")
        for h in f["hints"]:
            out.append("    - " + h)
        out.append("")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = doc_parser(__doc__)
    add_log_arguments(p)
    p.add_argument("--json", metavar="FILE", help="write the result as JSON ('-' for stdout)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # offset_files=False: unlike analyze_latency.py a lone extra file stays
    # "device" here even next to --peripheral/--central.
    paths = paths_from_args(args, offset_files=False)
    if not paths:
        build_parser().print_usage(sys.stderr)
        return 2
    result = analyze(paths)
    emit_json(args.json, result, report_text(result), note=False)
    if all(f["finding_count"] == 0 for f in result["files"]):
        print("no matching BLE events found - check that the debug snippet is enabled and that the capture "
              "covers a boot / connection attempt", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
