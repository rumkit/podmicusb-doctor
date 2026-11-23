# AGENTS.md

Instructions for AI coding agents (and a quick orientation for humans). Keep this file short. Details live in
`firmware/README.md`, `research/README.md` and `research/NOTES.md`; link to them, don't copy them here.

## What this repo is
Community fix for the RØDE PodMic USB (firmware 1.1.9) disconnect bug: I2C interrupt priority too low, so a
codec write never completes, the main loop hangs and the watchdog resets the mic. The repo holds patch sources,
a build script that applies them to the user's own stock image, a HID flasher (Windows; macOS not yet tested on
hardware), an installer and a monitor.

## Map
| Path | What |
|---|---|
| `firmware/tools/` | user-facing scripts: `extract_stock.py`, `build.py`, `podmic_flash.py`, `podmic_monitor.py`; `podmic_hid_mac.py` = macOS HID transport for the flasher (IOKit via ctypes; **not yet tested with a real mic**, keep that label until it is) |
| `firmware/patches/*.s` → `*.json` | add-on sources → assembled bytes + labels (`firmware/dev/assemble.py`, needs `rz-asm`) |
| `wizard/` | installer `podmicusb-doctor.exe` (Windows; on macOS run as a script with `python3`): reuses `firmware/tools/` (never duplicate their safety logic); tests in `wizard/tests/`; exe built by `.github/workflows/build.yml` (PyInstaller + certifi, build-time only) |
| `assets/` | logo and exe icon (`icon.svg` → `podmicusb-doctor.ico`, a static asset) |
| `firmware/tests/` | offline unit tests (synthetic images) |
| `firmware/dev/` | developer tools (assembler, `i2cfix-volume-holdmute` emulator; both need Rizin) |
| `research/README.md` | summary of the investigation; `research/NOTES.md` = chronological log, cite sections as (3s) etc. |
| `docker/` | Linux RE lab image (Rizin + rz-ghidra, TShark); not needed to use the fix |

## Hard rules: a real mic can be bricked or wedged
1. **Never raise the version marker** (u16 at image offset 0x20, `19 01` = 1.1.9): the bootloader's anti-downgrade
   check could then lock users out of stock (`research/protocol/bootloader-protocol.md` §6).
2. The flasher must refuse **only a lower** version than the mic reports (not "any different"). Keep that logic and
   its tests.
3. **Never write pages 46–47** (the mic's settings, bootloader-protocol.md §7). App images are at most 46 pages.
4. Every image needs a valid trailing CRC32 (zlib, little-endian, over all bytes except the last 4). The bootloader
   checks the image only after the last page; if the check fails it stops responding (recovery:
   `firmware/README.md#recovery`).
5. **Don't run device commands** (`flash`, `probe`, `diag-test`, the monitor) unless the human explicitly asks and has
   a mic attached. Tests use the simulated bootloader.
6. Don't send the hidden service commands `'G'`/`'S'` (codec register read/write over I2C) or `'T'`/`'L'` to a mic.
7. `build.py`'s `put()` checks the stock bytes before patching. Never bypass that check.

## Repo hygiene
- **Never commit** RØDE firmware (stock or patched `.bin`), RØDE software, USB captures (`.pcap*`), monitor logs, or
  anything containing a device serial number. `.gitignore` blocks the common extensions; still check `git status`.
- Users build images locally. Don't add download links to firmware binaries.
- Tools in `firmware/tools/` stay **Python standard library only** (Windows users run them with plain `py`, macOS
  users with `python3`). New OS backends implement the `WinHid` interface (`HidApiBase`, `hid_api()` in
  `podmic_flash.py`); protocol and safety logic stays in `podmic_flash.py`.

## Workflow
- Tests and the patch-change steps: `firmware/README.md` → "For developers". Run the tests after every change.
- Mark anything not run on hardware as such in the READMEs. Don't claim it's tested.

## Firmware facts you need
- Hardware and flash layout: `research/README.md` → "Hardware and firmware". App base **0x08004000**, Thumb-2.
- Disassemble: `rizin -a arm -b 16 -m 0x08004000 -e asm.cpu=cortexm podmicusb-1.1.9-stock.bin` (`af` then `pdg` with
  rz-ghidra).
- Add-on code goes in the 0xFF gap from **0x0800F460**. i2cfix-volume-holdmute also uses the zeroed reserved vector slots at
  0x08004104 (56 B). Add-on RAM up to 0x20001B10, zeroed by the (patched) startup `.bss` clear.
- Key stock addresses: I2C1 EV/ER NVIC priority `movs` at 0x08008eae/0x08008eb0 (the fix); I2C start 0x08008f24,
  done 0x08008f18; codec write 0x0800ad0c (its waits at 0x0800ad70/0x0800ad78); IWDG init 0x08009198 (prescaler byte
  at 0x0800919a); 4 ms task feeding the watchdog 0x08009b70; scheduler 0x0800c174; SysTick 0x0800c390; ms tick
  0x20001a9c.
- Bootloader HID protocol: `research/protocol/bootloader-protocol.md` (implemented in `podmic_flash.py`).
