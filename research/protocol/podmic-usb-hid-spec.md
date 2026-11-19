# RØDE PodMic USB — USB/HID Control Protocol Specification

Reverse-engineered protocol reference for implementing a configuration application (e.g. for Linux, which RØDE
does not support). Verified against:

- **firmware 1.1.9** (what the device accepts, replies, stores),
- **RØDE Central 2.2.0**: the Windows `rode_bridge.dll` plus the Android build of the same version (build 1539,
  identical Dart snapshot), whose bridge library keeps its symbol names (how Central encodes and decodes values),
- USB captures of RØDE Central (connect, factory restore, parameter changes).

Where a statement rests on only one of these, it says so.

---

## 1. Device identification

| Field | Value |
|---|---|
| idVendor | `0x19F7` (RØDE Microphones) |
| idProduct | `0x004A` (bootloader: `0x004B`, see [`bootloader-protocol.md`](bootloader-protocol.md)) |
| Product string | "RØDE PodMic USB" |
| Device class | `0xEF` (Miscellaneous, Interface Association Descriptor) |
| USB speed | Full Speed (USB 2.0) |

**Firmware version** is encoded in `bcdDevice` of the standard Device Descriptor (e.g. `0x0119` = 1.1.9), one hex
digit per version part. No vendor command is needed to read it (the service command `'V'`, §12, returns the same
value).

## 2. Interfaces

| Interface | Class | Function |
|---|---|---|
| 0 | Audio Control (UAC 1.0) | Feature/Mixer/Terminal Units |
| 1 | Audio Streaming | Mic capture: mono, 24-bit, 48 kHz, isochronous IN `0x82` |
| 2 | Audio Streaming | Headphone playback OUT `0x02`, feedback IN `0x83` |
| 3 | HID | Vendor control channel (all DSP configuration) |

Interface 3 has a single endpoint: `0x81`, Interrupt IN, 29 bytes, 16 ms interval. There is no Interrupt OUT
endpoint — all host→device commands are HID `SET_REPORT` class control transfers on EP0.

## 3. Input Level (gain) — standard USB Audio, not HID

The 22–63 dB input gain is Feature Unit 2's Volume control on interface 0. Use the OS audio API (on Linux: the
ALSA mixer) or raw UAC requests:

```
bmRequestType = 0x21          (host→device, class, interface)
bRequest      = 0x01          (SET_CUR)   |  0x81 (GET_CUR)
wValue        = 0x0200        (Volume control, channel 0)
wIndex        = 0x0200        (Entity 2, Interface 0)
wLength       = 2
Data          = int16 LE, units of 1/256 dB
```

Example: 57 dB → `57 × 256 = 0x3900` → data `00 39`. Read the current value at startup with GET_CUR; the device
persists gain across reconnects.

The headphone playback volume/mute is Feature Unit 6 (standard OS playback volume); it is not part of the vendor
protocol. The rear wheel sets the overall headphone level inside the mic; it is neither readable nor persisted
([`NOTES.md`](../NOTES.md) 3r).

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

On Linux, writing `[ReportID, content…]` to the `hidraw` node and reading the reply from it does the same.

Report IDs (from the 78-byte HID Report Descriptor; all fields are opaque vendor byte arrays):

| Report ID | Direction | Total bytes on wire | Function |
|---|---|---|---|
| `0x01` | Input | 10 | replies to Report `0x02` |
| `0x02` | Output | 10 | service commands (§12); RØDE Central uses only `'R'` (enter the bootloader for a factory restore) |
| `0x03` | Input | 29 | replies to Report `0x04` |
| `0x04` | Output | 29 | DSP block parameters (§6) |
| `0x05` | Input | 15 | replies to Report `0x06` |
| `0x06` | Output | 15 | DSP accumulators, read-and-clear (§12.1); not used by RØDE Central |
| `0x07` | Input | 28 | replies to Report `0x08` |
| `0x08` | Output | 28 | single parameters, status poll, Basic mode (§5) |

All content fields below are given as offsets into the content (i.e. after the Report-ID byte). Pad all unused
trailing bytes with `0x00`.

**Replies.** Every Report `0x02`/`0x04`/`0x06`/`0x08` request is answered on endpoint `0x81` with the paired Input
report, within a few ms:

```
[reply_id, echo, status, value bytes...]
status: 0x41 'A' = OK, 0x45 'E' = value out of range (nothing changed), 0x4E 'N' = unknown parameter / bad value
```

`echo` is the request's first content byte (param id or target). **The subfield/opcode bytes are not echoed** —
the value region starts right after the status byte. RØDE Central treats anything but `'A'` as a failure; it waits up to 2 s for a Report `0x07`
reply and 0.5 s for a Report `0x03` reply.

## 5. Report `0x08` — single parameters

```
content = [param_id, op, value_or_index, extra..., 0x00 padding to 27]
op: 0x00 = SET, 0x01 = GET
reply (Report 0x07): [param_id, status, value...]   (+ index first where the param is indexed)
```

### 5.1 Parameter table (firmware 1.1.9, handler 0x080084c4)

| param | Name | SET | GET reply value | Persisted | Used by Central |
|---|---|---|---|---|---|
| `0x01` | High-pass filter | `0x00` off, `0x01` on. **`≥ 0x02` → `'E'`, nothing changes** | 0/1 | yes | yes (on/off switch) |
| `0x03` | Direct monitoring (mic → headphones) | `0x00` off, `0x01` on | 0/1 | yes¹ | yes (read on connect) |
| `0x04` | Monitor mix: mic level in the headphone mix | `0..100` (`> 100` → `'E'`); level = `v·66/100 − 60` dB (−60…+6 dB, **91 = 0 dB**) | 0..100 | yes¹ | read on connect only, no UI |
| `0x05` | LED blink ("identify") | `≠ 0` on, `0` off | 0/1 | no | no |
| `0x06` | Host-app ping | **only `0x00`** (else `'N'`); sets the "host app present" flag | — (set only) | no | no (§5.3) |
| `0x07` | Raw mic-chain biquad slot | index `0..2` at content 2; 5 × int32 coefficients at content 6–25 + shift byte at 26 | same layout | no | no (§5.5) |
| `0x08` | Mic mute | `0x00` / `0x01` | 0/1 | no | yes (polled) |
| `0x0B` | Advanced mode | bool | bool | yes | yes |
| `0x0C` | Basic slider | index `0..2` at content 2, value `0..100` at content 3 | index, value | yes | yes |
| `0x0D` | Processing on/off | bool | bool | yes | yes |

Parameters `0x00`, `0x02`, `0x09`, `0x0A` and anything above `0x0D` → `'N'`; an index above 2 for `0x07`/`0x0C` → `'N'`.

¹ Monitoring (`0x03`) and monitor mix (`0x04`) are only written to the settings record while no host app has
pinged (§5.3).

**HPF.** Value 1 loads a 2nd-order high-pass biquad (−3 dB ≈ 54 Hz, −0.5 dB at 75 Hz, 12 dB/oct); value 0
loads pass-through coefficients. The firmware has no other filter set: the "100 Hz"/"150 Hz" options RØDE Central
shows for other microphones (shared enum `disabled=0, hz75=1, hz100=2`, "150 Hz" label only on the NT1 5th Gen)
do not exist on the PodMic. Central itself maps any request for 2 to 1 and logs an internal diagnostic.

**Processing mode** is two independent booleans in the firmware: `0x0D` (processing on = Basic) and `0x0B`
(Advanced).

### 5.2 Status poll

RØDE Central reads param `0x08` (mic mute, `content = [0x08, 0x01, 0x00, …]`) every **500 ms** (bridge timer).
The device does not require the poll.

### 5.3 Host-app ping and the button

The mic's button toggles **monitoring** by default. While a host app has pinged recently, it toggles **mute**
instead; the flag times out after a few seconds without host contact, and the mic unmutes if the host goes away
([`NOTES.md`](../NOTES.md) 3v, 3w). The ping is `SET 0x06 = 0` (or any Report `0x06` request). RØDE Central never pings
(captures show only the `0x08` polls); the bridge pings every 500 ms only for two other RØDE host applications
(very likely RØDE Unify / RØDE Connect, which make the click mute). A host can choose either behaviour.

### 5.4 Basic-mode slider sync

```
content = [0x0C, 0x00, control_index, value_percent, 0x00 × 23]
```

`control_index`: `0` = Depth, `1` = Sparkle, `2` = Punch; `value_percent` 0–100. RØDE Central then writes the
underlying Advanced parameters itself (§9); the firmware only stores the slider value.

### 5.5 Raw biquad access (`0x07`, hidden)

The mic signal passes three biquad slots (Q30 coefficients with a common headroom shift, renormalised on every
write). Slot 0 holds the HPF. Writing a slot takes effect immediately but is **not persisted**, and a later
param `0x01` write overwrites slot 0. A custom filter (e.g. a 100/150 Hz HPF) can therefore be loaded at runtime
only. Read a slot first and keep its format; this path is not used by any RØDE app.

## 6. Report `0x04` — DSP block parameters

```
content = [target_id, opcode, subfield_id, value bytes..., 0x00 padding to 28]
opcode: 0x02 = SET, 0x03 = GET
reply (Report 0x03): [target_id, status, value bytes...]   (no subfield byte)
```

- `target_id`: `0x00` Compressor, `0x01` Noise Gate, `0x02` Aural Exciter, `0x03` Big Bottom. (RØDE's shared code
  computes `slot·4 + block`; the PodMic has one slot, 0.)
- `subfield_id 0x00` on every block = enable: `0x00` off, `0x01` on.
- **The firmware does not range-check report 4.** Values are stored and applied as sent; the encoders in §10 are
  the only safeguard.
- Every SET updates the settings record (§11).

### 6.1 Field map

| Block | Field | subfield | Value bytes (content offsets) | Encoding (§10) | Range (RØDE Central 2.2.0) |
|---|---|---|---|---|---|
| Compressor | Threshold | `0x01` | 4 bytes LE @3–6 (low 16 bits always 0) | table `T_thr` | −60…0 dB |
| Compressor | Ratio ("shape") | `0x02` | 1 byte @3 | linear byte | 1.5…4.5 :1 |
| Compressor | Attack | `0x03` | 4 bytes LE @3–6 | table `T_att` | 0.1…10 ms |
| Compressor | Release | `0x04` | 4 bytes LE @3–6 | table `T_rel` | 5…200 ms |
| Compressor | Gain (make-up) | `0x05` | 4 bytes LE @3–6 | table `T_gain` | 0…9 dB |
| Noise Gate | Threshold | `0x01` | 4 bytes LE @3–6 | Q31 amplitude | −60…0 dB |
| Noise Gate | Attack | `0x02` | 4 bytes LE @3–6 | Q31 one-pole coefficient | 0.1…1000 ms |
| Noise Gate | Hold | `0x03` | 4 bytes LE @3–6 | Q31 ramp step | 0.05…2 s |
| Noise Gate | Release ("decay") | `0x04` | 4 bytes LE @3–6 | Q31 ramp step | 0.05…2 s |
| Noise Gate | Range | `0x05` | 4 bytes LE @3–6 | Q31 amplitude | −100…0 dB |
| Noise Gate | Hysteresis | `0x06` | 4 bytes LE @3–6 | Q31 amplitude window | 0…100 % |
| Aural Exciter | Harmonics ("mix") | `0x01` | fine 4 bytes LE @3–6 + coarse byte @7 | table `T_hd` | 0…100 % |
| Aural Exciter | Tune | `0x02` | A 4 bytes LE @3–6, B 4 bytes LE @7–10, coarse byte @11 | tables `T_tuneA`, `T_tuneB` | 600…5000 Hz |
| Aural Exciter | (subfield 3) | `0x03` | 1 byte @3 | raw | not exposed by Central; default 31 |
| Big Bottom | Drive | `0x01` | fine 4 bytes LE @3–6 + coarse byte @7 | table `T_hd` (same as Harmonics) | 0…100 % |
| Big Bottom | Tune | `0x02` | 1 byte @3 | linear byte | 60…312 Hz |
| Big Bottom | (subfield 3) | `0x03` | 1 byte @3 | raw | not exposed by Central; default 31 |

RØDE Central sends only the fields that changed (all fields on the first apply after connect), one SET per field.

## 7. Connect sequence (RØDE Central 2.2.0)

On connect, RØDE Central reads every value in this order (identical to the captured burst):

1. Report 4 GET `[target, 0x03, sub]`: Compressor sub 0–5, Noise Gate 0–6, Aural Exciter 0–3, Big Bottom 0–2.
2. Report 8 GET: `0x0C` index 0, 1, 2 (`[0x0C, 0x01, idx]`), then `0x0B`, `0x0D`.
3. Report 8 GET: `0x08` (mute), `0x03` (monitoring), `0x04` (monitor mix), `0x01` (HPF).

Then it polls `0x08` every 500 ms (§5.2). The device keeps all settings itself (§11), so a host only needs to read
them; it never has to push a full state.

**Decoding reads** (as Central does): table fields are converted back by searching the same table for the raw
value (→ index → UI value with the index formula of §10). Harmonics, Drive and Tune are decoded from the **coarse
byte only** (`% = b/255·100`, `Hz = 600 + b/255·4400`, BB `Hz = 60 + b/255·252`). Gate fields: invert the
formulas of §10.3 (Central's gate decoder was not read in detail).

## 8. Device→host channel summary

| Request | Reply | Value region starts |
|---|---|---|
| Report 2 `[cmd, …]` | Report 1 `[cmd, status, …]` | content 2 |
| Report 4 `[target, op, sub, …]` | Report 3 `[target, status, value…]` | content 2 |
| Report 6 `[sub]` | Report 5 `[sub, status, 12 bytes]` | content 2 |
| Report 8 `[param, op, …]` | Report 7 `[param, status, value…]` | content 2 (indexed params: index first) |

Example: Compressor Threshold is written at content offsets 3–6 (high half 5–6) and read back at content 2–5
(high half 4–5) — i.e. bytes 5–6 of the reply counting the report-ID byte.

## 9. Basic mode semantics (exact, from RØDE Central's `DepthSparklePunch`)

Basic mode is a front end over the Advanced parameters. After each `0x0C` slider write, Central writes
(p = Punch/100, values in UI units, then encoded per §10):

| Basic slider | Advanced writes |
|---|---|
| Depth d | Big Bottom enable = 1, Drive = 0.9·d % |
| Sparkle s | Aural Exciter enable = 1, Harmonics = 0.9·s % |
| Punch | Compressor enable = 1, Threshold = −14 − 16p dB, Gain = 3p dB (other compressor fields unchanged) |
| Punch | Noise Gate enable = 1, Threshold = 10p − 45 dB, Range = −13p dB |

Switching from Advanced back to Basic (`0x0B` = 0) makes Central re-apply the stored Depth/Sparkle/Punch.
The factory defaults (§11.2) follow these formulas exactly for Punch = 70.

## 10. Value encodings (exact)

All multi-byte values are little-endian. RØDE Central computes in 32-bit float and converts with C truncation
(`int(x)`), not rounding. "Q31" means `int32(x · 2³¹)`, saturated to `0x7FFFFFFF`.

### 10.1 Linear bytes

```
Compressor ratio:   b = int((r − 1.5) / 3 · 255)
Big Bottom tune:    b = int((Hz − 60) / 252 · 255)
Coarse bytes:       Harmonics/Drive b = int(pct / 100 · 255),  Tune b = int((Hz − 600) / 4400 · 255)
decode:             value = lo + b/255 · (hi − lo)
```

### 10.2 Table fields

Seven 256-entry `uint32` tables, identical in the Windows DLL and the Android bridge (2.0.94 and 2.2.0). The
encoder computes an index `i` and sends `T[i]`:

| Table | Index `i` (then `& 0xFF`) | `T[0]` | `T[255]` |
|---|---|---|---|
| `T_thr` (Compressor threshold) | `int((1 − (dB + 60)/60) · 255)` (0 dB → 0, −60 dB → 255) | `0x00000000` | `0x7FFD0000` |
| `T_att` (Compressor attack) | `int(log10(ms / 0.1) / log10(100) · 255)` | `0x0D724000` | `0x00011EB8` |
| `T_rel` (Compressor release) | `int(log10(ms / 5) / log10(40) · 255)` | `0x001E0000` | `0x00004EE3` |
| `T_gain` (Compressor gain) | `int(dB / 9 · 255)` | `0x00000000` | `0x74E2B063` |
| `T_hd` (Harmonics, Drive) | coarse byte `b` | `0x00000000` | `0x7FFFFFFF` |
| `T_tuneA`, `T_tuneB` (Exciter tune) | coarse byte `b` | `0x0B000000`, `0x24000000` | `0x54000000`, `0x7FFFFFFF` |

The full tables are in [`podmic-dsp-tables.md`](podmic-dsp-tables.md) (machine-readable:
[`podmic-dsp-tables.json`](podmic-dsp-tables.json)). They come from `rode_bridge.dll` (RØDE Central for Windows
2.2.0) VAs `0x180470FB0` (thr), `0x1804717B0` (att), `0x1804713B0` (rel), `0x180470BB0` (gain), `0x1804723B0` (hd),
`0x180471BB0` / `0x180471FB0` (tune A/B). All captured values match them bit for bit (19 Tune points, 3 Drive
points, the Release calibration points). The closed-form curves in earlier versions of this spec were fits and are
superseded; they are wrong at the low end of Threshold, for Attack and for Gain.

### 10.3 Noise Gate (closed form, Q31)

```
Threshold, Range:  Q31( 10^(dB / 20) )
Attack:            w = 5 / (48000 · t_s);  c = cos(w);  Q31( c − 1 + sqrt(c² − 4c + 3) )
Hold, Release:     Q31( 1 / (48000 · t_s) )
Hysteresis:        Q31( 10^((−1 − 7x) / 20) ),  x = percent / 100   (1…8 dB window)
```

The attack formula is a one-pole coefficient for a time constant of t/5; for small w it is ≈ `1/(9600·t_s)`,
which is where the earlier "÷9600" approximation came from. RØDE Central passes hysteresis as 0…1 and shows it
×100 as percent.

## 11. Settings persistence and defaults

### 11.1 When the mic saves

Every SET (report 4, and report 8 params `0x01`, `0x03`, `0x04`, `0x0B`, `0x0C`, `0x0D`) updates an 83-byte
settings record in RAM. It is written to flash (pages 46/47, `0x0800F800`/`0x0800FC00`) only:

- when the supply drops (the low-voltage-detect interrupt runs the save-and-halt routine) — i.e. on unplug,
- on service command `'L'` (halt) or `'R'` (bootloader).

So settings survive power cycles without an explicit save. RØDE Central's "save to microphone" does nothing for
the PodMic. **Not persisted:** mute, the wheel position, LED blink, raw biquad writes, the host-app flag.

Input gain (UAC, §3) is persisted as well (separately handled).

### 11.2 Factory defaults

Used when no valid settings record exists (blank settings pages); a firmware upload (factory restore) does not
erase the settings pages ([`bootloader-protocol.md`](bootloader-protocol.md) §7). Decoded from firmware 1.1.9:

| Setting | Default |
|---|---|
| HPF | off |
| Processing / Advanced | on (Basic) / off |
| Depth / Sparkle / Punch | 75 / 80 / 70 |
| Monitoring / monitor mix | 1 / 91 (0 dB) — note: the user has seen the LED show monitoring off by default; unconfirmed |
| Compressor | on; −24.9 dB, ratio 1.99, attack 0.69 ms, release 20.9 ms, gain 1.98 dB |
| Noise Gate | off; −38 dB, attack ≈ 30 ms, hold 0.305 s, release 0.2 s, range −9 dB, hysteresis 20 % |
| Aural Exciter | on; harmonics 69.8 %, tune 3499 Hz, subfield 3 = 31 |
| Big Bottom | on; drive 64.7 %, tune 90 Hz, subfield 3 = 31 |

## 12. Report `0x02` — service commands

Found in the firmware (handler at 0x08008684). RØDE Central uses only `'R'`, for a factory restore; the others
are hidden. Format `[0x02, command_letter, args…]`, zero-padded; the reply comes on endpoint `0x81` as Report
`0x01`: `[0x01, command_letter, 0x41 ('A') or 0x4E ('N' = unknown command), result…]`.

| Command | Effect |
|---|---|
| `'V'` | returns the firmware version (u16, same as `bcdDevice`) |
| `'R'` | saves settings and reboots into the bootloader (firmware upload: [`bootloader-protocol.md`](bootloader-protocol.md)) |
| `'L'` | saves settings and halts the CPU; the watchdog then resets the mic (same routine as the low-voltage interrupt) |
| `'T'` | factory test mode (disables the DSP blocks and settings saving) |
| `'G'` / `'S'` | read / write an audio-codec (NAU88L21) register over I2C. **Don't use: they access the codec directly** |
| `'B'`, `'E'` | purpose unknown in stock firmware (`'B'` is reused by this project's `diag` image) |

The bootloader reuses Report `0x02` with its own command set (`'I'`, `'E'`, `'R'`); see
[`bootloader-protocol.md`](bootloader-protocol.md).

### 12.1 Report `0x06` — DSP accumulators (hidden)

`content = [sub]`, sub `0..3`; any other value → `'N'`. Every Report `0x06` request also sets the host-app flag
(§5.3). The reply (Report `0x05`) carries 12 bytes from a per-block accumulator, which is **cleared on read**
(sub 0 and 1 read two different blocks, sub 2 and 3 a third). Most likely level / gain-reduction meters for a
host app; the exact meaning is not decoded. RØDE Central does not use it.

## 13. Worked examples

Set Compressor Ratio to 2.48:1 (byte `int((2.48−1.5)/3·255)` = 83 = `0x53`):

```
SET_REPORT wValue=0x0204 wIndex=3 wLength=29
04 00 02 02 53 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
```

Set Noise Gate Threshold to −40 dB (`int(10^(−2) · 2³¹)` = 21474836 = `0x0147AE14`):

```
04 01 02 01 14 AE 47 01 00 ... (29 bytes total)
```

Turn Noise Gate off:

```
04 01 02 00 00 00 ... (29 bytes total)
```

Switch the high-pass filter on, then read it back (reply `07 01 41 01 …`):

```
08 01 00 01 00 ... (28 bytes total)
08 01 01 00 00 ...
```

Switch processing to Basic:

```
SET_REPORT wValue=0x0208 wIndex=3 wLength=28
08 0D 00 01 00 ... (28 bytes total)
```

Read Compressor enable state (reply `03 00 41 <value> …`):

```
04 00 03 00 00 ... (29 bytes total)
```

## 14. Known unknowns

- Aural Exciter / Big Bottom subfield `0x03`: a writable byte (default 31) that RØDE Central never sets; its
  effect is not decoded.
- Report `0x06` accumulators: which DSP quantity each sub returns (§12.1).
- Param `0x05` (LED blink) is inferred from the code path; not tried on hardware.
- Monitoring default (§11.2): the defaults table says on; the user's mic showed off.
- The effect of omitting the status poll (§5.2) is untested (the firmware does not need it).
