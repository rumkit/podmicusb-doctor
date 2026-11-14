# PodMic USB — Parameter Encoding Matrix

Canonical inventory of every known configurable parameter, its wire encoding, the **encoding dialect**, a **confidence** rating, and a **recheck** flag. Compiled 2026-07-10 from the capture set + the DSP audit. Wire format details: [`podmic-usb-hid-spec.md`](podmic-usb-hid-spec.md).

## Encoding dialects

The protocol uses three encoding styles:

- **Physical** — exact textbook DSP constants; the wire value *is* a real DSP quantity (linear amplitude, sample-count ramp step, dB window). All Noise Gate fields.
- **Voiced** — hand-tuned tapers with non-clean exponents; no closed-form identity. Consistent with APHEX's ear-voiced design (the whole on-board DSP is "powered by APHEX"; Exciter + Big Bottom are definitively APHEX). For these, use a measured curve + interpolation — the exponents are tuning artifacts, not constants to recover.
- **Linear/index/toggle** — simple UI-domain byte or boolean.

Confidence: **High** = many points / exact fit / verified against raw; **Medium** = solved but sparse or provisional; **Low** = ≤2–3 points or structural-only.

## Numeric parameters (HID Report ID 4 — `[target, 0x02, subfield, …payload…]`)

| Block | Parameter | Subfield | Field (offset, width) | Dialect | Encoding | Confidence | Recheck |
|---|---|---|---|---|---|---|---|
| Compressor (t=0) | Ratio | 0x02 | off 3, 1 byte | Linear | `1.5 + byte/255·3.0` (1.5–4.5:1) | High | No |
| Compressor | Threshold | 0x01 | off 5–6, 16-bit LE | Voiced (cubic) | `raw = round(0.147·(−dB)³)` (0…−60 dB) | Medium | **Yes** — cubic is a UI value, not a physical amplitude; verify vs on-device amp / width |
| Compressor | Attack | 0x03 | off 3–6, 32-bit LE | Voiced (power) | `raw = 1.897e6·t_ms^{5/3}` (0.1–10 ms) | Medium | Partial — 3 pts; more points to firm exponent |
| Compressor | Release | 0x04 | off 3–6, 32-bit LE | Voiced (inv-power) | `raw = 1.309e7 / t_ms^{1.2414}` (5–200 ms) | Low | **Yes** — 2 pts, provisional; needs 10–20-click sweep |
| Compressor | Gain (makeup) | 0x05 | off 3–6, 32-bit LE | Voiced (power) | `raw = 9.15e7·dB^{1.394}` (0–9 dB) | High | No (20-pt validated) |
| Noise Gate (t=1) | Threshold | 0x01 | off 3–6, 32-bit LE | **Physical** | `raw = INT32_MAX·10^{dB/20}` (dBFS amp; 0…−60 dB) | High | No |
| Noise Gate | Attack | 0x02 | off 3–6, 32-bit LE | **Physical** (ramp) | `raw = INT32_MAX/(9600·t_s)` (0.1–1000 ms) | High | **Yes** — ÷9600 = fs/5 anomaly vs hold/release; confirm decimated control rate |
| Noise Gate | Hold | 0x03 | off 3–6, 32-bit LE | **Physical** (ramp) | `raw = INT32_MAX/(48000·t_s)` = full-scale ramp step (0.05–2.0 s) | High | No |
| Noise Gate | Release | 0x04 | off 3–6, 32-bit LE | **Physical** (ramp) | `raw = INT32_MAX/(48000·t_s)` (0.05–2.0 s) | High | No |
| Noise Gate | Range | 0x05 | off 3–6, 32-bit LE | **Physical** | `raw = INT32_MAX·10^{dB/20}` (0…−100 dB) | High | No |
| Noise Gate | Hysteresis | 0x06 | off 3–6, 32-bit LE | **Physical** (dB window) | `raw = INT32_MAX·10^{−(1+0.07·pct)/20}` = 1–8 dB window | High | No |
| Aural Exciter (t=2) | Harmonics | 0x01 | fine32 off 3–6 + coarse byte off 7 | Voiced (pw cubic) | `y=fine32/0x7FFFFFFF`; pw cubic/^3.5 in coarse byte | Medium | Partial — needs coarse 60–150 sampling |
| Aural Exciter | Tune | 0x02 | A off 3–6, B off 7–10 (Q24), W byte off 11 | Voiced (coeffs) | A, B = `floor(W·2²⁴/7)` with piecewise-linear W(byte) (spec §9.9; A ≈ Butterworth HPF coeff `377·(1−cos ω₀)`); coarse byte `floor((f−600)·255/4400)` (600–5000 Hz) | High (A), Low (B) | **Yes** — B between bytes 107 and 169 provisional |
| Aural Exciter | subfield 0x03 | 0x03 | — | Unknown | connect enumerates idx 0–3; reads 0x1F=31 | — | **Yes** — purpose unknown |
| Big Bottom (t=3) | Drive | 0x01 | fine32 off 3–6 + coarse byte off 7 | Voiced (pw cubic) | Same curve as AE Harmonics (0–100 %) | Low | **Yes** — 3 pts, coarse 57–169 only |
| Big Bottom | Tune | 0x02 | off 3, 1 byte | Linear (LPF corner) | `≈60 + byte/255·252` (~60–312 Hz); payload confirmed single-byte | Medium | Partial — range endpoints not confirmed |

## Toggles, mode & simple commands

| Control | Transport | Encoding | Confidence | Recheck |
|---|---|---|---|---|
| Input Gain | **USB Audio Class** (not vendor HID) | Feature Unit 2 Volume, `SET_CUR`, signed 16-bit, 1/256 dB (`57 → 0x3900`) | High | No |
| High-Pass Filter (on/off) | Report ID 8 | `[0x01, 0x00, value, 0x00…]`; value 0x01 = ON, 0x00 = OFF (default after factory restore) | High | Partial — direction confirmed by an off→on capture; frequency param (if any) unknown |
| Processing Off↔Basic | Report ID 8 | param `0x0d` (0x00=Off, 0x01=Basic) | Medium | Yes — capture Advanced→Off |
| Processing Basic↔Advanced | Report ID 8 | param `0x0b`, value 0x01 (modelled as a separate bool from 0x0d) | Medium | Yes — two bools vs 3-way not confirmed; capture Advanced→Off |
| Advanced block enables (Comp/NG/AE/BB) | Report ID 4 | `[target, 0x02 (SET), 0x00 (enable subfield), value]`, target 0x00–0x03; value 0x00 = off (captured), 0x01 = on (inferred) | Medium | Yes — capture off→on |
| Basic: Depth / Sparkle / Punch | Report ID 8 (`0x0c`) + Report 4 | `[0x0c,0x00,idx,pct]` idx 0=Depth/1=Sparkle/2=Punch; also writes Advanced engine (Sparkle→AE Harmonics, Depth→BB Drive, Punch→Comp thr+gain & NG enable+thr+range) | High | No |
| Firmware update / Factory Reset | Bootloader (PID 0x004B) | See [`bootloader-protocol.md`](bootloader-protocol.md) | High | No |

## Open items ranked by value

1. **AE Tune B** — one fine sweep 1400–2200 Hz + a hold-Tune/sweep-Harmonics control capture (isolates B, recalibrates top).
2. **Compressor Release** — 10–20-click sweep 5–200 ms.
3. **Compressor Threshold** — confirm whether the 16-bit cubic is a display value vs the DSP amplitude.
4. **NG Attack ÷5** — confirm decimated control-rate hypothesis.
5. **BB Drive** — ~8-click sweep incl. <20 % and >80 %.
6. **AE subfield 0x03** and the **processing-mode / block-enable** direction captures.
