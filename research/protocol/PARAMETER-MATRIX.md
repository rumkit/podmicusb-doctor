# PodMic USB — Parameter Encoding Matrix

Inventory of every known parameter: transport, encoding and how well it is established. Wire format, encoders and
the meaning of the table names: [`podmic-usb-hid-spec.md`](podmic-usb-hid-spec.md); the `T_*` tables are in [`podmic-dsp-tables.md`](podmic-dsp-tables.md). First compiled 2026-07-10 from
USB captures; **rebuilt 2026-11-19** from the firmware (1.1.9) and RØDE Central 2.2.0 (Windows DLL + Android build of
the same version), which replace the earlier curve fits.

**Sources:** **FW** = firmware handler read; **BR** = RØDE Central bridge encoder/decoder read; **UI** = Central's
Flutter model (ranges, defaults); **CAP** = USB capture. **Confidence:** High = two independent sources agree (or the
code is unambiguous and a capture matches); Medium = one source; Low = inferred.

## DSP blocks (Report `0x04` — `[target, 0x02, subfield, payload]`)

| Block | Parameter | Sub | Payload | Encoding | Range (default) | Sources | Confidence |
|---|---|---|---|---|---|---|---|
| all | Enable | 0x00 | 1 byte | 0 off / 1 on | — | FW, BR, CAP | High |
| Compressor (0) | Threshold | 0x01 | u32 @3–6 | `T_thr[int((1−(dB+60)/60)·255)]` | −60…0 dB (−20) | FW, BR, UI, CAP | High |
| Compressor | Ratio | 0x02 | u8 @3 | `int((r−1.5)/3·255)` | 1.5…4.5 (3) | BR, UI, CAP | High |
| Compressor | Attack | 0x03 | u32 @3–6 | `T_att[int(log10(ms/0.1)/2·255)]` | 0.1…10 ms (0.7) | BR, UI | High |
| Compressor | Release | 0x04 | u32 @3–6 | `T_rel[int(log10(ms/5)/log10(40)·255)]` | 5…200 ms (21) | BR, UI, CAP | High |
| Compressor | Gain | 0x05 | u32 @3–6 | `T_gain[int(dB/9·255)]` | 0…9 dB (2) | BR, UI | High |
| Noise Gate (1) | Threshold | 0x01 | i32 @3–6 | `Q31(10^(dB/20))` | −60…0 dB (−20) | BR, UI, CAP | High |
| Noise Gate | Attack | 0x02 | i32 @3–6 | one-pole: `w=5/(48000·t)`, `Q31(cos w − 1 + √(cos²w − 4cos w + 3))` | 0.1…1000 ms (0.7) | BR, UI, CAP (≈) | High |
| Noise Gate | Hold | 0x03 | i32 @3–6 | `Q31(1/(48000·t_s))` | 0.05…2 s (0.1) | BR, UI, CAP | High |
| Noise Gate | Release (decay) | 0x04 | i32 @3–6 | `Q31(1/(48000·t_s))` | 0.05…2 s (1.0) | BR, UI, CAP | High |
| Noise Gate | Range | 0x05 | i32 @3–6 | `Q31(10^(dB/20))` | −100…0 dB (−60) | BR, UI, CAP | High |
| Noise Gate | Hysteresis | 0x06 | i32 @3–6 | `Q31(10^((−1−7x)/20))`, x = %/100 | 0…100 % | BR, UI, CAP | High |
| Aural Exciter (2) | Harmonics | 0x01 | u32 @3–6 + coarse u8 @7 | `b=int(%/100·255)`, `T_hd[b]`, b | 0…100 % (50) | BR, UI, CAP | High |
| Aural Exciter | Tune | 0x02 | A u32 @3–6, B u32 @7–10, coarse u8 @11 | `b=int((Hz−600)/4400·255)`, `T_tuneA[b]`, `T_tuneB[b]`, b | 600…5000 Hz (2000) | BR, UI, CAP (19 pts exact) | High |
| Aural Exciter | Subfield 3 | 0x03 | u8 @3 | raw | not in Central (31) | FW (stored, applied) | Medium — effect unknown |
| Big Bottom (3) | Drive | 0x01 | u32 @3–6 + coarse u8 @7 | same as Harmonics (`T_hd`) | 0…100 % (50) | BR, CAP (3 pts exact) | High |
| Big Bottom | Tune | 0x02 | u8 @3 | `int((Hz−60)/252·255)` | 60…312 Hz (200) | BR, UI | High |
| Big Bottom | Subfield 3 | 0x03 | u8 @3 | raw | not in Central (31) | FW | Medium — effect unknown |

UI defaults are RØDE Central's model defaults; the mic's own factory defaults are in spec §11.2.

## Single parameters (Report `0x08` — `[param, op, value]`, op 0 = SET, 1 = GET)

| Param | Control | Values | Persisted | Sources | Confidence |
|---|---|---|---|---|---|
| — | Input gain | USB Audio FU2 Volume, int16 1/256 dB, 22…63 dB | yes | CAP | High |
| 0x01 | High-pass filter | 0 off / 1 on (2nd-order, −3 dB ≈ 54 Hz); ≥2 → `'E'` | yes | FW, BR, CAP | High |
| 0x03 | Direct monitoring | 0 / 1 | yes* | FW, BR | High |
| 0x04 | Monitor mix (mic level in headphones) | 0…100 → −60…+6 dB (91 = 0 dB); >100 → `'E'` | yes* | FW, BR | High |
| 0x05 | LED blink | ≠0 on / 0 off | no | FW | Low — inferred, not tried |
| 0x06 | Host-app ping | 0 only | no | FW, BR | High |
| 0x07 | Raw biquad slot 0…2 | 5 × i32 + shift | no | FW | Medium — format from code only |
| 0x08 | Mic mute | 0 / 1 | no | FW, BR, CAP | High |
| 0x0B | Advanced mode | bool | yes | FW, BR, CAP | High |
| 0x0C | Basic slider (0 Depth, 1 Sparkle, 2 Punch) | `[0x0C, 0, idx, 0…100]` | yes | FW, BR, CAP | High |
| 0x0D | Processing on (Basic) | bool | yes | FW, BR, CAP | High |

\* only while no host app is pinging (spec §5.3).

## Other

| Item | Where | Confidence |
|---|---|---|
| Basic → Advanced mapping (Depth 0.9×, Sparkle 0.9×, Punch formulas) | spec §9; BR, matches factory defaults | High |
| Report `0x06` accumulators (likely meters) | spec §12.1; FW | Low |
| Firmware upload / factory restore | [`bootloader-protocol.md`](bootloader-protocol.md) | High |
| Settings persistence (saved on unplug via low-voltage interrupt) | spec §11.1; FW | High (code), not observed directly |

## Open items

1. Exciter / Big Bottom subfield 3: what the byte controls.
2. Report `0x06`: which quantity each sub returns.
3. Param `0x05` and monitoring default: quick hardware checks.
4. Optional confirmation capture of a few Compressor Attack / Gain / Threshold values (the old fits disagreed with
   the tables; the tables are what Central sends).
