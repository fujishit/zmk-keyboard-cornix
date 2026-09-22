# Flashing Cornix firmware

A Cornix half in its UF2 bootloader appears as a small removable drive.
Flashing is nothing more than copying a `.uf2` file onto that drive: the
bootloader writes the blocks to flash and reboots into the new firmware, which
makes the drive disappear again. `scripts/flash.py` automates the fiddly parts
of that — finding the drive, showing you *which* half it is before you
overwrite it, and telling a successful write apart from a failed one.

```sh
python3 scripts/flash.py --list                                    # what is attached right now
python3 scripts/flash.py firmware/keymap/cornix_left.uf2  --label left
python3 scripts/flash.py firmware/keymap/cornix_right.uf2 --label right
python3 scripts/flash.py --wait-only                               # identify a half without writing
python3 scripts/flash.py firmware/keymap/cornix_left.uf2 --dry-run # everything except the copy
python3 scripts/flash.py --enter left                              # bootloader without the button
python3 scripts/flash.py --help
```

`--label` is a sanity check only: it goes into the messages and warns when you
point `--label left` at a file called `cornix_right.uf2`. What actually decides
which half gets written is the drive you connected, and the script prints the
`Model` / `Board-ID` / `Bootloader` lines from `INFO_UF2.TXT` so you can stop
before it is too late. Exit codes: `0` written, `1` copy failed, `2` usage or
bad file, `3` no bootloader drive appeared, `4` could not decide which drive
to write (several present without `--volume`, or a `--volume` that matched none
or several of them), `5` `--enter` could not touch the console port.

## Getting a half into the bootloader

There are three ways; the third needs neither a button nor a key, but only
works on a debug image (see [Remote bootloader entry](#remote-bootloader-entry-no-hands-on-the-keyboard)).

### Double-tap reset

Press the physical reset button twice within about half a second. The
bootloader takes over (its LED pattern replaces the normal indicator) and a
removable drive shows up on the host. This always works, including when the
firmware on the board is broken or missing, so it is the fallback for
everything below.

### No `&bootloader` key any more

The keymap briefly carried `&bootloader` keys on the Fn3 layer (positions 5
and 6). They were removed on 2026-09-17 at the user's request: entering the
bootloader always needs a hand on the keyboard anyway, so the double-tap reset
is the only supported way. `scripts/flash.py` still mentions the key in its
waiting message only as an option for keymaps that define one.

## Remote bootloader entry (no hands on the keyboard)

Debug images — anything built with the `cornix-debug-log` or
`cornix-remote-boot` snippet, i.e. `CONFIG_CORNIX_REMOTE_BOOT=y` — can be put
into their bootloader from the host, over the USB CDC console the debug log
already uses. This is the classic **"1200 bps touch"**: the host opens the
console port, sets the line-coding baud rate to a magic value and closes it
again. No bytes are sent; the baud rate *is* the message.

```sh
python3 scripts/flash.py --enter left           # left half -> its UF2 bootloader
python3 scripts/flash.py enter right            # right half -> its UF2 bootloader
python3 scripts/flash.py --enter reset-left     # plain reset of the left half
python3 scripts/flash.py --enter left firmware/led/cornix_left_remote_debug.uf2
```

| rate | what happens | log line |
| --- | --- | --- |
| 1200 | the half that owns the console (the left one) enters its UF2 bootloader | `remote boot: 1200 bps -> entering UF2 bootloader` |
| 2400 | the left half asks split peripheral 0 (the right half) to do the same | `remote boot: 2400 bps -> peripheral 0 bootloader` |
| 4800 | `sys_reboot(SYS_REBOOT_COLD)` of the left half — no bootloader | `remote boot: 4800 bps -> reset` |

Every other rate is ignored, so opening the port with a terminal program at
115200 cannot reboot the keyboard by accident. At boot the firmware logs
`remote boot: armed on CDC_ACM_0 (...)`, which is how you check an image has
the feature at all.

`--enter left <file.uf2>` is the whole flashing cycle in one command: touch,
wait for the bootloader drive, copy, wait for the reboot. Without a file it
stops after identifying the drive (like `--wait-only`). `--enter reset-left`
never produces a drive, so a file argument is ignored there.

### How the right half can do this without a USB port

The right half is a plain BLE peripheral: no USB stack, no console, no
firmware change. It does however run behaviours the central sends it — that is
how pressing an `&bootloader` key wired to a *right-hand* position has always
worked. ZMK's reset behaviour declares `BEHAVIOR_LOCALITY_EVENT_SOURCE`, so the
central forwards it by **name** over the split link
(`zmk_split_central_invoke_behavior`), and the peripheral looks the name up and
runs it. The name is `bootload` — eight characters, because the split payload's
name field is fixed-size — and it is compiled into every ZMK build
(`app/dts/behaviors/reset.dtsi`). So the stock right-half firmware obeys, as
long as the two halves are connected.

### Choosing the port

The console port is auto-detected exactly the way `scripts/log/capture.py`
does it: on WSL2 the Windows COM port whose USB VID is `1D50`, elsewhere a
`/dev/serial/by-id/*ZMK*` entry or `/dev/ttyACM*`. With more than one
candidate you have to name it:

```sh
python3 scripts/flash.py --enter left --com COM7          # WSL2 / Windows side
python3 scripts/flash.py --enter left --port /dev/ttyACM0 # Linux, macOS, usbipd
python3 scripts/log/capture.py --list                     # which port is which
```

A dongle-style build can expose two CDC ports (Studio RPC and the log); the
console is the one that produces log text, which is also the one `capture.py`
picks.

### It clashes with a running capture — and handles it

**Windows gives out exactly one handle per COM port.** While
`scripts/log/capture.py` is running it holds that handle, and a second open
fails with *access denied*; there is no way around it. So `--enter` asks the
capture to step aside first:

* `capture.py` now writes `logs/capture-<label>.pid` and installs a `SIGUSR1`
  handler that **closes the port and stays away for 15 s**
  (`--pause-seconds`), then lets its existing reconnect loop take it back —
  the same loop that already survives the device disappearing into its
  bootloader. The capture is *not* killed: the log file, its line numbering
  and the running process all stay, and the gap is recorded in the log as
  `--- capture: paused ... ---` / `--- capture: pause over ... ---`.
* `flash.py --enter` signals every `logs/capture-*.pid` it finds, waits
  `--release-pause` seconds (2 by default) and then opens the port.

```sh
python3 scripts/flash.py --enter left                     # signals the capture for you
python3 scripts/flash.py --enter left --no-release        # you stopped the capture yourself
python3 scripts/flash.py --enter left --log-dir /tmp/logs # pidfiles live elsewhere
kill -USR1 $(cat logs/capture-left.pid)                   # by hand
```

The pidfile is written by the capture process itself, so a capture started
with `--no-pidfile`, or one killed with `-9` (stale file, `ProcessLookupError`
— reported and skipped), cannot be asked politely. Then either stop the
capture or pass `--no-release` after doing so.

On Linux and macOS the conflict is milder (a second `open()` of the same tty
usually succeeds) but the same mechanism is used, because a capture reading
bytes while the port is being reconfigured is still confusing.

### Caveats

* **It is a debugging aid, not a feature.** Anything that can open the console
  port can put the keyboard into its bootloader. Keep
  `CONFIG_CORNIX_REMOTE_BOOT` on debug images only; the daily-driver build has
  no console port at all.
* **The peripheral must be connected.** `--enter right` only tells the *left*
  half to send a command. If no split transport is up at all the left half
  logs `remote boot: peripheral 0 did not accept ...`; if the transport is up
  but the right half is not currently connected, ZMK drops the command one
  layer deeper and only logs `Source not connected` (an `<err> zmk` line from
  `split/bluetooth/central.c`). Either way no drive appears and
  `scripts/flash.py` gives up after `--timeout`. Check the capture log.
* **Watch which half the drive belongs to.** After `--enter right` the drive
  that appears is the right half's — but only if the right half is the one
  plugged into USB, or if it has its own cable. A Cornix right half normally
  runs on battery, so plug it in before you ask it to flash.
* **The 4800 reset is not a settings reset.** It reboots into the same
  firmware; wiping settings still needs the `settings_reset` image (see
  below).
* **A half that is already in its bootloader has no console**, so `--enter`
  cannot reach it. Use `--wait-only` / a plain flash, or the reset button.

## Flashing the two halves

Each half has its own MCU, its own bootloader and its own firmware file. Flash
them one at a time — `scripts/flash.py` refuses to act (exit 4) if two UF2
drives are attached, because the two look identical and picking wrong bricks
the wrong half.

```sh
# left half: USB cable into the left half, double-tap reset
python3 scripts/flash.py firmware/keymap/cornix_left.uf2 --label left

# then the right half: move the cable, double-tap reset
python3 scripts/flash.py firmware/keymap/cornix_right.uf2 --label right
```

If you really do have both attached, name one:

```sh
python3 scripts/flash.py firmware/keymap/cornix_left.uf2 --volume E:          # WSL2 drive letter
python3 scripts/flash.py firmware/keymap/cornix_left.uf2 --volume /media/me/CORNIX
python3 scripts/flash.py firmware/keymap/cornix_left.uf2 --volume CORNIX      # volume label
```

## The reset → firmware two-step (settings_reset)

A settings-reset image is built from ZMK's `settings_reset` shield: it boots,
wipes the stored settings (BLE bonds, the last active output, profile
assignments) and then does nothing useful — no keymap, no split link. It is
never firmware you want to leave on a board, so clearing bonds is always
**two flashes in a row on the same half**:

```sh
# 1. wipe: put the half into the bootloader and flash the reset image
python3 scripts/flash.py firmware/debug/cornix_reset.uf2 --label right
#    the drive disappears and the board reboots into the reset image, which
#    wipes the settings.  Put it back into the bootloader with a double-tap
#    of the reset button - the reset image has no keymap, so there is no
#    `&bootloader` key on it to press.

# 2. restore: flash the real firmware onto the same half
python3 scripts/flash.py firmware/keymap/cornix_right.uf2 --label right
```

**The reset image is board-specific.** `build.yaml` builds `cornix_reset` from
`cornix_right//zmk` + `settings_reset`, and that is the image checked in as
`firmware/debug/cornix_reset.uf2`, so it matches the **right** half. There is
no `cornix_left` settings-reset target in `build.yaml` today; to wipe the left
half, build one the same way (`-DSHIELD=settings_reset` with
`-b cornix_left//zmk` and the `nrf52840-nosd` snippet) rather than flashing the
right half's image onto it.

Do this on **every** role whose bonds must be cleared — both halves when the
split link itself is broken, plus the dongle if you use one — and only then
pair with the host again. A half left holding only the reset image will not
type and will not join the split, so never stop after step 1.

## WSL2 specifics

WSL does not always auto-mount a removable drive that is plugged in after the
distribution started, and a bootloader drive appears *after* boot by
definition. `scripts/flash.py` therefore never relies on `/mnt/<letter>/`:

- it asks Windows for the drive list with
  `Get-CimInstance Win32_LogicalDisk` (`DriveType` 2 = removable) and
  `Get-Volume` (`DriveType` "Removable", plus the friendly label);
- it reads `INFO_UF2.TXT` through `/mnt/<letter>/` when that path exists, and
  through PowerShell `Get-Content -Raw` when it does not;
- it copies with `shutil.copy` when the drive is mounted, and otherwise with
  PowerShell `Copy-Item`, translating the source path with `wslpath -w` (which
  yields a `\\wsl.localhost\<distro>\...` UNC path — Windows can read those, so
  the firmware does not have to be staged on `C:` first).

All PowerShell calls set `[Console]::OutputEncoding` to UTF-8, without which
non-ASCII volume labels come back as mojibake.

You do **not** need `usbipd-win` to flash. `usbipd` is only for the USB serial
*log* port (see `scripts/log/README.md`); the bootloader is a mass-storage
device and Windows handles it.

If the Windows side is not reachable at all (no `powershell.exe` on `PATH`),
copy the file by hand from a Windows Explorer window, or from PowerShell:

```powershell
Copy-Item \\wsl.localhost\Debian\home\me\dev\zmk-keyboard-cornix\firmware\keymap\cornix_left.uf2 E:\
```

## Linux and macOS

Nothing special: the desktop mounts the drive and `scripts/flash.py` finds it
under `/media/<user>/<label>/`, `/run/media/<user>/<label>/`, `/media/<label>/`
or `/Volumes/<label>/`. The script looks for `INFO_UF2.TXT` at the root, not
for a particular volume name, so a renamed drive still works.

## When something goes wrong

| symptom | what it means |
| --- | --- |
| `no UF2 volume appeared within 120s` | the board never entered the bootloader: double-tap faster, or the cable is charge-only |
| `write interrupted (Input/output error)` followed by `ok:` | normal — the bootloader rebooted as the last block landed. The script only calls it a failure if the drive is *still there* afterwards |
| `... is still mounted after 30s - the copy may have failed` | the write did not take. `FAIL.TXT` on the drive says why and the script prints it |
| `FAIL.TXT: file contains an address outside of the allowed range` | wrong image for this flash layout — see [bootloader/README.md](../bootloader/README.md); Cornix has used the no-SoftDevice layout since v2.3 and older images need the SoftDevice restored first |
| drive reappears immediately after flashing | the new firmware crashed at boot and the bootloader took over again; flash a known-good image |
| half flashes fine but does not type | the other half or the split pairing, not the flash — clear the bonds with the reset two-step above |

## Building the files you flash

`firmware/` is git-ignored: the release images come from the GitHub Actions
build (`build.yaml`), and `firmware/keymap/*.uf2` is where a local build is
staged. To produce them locally (ARM toolchain in `.sim/sdk`, installed once
as described in [scripts/log/README.md](log/README.md)):

```sh
cd .sim/ws && source ../venv/bin/activate
export PATH=$PWD/../tools/bin:$PATH
export ZEPHYR_SDK_INSTALL_DIR=$PWD/../sdk/zephyr-sdk-0.17.0 ZEPHYR_TOOLCHAIN_VARIANT=zephyr
west build -s zmk/app -d ../../.build/keymap/cornix_left -b cornix_left//zmk -p \
    -- -DZMK_CONFIG=$PWD/../../config -DZMK_EXTRA_MODULES=$PWD/../..
cp ../../.build/keymap/cornix_left/zephyr/zmk.uf2 ../../firmware/keymap/cornix_left.uf2
```

(same with `cornix_right`). Run `just check` and `just sim-test` first — see
[TESTING.md](../TESTING.md); nothing in this repo can verify a keymap *after*
it is on the keyboard.
