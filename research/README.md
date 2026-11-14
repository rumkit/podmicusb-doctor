# Research: why the PodMic USB disconnects, and the fix

Summary of the investigation. The full chronological log, including dead ends, is in [`NOTES.md`](NOTES.md);
section numbers like (3s) below refer to it.

## Symptom

The mic periodically drops off USB and re-enumerates a couple of seconds later. Windows switches audio to
another device and back. Seen on **Windows 11 (25H2)** with the mic's zero-latency headphone output as the system
output device. It reproduced across laptops, cables and ports, and with and without RØDE Central running.
Exclusive mode, audio enhancements, USB power saving and Intel SST drivers were ruled out. Time to failure
varies from minutes to hours. It was not seen on Linux or macOS with the same mic, laptop and cable during about a
day of testing. That shows the problem is rarer there, not that those systems are immune (3t).

## Hardware and firmware

| | |
|---|---|
| MCU | GigaDevice GD32F350 (Cortex-M4, 64 KB flash, 8 KB RAM) |
| Audio codec | Nuvoton NAU88L21 on I2C (address 0x1B), identified from board markings (2b, "I2C slave identified") |
| Flash layout | bootloader 0x08000000–0x08003FFF · application 0x08004000 (46 × 1 KB pages, trailing CRC32) · settings 0x0800F800–0x0800FFFF |
| Firmware | 1.1.9, extracted from RØDE Central's `rode_bridge.dll` (2a, 3b); 1.1.0 found in an old macOS installer (3k) |
| Tools | Rizin + rz-ghidra (static analysis), Wireshark/USBPcap, the scripts in [`../firmware`](../firmware) |

## How the cause was found

1. **USB capture of a disconnect (2a–2d).** The host side is clean: isochronous audio and feedback keep
   flowing right up to the drop. At t = 30.5 s an HID status poll is accepted but **never answered**, while
   audio continues. 1.3 s later the device leaves the bus and re-enumerates. There were no host control requests
   before the drop.
2. **Firmware structure (2a).** Audio and USB run in interrupts and DMA. Everything else runs in a cooperative
   main loop. The independent watchdog (~1.64 s) is fed **only** by a 4 ms main-loop task. So a main loop that
   hangs gives exactly the captured pattern: audio keeps streaming, HID goes silent, and the watchdog resets the
   chip ~1.6 s later. The same 4 ms task runs a control loop that steers the codec clock from the playback buffer
   level, writing to the codec over I2C. The I2C driver waits for completion **without any timeout**, which made it
   the lead candidate (2b).
3. **Flashing became possible (3a, 3g, 3h).** A USB capture of RØDE Central's factory restore revealed a simple
   HID bootloader protocol, so a flasher could be written. Findings: the bootloader checks the CRC and **rejects an
   image whose version is lower than the installed one**. It **does not check signatures**. After a rejection it
   stops responding until the mic is power-cycled and restored.
4. **Proof that it's the watchdog (3n).** A test image with the watchdog lengthened to ~13 s changed the silent
   period before the drop from ~1.3 s to **13.0 s**. So the disconnect is the watchdog reset, and the hang is
   permanent: the main loop never recovers on its own.
5. **Where it hangs (3p, 3s).** A diagnostic image records the stuck code address and the I2C registers in RTC
   backup registers, which survive the reset. Result: stuck in the **done-wait of the codec write routine**, with
   the I2C peripheral idle, no error flags, no error interrupt, and one data byte not yet sent. The peripheral
   ended the transfer and the driver never saw it complete.
6. **Safety net (3t, 3u, 3aa).** Test image *fix1* bounds the waits (20 ms), then resets the I2C peripheral and lets
   the stock retry resend. Result: **no disconnects in ~10 h, but 188 caught I2C failures**, about one per 30,000
   codec writes, in bursts. The stall point varied (after byte 1, after all bytes, before the address phase) (3aa).
   That fits an interrupt that was **handled too late** better than an electrical fault: electrical faults would set
   error flags (3x).
7. **Root cause (3ab, 3ac, 3ad).** The I2C event/error interrupts run at priority **0xC0/0xD0, near the bottom of the
   priority order**, below the audio DMA interrupt (0x40) and USB (0x70). An I2C event can wait behind a full audio-block
   or USB interrupt. GigaDevice's GD32F3x0 errata (I2C section) and ST's equivalent errata describe I2C event
   handling as latency-sensitive and recommend a higher I2C interrupt priority or DMA. Test image *fix2p* raised
   both to 0x10. Result: **0 I2C failures and 0 disconnects in 4 h 15 min**, against ~19/h average for fix1. The test
   was continued and judged a success before the release images were built (3ae). The release images were then
   flashed and run on the same mic.

## The fix

| | Stock 1.1.9 | Fix |
|---|---|---|
| I2C1 event IRQ priority | 0xC0 | 0x10 |
| I2C1 error IRQ priority | 0xD0 | 0x10 |

In stock firmware that's two bytes (`movs r1,#0xc0` / `movs r2,#0xd0` at 0x08008eae/0x08008eb0). The I2C event
handler is short, straight-line code with no waits, so letting it preempt audio and USB costs microseconds (3ac).
The `i2cfix` image also keeps a bounded wait (10 ms) with I2C reset + retry as a safety net. Worst case with
a permanently failing I2C bus is ~180 ms blocked per task run, well inside the stock watchdog (3ae).

## Why Windows? (open)

Not proven. What is known:
- The codec writes don't depend on the OS. Once any audio stream has opened after power-up, the 4 ms task
  rewrites the codec clock registers **every tick (~250 times per second) until the next reboot**, whether audio
  is playing or not. A "dirty" flag is never cleared (3z). So the I2C traffic is the same on every OS.
- No UAC control requests precede the drop (3d). The firmware does tell hosts apart at enumeration and gives
  Windows a different configuration descriptor than Linux (3q), but the playback interface, the main-loop code
  and the codec-write path are identical for both.
- The most likely difference is **interrupt load and timing**: how often and when the USB and audio interrupts
  fire, and therefore how long a low-priority I2C event can be delayed. That depends on the host controller and
  driver behaviour. This is a hypothesis; a usbmon capture on Linux compared with Windows would test it.

## Other findings

- Version marker: a u16 at application offset 0x20, copied into USB `bcdDevice` (3b). RØDE Central offers an
  update only when its bundled version is higher than the mic's (2d).
- Hidden service commands and HID protocol errata: [`protocol/README.md`](protocol/README.md).
- The rear wheel (headphone level) resets to step 19 (−31.5 dB) on every power-up. That's stock behaviour, not a
  lost setting (3r). With RØDE Unify/Connect attached a wheel click mutes the mic; otherwise it toggles monitoring (3v).
- Windows, Linux and Apple hosts are told apart at enumeration (3q). Apple hosts get an iAP2 interface, Linux
  gets an extra 16-bit mic alt setting (inferred from the code, not checked with `lsusb`).

## Not included

The USB captures and raw monitor logs are not published. They contain the full firmware image (in the flash
stream) and the unit's serial number. All numbers quoted above come from them; the notes describe the
measurements in detail.
