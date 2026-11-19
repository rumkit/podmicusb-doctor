# PodMic USB HID protocols

Reverse-engineered description of how the PodMic USB is configured over USB HID (DSP parameters, single
parameters, status polls, gain), complete enough to write a configuration tool for other platforms such as Linux,
plus the bootloader protocol RØDE Central uses to upload firmware. Started from USB captures before the disconnect
investigation; completed in session 4 (2026-11-19) from the firmware and RØDE Central 2.2.0.

| File | What |
|---|---|
| [`podmic-usb-hid-spec.md`](podmic-usb-hid-spec.md) | Protocol specification: reports, every parameter with its exact encoder, connect sequence, persistence, defaults, service commands |
| [`podmic-dsp-tables.md`](podmic-dsp-tables.md), [`.json`](podmic-dsp-tables.json) | The seven 256-entry encoder tables used by the Compressor, Aural Exciter and Big Bottom fields |
| [`PARAMETER-MATRIX.md`](PARAMETER-MATRIX.md) | Every known parameter with its encoding and a confidence rating |
| [`bootloader-protocol.md`](bootloader-protocol.md) | Firmware upload: bootloader (PID 0x004B) commands, image format, what the bootloader accepts |