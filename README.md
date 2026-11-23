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
| [`wizard/`](wizard/) | The installer: source of `podmicusb-doctor.exe` (Windows), also run directly as a Python script (macOS). Uses the scripts in `firmware/tools/` |
| [`firmware/`](firmware/) | Patch variants, build script, flasher, monitor, recovery. [README](firmware/README.md) |
| [`firmware/tools/`](firmware/tools/) | `extract_stock.py` · `build.py` · `podmic_flash.py` · `podmic_monitor.py` (Python 3.8+, no dependencies; Windows. macOS: all but the monitor, not yet tested with a real mic) |
| [`firmware/patches/`](firmware/patches/) | Patch sources (Thumb-2 assembly) and assembled bytes |
| [`research/`](research/) | How the cause was found. [Summary](research/README.md) · [full lab notebook](research/NOTES.md) · [HID protocol](research/protocol/) · [firmware upload protocol](research/protocol/bootloader-protocol.md) |
| [`docker/`](docker/) | The Linux reverse-engineering lab (Rizin + rz-ghidra, TShark, …) the analysis ran in. [README](docker/README.md) |
| [`AGENTS.md`](AGENTS.md) | Orientation and safety rules for AI coding agents (and humans) working on this repo |

## Getting the fix

No firmware files are distributed here. The tools get the stock 1.1.9 firmware from RØDE
themselves (the installer downloads it from RØDE's own update server or copies it from RØDE Central, if you have it),
patch it on your machine, and check that the result is byte-identical to the reference build. Option A is for
Windows, Option B for Windows and macOS. Read the [safety notes and recovery steps](firmware/README.md#recovery)
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

### Option B: the Python scripts (Windows and macOS)

> [!IMPORTANT]
> On macOS this has **not been run with a real mic yet**; it is only covered by automated tests (run on macOS in
> CI, without a mic). If you try it, please [open an issue](https://github.com/rumkit/podmicusb-doctor/issues)
> with what happened, also if it worked. Start with the steps that change nothing (`info`, `probe`, see
> [`firmware/README.md`](firmware/README.md#macos)).

The same installer as a Python script (Python 3.8+, standard library only). This is how you run it on a Mac
(there is no macOS app). The bug was only seen on Windows, but you can install the fix from a Mac, e.g. for a mic
you also use on a Windows PC.

1. Install Python 3.8+:
   - Windows: from [python.org](https://www.python.org/downloads/windows/), then use `py`.
   - macOS: from [python.org](https://www.python.org/downloads/macos/) (macOS doesn't include it), or run
     `xcode-select --install`, which installs Apple's `python3`.
2. Download this repository: green **Code** button on the repository page → **Download ZIP** (or `git clone`), and
   unzip it. On a Mac, Safari unzips it into your Downloads folder.
3. Close RØDE Central (Windows: also check the system tray; macOS: RØDE Central menu → Quit) and plug in the mic.
4. Open a terminal in the unzipped folder and run:

```
py wizard\podmicusb_doctor.py               # Windows
python3 wizard/podmicusb_doctor.py          # macOS
```

The menu and the safety checks are the same as in the exe (see [Option A](#option-a-the-installer-no-python-needed)).
The stock firmware normally comes from RØDE's update server: the installer also looks in RØDE Central, but on
macOS it isn't known whether any version bundles 1.1.9. `--help` lists the options, e.g. `--self-test`, which needs
no mic and checks the download and the builds.

To run each step yourself instead (extract, build, check, flash) and keep the built image files, see the
[quick start in `firmware/README.md`](firmware/README.md#quick-start).

## Status

- Root cause identified and confirmed with test images on one unit (see [research](research/README.md)).
- Only firmware 1.1.9 is supported.
- Flashing tested on Windows. macOS support exists but has not been tested with a real mic yet.

Reports from other PodMic USB owners are welcome, especially `diag` results, and whether the problem happens on
other OSes or with `i2cfix`. Please open an issue.

## License

Code: MIT. Documentation: CC BY 4.0. See [`LICENSE`](LICENSE). RØDE, PodMic and RØDE Central are trademarks of
their respective owner.
