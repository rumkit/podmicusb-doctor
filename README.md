# RØDE PodMic USB: random disconnects on Windows — root cause and firmware fix

The **RØDE PodMic USB** (firmware 1.1.9) randomly drops off USB and reconnects a few seconds later. It was
reported on Windows 11, while the mic's headphone output was the system audio device. This repository documents
the investigation, the root cause and a community firmware patch.

**Root cause:** the firmware's I2C interrupts (the bus to the audio codec) are almost at the bottom of the priority
order, below audio and USB. Under audio/USB interrupt load an I2C event is occasionally handled too late. The codec write then never
completes, the firmware's main loop waits forever with no timeout, and the watchdog resets the mic, which is the
disconnect you see. **Fix:** raise the I2C interrupt priority (two bytes in stock firmware), plus a timeout as a
safety net. In testing, an image with the fix ran for over 4 hours with zero I2C failures, against ~19 per hour without it
(caught by the timeout).

Reported to RØDE support; the ticket was closed without a fix.

> [!WARNING]
> Flashing modified firmware is **at your own risk**. It may void your warranty, and a failed flash needs the
> recovery steps in [`firmware/README.md`](firmware/README.md#recovery). Not affiliated with or endorsed by RØDE.

## Contents

| Path | What |
|---|---|
| [`firmware/`](firmware/) | **Start here if you want the fix.** Patch variants, build script, flasher, monitor, recovery. [README](firmware/README.md) |
| [`firmware/tools/`](firmware/tools/) | `extract_stock.py` · `build.py` · `podmic_flash.py` · `podmic_monitor.py` (Windows, Python 3, no dependencies) |
| [`firmware/patches/`](firmware/patches/) | Patch sources (Thumb-2 assembly) and assembled bytes |
| [`research/`](research/) | **How the cause was found.** [Summary](research/README.md) · [full lab notebook](research/NOTES.md) · [HID protocol](research/protocol/) |
| [`AGENTS.md`](AGENTS.md) | Orientation and safety rules for AI coding agents (and humans) working on this repo |

## Getting the fix, in short

No firmware files are distributed here. The scripts build the patched image from the stock firmware inside your
own RØDE Central install, and check that the result is byte-identical to the reference build:

```
cd firmware\tools
py extract_stock.py
py build.py rel1
py podmic_flash.py flash podmic_usb_v1.1.9_rel1_i2cfix.bin
```

| Variant | |
|---|---|
| `rel1` | the fix only |
| `rel2` | the fix + hold-wheel-to-mute with a headphone jingle + louder headphone default |
| `diag` | records where the firmware hangs, to confirm you have the same problem; fixes nothing |

Read [`firmware/README.md`](firmware/README.md) first: status of each variant, safety rules, recovery.

## Status

- Root cause identified and confirmed with test images on one unit (see [research](research/README.md)).
- Release images `rel1`/`rel2` are built and checked offline, **not yet hardware-tested in their final form**.
- Only firmware 1.1.9 is supported.

Reports from other PodMic USB owners are welcome, especially `diag` results, and whether the problem happens on
other OSes or with `rel1`. Please open an issue.

## License

Code: MIT. Documentation: CC BY 4.0. See [`LICENSE`](LICENSE). RØDE, PodMic and RØDE Central are trademarks of
their respective owner.
