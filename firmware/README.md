# Firmware: patched images, flasher and monitor

> [!WARNING]
> **Use at your own risk.** Flashing modified firmware may void your warranty, and a failed or interrupted
> flash can leave the mic unusable until it is recovered (see [Recovery](#recovery)). This project is
> **not affiliated with or endorsed by RØDE**. Nothing here has been reviewed by RØDE. If you are not
> comfortable with the recovery steps below, don't flash.

Everything here is for the **RØDE PodMic USB** with firmware **1.1.9** only. The tools run on **Windows**
(Python 3.8+ from python.org, standard library only, no `pip install`). The build and test scripts also run
on Linux/macOS.

## Variants

This repository contains **no RØDE firmware**. You extract the stock image from your own RØDE Central install
and `build.py` patches it on your machine. The build output is checked against the sha256 below, so you know you
have exactly the reference image.

| Variant | What it changes vs stock 1.1.9 | Status | sha256 of the built image |
|---|---|---|---|
| `rel1` | **The fix.** I2C interrupt priority raised (root cause), plus a 10 ms timeout with I2C reset + retry on the codec writes as a safety net. Nothing else. | ⚠️ built, not yet hardware-tested as a whole (see below) | `be3f5fa5698f870b5ec7bfd631a66910474991deeccd3a6c50febc6b98fabc82` |
| `rel2` | rel1 **+ improvements:** headphone level at power-up −24 dB instead of −31.5 dB; **hold the wheel ≥ 0.7 s to mute/unmute the mic** (LED turns red, like the mute in RØDE apps); a short jingle in the headphones on mute/unmute. | ⚠️ built and emulated, not yet hardware-tested as a whole | `f47071d7e1e079b3bc05e96fe547bf810933fab95d7fad2562429330c948b840` |
| `diag` | **Debugging, does not fix anything.** Watchdog ~13 s instead of ~1.6 s, and records where the firmware hung (code address, I2C registers), readable after the reset. For confirming that your mic has the same problem. | ✅ used on hardware | `ee5a84bdb26ef2e7f16bb76813901db1e162f884d711f4e35f183d38b1e43f06` |
| stock | Unmodified 1.1.9, as extracted. Use it to go back. | — | `11703ba4b1f4ed1d4d59243225547b596ebcbae8fffe48c302a63fd5b70b8d4c` |

**What has been tested on hardware so far:** the test image *fix2p*, which has the same priority fix,
hold-to-mute and jingle as `rel2` plus a 20 ms timeout and diagnostics, ran with **0 I2C failures and 0 disconnects
for 4 h 15 min**. The image without the priority fix (*fix1*) had ~19 failures/hour on average, caught by the timeout. The
release images are those test images with the diagnostics removed and the timeout shortened to 10 ms. See
[`../research/README.md`](../research/README.md).

All variants keep the version marker **1.1.9**. RØDE Central therefore still sees stock 1.1.9 and doesn't offer
an "update". A factory restore in RØDE Central puts stock firmware back.

## Quick start (Windows)

1. Install **RØDE Central** (you probably have it already). **Close it** before running the tools; it polls
   the mic and gets in the way.
2. Download this repository and open a terminal in `firmware\tools`.
3. Extract the stock image, build, check, flash:

```
py extract_stock.py                        # finds rode_bridge.dll in Program Files -> podmic_usb_v1.1.9.bin
py build.py rel1                           # -> podmic_usb_v1.1.9_rel1_i2cfix.bin, prints "= reference build"
py podmic_flash.py check podmic_usb_v1.1.9_rel1_i2cfix.bin
py podmic_flash.py flash podmic_usb_v1.1.9_rel1_i2cfix.bin
```

If RØDE Central is installed somewhere else: `py extract_stock.py "D:\path\to\RODE Central"`.
`flash` asks you to type `FLASH` before it erases anything. The flash itself takes about 4 s. **Don't unplug the
mic while it runs.** At the end the mic restarts and the flasher prints the version it reports (1.1.9).

**Settings:** the flasher never touches the two flash pages that hold the mic's settings. Note that rel2 sets
the headphone level to −24 dB at every power-up; stock firmware does the same with −31.5 dB.

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

## podmic_monitor.py

Logs whether the mic stops responding or drops off USB, and for how long it was silent before the drop.
That tells a watchdog reset (firmware hung) from other causes. Works with any firmware.

```
py podmic_monitor.py                # basic: polls the mic once per second, logs STALL / GONE / BACK
py podmic_monitor.py --advanced     # also reads the diag image's hang record after every reconnect
```

Output goes to the console and `podmic_monitor.log`. Reading the GONE line:

| Silent before drop | Meaning |
|---|---|
| ~1.1–2.2 s (stock, rel1, rel2) | watchdog reset: the firmware's main loop hung (the bug this project fixes) |
| ~9–17 s (diag) | the same, with the diag image's longer watchdog |
| ~0 s | instant reset (fault) or the cable was unplugged |

## Confirming the bug on your mic (diag image)

```
py build.py diag
py podmic_flash.py flash podmic_usb_v1.1.9_diag_wdt13s.bin
py podmic_monitor.py --advanced     # leave it running until a disconnect happens
```

After the next disconnect the monitor prints a DIAG line. The signature of this bug is
`last hang: PC 0x0800ad7c (or 0x0800ad78) = codec write 0x0800ad0c: start/done wait loop, in thread mode`.
The record survives the watchdog reset but **not unplugging**, so read it before you unplug. Please open
an issue with the DIAG lines if you see something different.

## Recovery

- **Back to stock:** `py podmic_flash.py flash podmic_usb_v1.1.9.bin` (the extracted stock image), or
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
PODMIC_STOCK=podmic_usb_v1.1.9.bin python -m unittest ...   # also checks the reference sha256s
```

| Path | What |
|---|---|
| `tools/` | user-facing scripts (above) |
| `patches/*.s` | add-on sources (Thumb-2 assembly, see each file's header) |
| `patches/*.json` | assembled add-on bytes + labels, read by `build.py` |
| `dev/assemble.py` | `.s` → `.json`; needs `rz-asm` from [Rizin](https://rizin.re) |
| `dev/emu_rel2.py` | emulates rel2's jingle and button code from a built image, writes WAV previews; needs `rizin` |
| `tests/` | offline tests (synthetic images) |

If you change a patch, update `REFERENCE_SHA256` in `build.py` and the table above. Agents: see
[`../AGENTS.md`](../AGENTS.md).
