# Firmware: patched images, flasher and monitor

> [!WARNING]
> **Use at your own risk.** Flashing modified firmware may void your warranty. A failed or interrupted
> flash usually leaves the mic unusable until it is recovered (see [Recovery](#recovery)), but there is a
> small yet real chance that something goes wrong in a way the recovery can't fix, and the mic is
> permanently bricked. The authors accept no responsibility for damaged or bricked devices, lost
> settings or any other consequences (see also the [license](../LICENSE)). This project is
> not affiliated with or endorsed by RØDE. Nothing here has been reviewed by RØDE. If you are not
> comfortable with that risk and the recovery steps below, don't flash.

Everything here is for the **RØDE PodMic USB** with firmware **1.1.9** only. The tools run on **Windows**
(Python 3.8+ from python.org, standard library only, no `pip install`). `build.py` and the tests also run on
Linux/macOS. On **macOS** `extract_stock.py` and `podmic_flash.py` (check/info/probe/flash) run too, but have
**not been tested with a real mic yet**, see [macOS](#macos).

## Variants

| Variant | What it changes vs stock 1.1.9 | sha256 of the built image |
|---|---|---|
| `i2cfix` | **The fix.** I2C interrupt priority raised (root cause), plus a 10 ms timeout with I2C reset + retry on every blocking I2C wait, as a safety net. Nothing else. | `be3f5fa5698f870b5ec7bfd631a66910474991deeccd3a6c50febc6b98fabc82` |
| `i2cfix-volume-holdmute` | i2cfix **+ improvements:** headphone level at power-up −24 dB instead of −31.5 dB; **hold the wheel ≥ 0.7 s to mute/unmute the mic** (LED turns red, like the mute in RØDE apps); a short jingle in the headphones on mute/unmute. | `f47071d7e1e079b3bc05e96fe547bf810933fab95d7fad2562429330c948b840` |
| `diag` | **Debugging, does not fix anything.** Watchdog ~13 s instead of ~1.6 s, and records where the firmware hung (code address, I2C registers), readable after the reset. For confirming that your mic has the same problem. | `ee5a84bdb26ef2e7f16bb76813901db1e162f884d711f4e35f183d38b1e43f06` |
| stock | Unmodified 1.1.9, as extracted. Use it to go back. | `11703ba4b1f4ed1d4d59243225547b596ebcbae8fffe48c302a63fd5b70b8d4c` |

All variants above have been flashed and run on a real PodMic USB (one unit so far; reports from other units
are welcome). The test runs showing that the priority fix works are in
[`research/README.md`](../research/README.md#how-the-cause-was-found).

All variants keep version 1.1.9, so RØDE Central sees stock 1.1.9 and its factory restore still works.

## Quick start (Windows)

Don't want to install Python? Use the installer `podmicusb-doctor.exe` instead, see
[the main README](../README.md#option-a-the-installer-no-python-needed). It does the same steps.

This repository contains **no RØDE firmware**. You extract the stock image from your own RØDE Central install
and `build.py` patches it on your machine. The build output is checked against the sha256 in the table above, so
you know you have exactly the reference image.

1. Install **RØDE Central** (you probably have it already). **Close it** before running the tools; it polls
   the mic and gets in the way.
2. Get the tools: download the zip from the
   [latest release](https://github.com/rumkit/podmicusb-doctor/releases/latest) and unzip it, or clone this
   repository. Open a terminal in its `firmware\tools` folder.
3. Extract the stock image, build, check, flash:

```
py extract_stock.py                        # finds rode_bridge.dll in Program Files -> podmicusb-1.1.9-stock.bin
py build.py i2cfix-volume-holdmute         # -> podmicusb-1.1.9-i2cfix-volume-holdmute.bin, prints "= reference build"
py podmic_flash.py check podmicusb-1.1.9-i2cfix-volume-holdmute.bin
py podmic_flash.py flash podmicusb-1.1.9-i2cfix-volume-holdmute.bin
```

This installs the fix **with** the extras: hold the wheel for about 0.7 s to mute or unmute the mic. For the fix
alone, use `i2cfix` in the last three lines instead (see [Variants](#variants)).

If RØDE Central is installed somewhere else: `py extract_stock.py "D:\path\to\RODE Central"`.
`flash` asks you to type `FLASH` before it erases anything. The flash itself takes about 4 s. **Don't unplug the
mic while it runs.** At the end the mic restarts and the flasher prints the version it reports (1.1.9).

The headphone level is not a stored setting: stock firmware also resets it at every power-up (to −31.5 dB).

## podmic_flash.py

```
py podmic_flash.py check IMAGE   # offline: CRC32, vector table, size, version in the image
py podmic_flash.py info          # list RØDE HID devices (sends nothing)
py podmic_flash.py probe         # app -> bootloader -> query -> back to app; erases nothing
py podmic_flash.py flash IMAGE   # full flash, asks for confirmation first
py podmic_flash.py diag          # diag image only: read the hang record
py podmic_flash.py diag-test     # diag image only: check the record survives a reset
```

It replicates the USB HID protocol RØDE Central uses for a factory restore (taken from a USB capture).
Built-in safety rules:

- The image must have a valid trailing CRC32, a sane vector table and at most 46 pages.
- **The settings pages (0x0800F800, 0x0800FC00) are never erased**, although the bootloader would allow it.
- The bootloader must identify itself exactly as observed, or the flasher stops before erasing anything.
- **It refuses an image with a version lower than the one the mic reports.** The bootloader rejects such an
  image only after the last page is written, and then stops responding (see Recovery). An equal version is
  fine. A higher version gets a warning: don't do that, it could make going back to stock impossible.
- Every erase/write must be acknowledged within 2 s. On the first failure it stops and leaves the mic in
  bootloader mode.

## macOS

> [!IMPORTANT]
> Not yet tested with a real mic. The macOS transport (`tools/podmic_hid_mac.py`, IOKit through `ctypes`) is
> covered by offline tests and a CI job on macOS (no mic there), but nobody has flashed a mic with it yet. The mic also presents a
> different USB configuration to Apple computers (an extra iAP2 interface, [research notes 3q](../research/NOTES.md));
> RØDE Central for macOS updates the mic over HID, so the HID interface the flasher uses should be there, but
> that is not verified either.

Same commands with `python3` instead of `py`, in Terminal in the `firmware/tools` folder of the downloaded
repository (see [the main README](../README.md#option-c-macos-not-yet-tested-with-a-real-mic)). Quit RØDE Central
first (RØDE Central menu → Quit).

`extract_stock.py` searches `/Applications` for RØDE Central, but it isn't known whether any mac version bundles
1.1.9 (2.0.21 bundled 1.1.0). If it finds none, download the stock image from RØDE's update server (the file the
installer and RØDE Central download) and compare its sha256 with the stock line in [Variants](#variants):

```
curl -o podmicusb-1.1.9-stock.bin https://update.rode.com/podmicusb/firmware.bin
shasum -a 256 podmicusb-1.1.9-stock.bin     # must be 11703ba4...8d4c; if not, stop
```

If you are the first to try it, please go in this order and stop at the first error:

```
python3 podmic_flash.py info     # sends nothing: should list "PodMic USB (app) ... bcdDevice 1.1.9 ... usage page 0xff00"
python3 podmic_flash.py probe    # app -> bootloader -> query -> back to app; erases nothing
python3 podmic_flash.py flash podmicusb-1.1.9-stock.bin     # same version as on the mic: lowest risk
```

Then [open an issue](https://github.com/rumkit/podmicusb-doctor/issues) with the output, including the `info`
line. Only after that use the installer (`python3 ../../wizard/podmicusb_doctor.py`) or build and flash a fix
variant (`python3 build.py i2cfix podmicusb-1.1.9-stock.bin`, see [Quick start](#quick-start-windows)).

- `diag`, `diag-test` and `podmic_monitor.py` are Windows only.
- If something fails, the [recovery](#recovery) steps are the same. If RØDE Central on your Mac can't restore
  1.1.9, unplug/replug and flash the stock image again with the installer ("Original RØDE firmware 1.1.9"), or
  use RØDE Central on a Windows PC.

## podmic_monitor.py

Logs whether the mic stops responding or drops off USB, and for how long it was silent before the drop.
That tells a watchdog reset (firmware hung) from other causes. Works with any firmware.

```
py podmic_monitor.py                # basic: polls the mic once per second, logs STALL / GONE / BACK
py podmic_monitor.py --advanced     # also reads the diag image's hang record after every reconnect
```

The i2cfix images keep no record: an I2C failure caught by their timeout is invisible to the monitor, so with
them it only shows disconnects.

Output goes to the console and `podmic_monitor.log`. Reading the GONE line:

| Silent before drop | Meaning |
|---|---|
| ~1.1–2.2 s (stock, i2cfix, i2cfix-volume-holdmute) | watchdog reset: the firmware's main loop hung (the bug this project fixes) |
| ~9–17 s (diag) | the same, with the diag image's longer watchdog |
| ~0 s | instant reset (fault) or the cable was unplugged |

## Confirming the bug on your mic (diag image)

```
py build.py diag
py podmic_flash.py flash podmicusb-1.1.9-diag.bin
py podmic_monitor.py --advanced     # leave it running until a disconnect happens
```

After the next disconnect the monitor prints a DIAG line. The signature of this bug is
`last hang: PC 0x0800ad7c (or 0x0800ad78) = codec write 0x0800ad0c: start/done wait loop, in thread mode`.
The record survives the watchdog reset but **not unplugging**, so read it before you unplug. Please open
an issue with the DIAG lines if you see something different.

## Recovery

- **Back to stock:** `py podmic_flash.py flash podmicusb-1.1.9-stock.bin` (the extracted stock image), or
  the installer (`podmicusb-doctor.exe`, or the script on macOS) → "Original RØDE firmware 1.1.9", or
  **RØDE Central → factory restore**.
- **Flash failed / mic not responding:** unplug the mic, wait ~10 s, plug it back in, then run RØDE Central's
  **factory restore**. This has been tested after a rejected image. The bootloader lives in its own flash area
  (0x08000000–0x08003FFF), which the flasher never writes, so it stays usable.
- **"bootloader info differs"** or other errors before erasing: nothing was written. Unplug/replug and the mic
  starts the old firmware.

## Firmware updates from RØDE

If RØDE releases a newer firmware (e.g. 1.2.0) and you install it, it replaces these images. After that the
bootloader refuses 1.1.9-based images, because their version is lower. That's intended: an official update is
supposed to win. If the new version still has the bug, the patches would need porting.

## For developers

```
python -m unittest discover -s firmware/tests -v            # from the repo root; no RØDE data needed
python -m unittest discover -s wizard/tests -v              # the installer (simulated mic)
```

The installer exe is built by `.github/workflows/build.yml` (PyInstaller) on every push; a `v*` tag also creates
a draft release with the exe, the tools zip and `SHA256SUMS`. The same workflow runs the tests on macOS, plus
`podmic_flash.py info` (no mic on the runner: proves the IOKit bindings load) and the installer's `--self-test`.

With a stock image the tests also check the reference sha256s:

```
$env:PODMIC_STOCK="podmicusb-1.1.9-stock.bin"; python -m unittest discover -s firmware/tests -v    # PowerShell
PODMIC_STOCK=podmicusb-1.1.9-stock.bin python -m unittest discover -s firmware/tests -v            # Linux/macOS
```

| Path | What |
|---|---|
| `tools/` | user-facing scripts (above) |
| `patches/*.s` | add-on sources (Thumb-2 assembly, see each file's header) |
| `patches/*.json` | assembled add-on bytes + labels, read by `build.py` |
| `dev/assemble.py` | `.s` → `.json`; needs `rz-asm` from [Rizin](https://rizin.re) |
| `dev/emu_holdmute.py` | emulates the `i2cfix-volume-holdmute` jingle and button code from a built image, writes WAV previews; needs `rizin` |
| `tests/` | offline tests (synthetic images) |

Changing a patch (paths from the repo root):

1. Edit `firmware/patches/<variant>.s`, then assemble it:
   `python3 firmware/dev/assemble.py firmware/patches/<variant>.s` (writes the `.json`).
2. Build the image with `firmware/tools/build.py`.
3. Update `REFERENCE_SHA256` in `build.py` **and** the sha256 table above.
4. Run the tests. For `i2cfix-volume-holdmute` also run `dev/emu_holdmute.py` on the built image.

Agents: see [`../AGENTS.md`](../AGENTS.md).
