# RØDE PodMic USB — USB/HID Control Protocol Specification

Reverse-engineered protocol reference for implementing a configuration application. Verified against firmware 1.1.9 and RODE Central (Windows).

---

## 1. Device identification

| Field | Value |
|---|---|
| idVendor | `0x19F7` (RØDE Microphones) |
| idProduct | `0x004A` |
| Product string | "RØDE PodMic USB" |
| Device class | `0xEF` (Miscellaneous, Interface Association Descriptor) |
| USB speed | Full Speed (USB 2.0) |

**Firmware version** is encoded in `bcdDevice` of the standard Device Descriptor (e.g. `0x0119` = 1.1.9), one hex digit per version part. No vendor command is needed to read it (the service command `'V'`, §11, returns the same value).

## 2. Interfaces

| Interface | Class | Function |
|---|---|---|
| 0 | Audio Control (UAC 1.0) | Feature/Mixer/Terminal Units |
| 1 | Audio Streaming | Mic capture: mono, 24-bit, 48 kHz, isochronous IN `0x82` |
| 2 | Audio Streaming | Headphone playback OUT `0x02`, feedback IN `0x83` |
| 3 | HID | Vendor control channel (all DSP configuration) |

Interface 3 has a single endpoint: `0x81`, Interrupt IN, 29 bytes, 16 ms interval. There is no Interrupt OUT endpoint — all host→device commands are HID `SET_REPORT` class control transfers on EP0.

## 3. Input Level (gain) — standard USB Audio, not HID

The 22–63 dB input gain is Feature Unit 2's Volume control on interface 0. Use the OS audio API or raw UAC requests:

```
bmRequestType = 0x21          (host→device, class, interface)
bRequest      = 0x01          (SET_CUR)   |  0x81 (GET_CUR)
wValue        = 0x0200        (Volume control, channel 0)
wIndex        = 0x0200        (Entity 2, Interface 0)
wLength       = 2
Data          = int16 LE, units of 1/256 dB
```

Example: 57 dB → `57 × 256 = 0x3900` → data `00 39`. Read the current value at startup with GET_CUR; the device persists gain across reconnects.

The headphone monitor volume/mute is Feature Unit 6 (standard OS playback volume); it is not part of the vendor protocol.

## 4. HID transport

All vendor commands:

```
bmRequestType = 0x21           (host→device, class, interface)
bRequest      = 0x09           (SET_REPORT)
wValue        = 0x0200 | ReportID   (ReportType 2 = Output)
wIndex        = 3              (interface number)
wLength       = report size including the leading Report-ID byte
Data          = [ReportID, content...]
```

Report IDs (from the 78-byte HID Report Descriptor; all fields are opaque vendor byte arrays):

| Report ID | Direction | Total bytes on wire | Function |
|---|---|---|---|
| `0x01` | Input | 10 | replies to Report `0x02` service commands (§11) |
| `0x02` | Output | 10 | service commands (§11); not used by RODE Central |
| `0x03` | Input | 29 | read-back/echo channel for Report `0x04` |
| `0x04` | Output | 29 | DSP block parameters (write + read requests) |
| `0x05` | Input | 15 | reserved/unobserved |
| `0x06` | Output | 15 | unobserved; the firmware has a handler for it (not decoded) |
| `0x07` | Input | 28 | read-back/echo channel for Report `0x08` |
| `0x08` | Output | 28 | status poll, simple toggles, Basic-mode sync |

All content fields below are given as offsets into the content (i.e. after the Report-ID byte). Pad all unused trailing bytes with `0x00`.

## 5. Report `0x08` — simple commands and status

### 5.1 Simple parameter write

```
content = [param_id, 0x00, value, 0x00 × 25]
```

| param_id | Control | Values |
|---|---|---|
| `0x01` | High-Pass Filter | `0x01` = filter ON, `0x00` = OFF (default after a factory restore) |
| `0x0B` | Processing: Advanced mode enable | `0x00` / `0x01` |
| `0x0D` | Processing: Basic mode enable | `0x00` / `0x01` |

The HPF direction was confirmed with a capture of switching the filter on (`08 01 00 01`). Processing mode is two independent booleans, not a 3-state enum.

### 5.2 Parameter read

```
content = [param_id, 0x01, index, 0x00 × 25]
```

The device answers on endpoint `0x81` with Report `0x07` (see §7).

### 5.3 Status poll

RODE Central polls param `0x08` (`content = [0x08, 0x01, 0x00, ...]`): every ~1.15 s in the original captures, every 0.5 s in later ones. The response value is `0x00` in normal operation. In the firmware, param `0x08` reads the **mic mute** state (and param `0x03` the monitoring state). Whether the device requires this poll is unverified.

### 5.4 Basic-mode slider sync

When a Basic-mode slider changes, the app writes a UI-sync value:

```
content = [0x08, 0x0C, 0x00, control_index, value_percent, 0x00 × 23]
```

`control_index`: `0` = Depth, `1` = Sparkle, `2` = Punch. `value_percent` is the literal 0–100 value. In addition to this sync write, the app writes the underlying Advanced parameters (§8).

## 6. Report `0x04` — DSP block parameters

```
content = [target_id, opcode, subfield_id, value bytes..., 0x00 padding to 28]
```

- `opcode` = `0x02` (SET) or `0x03` (GET). For GET, send zeros in the value region; the device answers on `0x81` with Report `0x03` (§7).
- `target_id`: `0x00` Compressor, `0x01` Noise Gate, `0x02` Aural Exciter, `0x03` Big Bottom.
- `subfield_id 0x00` on every block = enable toggle: `content = [target, 0x02, 0x00, value]`, value `0x01` = on, `0x00` = off.

### 6.1 Field map

| Block | Field | subfield | Value bytes (content offsets) | Encoding (§9) | UI range |
|---|---|---|---|---|---|
| Compressor | Threshold | `0x01` | 2 bytes LE @5–6 (bytes 3–4 zero) | cubic taper | −60…0 dB |
| Compressor | Ratio | `0x02` | 1 byte @3 | linear byte | 1.5…4.5 :1 |
| Compressor | Attack | `0x03` | 4 bytes LE @3–6 | 5/3-power | 0.1…10 ms |
| Compressor | Release | `0x04` | 4 bytes LE @3–6 | inverse power | 5…200 ms |
| Compressor | Gain | `0x05` | 4 bytes LE @3–6 | power taper | 0…9 dB |
| Noise Gate | Threshold | `0x01` | 4 bytes LE @3–6 | dB→int32 | −60…0 dB |
| Noise Gate | Attack | `0x02` | 4 bytes LE @3–6 | inverse time (9600) | 0.1…1000 ms |
| Noise Gate | Hold | `0x03` | 4 bytes LE @3–6 | inverse time (48000) | 0.05…2 s |
| Noise Gate | Release | `0x04` | 4 bytes LE @3–6 | inverse time (48000) | 0.05…2 s |
| Noise Gate | Range | `0x05` | 4 bytes LE @3–6 | dB→int32 | −100…0 dB |
| Noise Gate | Hysteresis | `0x06` | 4 bytes LE @3–6 | dB→int32 w/ pre-scale | 0…100 % |
| Aural Exciter | Harmonics | `0x01` | fine 4 bytes LE @3–6 + coarse byte @7 | byte table (§9.8) | 0…100 % |
| Aural Exciter | Tune | `0x02` | A 4 bytes LE @3–6, B 4 bytes LE @7–10, coarse byte @11 | byte table (§9.9) | 600…5000 Hz |
| Big Bottom | Drive | `0x01` | fine 4 bytes LE @3–6 + coarse byte @7 | byte table (§9.10) | 0…100 % |
| Big Bottom | Tune | `0x02` | 1 byte @3 | linear byte | 60…312 Hz |

## 7. Device→host channel (endpoint `0x81`)

Every Report `0x04`/`0x08` write triggers an unsolicited Interrupt IN packet on `0x81` within ~1 ms, carrying the paired Input report (`0x04`→`0x03`, `0x08`→`0x07`). GET requests are answered the same way.

Response format:

```
Report 0x03: [0x03, target_id, 0x41, value bytes...]
Report 0x07: [0x07, param_id, 0x41, (index,) value bytes...]
```

**The subfield byte is not present in responses** — the value region starts immediately after the `0x41` marker, one byte earlier than in a write. Apply each field's within-value-region offset accordingly (e.g. Compressor Threshold's 2 bytes sit at response offsets 5–6 → content offsets 2–3 after the marker).

**Recommended connect sequence**: all settings persist in device memory and there are no fixed power-on defaults. On connect, issue a GET for every parameter (RODE Central reads ~58 values in a burst) and populate the UI from the responses.

## 8. Basic mode semantics

Basic mode is a front-end over the same Advanced parameters. Each Basic slider change writes the §5.4 sync value plus Advanced fields:

| Basic control | Writes |
|---|---|
| Sparkle | Aural Exciter Harmonics (≈ 0.9 × Sparkle) |
| Depth | Big Bottom Drive (≈ 0.9 × Depth) |
| Punch | Compressor Threshold + Gain, Noise Gate Enable + Threshold + Range |

## 9. Value encodings

All multi-byte values are little-endian. Fields never exceed the positive half of their signed type.

### 9.1 Linear byte

```
raw  = floor( (value − lo) / (hi − lo) × 255 )
value = lo + raw/255 × (hi − lo)
```

Used by: Compressor Ratio, Big Bottom Tune, and every "coarse byte" in two-part payloads. Note the encode rounding is floor, not round-to-nearest.

### 9.2 Compressor Threshold — cubic taper

```
raw = round( 0.147 × (−dB)³ )          (2 bytes LE)
dB  = −(raw / 0.147)^(1/3)
```

### 9.3 Compressor Gain — power taper

```
raw = round( 91499279.4 × dB^1.394 )   (4 bytes LE)
dB  = (raw / 91499279.4)^(1/1.394)
```

Resolution is inherently coarse near 0 dB.

### 9.4 Compressor Attack — 5/3-power taper

```
raw  = round( 1.897e6 × t_ms^(5/3) )   (4 bytes LE)
t_ms = (raw / 1.897e6)^(3/5)
```

### 9.5 Compressor Release — inverse power taper

```
raw  = round( 1.309e7 / t_ms^1.2414 )  (4 bytes LE)
t_ms = (1.309e7 / raw)^(1/1.2414)
```

Calibrated at 20.94 ms and 74.78 ms; accuracy outside 20–75 ms is unverified.

### 9.6 Noise Gate Threshold / Range — dB to linear amplitude

```
raw = round( 2147483647 × 10^(dB/20) )
dB  = 20 × log10( raw / 2147483647 )
```

### 9.7 Noise Gate time constants and Hysteresis

```
Hold, Release:  raw = round( 2147483647 / (48000 × t_seconds) )
Attack:         raw = round( 2147483647 / ( 9600 × t_seconds) )
Hysteresis:     dB_equiv = −(1.0 + 0.07 × percent)
                raw = round( 2147483647 × 10^(dB_equiv/20) )
```

Note Attack uses the 9600 constant, not 48000.

### 9.8 Aural Exciter Harmonics — closed-form byte curve

The fine value is a function of the coarse byte `x` only (`x = floor(percent × 2.55)`); effective resolution is 256 steps. Working in the normalized domain `y = fine32 / 2147483647`:

```
        ┌ 0.13·t + 0.08·t³ ,            t = x/179 ,        0 ≤ x ≤ 179
y(x) =  │
        └ 0.21 + 0.79·((x−180)/75)^3.5 ,                  180 ≤ x ≤ 255

fine32 = round( y(x) × 2147483647 )     # 0x7FFFFFFF; low byte is 0x00 except at saturation
```

Endpoints are exact: `x=0 → 0` (mute), `x=255 → 0x7FFFFFFF` (saturation). The two segments meet at `0.21` (byte 179 → 180). Fit accuracy vs. all logged captures: **RMS 0.11 %, max 0.29 %** of full scale — replaces the earlier sampled table / log-interpolation. Write both the computed fine value and the coarse byte.

> **Needs revalidation:** the fit rests on ~6 captured points with no sampling in bytes 60–150. A short sweep there would confirm the lower cubic against quantization noise. Unrounded coefficients (`0.1310·t − 0.0095·t² + 0.0894·t³`) give RMS 0.039 % if higher precision is wanted. Full residual table: `aural-exciter-tune-harmonics-raw-tables.md`.

### 9.9 Aural Exciter Tune — piecewise-linear byte table

A and B are functions of the coarse byte only (`byte = floor((f − 600) × 255/4400)`); effective resolution is 256 steps (~17.25 Hz). Both are generated as `value = floor(W × 2²⁴ / 7)` where `W(byte)` is piecewise-linear. Confirmed exact segments:

| byte range | W_A(b) | W_B(b) |
|---|---|---|
| 0–23 | `77 + (3/4)·b` | `252` |
| 45–56 | `(2086 + 28·b)/29` | `252` (floor clamp through byte ≥47) |
| 56–78 | `(126 + 63·b)/29` | — |
| 105–107 | `(3129 + 28·b)/29` | `280` |
| 169–198 | `371 + (77/29)·(b−169)` | joins W_B(198) = 504 |
| 198–219 | `448 + 3·(b−198)` | `504 + (11/2)·(b−198)` |
| 255 | `588` | `0x7FFFFFFF` (saturated) |

Full sampled table:

| byte | A | B |
|---|---|---|
| 0 | `0x0B000000` | `0x24000000` |
| 19 | `0x0D092492` | `0x24000000` |
| 23 | `0x0D76DB6D` | `0x24000000` |
| 45 | `0x107B9611` | `0x24000000` |
| 47 | `0x10C234F7` | `0x24000000` |
| 70 | `0x1658469E` | `0x25EE5846` |
| 78 | `0x18D3DCB0` | `0x2708D3DC` |
| 105 | `0x1DE58469` | `0x28000000` |
| 107 | `0x1E2C234F` | `0x28000000` |
| 138 | `0x27092492` | `0x2B924924` |
| 168 | `0x34892492` | `0x34ADB6DB` |
| 169 | `0x35000000` | `0x35000000` |
| 173 | `0x368469EE` | `0x379EE584` |
| 196 | `0x3F3DCB08` | `0x46B08D3D` |
| 200 | `0x40DB6DB6` | `0x49922449` |
| 209 | `0x44B6DB6D` | `0x50A49249` |
| 219 | `0x49000000` | `0x58800000` |
| 237 | `0x527B9611` | `0x6AE58469` |
| 255 | `0x54000000` | `0x7FFFFFFF` |

For bytes inside confirmed segments, compute exactly; elsewhere interpolate `W` linearly in the byte between the nearest samples (error < 1 %). Write A, B, and the coarse byte together.

### 9.10 Big Bottom Drive — same curve as Harmonics

Same payload layout as Harmonics (fine 32-bit LE + coarse byte), and — to current evidence — the **same closed-form transfer curve (§9.8)**. Use `y(x)` from §9.8 with `x = floor(percent × 2.55)`. The unchanged Harmonics curve reproduces all three known Drive points to ≤0.11 % of full scale:

| byte | fine32 (actual) | model diff |
|---|---|---|
| 57 | `0x059E6C00` | +0.008 %FS |
| 165 | `0x17382E00` | +0.109 %FS |
| 169 | `0x183BFA00` | +0.073 %FS |

> **Needs revalidation — weaker than Harmonics:** only 3 captured points, all in bytes 57–169 (two nearly coincident). The low end (< byte 57) and the entire upper `^3.5` segment (> byte 170) are untested for Drive. An ~8-click Drive sweep with points below 20 % and above 80 % would confirm the curve is shared.

## 10. Worked examples

Set Compressor Ratio to 2.48:1 (byte `floor((2.48−1.5)/3×255)` = 83 = `0x53`):

```
SET_REPORT wValue=0x0204 wIndex=3 wLength=29
04 00 02 02 53 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

Turn Noise Gate off:

```
04 01 02 00 00 00 ... (29 bytes total)
```

Switch processing to Basic:

```
SET_REPORT wValue=0x0208 wIndex=3 wLength=28
08 0D 00 01 00 ... (28 bytes total)
```

Read Compressor enable state (response arrives on 0x81 as `03 00 41 <value> ...`):

```
04 00 03 00 00 ... (29 bytes total)
```

## 11. Report `0x02` — service commands

Found in the firmware (handler at 0x08008684), **not used by RODE Central**. Format `[0x02, command_letter, args…]`, zero-padded; the reply comes on endpoint `0x81` as Report `0x01`: `[0x01, command_letter, 0x41 ('A') or 0x4E ('N' = unknown command), result…]`.

| Command | Effect |
|---|---|
| `'V'` | returns the firmware version (u16, same as `bcdDevice`) |
| `'R'` | saves settings and reboots into the bootloader (firmware upload: [`bootloader-protocol.md`](bootloader-protocol.md)) |
| `'L'` | halts the CPU after saving settings; the watchdog then resets the mic |
| `'T'` | factory test mode (disables the DSP blocks and settings saving) |
| `'G'` / `'S'` | read / write an audio-codec (NAU88L21) register over I2C. **Don't use: they access the codec directly** |
| `'B'`, `'E'` | purpose unknown in stock firmware (`'B'` is reused by this project's `diag` image) |

The bootloader reuses Report `0x02` with its own command set (`'I'`, `'E'`, `'R'`); see [`bootloader-protocol.md`](bootloader-protocol.md).

## 12. Known unknowns

- Report IDs `0x05/0x06` are declared but never used by RODE Central; the firmware handles `0x06` (not decoded).
- Aural Exciter has an extra readable subfield `0x03` (observed value `0x1F`) with no corresponding UI control.
- Report-8 param `0x04` is read at connect (value `0x5B` observed) but maps to no known control (`0x03` is monitoring, §5.3).
- Factory Reset is not a separate command: RODE Central re-uploads the stock firmware through the bootloader ([`bootloader-protocol.md`](bootloader-protocol.md)) and then writes the DSP parameters back with Report `0x04`.
- The effect of omitting the status poll (§5.3) is untested.
