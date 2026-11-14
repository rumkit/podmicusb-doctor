#!/usr/bin/env python3
"""
build.py - build a patched PodMic USB firmware image from YOUR stock 1.1.9 image.

This repository contains no RØDE code. Get the stock image with extract_stock.py, then:

  py build.py VARIANT [STOCK] [-o OUT]
     VARIANT  i2cfix | i2cfix-volume-holdmute | diag   (see firmware/README.md)
     STOCK    stock image (default: podmicusb-1.1.9-stock.bin in the current folder)
     OUT      output file (default: podmicusb-1.1.9-<variant>.bin)
  py build.py all [STOCK]       build all three

The result is compared with the sha256 of the reference build listed in firmware/README.md. A match
means you have byte-for-byte the same image the author built and checked. The version marker stays 1.1.9 in every variant (on purpose:
never raise it, the bootloader's anti-downgrade could then block going back to stock).

Variants
  i2cfix  the fix: I2C1 event/error interrupt priority 0xc0/0xd0 -> 0x10 (the root-cause fix), plus a
        10 ms timeout on the blocking I2C waits with peripheral reset + the stock retry (safety net).
  i2cfix-volume-holdmute
        i2cfix + headphone level at power-up -24 dB instead of -31.5 dB (wheel step 19 -> 24)
        + hold the wheel button >= 0.7 s to mute/unmute the mic (short click unchanged, acts on release)
        + short mute/unmute jingle in the headphones. --tone-db sets its level (default -18.06 dBFS,
        the level tested on hardware; other values give a custom build).
  diag  debugging only, does NOT fix anything: watchdog ~13 s instead of ~1.6 s, records where the
        main loop hung (code address + I2C registers) in RTC backup registers; read back with
        `podmic_flash.py diag` or `podmic_monitor.py --advanced`.
"""
import argparse
import hashlib
import json
import math
import os
import struct
import sys
import zlib

STOCK_SHA256 = "11703ba4b1f4ed1d4d59243225547b596ebcbae8fffe48c302a63fd5b70b8d4c"
BASE = 0x08004000
ADDON_AT = 0x0800f460         # free 0xFF area after the .data init image
TABLE_AT = 0x08004104         # reserved vector slots (zero in stock); i2cfix-volume-holdmute.s hardcodes TABLE_AT + 48
PATCHES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "patches")
DEFAULT_TONE = 0x10000000     # -18.06 dBFS, jingle level tested on hardware (test image fix2p)

REFERENCE_SHA256 = {
    "i2cfix": "be3f5fa5698f870b5ec7bfd631a66910474991deeccd3a6c50febc6b98fabc82",
    "i2cfix-volume-holdmute": "f47071d7e1e079b3bc05e96fe547bf810933fab95d7fad2562429330c948b840",
    "diag": "ee5a84bdb26ef2e7f16bb76813901db1e162f884d711f4e35f183d38b1e43f06",
}
OUT_NAMES = {
    "i2cfix": "podmicusb-1.1.9-i2cfix.bin",
    "i2cfix-volume-holdmute": "podmicusb-1.1.9-i2cfix-volume-holdmute.bin",
    "diag": "podmicusb-1.1.9-diag.bin",
}


class BuildError(Exception):
    pass


def thumb_bl(at, target, link=True):
    """Encode a Thumb-2 BL (link=True) or B.W at address `at` to `target`."""
    off = target - (at + 4)
    s = 1 if off < 0 else 0
    off &= (1 << 25) - 1
    i1, i2 = (off >> 23) & 1, (off >> 22) & 1
    j1, j2 = (1 - (i1 ^ s)) & 1, (1 - (i2 ^ s)) & 1
    hi = 0xF000 | (s << 10) | ((off >> 12) & 0x3FF)
    lo = (0xD000 if link else 0x9000) | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hi, lo)


def load_addon(name):
    with open(os.path.join(PATCHES, name + ".json"), encoding="utf-8") as f:
        j = json.load(f)
    code = bytes.fromhex("".join(j["code"]))
    if int(j["org"], 16) != ADDON_AT or len(code) != j["length"]:
        raise BuildError("%s.json is inconsistent (org/length)" % name)
    return code, {k: int(v, 16) for k, v in j["labels"].items()}


def tone_tables(amp, fs=48000):
    """Resonator start values for the holdmute jingle: 1000 Hz, 667 Hz, silence + the two sequences."""
    def segment(freq, n):
        if freq == 0:
            return struct.pack("<IiIi", n, 0, 0, 0)
        w = 2 * math.pi * freq / fs
        return struct.pack("<IiIi", n, round(-amp * math.sin(w)), round(2 * math.cos(w) * 2 ** 30),
                           round(-amp * math.sin(2 * w)))
    return segment(1000, 3840) + segment(fs / 72, 3888) + segment(0, 1920) + bytes([1, 2, 0, 3, 0, 2, 1, 3])


def build(stock, variant, tone_amp=DEFAULT_TONE):
    if hashlib.sha256(stock).hexdigest() != STOCK_SHA256:
        raise BuildError("input is not the stock PodMic USB 1.1.9 image (sha256 mismatch) - "
                         "use extract_stock.py to get it")
    if variant not in REFERENCE_SHA256:
        raise BuildError("unknown variant %r (i2cfix, i2cfix-volume-holdmute, diag)" % variant)
    addon, L = load_addon(variant)
    if ADDON_AT + len(addon) > BASE + len(stock) - 4:
        raise BuildError("add-on too long")
    img = bytearray(stock)

    def put(addr, new, old):
        """Replace bytes at addr, but only if they are exactly the expected stock bytes."""
        o = addr - BASE
        if bytes(img[o:o + len(old)]) != old:
            raise BuildError("unexpected bytes at %#x" % addr)
        img[o:o + len(new)] = new

    if variant in ("i2cfix", "i2cfix-volume-holdmute"):
        # root-cause fix: I2C1 EV/ER NVIC priority 0xc0/0xd0 -> 0x10 (above audio DMA 0x40 and USB 0x70)
        put(0x08008eae, b"\x10\x21\x10\x22", b"\xc0\x21\xd0\x22")
        # extend the startup .bss clear so the add-on's RAM variables start at zero
        put(0x08007d78, struct.pack("<I", 0x20001b10), struct.pack("<I", 0x20001aa8))
        # the 3 blocking I2C start-waits and 3 done-waits -> bounded versions in the add-on
        for at in (0x0800ad70, 0x080091d0, 0x0800b0d2):
            put(at, thumb_bl(at, L["start_hook"]), thumb_bl(at, 0x08008f24))
        for at in (0x0800ad78, 0x080091d8, 0x0800b0da):
            put(at, thumb_bl(at, L["wait_done"]), thumb_bl(at, 0x08008f18))
    if variant == "i2cfix-volume-holdmute":
        put(0x08009be4, b"\x18\x20", b"\x13\x20")                       # boot wheel step 19 -> 24
        put(TABLE_AT, tone_tables(tone_amp), b"\x00" * 56)              # jingle tables
        put(0x0800a0aa, thumb_bl(0x0800a0aa, L["tone_hook"]), bytes.fromhex("604b1a78"))  # audio ISR mix hook
        put(0x080092f4, struct.pack("<I", L["btn_task"] | 1), struct.pack("<I", 0x08009299))  # button task
    if variant == "diag":
        put(BASE + 0x519a, b"\x05", b"\x02")                            # IWDG prescaler /16 -> /128 (~13 s)
        put(BASE + 0x04, struct.pack("<I", L["reset_stub"] | 1), struct.pack("<I", 0x08007d31))  # reset vector
        put(BASE + 0x3c, struct.pack("<I", L["st_hook"] | 1), struct.pack("<I", 0x0800c391))     # SysTick vector
        put(0x08007f82, thumb_bl(0x08007f82, L["ml_hook"]), thumb_bl(0x08007f82, 0x08009c4c))   # main-loop counter
        put(0x080086c0, thumb_bl(0x080086c0, L["diag_reply"], link=False), bytes.fromhex("00236372"))  # HID 'B'
    put(ADDON_AT, addon, b"\xff" * len(addon))
    img[-4:] = struct.pack("<I", zlib.crc32(bytes(img[:-4])))
    return bytes(img)


def tone_amp_from_db(db):
    if not -40 <= db <= -6:
        raise BuildError("--tone-db out of range -40..-6")
    return round(2 ** 31 * 10 ** (db / 20))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("variant", choices=sorted(REFERENCE_SHA256) + ["all"])
    ap.add_argument("stock", nargs="?", default="podmicusb-1.1.9-stock.bin")
    ap.add_argument("-o", "--out", help="output file (not with 'all')")
    ap.add_argument("--tone-db", type=float, help="holdmute jingle level in dBFS (-40..-6), default -18.06")
    a = ap.parse_args(argv)
    try:
        if a.out and a.variant == "all":
            raise BuildError("-o can't be used with 'all'")
        amp = tone_amp_from_db(a.tone_db) if a.tone_db is not None else DEFAULT_TONE
        with open(a.stock, "rb") as f:
            stock = f.read()
        for v in sorted(REFERENCE_SHA256) if a.variant == "all" else [a.variant]:
            img = build(stock, v, amp)
            out = a.out or OUT_NAMES[v]
            with open(out, "wb") as f:
                f.write(img)
            h = hashlib.sha256(img).hexdigest()
            verdict = ("= reference build" if h == REFERENCE_SHA256[v] else
                       "CUSTOM build (differs from the reference)")
            print("wrote %s\n  sha256 %s  %s" % (out, h, verdict))
    except (BuildError, OSError) as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
