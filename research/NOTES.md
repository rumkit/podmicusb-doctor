> **About this file.** This is the raw, chronological lab notebook of the investigation (sessions 2a–4c,
> 2026-11-04 → 2026-11-19), kept as written, including hypotheses that later turned out wrong (corrections are noted
> in later sessions). For the summary read [`README.md`](README.md). File paths mentioned here refer to the original
> working folder, not to this repository: `flasher/*` → `firmware/tools/`, `firmware/release/rel_v1.s` → `firmware/patches/i2cfix.s`,
> `rel_v2.s` → `i2cfix-volume-holdmute.s`, `firmware/diag/diag.s` → `diag.s`; image `wdt13s_diag` → `diag`, release images rel1/rel2 → `i2cfix` /
> `i2cfix-volume-holdmute`, image builders → `firmware/tools/build.py`. The test images
> (wdt6s/wdt13s, fix1, fix2, fix2p) are not reproducible from this repository; their add-on code is a superset of
> `i2cfix-volume-holdmute.s` + `diag.s`. The unit's USB serial number is redacted.

# PodMic USB fw 1.1.9 — analysis notes (2026-11-04)

**Load base is 0x08004000, not 0x08000000** (16 KB bootloader precedes the app). Determined from the vector table:
default handler 0x08007d7d -> `b .` at file offset 0x3d7c; 14/19 distinct handlers land on prologues only at this base;
reset 0x08007d31 = proper startup (SP=0x20002000, .data copy from 0x0800eb10 to 0x20000000–0x20000948, .bss to 0x20001aa8).
Rizin: `rizin -a arm -b 16 -m 0x08004000 -e asm.cpu=cortex firmware/podmic_usb_v1.1.9.bin`. Addresses below are VAs at this base.

MCU (unconfirmed): Cortex-M3/M4 (MemManage/BusFault/UsageFault vectors), 8 KB SRAM, STM32F0/F3-style map
(RCC 0x40021000, GPIOA/B 0x48000000/0x48000400, DMA 0x40020000, TIM6 0x40001000, IWDG 0x40003000). Clock constant 96,000,000
passed at init — above STM32F3's 72 MHz, so possibly a clone (GD32/AT32/APM32). Determines LSI freq -> watchdog timeout.

## Watchdog (IWDG)
- 0x08004400 refresh (KR=0xAAAA), 0x08004410 start (0xCCCC), 0x08004420 init(reload=r0, prescaler=r1).
- 0x08009198 (called from main init 0x08007f00): init(reload 0xFFE, prescaler 2 = /16) + start.
  Timeout = 16*4095/f_LSI: 1.64 s @40 kHz, 2.05 s @32 kHz. Capture: hang onset between 30.04 s (last answered poll) and
  30.535 s (unanswered), drop at 31.85 s -> 1.31–1.81 s. Consistent with ~40 kHz LSI.
- Refreshed only by task 0x08009b70 (refresh + read GPIOA pin 10, debounce state 0x20000d7c..), registered in 0x08009bac via
  0x0800c248 with period 4 ms.

## Scheduler (cooperative)
- 16 slots × 20 B at 0x2000195c: +4 reload, +8 countdown, +0xc priority, +0xd state (0xfe=free), +0x10 callback.
- SysTick 0x0800c390: tick counters 0x20001a9c/0x20001aa0, then 0x0800c2f8 just decrements countdowns (no callbacks in ISR).
- Dispatcher 0x0800c174 runs one due task (highest prio) per call, from the main loop.
- Main: init 0x08007ef8; loop 0x08007f76: 0x0800c174 (scheduler), 0x08008ee0, 0x080087bc, 0x08009c4c — forever.
- => **Any main-loop function or task that blocks > ~1.6 s stops the watchdog kick -> reset.** USB ISR keeps running meanwhile,
  matching the capture (iso streams continue, HID reply never produced). Next: find blocking loops (unbounded `while` on
  flags/peripheral status) in 0x08008ee0 / 0x080087bc / 0x08009c4c and in tasks, esp. ones touching the playback buffer/feedback.
- Also registered: 500 ms timer (cb 0x0800b17c sets flag 0x2000187c).

## Session 2b (2026-11-04): hang candidates
MCU very likely **GD32F350x8** (USBFS DWC-compatible @0x50000000 IRQ67, I2C1 @0x40005800, FMC @0x40022000 1 KB pages,
8 KB SRAM, 96 MHz, IRC40K -> IWDG ~1.64 s nominal). Vector table ends at IRQ 67 (0x08004150 = FMC unlock code).

Ruled out as the stall (capture shows USB ISR still running during the 1.3 s): HardFault/NMI (-> immediate NVIC_SystemReset
0x08007e14), LVD IRQ1 0x080093f4 (cpsid i + spin), reboot cmd 0x08008670 (immediate reset). => thread-mode spin, IRQs on.

### Candidate 1 (best fit incl. no-RØDE-Central case): blocking I2C with no timeout
- I2C1 interrupt-driven master, state 0x200000dc ([0] 0=busy 1=done 2=error, +0x1c async callback).
  start 0x08008f24 (fails if cb pending / not done / STAT1.I2CBSY), done 0x08008f18, EV ISR 0x08008fa4 (IRQ24, prio 0xc0),
  ER ISR 0x08009128 (IRQ34, prio 0xd0, sets 2), cb runner 0x08008ee0 (main loop).
- Blocking users: `while(!start()); while(!done());` at 0x080091c0 (addr 0x10 read), 0x0800ad60 (fcn 0x0800ad0c = codec
  register write list, slave addr byte @0x20000314), 0x0800b0c2 (fcn 0x0800b094, HID cmd). Spins forever if bus stays BUSY
  or a transfer gets no EV/ER interrupt (e.g. SCL held / I2C-v1 event mishandled).
- **4 ms scheduler task 0x0800af08** (reg. in 0x0800ac84): PI loop 0x0800bf40 (integral 0x20001948, input 0x2000194c,
  gain -1000, clamp ±13744 around 0x083126e9 ≈ 2^32*0.032 → codec clock/ratio word, ±100 ppm) -> 0x0800b134 sets dirty
  0x20001855 if word changed -> blocking I2C write of 3 words (0x20000320). So I2C traffic rate while playback streams
  depends on playback-buffer jitter = host delivery pattern (OS-dependent!). Only active when the OUT stream runs.
- Error input written in OUT-packet handler (~0x0800bf8c..0x0800c0b0, USB ISR context): fill level r6 (bytes) vs target 576;
  feedback is bang-bang: fill>=576 -> 0x0bff48, <576 -> 0x0c00c7 (exactly the two values in the Windows capture);
  error = 576 - fill. Re-arms EP2 OUT (DOEPCTL2 0xb40) & EP3 IN feedback (DIEPCTL3 0x960) with even/odd frame bit.
- Also async I2C users 0x080067a8/0x080067d4/0x08006800 (callbacks; near iAP2 code -> probably Apple MFi auth CP).
- USB IRQ67 priority 0x70 > I2C EV 0xc0 / ER 0xd0: USB ISR (incl. sample processing 0x08005b34) preempts I2C servicing.

### Candidate 2 (only with RØDE Central polling): HID send busy-wait
- HID send 0x0800b2f8 spins (no timeout) on busy 0x200018cc (selector 0x20000348 init 1, cleared by first class request;
  if 1 waits on iAP flag 0x2000187d). 0x200018cc cleared only by EP1 IN XferCompl via class event cb 0x0800b280 (dir 1, ep 1).
- HID reports are only sent as replies to SET_REPORT (main-loop fn 0x080087bc, flag 0x200018a4). Report 8 sub 8 GET
  (poll `08 08 01`) = 0x0800859c -> returns RAM byte 0x200001c5, no I2C. Can't explain resets without Central.

### Other notes
- USB ISR 0x080050b0: incomplete-iso-IN (GINTSTS bit20) only cleared, EP not disabled/flushed (non-standard; EP3 feedback
  is skipped by host 31/32 frames).
- Main-loop fns: 0x0800c174 scheduler, 0x08008ee0 I2C cb runner, 0x080087bc HID processor, 0x08009c4c gain encoder (PA2/PB3).
- Flash page erase 0x08009670 (settings) stalls all execution ~tens of ms (bounded).
- Baseline (Windows capture, EP 0x83 feedback samples every 32 ms): 995 samples, 74% = 0x0bff48 (fill >= 576 B, i.e. buffer
  above target), 26% = 0x0c00c7; value flips on 21% of samples; common run lengths 3/9/2/4. Compare with Linux capture.

## I2C slave identified (2026-11-04, from user's board photos): Nuvoton NAU88L21 codec (marking "NAU88L21YG 205 GD 2204b0 32-N1")
- I2C 7-bit addr 0x1B (byte @0x20000314, .data init). Codec writes = 16-bit reg + 16-bit value (rev16 in 0x0800ad0c).
- 4 ms PI task writes regs 0x06=0x0008, 0x0A=0x0031, 0x0B=0x26E9 → FLL integer 8, 24-bit fraction 0x3126E9 → ratio 8.192
  (= 12.288 MHz / 1.5 MHz → FLL makes 256·fs MCLK from a 1.5 MHz ref, inferred). Layout matches Linux `nau8821` driver's
  FLL3/FLL7/FLL8 as recalled — verify against datasheet. So the device steers the codec's sample clock to the host's
  delivery rate (±100 ppm) *and* sends bang-bang feedback, both driven by the same buffer fill level.
- Codec is not involved in USB; USB (PHY + controller) is on the GD32 MCU.

## Session 2c (2026-11-04): USB ISR audit (0x080050b0)
- GINTEN at init (0x08004ac8..): 0x800c3810 = RXFNE, ESP(suspend), RST, ENUMF, IEPIF, OEPIF, WKUPIF. (IISOIXFR bit20 is NOT
  enabled; the ISR merely clears it if seen — correct earlier note.) DIEPINTEN = DOEPINTEN = 0x0b.
- Every enabled source is cleared on its path (IN: bits 0,1,3,6 + TXFE via DIEPFEINTEN 0x834; OUT: 0,1,3,6; suspend, reset,
  enum, wakeup cleared). ISR loops until GINTSTS&GINTEN == 0 — no stuck-flag path found => "ISR never exits" not supported
  by the flag logic (still possible only via HW misbehaviour).
- **RX FIFO is read via GRSTATR/GRSTATP (+0x1c peek, +0x20 pop), not the 0x1000 FIFO window, with a read-until-3-equal
  loop** (0x0800489c, 0x080051d0, audio cb 0x0800bdc4/0x0800be10). Looks like a workaround for unreliable FIFO reads.
- Audio OUT cb 0x0800bd70: ring {start 0x200011c4, end 0x20001644 (1152 B = 4 ms), wp, rp} @0x20000200; drains FIFO when not
  ready (0x0800be3e); no overrun check vs rp. Wrap with non-word-aligned wp (after 282/294-B packets) stores straddling
  word at start-k → 1–3 B written into end of the preceding ring 0x20000f80–0x200011c0 (576 B, likely mic capture) → minor
  mic-sample glitch, not a hang.
- Suspend path 0x0800562c (if [pdev+6] && SUSPSTS && configured): PCGCCTL|=3 then 0x08004838(1,0) = PMU deep-sleep + WFI
  **inside the USB ISR**; IWDG keeps running → a suspend not resumed within ~1.6 s ends in watchdog reset. Not this capture
  (stream was active), but relevant to Windows suspend behaviour.
- Still unchecked: EP0 control/setup path (0x08005a04, 0x080056f0), EP1-OUT special case 0x080050cc, wakeup clock restore.

## Session 2d (2026-11-04): RØDE Central update-decision logic (rode_bridge.dll, sha256 4b0c7d47…)
Decision fn `fcn.180142080` (= FUN_180142080). Three version sources:
- Device's CURRENT version: read from the mic over HID (via fcn.1801406d0/180140220 near entry).
- BUILT-IN version: `fcn.18013f5a0`, hardcoded per device type; PodMic (type 0x4a/0x4b) → 1.1.9. Bytes live in .rdata table
  around 0x181d5ce70+ (3 bytes each: major,minor,patch).
- MANIFEST version string: `update-version` (or `os-version`) from update.rode.com/rode-devices-manifest.json, parsed by
  `fcn.1801418c0` (splits on ".", 3 bytes).
Each packed to 24-bit (maj<<16|min<<8|patch) and compared; update is offered only when a reference version is strictly
GREATER than the device's current version. With stock bits everything is 1.1.9 == device 1.1.9 → never offered.
Firmware image for the flash is the one bundled in .rdata (VA 0x181b35710), selected by device type → manifest key
(`fcn.1801419f0` → "podmic-usb-manifest"); integrity is checked by `firmware-md5` from the manifest (md5 only; no signature).

### To offer our diagnostic image, two routes (NOT yet done):
1. Local patch of a COPY of rode_bridge.dll: (a) bump built-in PodMic bytes (0x181d5ce70-region) to e.g. 1.2.0 so the
   device (1.1.9) looks outdated; (b) replace the bundled image at 0x181b35710 with firmware/patched/..._wdt6s.bin
   (same 47104 B); (c) fix any md5 the code checks against the embedded image (find where firmware-md5 is compared — if it
   compares against the manifest's value, offline/local path may skip it; TBD). Then point Central at the patched DLL copy.
2. Can't easily MITM update.rode.com (TLS). Local-DLL route is cleaner.
STILL TBD before attempting: (i) exact md5/length checks on the image before flashing; (ii) the device bootloader's own
acceptance checks (we only know the 47104-B image carries a trailing CRC32, already handled in the patched builds);
(iii) confirm the HID flash protocol writes app region only, leaving the 16 KB bootloader intact (recovery path).

## Session 2e (2026-11-04): HOW THE MIC REPORTS ITS VERSION — corrected (from prior host-PC session's protocol analysis)
RØDE Central reads the mic's firmware version from the **USB Device Descriptor `bcdDevice` field** via the standard
GET_DESCRIPTOR(DEVICE) at plug-in. NO vendor/HID "get version" command exists (connect capture: only HID SET_REPORT on
report IDs 4 & 8 + SET_IDLE). Decoding: one hex digit per version part, drop leading zero → `0x0119` = "1.1.9".
(Strict BCD would be 01.19, so it's hex-nibble-per-part. Serial string on this unit: "<redacted>".)
Update decision then = compare device bcdDevice vs built-in (DLL 0x181d5ce84 = 1.1.9) / manifest `update-version`; offer when
reference > device. Equal (1.1.9==1.1.9) → never offered. => I was wrong earlier to doubt the device reports it; it does,
via bcdDevice.

Encoding consequences for the diagnostic plan:
- Report 1.1.1 → bcdDevice 0x0111. Report 1.2.0 → 0x0120 (TRAP: a future 1.1.10 = 0x011A < 0x0120 → never offered → stuck).
- 1.1.1 (0x0111) is future-safe: a real 1.1.10 (0x011A) > 0x0111 → still offered. But stock Central would see 0x0111 < 0x0119
  and nag to "update" back to 1.1.9 (harmless; decline). Keeping 0x0119 avoids the nag but gives no "diagnostic build" marker.

OPEN: the app-image Device Descriptor template at file 0xaf94 (VA 0x800ef94) has bcdDevice=0x0100 AND idProduct=0x0000 —
i.e. PLACEHOLDERS patched at runtime. So the real 0x0119/PID come from elsewhere (bootloader 0x08000000-0x08003fff NOT in
our image, or a flash config page). MUST locate where bcdDevice is injected before we can know if it's patchable in the
flashable app region. If it lives in the bootloader/config page, we cannot change the reported version by flashing the app.

## Session 2f (2026-11-04): version source + flash-confirmation marker
- Flasher does NOT get version from image content: embedded PodMic image is RAW/headerless (no version field). Version string
  for the update decision/display comes from manifest `update-version` and/or resource filename "Application-Podmic_v1.1.9.bin"
  / DLL built-in table (0x181d5ce84 = 1.1.9).
- Mic's reported version = bcdDevice, stored in bootloader/config flash (0x0800f800+ / bootloader 0x08000000-3fff), NOT in the
  app image → flashing the app can't change it; a real version bump must be written to config by the DFU/flasher step (exact
  mechanism = still-unknown flash protocol). So a custom version marker is NOT achievable by app-patching alone.
- CONFIRMATION WITHOUT VERSION: USB string descriptors ARE in the flashable app image (unlike the serial, which is injected):
  manufacturer "RØDE" @VA 0x800eaae, product "RØDE PodMic USB" @VA 0x800eaba (hdr 0x20,0x03; 15 chars UTF-16LE). A same-length
  in-place char edit (e.g. one letter of "PodMic") → visible in Windows Device Manager / RØDE Central = positive proof our
  image runs. No touch to version/config/bootloader. (Marker is moot until the flash path/bootloader-preservation/md5 are
  resolved — still the gating unknown.)
- FirmwareUpdater class hierarchy in DLL: FirmwareUpdater(base), Default/Generic/Wireless/WMicro/WMe/WPro/Ota/QccHid/IntPro…
  PodMic (wired raw image) → Default/GenericFirmwareUpdater. Trace these next for the actual flash/DFU byte protocol.

## Session 2g (2026-11-04): flash routine — attempted, partial; architecture clarified
- Mic side: NO self-flash. HID update path in fcn.080087bc ends by soft-disconnecting USB (DCTL 0x50000804 |= SDIS) then
  plain AIRCR system reset (0x08008670). No backup-register "stay in bootloader" magic seen in the app → the separate
  bootloader (0x08000000-0x08003fff, NOT in our image) decides DFU-vs-app and performs the actual flashing.
- Serial source confirmed: chip unique-ID reg 0x1FFFF7AC (not flash) → silicon-derived.
- DLL side: embedded PodMic image at 0x181b35710 exposed via JUCE-style resource getter (0x18001bb80, caches ptr to
  0x181f18d38). firmware-md5 string at 0x181d5d1b0. FirmwareUpdater class tree present. The actual chunked write/erase/verify
  command construction + target address range is deep in C++ virtual dispatch; not recovered in bounded effort.
- CONFIDENCE CEILING: even a full DLL trace shows only what the HOST sends; the bootloader's acceptance checks and whether it
  preserves ITSELF cannot be proven from the files we have (no bootloader image). Bootloader+app split (app@0x08004000, 16 KB
  reserved) strongly implies the bootloader flashes only the app region and preserves itself (standard design), but this is
  INFERENCE, not proof.
- RECOMMENDATION: before any patched flash, capture a REAL flash of the STOCK 1.1.9 image (non-destructive — same official
  firmware) via a minimal forced-"update available" decision patch. The USB capture reveals the exact DFU protocol, target
  address range, and acceptance checks directly — higher confidence than static RE of a 32 MB stripped DLL.

## Session 3a (2026-11-11): FACTORY RESTORE capture → flash/DFU protocol RECOVERED
Source: `factory_reset_end_with_high_pass_on.pcapng` (user, Win11 26H2 build 26300, i9-13980HX; sha256 5b410358…d270, 14k pkts,
mostly other devices). PodMic-only extract: `podmic_factory_reset_filtered.pcapng` (filter `usb.bus_id==1 && usb.device_address
in {12,13,14}`, 1046 pkts, sha256 a6d867ea…2de7). Other bus-1 devices (Logitech cam/receiver, Realtek hub, Novation, etc.) dropped.

**Factory restore = FULL app reflash of stock 1.1.9.** Reassembled written data (46×1024 B) is BYTE-IDENTICAL to
`podmic_usb_v1.1.9.bin` (sha256 11703ba4…8d4c). So the embedded/official image we have is exactly what goes on the chip.

Timeline: addr 12 (app, PID 0x004a, bcd 0x0119) → 17.18 s `R` → drops 17.25 s → addr 13 **bootloader** (PID 0x004b, bcd 0x0100,
product "RØDE PodMic Bootloader", serial "<redacted>", one HID iface, EP 0x81 int IN 16 B) at 17.62 s → flash 18.68–22.57 s
(~3.9 s) → `R` → addr 14 (app again, PID 0x004a, **bcd 0x0119 unchanged**, product "RØDE PodMic USB") at 24.0 s.

Bootloader HID report descriptor (39 B, usage page 0xFF00): report 1 IN 9 B; report 2 OUT 9 B; report 3 OUT 1024 B.
All host→dev via SET_REPORT on EP0 (class/interface); replies on int IN EP 0x81 as report 1. Reply byte 2 = 0x41 'A' (ack).
| Cmd (report 2, 9 B) | Meaning | Reply (report 1) |
|---|---|---|
| `02 52 00…` 'R' | reboot. Sent to the APP (enter bootloader) and to the BOOTLOADER (run app). No reply; device drops ~10–75 ms later. Same report ID/format in app → this is the app's 0x080087bc→0x08008670 path. |
| `02 49 00…` 'I' | info | `01 49 41 01 00 00 04 30 00 00` → likely u16 LE: 0x0001 (bl ver?), **0x0400 page size**, **0x0030 = 48 pages** (48 KB = 0x08004000–0x0800FFFF on a 64 KB GD32F350x8) |
| `02 45 NN 00…` 'E' | erase app page NN (index, NOT address) | `01 45 41 00…` (~20–30 ms) |
| `03 <1024 B>` | write 1024 B to the page just erased | `01 50 41 00…` 'P' (~30–48 ms) |
Sequence: I, then for NN=0x00..0x2d: E NN, W(page NN); then R. Strictly sequential, one outstanding command.
- **No absolute addresses, no MD5/CRC/length, no read-back verify, no version/bcdDevice/config write** on the wire.
  firmware-md5 is host-side only (download integrity). The in-image CRC32 (see patched/README) is presumably checked by the
  bootloader at boot — unverified.
- Page index 0 = 0x08004000 (data starts with the app vector table: SP 0x20002000, reset 0x08007d31). The host can only name
  pages 0..47 of the app region ⇒ the bootloader (0x08000000–3fff) and the config/version area are not targeted by this
  protocol. (Bootloader's own bounds checking still not visible, but the stock tool never sends anything that could hit it.)
- bcdDevice 0x0119 before AND after; bootloader reports its own 0x0100 ⇒ confirms 2e/2f: version lives outside the app image.

App-side settings around the restore (app HID iface, report 4/3 = 28-B param block, report 8/7 = 27-B single param):
- Before (2.76–3.21 s): Central READS report-4 params `04 GG 03 II` (group GG 0..3, index II) → `03 GG 41 <value>`, and report-8
  params `08 PP 01` → `07 PP 41 <val>` for PP = 0c(×3 sub), 0b, 0d, 08, 03, 04, 01.
- After (28.08 s): `08 01 00 00` (set param 1 = 0) then WRITES report-4 params back `04 GG 02 II <value>` with the SAME values
  read before (e.g. group1 idx3 = 40a70d00) ⇒ per-unit/calibration-like block is preserved across a factory restore.
- 42.55 s `08 01 00 01` = user turning **high-pass ON** (capture name) ⇒ report-8 param 0x01 = high-pass filter; `08 PP 01` = get,
  `08 PP 00 VV` = set. Poll `08 08 01` every 0.5 s = get param 8 (same as the disconnect capture).
- 37.9 s SET_INTERFACE + SET_CUR sample rate `80 bb 00` (48 kHz) = an audio stream being opened.

### Implications for the diagnostic (watchdog-lengthened) flash
- The forced-decision `rode_bridge.dll` patch is NO LONGER NEEDED: the protocol is simple enough to drive directly from a small
  host script (hidapi/pywinusb on Windows): app `R` → wait for VID 19F7 PID 004B → `I` → 46×(E, W) → `R`. Image size 47,104 B =
  46 pages exactly (patched images are the same size).
- Recovery: a factory restore in Central reflashes stock 1.1.9 via this same path (proven by this capture). Residual risk = if a
  patched app ran but could no longer process the HID `R` command AND the bootloader doesn't fall back on its own; our patches
  only change the IWDG reload, so the HID path is untouched. Whether the bootloader can be entered without the app (e.g. on CRC
  fail / button-held power-on) is still unknown — worth testing what Central does with a mic stuck in PID 0x004B.

## Session 3b (2026-11-11): VERSION IS IN THE APP IMAGE — corrects 2e/2f/3a
User's argument (no version on the wire in the 3a flash ⇒ the mic can only get it from the image) was right.
- **App vector table reserved slot 8 (file 0x20, VA 0x08004020) = `19 01 00 00` = 0x0119 = v1.1.9.** Not used by the CPU.
- USB init (fn containing 0x0800889c): `r5=0xE000ED00` (SCB); `ldr r3,[r5,#8]` = **VTOR** (=0x08004000); `ldrh r1,[r3,#0x20]`
  → formats "maj.min.patch" (maj = v>>8, min = (v>>4)&0xF, patch = v&0xF; decimal via 0x08008838) into RAM 0x20000d10, then
  `strh r3,[r8,#0xc]` @0x0800897e writes it to **device-descriptor bcdDevice** (r8 = RAM desc 0x20000484).
- Same fn: `movs r3,#0x4a; strb [r8,#0xa]` @0x080089b8 → **idProduct 0x004A is hardcoded in the app**. Serial = UID 0x1FFFF7AC
  (hex-formatted, 0x08008b42…). So the "placeholders" at 0x800ef94 (bcd 0x0100 / PID 0) are just a template patched at init.
- ⇒ A version marker IS possible by app-patching: set file 0x20 to e.g. `11 01` (reports 1.1.1) + recompute the trailing CRC32.
  Lets the stock "built-in > device" logic (1.1.9 > 1.1.1) offer the restore back to stock — and the device-version check confirms
  our image runs. Caveat: nibble encoding (minor/patch max 15).
- Unexplained literal 0x08003FE0 (bootloader's last 32 B) at 0x08008bcc — some app code reads bootloader-area data; not traced.

## Session 3c (2026-11-11): user's protocol docs (`protocol/`) + HIDDEN app service commands (report 2)
`protocol/podmic-usb-hid-spec.md` + `PARAMETER-MATRIX.md` (user's earlier RE, 2026-07): DSP control over HID reports 4/3 and
8/7, gain via UAC FU2. They list reports 1/2/5/6 as "never used". Firmware says otherwise.
App HID processor 0x080087bc: switch on report ID (tbb @0x080087f8): **2→0x08008684**, 4→0x08008058, **6→0x08008434**, 8→0x080084c4;
3/5/7 ignored. Reply buffer 0x20000cd4 (+6 report id, +7 echo, +8 0x41 'A' / 0x4E 'N'), sent via 0x0800b2f8; reply on report 1.
**Report 2 = single-letter service commands** `02 <cmd> <args…>` (cmd 'B'..'V', tbb @0x08008696; unknown → 'N' NAK):
| cmd | handler | effect |
|---|---|---|
| 'B' | 0x080086c0 | reply byte9 = 0 (ping?) |
| 'E' | 0x080086c6 | arg==1 → returns byte @0x20001695 (+0xF1 @+0xE); purpose unknown |
| **'G' reg** | 0x080086de | **I2C READ of codec (NAU88L21) register** via 0x0800b094 (the blocking-I2C hang candidate). Reply: reg, value16 (rev16) |
| **'S' reg val16** | 0x08008720 | **I2C WRITE codec register** via 0x0800ad0c |
| 'T' | 0x08008746 | sets flag 0x20000ccc (=skip settings save), disables DSP blocks (0x0800a3f4/a484/a44c/a824/a380 with 0), returns 0x0800921c() — factory test mode? |
| **'V'** | 0x08008772 | **returns u16 @VTOR+0x20 = firmware version** (0x0119). A real get-version cmd; Central doesn't use it |
| **'L'** | 0x0800870a→0x080093f4 | reply, 100 ms delay, `cpsid i`, GPIOs/codec (0x0800ace0) off, clocks off, save settings to flash (0x080097d8, FMC 0x40022000, 0x53 B) unless 'T' flag, then **infinite loop ⇒ IWDG reset**. "Power-off/halt" |
| 'R' | 0x08008788 | 0x08008c40, reply, save settings (0x080097d8), USB soft-disconnect, reset → bootloader (see 3a) |
Report 6 (→0x08008434) also has a handler (sub 0/1, calls 0x080099c8/0x0800abf0, returns ~12 bytes) — not decoded.
Bootloader reuses report 2 with 'I'/'E'/'R' (different meanings: there 'E' = erase page).

### Why it matters
- **'L' = built-in, non-destructive WATCHDOG STOPWATCH**: it deliberately hangs the CPU, so the time from 'L' to re-enumeration ≈
  IWDG period. On stock this should read ≈1.6 s (check against the 1.3 s silent gap in the disconnect capture); on `wdt13s`
  ≈13 s ⇒ direct proof the patch is running, besides the version marker. Side effect: it saves settings (harmless) and the mic
  reboots. (IRQs are off, so unlike the field hang the USB stream also stops — fine for timing.)
- **'G'/'S' = live codec register access without opening the mic.** Can dump NAU88L21 clock/PLL/FLL registers during playback on
  Windows vs Linux, and see what the 4 ms PI loop writes. Caveat: they use the same no-timeout I2C path, so they could in theory
  trigger the same hang — read-only 'G' first, and only while not streaming at first.
- 'V' confirms 3b independently (version = VTOR+0x20).

### Spec vs captures — discrepancies
- **HPF direction**: spec §5.1 says report-8 param 0x01 value 0x00 = filter ON, 0x01 = bypassed. In the 3a capture the factory
  restore wrote 0x00, and the user's later action (file name "…end_with_high_pass_on") wrote **0x01** ⇒ suggests 0x01 = ON, spec
  inverted. PARAMETER-MATRIX already flags this "direction unconfirmed". Needs user confirmation.
- Status poll (report 8 param 0x08): spec says every ~1.15 s; both of our captures show **every 0.5 s**.
- Report-4 values written back after the factory restore = the values read before (e.g. NG hold 0x000da740 = 0.05 s, NG release
  0x000369d0 = 0.2 s by spec §9.7) ⇒ either the mic was already at defaults, or Central restores the user's DSP settings.

## Session 3d (2026-11-11): HPF direction confirmed; codec access dropped; focus = stream delivery
- **HPF CONFIRMED by user**: they turned high-pass ON manually after the restore → write `08 01 00 01` ⇒ report-8 param 0x01:
  **0x01 = HPF ON, 0x00 = OFF** (factory default after restore = OFF). `protocol/podmic-usb-hid-spec.md` §5.1 is inverted.
- User decision: do NOT use 'G'/'S' codec register access (no codec poking).
- User's point, confirmed in the disconnect capture: before the drop (addr 10) there are ZERO standard UAC control requests
  (no SET_CUR volume/mute/rate, no SET_INTERFACE) — only Central's HID polls (and resets happen without Central too). So in the
  hang window the only host input is the iso stream itself (282/288/294-B OUT packets in 10 ms URBs, 0x83 feedback, Windows'
  per-completion pipe reset on 0x83). OS difference ⇒ stream delivery/timing, not configuration.
- Next: Linux usbmon capture of the same scenario (monitoring as system output, few minutes) to diff vs Windows baseline;
  optionally a Windows capture that includes stream START (alt-setting / SET_CUR rate / volume). 'L' watchdog timing test optional.

## Session 3e (2026-11-11): flasher script `flasher/podmic_flash.py`
- Settings flash = **0x0800F800 / 0x0800FC00 = app-region pages 46–47** (refs at 0x08009446/0x0800988a/0x08009890). Bootloader
  reports 48 pages, so a naive flasher could wipe settings; the stock image is exactly 46 pages. Script caps at 46.
- Script: Windows, stdlib ctypes HID (no hidapi), subcommands check/info/probe/flash. Offline test reproduces the captured
  94-report bootloader stream byte-for-byte. Windows layer compiled only, not yet run. Plan: `probe` (ideally with USBPcap
  running to confirm wire lengths 10/1025), then flash STOCK 1.1.9, then wdt13s (+ optional 1.1.1 marker at file 0x20 + CRC).

## Session 3f (2026-11-11): stock flash OK via script; patched build ready
- User flashed STOCK 1.1.9 with `flasher/podmic_flash.py` — success (script verified on real hardware).
- Built `patched/podmic_usb_v1.1.9_wdt13s_v111.bin` (wdt13s + version 1.1.1 marker + CRC). Next: user flashes it, reproduces
  with Central open (HID poll = hang marker) + USBPcap. Read-out: no dropouts ⇒ transient stall; poll silent ~9–17 s then drop ⇒
  permanent hang confirmed; drop still ~1.3–1.6 s after poll stops ⇒ not the IWDG.

## Session 3g (2026-11-11): patched image REJECTED by bootloader; recovery PROVEN
- Flashing `patched/podmic_usb_v1.1.9_wdt13s_v111.bin` (diff vs stock: 0x20 version 1.1.1, 0x519a IWDG prescaler, CRC32):
  pages 1–45 ACKed normally; the **last page (index 45, holds CRC32) SET_REPORT never completed → Windows error 121
  (ERROR_SEM_TIMEOUT)**. Stock restore capture: same page completes in 2.6 ms, ACK in 32 ms ⇒ bootloader validates the whole
  image when the last page arrives, and on failure it WEDGES (still enumerated as PID 0x004B, accepts SET_REPORT, never replies
  — 'I' timed out).
- Recovery: power cycle → bootloader came up (app invalid ⇒ it stays in bootloader: boot-time validation exists) → RØDE Central
  factory restore succeeded. **Incomplete/rejected flash is recoverable without SWD.**
- Page 45 content: last 92 B of the .data init image (0x0800eb10+0x948 ends at 0x0800F458) + FF padding + CRC32.
- OPEN: what the last-page check is. H1 version check (anti-downgrade: 1.1.1 < installed/min 1.1.9) — most likely.
  H2 integrity check beyond our CRC32 (signature/hash) — any modification fails. Discriminating test (code identical to stock):
  stock + one byte changed in the FF padding (file 0xb780–0xb7fb) + CRC fixed, version still 1.1.9. Passes ⇒ H1 (then wdt13s
  with version 1.1.9, or a HIGHER marker e.g. 1.2.0 = 0x0120, should flash). Fails ⇒ H2.
- Script: `--skip-info` added (recovery attempt when already in bootloader); failure message now points to power cycle +
  Central restore.

## Session 3h (2026-11-11): wdt13s (version 1.1.9) FLASHED OK — bootloader = version check, no signature
- `patched/podmic_usb_v1.1.9_wdt13s.bin` (diff: 0x519a + CRC) flashed: all 46 pages ACKed in 3.4 s, mic back as 1.1.9.
  ⇒ No signature/hash beyond CRC32. The 3g rejection was a **version check: lower than 1.1.9 rejected, equal accepted**.
  Unknown whether it compares to the installed version or a fixed minimum (both 1.1.9 here). Where the bootloader gets the
  "installed" version is unknown (page 0 was already erased by then — maybe a copy in bootloader flash, cf. the app's
  reference to 0x08003FE0).
- **RULE: never flash a HIGHER version marker** — under anti-downgrade, stock 1.1.9 would then be rejected and neither the
  script nor Central could restore it. Script refuses only an image version LOWER than the mic's reported bcdDevice; warns if higher or unknown (user request).
- MIC NOW RUNS wdt13s (IWDG ≈13 s, range 8.7–17.5). Next: reproduce dropout with Central open (0.5 s HID poll = hang
  marker) + USBPcap.

## Session 3i (2026-11-11): older RØDE Central 2.0.65 installer — same PodMic 1.1.9
- `Rode Central Dist/RODE Central (2.0.65).msi` (sha256 7a7978b4…1c91). Unpacked with a stdlib MSZIP/CAB extractor (no tools
  installed): 2 cabinets → 4 fonts + one 80 MB `RODEConnect.exe` (older single-exe JUCE app, classes `PodMicUsb`,
  `PodMicUsbSettings`; no Flutter/rode_bridge.dll split yet).
- PodMic image at exe file offset 0x2c34da0, resource name `Application-Podmic_v1.1.9.bin`: **byte-identical to our 1.1.9**
  (sha256 11703ba4…8d4c). No other PodMic version inside. Other CRC-valid app images in the exe: Charge Case+ 1.0.2 (23,552 B),
  "Rode Hub" 1.0.1 (11,264 B), Wireless PRO Charge Case 1.0.2 (23,552 B).
- ⇒ No older PodMic firmware available from this source; and an older one would be rejected by the bootloader anyway
  (anti-downgrade, 3g) unless the installed version were lower.

## Session 3j (2026-11-11): RØDE Central Mobile 1.0.46 APK — no PodMic firmware
- `Rode Central Dist/RØDE+Central+Mobile_1.0.46_APKPure.apk` (sha256 8d5319c2…d649). Scanned every entry incl. nested zips and
  native libs for the PodMic USB string descriptor / CRC-valid app images: **zero hits**. CORRECTION (3k): `libjuce_jni.so`
  (all 4 ABIs) lists bundled-asset names incl. **`Application-Podmic_v1.1.7.bin`** — the only listed asset ABSENT from `assets/`
  (build or APKPure repack omitted it). So PodMic 1.1.7 existed; image not in this APK.
- Bundled instead: AI_Micro 1.0.5/1.2.1, NT-USBplus 1.0.7/1.0.8, **VMGOII (VideoMic GO II) 1.0.3/1.1.8** (≠ PodMic 1.1.8),
  WGo2/WGo2.5/WGo2+/WMe/M2GO packages, Fred.bin, Pebbles.bin.
- Still wanted for STATIC diffing (not flashable — anti-downgrade): PodMic 1.1.4 / 1.1.8. Sources to try: older desktop
  installers (< 2.0.65), or old `firmware-URL`s on update.rode.com (not fetched; ask user first).
- RØDE Central Mobile **2.0.94** XAPK (sha256 d404c577…): Flutter + `librode_bridge.so` (arm64). Bridge only REFERENCES
  `Application-Podmic_v1.1.9.bin` (name, PodMicUsb class) — image NOT embedded (no string-descriptor / app-image hits anywhere).
  Other names referenced: AI_Micro 1.2.2, NT-USBplus 1.0.9, NT1_5th_Gen **1.1.4**, XCM50 1.0.9, ChgcPlus/WpChgc 1.0.2.
  ⇒ So far every RØDE Central build (desktop 2.0.65, current desktop, mobile 2.0.94) names/ships PodMic **1.1.9** only.
  NB "1.1.4"/"1.1.8" seen in these packages belong to NT1 5th Gen / VideoMic GO II, not PodMic.

## Session 3k (2026-11-11): macOS RØDE Central 2.0.21 → **PodMic firmware 1.1.0** found
- `Rode Central Dist/RØDE Central Installer (2.0.21).pkg` (xar → pbzx/cpio, unpacked with stdlib). Universal Mach-O
  `RODE Central.app/Contents/MacOS/RODE Central` embeds `Application-Podmic_v1.1.0.bin` twice (one per arch slice, identical;
  file offsets 0x112d9c0 / 0x353b96e — byte-unaligned, which is why the first scan missed it).
- Saved as `firmware/podmic_usb_v1.1.0.bin`: **43,008 B (42 pages)**, trailing CRC32 valid, version slot 0x20 = `10 01` (1.1.0),
  SP 0x20002000, product string descriptor at image+0x9bf6. sha256 df9c99598029373edd63286499d8869e051e78c03509d50c0bd8521e51a5525d.
- Spot checks vs 1.1.9: IWDG init byte sequence (`movs r1,#2; movw r0,#0xffe` = /16, reload 0xFFE ≈1.6 s) identical, at file
  0x4842 in 1.1.0. I2C wait-loop byte patterns from 1.1.9 not found verbatim (code moved → bl offsets differ) — inconclusive;
  needs a function-level diff.
- Not flashable onto a 1.1.9 mic (anti-downgrade). Use for static diffing only.

- Version evidence so far: PodMic **1.1.0** (mac 2.0.21, image ✔), **1.1.7** (Android 1.0.46, name only), **1.1.9** (all others, image ✔).
  Wanted next: 1.1.7 image (original Play Store APK of 1.0.46, contemporaneous desktop builds, or update server with user OK).

## Session 3l (2026-11-11): where firmware is downloaded from (static, all builds)
- Manifest: `update.rode.com/rode-devices-manifest.json` — current desktop bridge + Mobile 2.0.94 have https AND http forms;
  Win 2.0.65 + mac 2.0.21 only **http://**. Current also: `https://update.rode.com/manifest.json`, `rcp2-mini-manifest.json`
  (RØDECaster Pro II); Win 2.0.65 also `https://update.rode.com/rcv/rcv-update-manifest.json` (RØDECaster Video).
- Per-device section key (PodMic) `podmic-usb-manifest` with fields `update-version`, `os-version`, `firmware-URL`,
  `firmware-md5`. **The firmware URL lives only in the server manifest — not in any binary.** md5 only, no signature.
- mac 2.0.21 has the generic fields but NO `podmic-usb-manifest` string ⇒ no online PodMic update there (embedded 1.1.0 only).
  Mobile 1.0.46: field names + host `update.rode.com`, no full manifest URL literal (built at runtime).
- Not fetched. With user OK: GET the manifest → podmic `firmware-URL` → try older file names (e.g. Application-Podmic_v1.1.7.bin).
- Manifest fetched by user (2026-11-11): `podmic-usb-manifest` main AND beta: update-version 1.1.9,
  firmware-URL `update.rode.com/podmicusb/firmware.bin`, firmware-md5 `update.rode.com/podmicusb/firmware.md5` (a URL to an md5
  file, not the hash). Unversioned filename ⇒ guessing old names on the server is pointless. Expected md5 of our 1.1.9:
  1d7a754c74c9e1f2b4ace197c3fa47d4 (1.1.0: f68313d7dd97506896b2ae2cc8405adc). Best remaining source for 1.1.7: Wayback Machine
  captures of update.rode.com/podmicusb/firmware.bin and of old manifests.

## Session 3m (2026-11-11): disconnect STILL happens on wdt13s; reset sources audited
- User (watching YouTube, wdt13s flashed): same symptom — sudden silence (noise floor gone too), audio sink back in ~3–5 s.
  NOT yet conclusive: the hang is inaudible (audio keeps streaming), the silence = the reset. Need capture: HID-poll-silence →
  drop interval (≈9–17 s ⇒ IWDG, patch works; ≈1–2 s ⇒ patch ineffective or non-IWDG reset; ≈0 ⇒ instant fault/NMI reset).
- Device-side reboot supported: disconnect capture drop→re-enum 1.39 s ≈ factory-restore capture bootloader 'R'→app re-enum 1.43 s.
- **Reset sources in 1.1.9:** NMI (vec 0x08007e15), HardFault/MemManage/BusFault/UsageFault (0x08007e39/41/49/51) all
  `bl 0x08007e14` = NVIC_SystemReset (AIRCR 0x05FA0004) — **instant reset, unaffected by the wdt patch**. Other SW reset:
  0x08008670, only from HID 'R' (0x080087ac). Plus IWDG. GD32 NMI also = HXTAL clock-security-system failure.
- Proposed: `watchdog-test` flasher command (HID 'L' → halt → time until drop) to verify the patched IWDG period on the device.
- User: neither Central nor USBPcap was running at that disconnect. Wrote `flasher/podmic_monitor.py` (stdlib, reuses the
  flasher's HID layer): polls report 8 `08 08 01` every 1 s, logs STALL/RECOVER/GONE(silent-before-drop)/BACK to
  podmic_monitor.log. State machine tested offline (watchdog-like, instant-reset, transient-stall timelines). Not yet run on HW.

### 3n. Monitor result on wdt13s (2026-11-11 23:00, Central closed, 1 s polling)
```
23:00:03.683 last answer · 23:00:04.682 first unanswered poll
23:00:17.686 GONE  SILENT BEFORE DROP 13.004 s (13 missed; last answer 14.003 s before) [SetOutputReport error 1167 = device not connected]
23:00:19.149 BACK  re-enumerated 1.462 s after drop (1.1.9)
```
⇒ silent 13.0–14.0 s = the patched IWDG period (≈13 s nominal). **The disconnect IS the watchdog reset**; the /128 patch works
on the device (in-situ check of the period, no 'L' test needed); the main-loop hang is **permanent** (no recovery in 13 s),
not a transient stall. Re-enum 1.46 s matches 1.39 s (original capture) / 1.43 s (bootloader→app). Fault/NMI instant reset
ruled out for this event. Next: find what the main loop blocks in (no-timeout I2C waits are the lead candidates).
User observation, same event (console open): the STALL line appeared first, audio **kept playing normally** during the
silent-HID period, and the sound cut out only together with the GONE line (Windows moved to another output device, then
back after BACK). ⇒ USB audio (iso EP + DMA/IRQ path) is unaffected by the hang; only main-loop work stops (HID replies,
the 4 ms task incl. codec-clock PI steering, IWDG refresh). The audible dropout = the watchdog reset + re-enumeration,
not the hang itself. Also: ~13 s without PI clock steering was not audible ⇒ the codec clock drift was too small to notice.
Next-step candidates: (a) static: list every main-loop wait whose exit depends on hardware/IRQ state (I2C flags, buffer
pointers, USB state), not just the 3 I2C waits; (b) diagnostic patch: an IRQ that keeps running during the hang records the
main loop's stacked PC into RAM that survives the IWDG reset, read back after reboot (needs a no-init RAM spot and a read
path) - would pinpoint the exact loop.

### 3o. Static search for main-loop waits that never end (2026-11-11)
Method: full linear disassembly (rizin `pD` of the image), every backward branch ≤64 B classified. Rizin `aaa` function
boundaries are unreliable in this image (overlapping fns, missed the known I2C loops) - don't trust its call graph.
I2C1 helpers decoded (base 0x40005800): 0x08004180 ACKEN on/off, 0x08004194 POAP, 0x080041b4 START, 0x080041c0 STOP,
0x080041cc / 0x080041d4 set / clear CTL1 bits (0x100 ERRIE, 0x200 EVIE, 0x400 BUFIE).
I2C state 0x200000dc: [0] 0 busy / 1 done / 2 error, [1] addr, [4] hdr bytes, [8] hdr cnt, [0xc]/[0x10] tx ptr/cnt,
[0x14]/[0x18] rx ptr/cnt, [0x1c] async callback, [0x20] byte count.
**Not a cause of this hang (bounded or not in main-loop context while streaming):**
- 0x0800c1e8 delay(ms): spins on a SysTick countdown; SysTick keeps running. Callers: init, HID 'L'/'R', 0x080091b0, codec init.
- 0x0800afcc `while(!byte[0x200001c4])` - inside codec init 0x0800af8c, called only from main init (0x08007f3a).
- FMC busy 0x080096a8 (bounded); RCC/clock waits 0x08008c96/0x08008d00/0x08008d10/0x08008d38/0x08008cd2 (init).
- USB FIFO "read GRXSTSR until two reads agree" loops 0x080048ac/0x080051d0/0x0800bdc4/0x0800be10 run in the USB ISR;
  a spin there would stop audio, and audio kept playing ⇒ not these.
- I2C done-wait exits on error too (state 2 set by ER ISR 0x08009128). Codec write 0x0800ad0c retries 4x, toggling the
  slave address byte 0x1B↔0x1A on error (!). 0x080091b0 = 1-byte read from 7-bit addr 0x10, 4 retries, init only.
**Remaining unbounded main-loop waits:**
1. I2C start-wait `while(!start())` (0x080091c0 / 0x0800ad60 / 0x0800b0c2): start() 0x08008f24 refuses while
   (a) an async callback is pending ([0x1c]≠0), (b) state==0 (transfer in flight), or (c) STAT1.I2CBSY.
   **Possible deadlock (a):** the async callback is cleared only by the main-loop runner 0x08008ee0, and only when state≠0
   and I2CBSY clear. If a task does a blocking codec write while an async transfer is in flight or its callback is pending,
   the blocking caller spins inside the scheduler and the runner never runs again ⇒ permanent hang, IRQs/audio unaffected.
   Async users = state machine 0x080067a8/0x080067d4/0x08006800 (I2C addr from struct 0x20000035.., likely the Apple MFi
   auth chip), started via 0x08006ba8 (registers a task with 0x0800c2a0) from init only (0x0800914c via 0x08007f4a;
   0x08009928 via 0x08007f56). **Resolved (below): runs at boot, plus iAP2 (Apple hosts) only ⇒ not on Windows.**
2. I2C done-wait (state stays 0): an EV sequence that ends without state=1 and without an error interrupt.
3. HID send busy-wait 0x0800b308 (EP1 IN never completes) - only reachable when the host sends HID requests (not in the
   no-Central case).

#### 3o-2. Is the async I2C (MFi) machine active while streaming on Windows? → No
- Scheduler 0x0800c174: 16 slots × 20 B @RAM; slot {+4 period, +8 countdown, +0xc prio, +0xd state (0 run, 0xfe free,
  0xff suspended), +0x10 cb}. Runs one due task per call; period 0 ⇒ slot freed after one run. 0x0800c248 = periodic
  register (4 ms task), **0x0800c2a0 = one-shot** (period 0).
- Async chain: 0x08006ba8 arms 0x080067a8 as a 5 ms one-shot; completion handler 0x0800682c: success → next step in 2 ms
  (0x080067d4); error → step 0x08006a8c retries ≤9× toggling the address bit, other steps retry every 10 ms unbounded;
  end → final cb [0x20000028+0x18] (0x08007ac8 → 0x08007864, iAP state), then idle. Generic async-read API 0x08006c08
  (busy flag +0x38) is called only from iAP2 code (0x08006d46/0x08006d64 ← 0x080070f0 / 0x08007c7c).
- Starters: main init only (0x08007f4a, 0x08007f56) and the iAP2 class init 0x0800b760 (class table RAM 0x200008e0, .data
  copy of 0x0800f3f0) via 0x08007ae4 (200 ms one-shot 0x080078ac).
- Config-descriptor callback 0x0800b3cc picks a personality from pdev+0x18: 271-B config (0x10f) + class table
  0x200008e0 (iAP2), 309-B (0x135) + table 0x20000910, or **257-B (0x101) with extra class table = NULL**. Both Windows
  captures show **wTotalLength 257, 4 interfaces (HID 3, AC 0, AS 1 mic, AS 2 playback+fb)** ⇒ no iAP2 class on Windows.
- ⇒ On Windows the async machine runs only at boot (finishes in ms if the CP answers). Deadlock (a) needs it active
  while the 4 ms task writes ⇒ **ruled out for this hang** (unless the boot chain never finishes: it would retry every
  10 ms forever - not checked on the device, but then every boot would be at risk, not random hours later).
**Left:** (b) a codec write whose interrupt sequence ends without state=1 and without an error interrupt, or (c)
STAT1.I2CBSY stuck (GD32/STM32-F1-style I2C can latch BUSY after a bus glitch / after LOSTARB; the ER ISR only clears
STAT0 + sets STOP, never resets the peripheral). Write path reviewed (SBSEND→addr, ADDSEND→BUFIE on, TBE→hdr/data,
last TBE→BUFIE off, BTC→EVIE off+STOP+state 1): tolerant of ISR latency (the peripheral stretches SCL), no lost-event
hole found statically. Static analysis can't choose between (b) and (c) → diagnostic firmware (stuck-PC + I2C register
snapshot) is the next decisive step.

#### 3o-3. Independent review (Fable 5.1 subagent, read-only, 2026-11-11) - agrees, adds:
- Async-callback deadlock on Windows while streaming: **confirmed excluded** (set-config 0x0800b6f8 iterates class list
  0x20000794 = {0x2000092c, 0x2000032c, slot}; slot = 0 for the 257-B config). Caveat: at boot the async chain can overlap a
  codec write from the knob (0x08009c4c) or HPF task 0x0800af2c - boot-time risk only.
- **No I2C re-init / SWRST / bus-clear anywhere** (all 0x40005800 refs = driver + init 0x08008e72) ⇒ once STAT1.I2CBSY latches
  (bus glitch, or LOSTARB → slave mode, where the ER ISR's STOP does nothing), start() refuses forever.
- ER ISR 0x08009128 leaves EVIE/BUFIE on and doesn't flush DATA; after a NACK a stale TX byte may go out as the next
  "address" (hardware-specific, INFERRED). Worst case → receive mode with rxptr 0, no STOP, state stays 0 → done() spin.
- Possible EV-interrupt storm if the peripheral ever enters slave mode (unhandled STPDET) - inferred, low probability.
- Codec read 0x0800b0f0 toggles address with `eor 0x1b` (0x1B↔0x00); 4 tries, returns. Write path: no lost-event race.
- HID send spin 0x0800b308 waits on EP1-IN completion (0x0800b290) - reachable on every HID reply (not the no-Central case).
- Ranking: 1 BUSY stuck / START without SB; 2 NACK → stale-DR receive wedge; 3 HID send spin; 4 EV storm; 5 async deadlock (excluded).
- Expected stuck signatures: R3 → PC 0x08008f24–46 / 0x0800ad60–76, state 1|2, STAT1=0x0002; no-SB → PC 0x0800ad78–7e,
  state 0, CTL0.START set, STAT0=0; stale-DR wedge → state 0, STAT1.TR=0, rxcnt [0x200000f4]=0xFFFFFFFF.., CTL1=0x700.
- Instrumentation idea: cap the waits (~1 s); on expiry store tag + STAT0/STAT1/CTL0/CTL1 + state words in **RTC backup
  registers (survive IWDG reset)**, read back over HID. Cheap discriminator: make the PI loop write every 4 ms on Linux
  (expect hangs there), or add hysteresis on Windows (expect fewer hangs).

### 3p. Diagnostic firmware `patched/podmic_usb_v1.1.9_wdt13s_diag.bin` (built 2026-11-11, not yet flashed)
Goal: one disconnect → exact stuck location. Design + layout: `patched/README.md`; source `diag/diag.s`; builder
`make_patched_diag.py` (stock sha256-checked; only our bytes inside). Facts used:
- SysTick = 1 kHz (LOAD = SystemCoreClock/1000, 0x0800c334), priority 0xa0 (SHPR3 byte @0x0800c370): above I2C EV 0xc0 /
  ER 0xd0, below USB 0x70 ⇒ samples the stuck PC even inside an I2C IRQ storm; IPSR in the frame tells thread vs IRQ.
- Exceptions: I2C EV = 40, ER = 50, USB = 83 (stub 0x08007ed0), SysTick 15. No RAM above .bss end 0x20001aa8 is
  referenced except the stack (top 0x20002000; biggest frame 76 B) ⇒ add-on RAM at 0x20001ab0..c7.
- HID report descriptor (0x0800ea58): in 1 = 9 B, out 2 = 9, in 3 = 28, out 4 = 28, in 5 = 14, out 6 = 14, in 7 = 27,
  out 8 = 27. HID send 0x0800b2f8 length by report id: 1→10, 3→29, 5→15, 7→28 (incl. id). 'B' original handler
  0x080086c0 (`movs r3,0; strb [r4,9]; b reply`), only tbb target; 'E' also replies on report 3.
- PMU_CTL writes in the firmware are read-modify-write (0x08004838, 0x080093a2) ⇒ BKPWEN we set stays set.
- rizin: use `-e asm.cpu=cortexm` for M-profile system instructions (mrs/msr); `cortex` shows them as invalid.

### 3q. Host-OS detection at enumeration: Windows vs Linux vs Apple (2026-11-12)
- Mode byte pdev+0x18 (mirror 0x2000191d), reset to 0 on first bus reset (0x08005386). Fingerprinting:
  device-desc cb 0x0800b38c: 1st GET_DESCRIPTOR(DEVICE) with wLength 64 → mode 1 (non-Apple), else on 2nd → mode 2 (Apple/iAP2,
  bNumConfigurations 0x20000484+0x11 = 2). Config cb 0x0800b3cc in mode 1: **wLength 0xFF (Windows) → mode 3 → 257-B config**;
  any other wLength (Linux asks 9 then wTotalLength) → **mode 4 → 309-B config**; string cb 0x08005670: string 0 with wLength 0xFE
  in mode 4 → mode 5 → 257-B config (host unidentified). Windows captures show the 257-B config ✔. Linux 309-B: INFERRED, check `lsusb -v`
  (wTotalLength 0x0135, bNumInterfaces 5).
- 309-B (Linux) vs 257-B (Windows) differences: (1) mic AS if 1 gets alt 2 = 1 ch 16-bit 48 k (EP 0x82 mps 98) besides alt 1
  24-bit; (2) extra vendor interface 5 (class ff/01, 0 endpoints), class table 0x20000910 = {desc, init NULL, deinit NULL,
  req = 0x0800ba59 (the same UAC request handler), rest NULL} ⇒ **no init, no task, no endpoint, no I2C**.
  **Playback AS if 2 (EP 0x02 async 294 B + feedback EP 0x83, 3 B, 10.14) and AC if 0 are byte-identical.**
- Apple (mode 2, 271 B): no feedback EP (playback EP 0x02 no sync ep; mic EP 0x82 attr 0x25 = implicit feedback), plus iAP2 vendor
  if 4 with bulk EP 0x83/0x03, iAP2 class table 0x200008e0 → MFi CP async I2C. Only here does the main loop really differ.
- ⇒ Windows vs Linux: same main-loop code, same 4 ms PI task, same codec-write path. The only OS-dependent inputs remain the
  playback stream timing/packet sizes, how the host reads EP 0x83, alt-setting/UAC request patterns, and suspend.

### 3r. Rear wheel level resets on every boot (2026-11-12)
- Wheel = quadrature encoder PA2/PB3, read in main-loop fn 0x08009c4c; position byte 0x20000162 (mirror 0x20000163), 0..44
  (0x2c), table 0x0800e73c (int16, value>>1 = dB: step 0 ≈ −70 … 40 = 0, 44 = +5), clamped −60..+10, −60 = mute → param
  struct 0x200001c4+4. Range/mute ⇒ headphone/monitor level (input gain is UAC FU2 22–63 dB and is persisted).
- Init 0x08009bac: `movs r0, 0x13` @0x08009be4 (file offset 0x5be4, bytes `13 20`) ⇒ **every boot starts at step 19 ≈ −32 dB**.
  The position is not in the settings block (only refs: init + encoder fn) ⇒ never persisted. Patch = 1 byte (0..0x2c) + CRC.

### 3s. FIRST DIAG RESULT (2026-11-12 14:48, wdt13s_diag, monitor log pasted by user)
- Silent 13.005 s before drop; boots 2→3, hangs caught 1. Reset cause latch read 0x00 (bootloader probably clears RCU_RSTSCK).
- **Stuck PC 0x0800ad7c, 1 s later 0x0800ad78 = done-wait loop in codec write 0x0800ad0c (`bl 0x08008f18; cmp r0,0; beq`)**,
  thread mode. I2C: STAT0 0x0000, STAT1 0x0000 (not MSTR, not BUSY, TR 0), CTL0 0x0801 (I2CEN + POAP, no START/STOP),
  CTL1 0x0718 (FREQ 24, ERRIE+EVIE+BUFIE), state byte 0, data-remaining [0x200000dc+0x10] = 1, rx 0.
- Reading: start() succeeded, SB/ADDSEND/2 header TBEs/1st data byte were handled (BUFIE is set only at ADDSEND), then the
  peripheral left master mode with no flags and no ER interrupt (ER ISR 0x08009128 unconditionally sets state 2 + STOP, so it
  did not run after start()). Bus idle ⇒ **NOT BUSY-stuck, not START-without-SB, not HID spin, not async deadlock**.
  ⇒ "transfer silently abandoned by the peripheral, done-wait has no timeout". Trigger unknown (glitch / peripheral quirk);
  Windows link = more PI writes/hour (hypothesis). POAP set = leftover from an earlier 2-byte read (fcn 0x08004194), likely benign.
- I2C regs touched only by driver + boot init (no re-init from USB ISR).
- Diag gap: header count (+8) and progress (+0x20) not captured.
- Fix plan (fix1): bound done-waits (0x0800ad78, 0x080091c0 site, 0x0800b0c2 site) to a few ms; on timeout SRESET-pulse + re-init
  I2C, state=2 → existing retry (note: retry toggles addr 0x1B↔0x1A). Count recoveries + snapshot (incl. +8/+0x20) in BKP.

### 3t. fix1 firmware built (2026-11-12) — see patched/README.md
- User note: "never on Linux" = not caught in ~1 day; Windows repro time varies from minutes to hours. Linux may just be rarer
  (fewer PI codec writes) — not immune. Treat the OS difference as rate, not cause.
- `patched/podmic_usb_v1.1.9_wdt13s_fix1.bin`: diag + bounded I2C waits (20 ms) + peripheral reset/retry + recovery page
  (HID 'B' 01) + wheel default 24. Success criterion: RECOVER-I2C lines appear, no GONE.

### 3u. fix1 first run (2026-11-12 15:04–15:20, Windows, Central closed)
- 21 recoveries in ~16 min, **no GONE**. All identical: done-wait timeout in codec write (ret 0x0800ad7c), header 0, data 1 left,
  3 bytes moved (2 reg-addr + 1st value byte), addr 0x1B, STAT0/STAT1 0, state 0. ⇒ deterministic failure point: the peripheral
  drops the transfer right after the 1st data byte.
- 1st event CTL0 0x0801 (POAP leftover), later 0x0001 (cleared by our SRESET) ⇒ POAP not the cause.
- Rate (~1/45 s, bursty) ≫ stock MTBF (minutes–hours): either strongly condition-dependent, or the recovery raises the rate.
- Next: day-long run (watch GONE / audible clicks at RECOVER times / wheel default); later count recoveries on Linux (needs a
  hidraw reader for 'B' 01) to quantify the Windows-vs-Linux rate.

### 3v. Wheel button + existing mic mute (2026-11-12, static)
- Button = PA10, sampled by 4 ms task 0x08009b70 (edge 1→0 sets flag 0x20000d7d). Consumer task 0x08009298:
  if host-app flag 0x20000d71 → toggle **mic mute** (struct 0x200001c4+1 via 0x08008de8(new)); else toggle **monitoring**
  (struct+2 via 0x08008e10). d71 set by HID report 6 / report-8 param 6 (0x080099c8); task 0x0800995c clears it after >6
  ticks without host contact and re-applies settings ⇒ **with RØDE Central connected the click = mute** (INFERRED, untested).
- Mute effect: DSP fn ~0x0800a11e: if struct+1 → zero mic block buffer (0x20000e00+0x100, 32 words) ⇒ real mic mute.
  Report-8 param 0x08 = mute (GET returns 0x200001c5, the 0.5 s Central poll); param 0x03 = monitoring. LEDs PB6/PB7 via
  0x08008d8c show mute/monitor state.
- Long-press mute = small patch (own button task: short click on release → existing flag; hold ≥ N ms → 0x08008de8(!mute)).
  Jingle needs the DAC output buffer / monitor mix location — not yet analysed.

#### 3v-2 LED logic (corrected after user feedback, 2026-11-12)
User: with RØDE Unify / RØDE Connect (not Central) the app controls monitoring and the click mutes; LED always
shows red = monitoring off (default), green = monitoring on, also without any app.
Code 0x08008d8c (LED update, called after both toggles): 0x200000d8 = NOT-muted (0x08008de8 stores mute^1),
0x20000cf8 = NOT-monitoring (0x08008e10 stores arg^1).
  if (muted || !monitoring)  PB6 set (BOP 0x18), PB7 reset (BC 0x28)  -> RED
  else                       PB6 reset, PB7 set                       -> GREEN
=> mute forces RED even with monitoring on; muted and monitoring-off look the same. For hold-to-mute a distinct
indication (e.g. blink while muted) would need an extra LED task.

### 3w. fix2 = fix1 + hold-to-mute + jingle (2026-11-12, built + emulated, not flashed)
User: LED is on the back, can't be seen/felt -> wants audible feedback (jingle), LED irrelevant.
- Output path (DMA ch1/2 IRQ, vector 26 -> ptr 0x2000164c -> 0x08009f04, every 16 frames @48 kHz): half/full flag
  (DMA INTF 0x40/0x20) selects out block r5 = 0x20000d80/0x20000e00 (32 words = 16 stereo 32-bit frames), mic RX at
  r5+0x100. Copies 24-bit playback from ring 0x20000200 (unaligned reads -> top 24 bits), mic processing (mute zeroes
  RX block, AGC/limiter/filters, capture ring 0x20000210); if monitoring (struct+2) mic -> 0x08009d90 (monitor level
  0x200001d4) -> qadd into both channels; join at 0x0800a0aa; if !0x20001648 && 0x2000187c: wheel gain 0x08009d90(r5,
  0x200001c8,...) else block zeroed; then ~ and halfword swap -> I2S TX.
- Hook at 0x0800a0aa (all paths join; r0/r1/r12 dead, r2/r3 = replaced ldr/ldrb). tone_hook watches mute byte
  0x200001c5 (any source) and plays a 3-segment sequence (2-pole resonator, Q30).
- btn_task replaces consumer 0x08009298 via literal 0x080092f4: edge 0x20000d7d -> count while level 0x20000d7c == 0;
  175 ticks -> 0x08008de8(!mute); release earlier -> stock 0x080092a2 (with the {r3,lr} frame stock expects).
- Host-loss task 0x0800995c (only when d71 was set) calls 0x0800a380(0) = unmute when Unify/Connect goes away (stock).
- Space: free tail was 178 B; dropped reset-cause capture + snap2 (62 B), tables -> reserved vector slots.
- Emulator fix2/test/emu_fix2.py (mini Thumb interpreter over rizin pDj of the real image) - all checks pass.

### 3x. EMI / grounding as the I2C cause? (2026-11-12, static, user question)
- Only I2C1 writers: init 0x08008e38 (boot), start 0x08008f24 (CTL1 |= EVIE|ERRIE 0x300, START), EV ISR 0x08008fa4
  (BUFIE after ADDSEND; TX path writes DATA without checking TBE; STOP only when hdr == data == 0 + BTC, or RX paths),
  ER ISR 0x08009128 (STAT0 = 0, STOP, state = 2). No other refs to 0x40005800/0x40005000. NVIC prio EV 0xc0, ER 0xd0,
  USB 0x70 (USB preempts I2C).
- Electrical faults (glitch, misplaced START/STOP, NACK, lost arbitration) set BERR/AERR/LOSTARB -> ER IRQ -> state 2 ->
  stock retry loop already handles them. All 21 fix1 events: state 0, STAT0/STAT1 0 (bus released, not master, no
  flags), same byte every time (3 of 4 moved) => not an error-flag event, not random => EMI/grounding unlikely.
  Plus OS dependence with identical hardware/cable. Bus is on-PCB MCU<->codec.
- Not explained: what released the bus (BUSY = 0) with no STOP from our code and no error flag. Leading idea: event
  handling race (ISR latency under USB IRQ load, host-dependent).
- Cheap discriminating tests: (1) recoveries/hour with laptop on battery vs charger, powered hub, other cable;
  (2) firmware: I2C EV/ER priority above USB (init bytes 0x08008eae movs r1,0xc0 / 0x08008eb0 movs r2,0xd0) and compare
  recoveries/hour (race -> ~0; EMI -> unchanged).

### 3y. fix1 day run, evening (user log 17:24-18:25, 2026-11-12)
- Total 144 recoveries by 17:33 (boot ~15:04 -> ~1/min average, bursty: 6 in 3.5 min at 17:26-17:29), then **0 for
  >= 51 min** (17:33-18:25+). At ~1/min, 51 min of silence is not chance (Poisson p ~ e^-50) -> conditions changed.
- New variant at 17:33: header 0, **data 0, 4 bytes moved**, state 0, STAT0/STAT1 0 -> all bytes written, but the
  completion (BTC -> STOP + state 1) never ran, yet the bus is idle. Same signature otherwise (codec write done-wait,
  0x1B, no error flags). => the bug is "transfer ends on the bus without the driver seeing it", not tied to one byte.
- Errors only occur while the PI task writes the codec, i.e. while the USB playback (OUT) stream runs. Most likely
  explanation for the drop: nothing playing -> Windows stops the stream -> no I2C traffic. To check with the user.
- Monitor can't tell traffic from failure probability. Next build: count I2C transfers (start_hook) on 'B' 01 ->
  recoveries per 1000 writes.

### 3z. PI write cadence correction + "skip instead of retry" question (2026-11-12)
- Dirty flag 0x20001855 is set by 0x0800b134 on a word change but cleared ONLY in codec init (0x0800afe4, boot). So after
  the first change the 4 ms task (0x0800af08) rewrites the list 0x20000320 EVERY tick: 0x0A+0x0B merged (reg auto-inc,
  2+4 B) then 0x06 (2+2 B). Return value of 0x0800ad0c ignored (tail call). Corrects 3o/3y "writes only when the word
  changes": the rate is a constant ~250 lists/s while the task runs -> 1 recovery/min ~ 1 per ~30k transfers.
  The 3y evening drop is therefore NOT explained by a lower write rate (unless the task itself stops); open.
- All recorded failures had a 2-byte data phase (3u: 3 B moved, 1 left; 3y: 4 B moved) = the 0x06 write (constant 0x0008),
  i.e. after the fraction regs were already written. Retrying vs skipping to the next tick makes no practical difference.
- Lean-release candidates (not experiments): pre-flip 0x20000314 to 0x1A in recovery at the codec-WRITE sites so the stock
  eor-1 restores 0x1B (no 0x1A probe); shorter timeout (~3-5 ms; a 6-byte transfer takes <1 ms) to shrink the stall.
- CORRECTION (user report + code): hangs also happen with the mic merely connected (no playback). Code agrees: the 4 ms
  task is registered unconditionally at boot (0x0800ac84 -> scheduler 0x0800c248, period 4, repeat -1) and the dirty flag,
  once set by the first word change after power-up (any stream open, e.g. a system sound), is never cleared -> 250
  write rounds/s until reboot, playing or idle. So "errors only during playback" (3y) and "rate depends on Windows
  delivery jitter" (3o) are wrong. The OS dependence must come from elsewhere (USB IRQ load incl. SOF? unproven).
  Priority theory weaker without OUT stream, but the audio DMA ISR runs continuously regardless of USB.

### 3aa. fix1 day run result (log 2026-11-12 15:04 -> 2026-11-13 01:00+)
- ~10 h, **0 STALL / 0 GONE**, 188 I2C recoveries (all done-wait timeouts in codec write, addr 0x1B, no error flags).
  One ODD line = RØDE Central opened in parallel (its report-7 reply 07 0d 41..). The 14:48 GONE in the log predates fix1.
- Rate is bursty: 144 by 17:33; 6 isolated 17:33-21:15; ~38 at 21:15-22:45; none 22:45-01:00+.
- Snapshot variants (last-of-batch only): 46x data 1 left/3 B moved/CTL1 0x0718; 7x data 0/4 B moved; 4x data 0/2 B
  moved; 1x data 2/2 B moved; 1x header 2,data 4/0 B moved (stalled before the address phase); CTL1 0x0318 (BUFIE off =
  all data loaded, waiting for BTC) 5x - all in the quiet 18:53-20:03 window; CTL0 0x0801 once. => the stall point
  varies; not tied to one byte or one interrupt. Consistent with "late/lost event IRQ", not with a codec/bus fault.

### 3ab. Vendor errata check (2026-11-13)
- GigaDevice "Device limitations of GD32F3x0" Rev 1.3 (gd32mcu.com errata sheet), I2C section has two items:
  2.8.1 "Read one more data because the BTC flag was not cleared": if an interrupt intervenes while RBNE is set and BTC
  gets set meanwhile, an extra byte is read. Workarounds: interrupt method "(need higher interrupt priority)" or DMA
  (recommended). 2.8.2: multi-master START timing I2C0 vs I2C1 -> arbitration winner gets no ACK, SCL held low, bus stuck;
  no workaround. Neither describes our case exactly (we hang on WRITES, single master, bus idle, no flags), but 2.8.1
  confirms the I2C event handling is latency-sensitive and the vendor fix is higher I2C IRQ priority or DMA.
- Same class as ST's STM32F10x I2C errata ("software events must be managed before the current byte is transferred";
  workaround: DMA or highest-priority I2C IRQ, as cited e.g. by libmaple). Stock fw: I2C EV 0xc0 / ER 0xd0 < USB 0x70.

### 3ac. fix2p = fix2 + I2C IRQ priority (2026-11-13, built, not flashed; user chose to combine stages 2a+2b)
- Stock NVIC priorities (PRIGROUP 0, 4 bits all preemptive): IRQ7 EXTI4_15 0x20, IRQ16/19 timers 0x30, IRQ10 DMA ch1/2
  (audio DSP) 0x40, USB IRQ67 0x70, IRQ42 0xf0, SysTick 0xa0, I2C1 EV 0xc0 / ER 0xd0 (+ 2 timers via 0x08004480).
  => I2C events could wait behind the whole audio block ISR and USB ISR.
- fix2p sets EV/ER to 0x10. EV ISR 0x08008fa4 checked: straight-line, one event per entry, no waits -> preempting audio
  (333 us block deadline) / USB costs microseconds. fix1/fix2 i2c_recover runs with cpsid i, so no interference.
- Baseline = fix1 day run (3aa: 188 recoveries / ~10 h, bursty). fix2's I2C path is identical to fix1's.
  Read: ~0 recoveries over a comparable day => late-IRQ root cause confirmed. Fallback image without prio: fix2.bin.

### 3ad. fix2p first run (user log 2026-11-13 12:56 -> 17:11+, fix2p confirmed by user)
- podmic_monitor --advanced, 1 s polls, recovery page read every 30 s: **0 I2C recoveries, 0 STALL/GONE over 4 h 15 min**
  (15,294 polls). DIAG at start: boots 10, hangs caught 1 = old record from before this image (PC 0x0800ad7c, same
  codec done-wait signature), recoveries since boot 0.
- Baseline fix1 (3aa): 188 / ~10 h (~19/h avg, 21 in the first 16 min of 3u) -> ~80 expected in 4.25 h at the average
  rate. But fix1 was bursty with quiet spells of >= 2 h (22:45-01:00+), so 4 h of zero is strong, not yet conclusive.
- Reading: supports the late-I2C-event-IRQ root cause (3ab/3ac) — I2C EV/ER at 0x10 instead of 0xc0/0xd0 below audio
  DMA 0x40 / USB 0x70. To close: >= 24 h incl. the usage that produced bursts, still 0. Keep the 20 ms timeout/recovery
  as a safety net. Then report to RØDE: one-line fix = NVIC priority bytes 0x08008eae/0x08008eb0.
- 2026-11-13: user confirms fix2p is flashed and the jingle works on hardware; asked for it a couple dB quieter ->
  fix2p_t21 (jingle -21 dBFS instead of -18.06, tables only; patched/README.md). Not flashed yet.

### 3ae. Release images (2026-11-14) — firmware/release/README.md
- User: test stage a success (fix2p, priority fix). Requested: rel1 = priority fix + I2C timeout safeguard; rel2 = rel1 +
  boot volume + hold-to-mute + jingle (jingle level as fix2p, -18 dBFS). Stock watchdog, all diagnostics removed.
- Add-on RAM now zeroed by extending the stock .bss clear (literal 0x08007d78 -> 0x20001b10) instead of a reset hook.
- Watchdog margin: worst case ~340 ms blocked per 4 ms tick with permanent I2C failure < 1.64 s IWDG.
- Built, diffed, emulated (rel2), not flashed. fix2p_t21 (jingle -21 dBFS) remains an option for later.
- 2026-11-14: release timeout 20 -> 10 ms (rel_v1.s/rel_v2.s `cmp r2/r3, 10`), both images rebuilt (new sha256 in
  release/README.md). Worst-case block per tick ~180 ms. Address toggle 0x1B<->0x1A on retry left as stock (user: unknown
  why RØDE does it -> don't touch).

## Session 4 (2026-11-19): protocol completion, firmware side (stock 1.1.9)
RØDE Central side (bridge encoders, connect sequence, Basic mode): summarised in
[`protocol/podmic-usb-hid-spec.md`](protocol/podmic-usb-hid-spec.md). Here: what the firmware accepts.

### 4a. Report-8 handler 0x080084c4 (request `[id 8, param, op, value…]`, op 1 = get, else set; reply report 7)
Reply byte 2 (after id/param): 'A' ok, **'E' (0x45) = value out of range**, 'N' = unknown param/bad value.
| param | set | get | notes |
|---|---|---|---|
| 0x01 HPF | **v < 2** → 0x0800a824(v) + settings image; **v ≥ 2 → 'E'**, nothing applied | struct+0x35 | v=1 loads biquad 0x0800e96c (2nd-order HPF, Q30: b 0.99998/−1.99997/0.99998, a1 1.99017, a2 −0.99023; −3 dB ≈ 54 Hz, −0.5 dB @75 Hz, −7 dB @40 Hz); v=0 loads pass-through 0x0800e984 (b0=1). **1 = on, 0 = off (bypass)**; no third (150 Hz) set exists. Settings image reserves 2 bits for it. |
| 0x03 monitoring | 0x08008e10 (struct+2, LED) | struct+2 | 1 = mic routed to headphones |
| 0x04 monitor mix | **0..100**, >100 → 'E'; 0x0800a3b0: dB = v·66/100 − 60 → struct+0x10 (0x200001d4) | stored % | **mic level in the direct-monitoring mix** (−60..+6 dB, **91 = 0 dB**, factory 0x5B). Separate from the rear wheel (struct+4 = overall headphone level). Central has no UI for it. |
| 0x05 (unused by Central) | v≠0 → flag 0x20000d6a = 1; v=0 → clear + LED refresh | flag | flag drives 0x08009348 (called from a periodic task at 0x08007ee6): toggles 0x20000d69 → LED on/off ⇒ **LED blink / "identify"** (inferred) |
| 0x06 host ping | **only value 0** (else 'N') → 0x080099c8 (host-app flag 0x20000d71) | — | set-only. Bridge `ToPingMessage` sends 0 for Unify/Connect-type hosts; Central never. Flag makes the button mute instead of toggling monitoring; times out (0x0800995c). |
| 0x07 biquads (unused by Central) | idx ≤ 2: 24 B (6 × i32) at content offset 7 → slot idx of the 3-biquad mic chain (0x20000164 + idx·0x18), renormalised (0x0800a4e0) | same 24 B | **raw filter access**: slot 0 = the HPF, so a custom 150 Hz HPF *could* be loaded here, but it is volatile (not in the settings image) and param 0x01 overwrites slot 0. |
| 0x08 mute | 0x08008de8 | 0x0800a38c != 0 | not persisted |
| 0x0B advanced | bool | | persisted |
| 0x0C Basic slider | idx ≤ 2, value at content+2 | | persisted (image +0x4F..0x51) |
| 0x0D processing | bool | | persisted |

### 4b. Settings persistence
- 0x080096cc rebuilds the 83-byte RAM image 0x20000100 after every set (report 4 and report 8 0x01/0x03/0x04/0x0B/0x0C/0x0D).
  Layout: u16 +0: bits 0–1 HPF, bit 2 advanced, bit 3 monitoring, bits 4–10 monitor mix (bits 3/4–10 only updated
  while no host app is flagged); +0x02 compressor (18 B: en, thr u32, ratio u8, attack u32, release u32, gain u32);
  +0x14 gate (25 B: en + 6 × i32 Q31); +0x2D exciter (25 B: en, harm u32+coarse, 3 pad, tuneA u32+coarse, 3 pad,
  tuneB u32, sub3 u8, 3 pad); +0x46 Big Bottom (9 B: en, drive u32+coarse, tune u8, sub3 u8, pad); +0x4F..0x51
  Depth/Sparkle/Punch; +0x52 bit 0 processing. Mute, wheel position, param 5/7 state: not persisted.
- Flash write 0x080097d8 only from: 'L' (0x080093f4), 'R' (0x08008792), factory reset (0x08009888). **0x080093f4 is
  also vector-table entry IRQ1 = LVD (low-voltage detect)** ⇒ settings are written when the supply drops (unplug).
  This explains why settings survive without any explicit save; `saveToMicrophone` in Central is a no-op for PodMic.
- Boot 0x080098c0: loads the newest valid record from pages 46/47 (0x0800F800/0x0800FC00); none valid ⇒ factory reset
  0x08009888 (erase both pages, copy defaults 0x0800e798, save, apply). The defaults only come back when pages 46/47
  hold no valid record. RØDE Central's factory restore does NOT get there: it writes app pages 0–45 only (3a,
  bootloader-protocol.md §7), then writes the previously read DSP values back with report 4 and sets HPF = 0.

### 4c. Factory defaults (0x0800e798, decoded with the bridge tables)
HPF off; advanced off; processing on (Basic); monitoring 1; monitor mix 91 (0 dB). Depth 75, Sparkle 80, Punch 70.
Compressor on: −24.9 dB, ratio 1.99, attack 0.69 ms, release 20.9 ms, gain 1.98 dB. Gate off: −38 dB, attack ≈30 ms,
hold 0.305 s, release 0.2 s, range −9 dB, hysteresis 0.2. Exciter on: harmonics 69.8 % (byte 178), tune 3499 Hz
(byte 168), sub3 31. Big Bottom on: drive 64.7 % (byte 165), tune 90 Hz (byte 30), sub3 31.
Punch 70 reproduces the Basic formulas exactly (comp −14−16p = −25.2, gain 3p = 2.1, gate −45+10p = −38, range −13p
= −9.1). Note: the defaults say monitoring = 1, but the user saw red (= monitoring off) by default — to recheck on hardware.
