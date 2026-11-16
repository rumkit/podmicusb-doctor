<p align="center">
  <img src="assets/podmicusb-doctor.svg" alt="podmicusb-doctor: firmware patches for a healthier mic" width="640">
</p>

# RØDE PodMic USB: random disconnects on Windows — root cause and firmware fix

The **RØDE PodMic USB** (firmware 1.1.9) randomly drops off USB and reconnects a few seconds later. It was
reported on Windows 11, while the mic's headphone output was the system audio device. This repository documents
the investigation, the root cause and a community firmware patch.

**Root cause:** the firmware's I2C interrupts (the bus to the audio codec) are almost at the bottom of the priority
order, below audio and USB. Under audio/USB interrupt load an I2C event is occasionally handled too late. The codec write then never
completes, the firmware's main loop waits forever with no timeout, and the watchdog resets the mic, which is the disconnect you see. 

**Fix:** raise the I2C interrupt priority (two bytes in stock firmware), plus a timeout as a
safety net. In testing, an image with the fix ran for over 4 hours with zero I2C failures, against ~19 per hour without it
(caught by the timeout).

The original issue was reported to RØDE support; the ticket was closed without a fix.

> [!WARNING]
> Flashing modified firmware is at your own risk. It may void your warranty, brick your mic or make demons fly
> out of your nose. If a flash fails, follow the recovery steps in [`firmware/README.md`](firmware/README.md#recovery).
> Not affiliated with or endorsed by RØDE.

## Contents

| Path | What |
|---|---|
| [`wizard/`](wizard/) | Source of the installer `podmicusb-doctor.exe` (uses the scripts in `firmware/tools/`) |
| [`firmware/`](firmware/) | Patch variants, build script, flasher, monitor, recovery. [README](firmware/README.md) |
| [`firmware/tools/`](firmware/tools/) | `extract_stock.py` · `build.py` · `podmic_flash.py` · `podmic_monitor.py` (Windows, Python 3.8+, no dependencies) |
| [`firmware/patches/`](firmware/patches/) | Patch sources (Thumb-2 assembly) and assembled bytes |
| [`research/`](research/) | How the cause was found. [Summary](research/README.md) · [full lab notebook](research/NOTES.md) · [HID protocol](research/protocol/) · [firmware upload protocol](research/protocol/bootloader-protocol.md) |
| [`docker/`](docker/) | The Linux reverse-engineering lab (Rizin + rz-ghidra, TShark, …) the analysis ran in. [README](docker/README.md) |
| [`AGENTS.md`](AGENTS.md) | Orientation and safety rules for AI coding agents (and humans) working on this repo |

## Getting the fix

No firmware files are distributed here. The tools get the stock 1.1.9 firmware from RØDE
themselves (the installer downloads it from RØDE's own update server or copies it from RØDE Central, if you have it),
patch it on your machine, and check that the result is byte-identical to the reference build. Everything runs on Windows. Read the [safety notes and recovery steps](firmware/README.md#recovery)
first.

### Option A: the installer (no Python needed)

1. Download `podmicusb-doctor.exe` from the [latest release](https://github.com/rumkit/podmicusb-doctor/releases/latest).
2. Close RØDE Central, plug in the mic and double-click the exe. Windows may warn "Windows protected your PC",
   because the exe isn't code-signed: click **More info → Run anyway**.
3. Pick what to install and confirm. The update takes about 10 seconds. Don't unplug the mic while it runs.

The installer works only with mics on firmware 1.1.9, the version the fix is made for; with any other version it
stops without changing anything. If the mic is stuck in update mode (after an interrupted update), it offers only
the original firmware, to recover it.

| Menu entry | What it does |
|---|---|
| Fix only | the fix, nothing else changes |
| Fix + extras | the fix + louder headphone default + **hold the wheel to mute**, with a headphone jingle |
| Diagnostic | records where the firmware hangs, to confirm you have the same problem; fixes nothing |
| Original RØDE firmware 1.1.9 | go back to stock |

The exe is built from this repository by GitHub Actions. To check that the file you downloaded is exactly that
build: `gh attestation verify podmicusb-doctor.exe --repo rumkit/podmicusb-doctor`.
`podmicusb-doctor.exe --help` lists the options (a self-test that needs no mic, more detailed output, preferring the
download over a local RØDE Central copy).

### Option B: the Python scripts

The same steps as separate scripts (Python 3.8+, standard library only), for people who want to see each step or
keep the built image files. See [`firmware/README.md`](firmware/README.md).

## Status

- Root cause identified and confirmed with test images on one unit (see [research](research/README.md)).
- Only firmware 1.1.9 is supported.

Reports from other PodMic USB owners are welcome, especially `diag` results, and whether the problem happens on
other OSes or with `i2cfix`. Please open an issue.

## License

Code: MIT. Documentation: CC BY 4.0. See [`LICENSE`](LICENSE). RØDE, PodMic and RØDE Central are trademarks of
their respective owner.
