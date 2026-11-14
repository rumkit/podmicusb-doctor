# PodMic USB HID protocols

Reverse-engineered description of how RØDE Central configures the PodMic USB over USB HID (DSP parameters,
status polls, gain), written before the disconnect investigation and used during it, plus the bootloader
protocol RØDE Central uses to upload firmware, recovered during the investigation.

| File | What |
|---|---|
| [`podmic-usb-hid-spec.md`](podmic-usb-hid-spec.md) | Protocol specification: reports, parameter reads/writes, polls, service commands |
| [`PARAMETER-MATRIX.md`](PARAMETER-MATRIX.md) | Every known parameter with its encoding and a confidence rating |
| [`bootloader-protocol.md`](bootloader-protocol.md) | Firmware upload: bootloader (PID 0x004B) commands, image format, what the bootloader accepts |