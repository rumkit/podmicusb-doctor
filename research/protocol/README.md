# PodMic USB app-mode HID protocol

Reverse-engineered description of how RØDE Central configures the PodMic USB over USB HID (DSP parameters,
status polls, gain), written before the disconnect investigation and used during it.

| File | What |
|---|---|
| [`podmic-usb-hid-spec.md`](podmic-usb-hid-spec.md) | Protocol specification: reports, parameter reads/writes, polls |
| [`PARAMETER-MATRIX.md`](PARAMETER-MATRIX.md) | Every known parameter with its encoding and a confidence rating |

## Errata and additions from the firmware analysis

- **§5.1 high-pass filter: the direction is inverted.** Param `0x01` value `0x01` = HPF **ON**, `0x00` = OFF.
  Confirmed from a capture of the user switching the HPF on (NOTES 3c/3d).
- **Hidden service commands** on report 2 (`02 <letter>`) in app mode, found in the firmware: `'V'` version,
  `'G'`/`'S'` codec register read/write over I2C (**don't use: they write to the audio codec directly**),
  `'L'` halt (the watchdog then resets the mic), `'T'` test mode, `'R'` reboot into the bootloader,
  `'B'`/`'E'` unknown in stock firmware (`'B'` is used by this project's diag image). See NOTES 3c.
- The **bootloader protocol** (PID 0x004B) used for flashing is documented in `firmware/tools/podmic_flash.py`
  and NOTES 3a.
- `PARAMETER-MATRIX.md` refers to companion documents (`PodMic-USB-Protocol.md`, exciter analyses) that are
  not part of this repository.
