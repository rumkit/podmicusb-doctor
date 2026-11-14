# PodMic USB bootloader (firmware upload) protocol

How RØDE Central writes the application firmware to a PodMic USB. It was recovered from a USB capture of
RØDE Central's **factory restore** on Windows and confirmed by
[`firmware/tools/podmic_flash.py`](../../firmware/tools/podmic_flash.py), which reproduces it and has
flashed stock and patched images on a real mic. Sources: [NOTES](../NOTES.md) 3a (capture), 3b (version),
3e (settings pages), 3g/3h (acceptance rules).

> [!CAUTION]
> Everything here was observed on **one unit with app firmware 1.1.9** and bootloader reporting bcdDevice
> 0x0100. The bootloader's own code was never read (it isn't in any file we have), so its behaviour is
> described only from the outside. Read the [recovery steps](../../firmware/README.md#recovery) before
> experimenting.

## 1. Devices

| Mode | VID | PID | bcdDevice | Product string |
|---|---|---|---|---|
| Application | 0x19F7 | **0x004A** | firmware version, e.g. 0x0119 = 1.1.9 (see §5) | "RØDE PodMic USB" |
| Bootloader | 0x19F7 | **0x004B** | 0x0100 | "RØDE PodMic Bootloader" |

The bootloader exposes a single HID interface (vendor usage page 0xFF00) with one interrupt IN endpoint
(0x81, 16 B). Its report descriptor (39 B) defines:

| Report ID | Direction | Size (without ID) | Use |
|---|---|---|---|
| 1 | IN | 9 B | every reply |
| 2 | OUT | 9 B | commands |
| 3 | OUT | 1024 B | page data |

All host→device reports go as **HID SET_REPORT (Output) on EP0** (class request to the interface), not on
an interrupt OUT endpoint. Replies arrive as input report 1 on EP 0x81. Commands are strictly sequential:
one outstanding command, wait for its reply.

## 2. Entering the bootloader

In application mode, send report 2 with the service command **`'R'`** (`0x52`):

```
host → app   02 52 00 00 00 00 00 00 00 00      (report 2, 'R', zero padding)
```

There is no reply. The app saves its settings, soft-disconnects from USB and resets; ~10–75 ms later the mic
drops off the bus and re-enumerates as the bootloader (PID 0x004B), ~0.4 s later in the capture. RØDE
Central then waited ~1 s before its first command.

If the application image is invalid (e.g. after a rejected or interrupted flash), the mic **stays in the
bootloader** after a power cycle, so it can still be flashed (NOTES 3g).

## 3. Bootloader commands

Report 2 commands are `02 <letter> <args…>` zero-padded to 9 bytes. Replies are report 1:
`01 <echo> 41 …`, where byte 2 = `0x41` ('A') means acknowledged.

| Command (host → device) | Meaning | Reply (device → host) | Observed reply time |
|---|---|---|---|
| `02 49` `'I'` | info | `01 49 41 01 00 00 04 30 00 00` | — |
| `02 45 NN` `'E'` | erase application page `NN` | `01 45 41 00 …` | ~20–30 ms |
| `03` + 1024 B | write the page that was just erased | `01 50 41 00 …` (`'P'`) | ~30–48 ms |
| `02 52` `'R'` | leave the bootloader, start the application | none (device drops off the bus) | — |

**`'I'` reply**, after `01 49 41`: three little-endian u16 values:

| Bytes | Value | Meaning |
|---|---|---|
| `01 00` | 0x0001 | probably the bootloader protocol version |
| `00 04` | 0x0400 | page size: 1024 B |
| `30 00` | 0x0030 | page count: 48 (0x08004000–0x0800FFFF) |

**Page index `NN`** counts 1 KB pages from the application base **0x08004000** (index 0 = the app's vector
table). It is an index, not an address. The bootloader area (0x08000000–0x08003FFF) can't be named by this
protocol.

What is **not** on the wire: absolute addresses, image length, MD5/CRC/hash, read-back verification, version
numbers. The firmware MD5 RØDE Central knows about is only used to check its own download.

## 4. Full sequence (factory restore)

```
app        → 02 'R'                         (re-enumerate as PID 0x004B, wait ~1 s)
bootloader → 02 'I'                         ← 01 49 41 01 00 00 04 30 00 00
for NN in 0x00 .. 0x2D (46 pages):
             02 'E' NN                      ← 01 45 41 …
             03 <1024 bytes of page NN>     ← 01 50 41 …
             02 'R'                         (re-enumerates as the app, PID 0x004A)
```

The whole 46-page write took ~3.9 s (3.4 s with `podmic_flash.py`). The data written was byte-identical to
the stock 1.1.9 image bundled in RØDE Central (47,104 B = exactly 46 pages).

## 5. Image format

| Offset in image | Content |
|---|---|
| 0x00 | Cortex-M vector table: initial SP (0x20002000), reset vector (Thumb address in the image) … |
| **0x20** | **version**, u16 LE, one hex digit per part: `19 01` = 0x0119 = 1.1.9. A reserved vector-table slot the CPU ignores. The app copies it into its USB bcdDevice at start-up. |
| … | code and data, 0xFF padding |
| last 4 bytes | **CRC32** (zlib / IEEE, as `zlib.crc32`), little-endian, over all preceding bytes |

## 6. What the bootloader accepts

- It **validates the image only when the last page arrives**. Earlier pages are acknowledged normally.
- **Version check (anti-downgrade):** an image whose version is **lower** than the installed one was rejected
  (1.1.1 over 1.1.9); an **equal** version is accepted. It's unknown whether it compares against the
  previously installed version or a fixed minimum (both were 1.1.9), and where it keeps that value.
- **No signature:** a modified 1.1.9 image with a correct CRC32 was accepted and runs.
- On **rejection the bootloader wedges**: the SET_REPORT for the last page never completes (Windows error
  121, ERROR_SEM_TIMEOUT), and it stays enumerated as PID 0x004B but stops replying. Recovery: unplug ~10 s,
  replug (the mic comes up in the bootloader because the app is invalid), then flash a valid image, e.g. with
  RØDE Central's factory restore. Proven once (NOTES 3g).

Consequence: **never flash an image with a higher version marker than the stock firmware you may want to go
back to.** Once installed, the anti-downgrade check could reject stock forever.

## 7. Settings pages: don't write them

The bootloader reports 48 pages, but the application keeps the mic's settings in **pages 46 and 47**
(0x0800F800, 0x0800FC00). RØDE's factory restore writes only pages 0–45, and a flasher should do the same.
After the restore RØDE Central writes the DSP parameters back through the application's own HID protocol
(see [`podmic-usb-hid-spec.md`](podmic-usb-hid-spec.md)).

## 8. Open questions

- Bootloader version semantics of the `'I'` reply's first value.
- Where the bootloader stores the version it compares against (the app reads something from 0x08003FE0,
  the last 32 B of the bootloader area; not traced).
- Whether the bootloader can be entered without a working app other than through an invalid image (e.g. a
  button held at power-up).
- Behaviour on images shorter than 46 pages, on writing pages 46–47, and on an out-of-range `NN`. Not tried.
