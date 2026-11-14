"""
Offline tests for build.py.

Without RØDE data: a synthetic "stock" image carries only the stock bytes at the patched sites; the tests
check that each variant changes exactly the intended places, branches land on the add-on labels, the
CRC is valid and the version marker stays 1.1.9.

With your real stock image: set PODMIC_STOCK=path/to/podmicusb-1.1.9-stock.bin and the reference sha256s
are checked too (otherwise that test is skipped).

run from the repo root:  python -m unittest discover -s firmware/tests -v
"""
import hashlib
import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import build as b  # noqa: E402
import podmic_flash as pf  # noqa: E402

SIZE = 47104


def off(addr):
    return addr - b.BASE


def synthetic_stock():
    img = bytearray(SIZE)
    struct.pack_into("<II", img, 0, 0x20002000, 0x08007d31)          # SP, reset vector
    struct.pack_into("<I", img, 0x3c, 0x0800c391)                    # SysTick vector
    struct.pack_into("<H", img, 0x20, 0x0119)                        # version 1.1.9
    img[off(b.ADDON_AT):SIZE - 4] = b"\xff" * (SIZE - 4 - off(b.ADDON_AT))
    sites = {0x08008eae: b"\xc0\x21\xd0\x22", 0x08007d78: struct.pack("<I", 0x20001aa8),
             0x08009be4: b"\x13\x20", 0x0800a0aa: bytes.fromhex("604b1a78"),
             0x080092f4: struct.pack("<I", 0x08009299), b.BASE + 0x519a: b"\x02",
             0x08007f82: b.thumb_bl(0x08007f82, 0x08009c4c), 0x080086c0: bytes.fromhex("00236372")}
    for at in (0x0800ad70, 0x080091d0, 0x0800b0d2):
        sites[at] = b.thumb_bl(at, 0x08008f24)
    for at in (0x0800ad78, 0x080091d8, 0x0800b0da):
        sites[at] = b.thumb_bl(at, 0x08008f18)
    for addr, data in sites.items():
        img[off(addr):off(addr) + len(data)] = data
    img[-4:] = struct.pack("<I", zlib.crc32(bytes(img[:-4])))
    return bytes(img)


def decode_bl(hw):
    """Inverse of thumb_bl: return (target offset from at+4, is_link)."""
    hi, lo = struct.unpack("<HH", hw)
    s = (hi >> 10) & 1
    j1, j2 = (lo >> 13) & 1, (lo >> 11) & 1
    i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
    v = (s << 24) | (i1 << 23) | (i2 << 22) | ((hi & 0x3FF) << 12) | ((lo & 0x7FF) << 1)
    if s:
        v -= 1 << 25
    return v, bool(lo & 0x4000)


def bl_target(img, at):
    rel, link = decode_bl(img[off(at):off(at) + 4])
    return at + 4 + rel, link


def changed_ranges(a, c):
    out, i = [], 0
    while i < len(a):
        if a[i] != c[i]:
            j = i
            while j < len(a) and a[j] != c[j]:
                j += 1
            out.append((b.BASE + i, b.BASE + j))
            i = j
        else:
            i += 1
    return out


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.stock = synthetic_stock()
        self._orig = b.STOCK_SHA256
        b.STOCK_SHA256 = hashlib.sha256(self.stock).hexdigest()

    def tearDown(self):
        b.STOCK_SHA256 = self._orig

    def allowed(self, variant):
        code, _ = b.load_addon(variant)
        r = [(b.ADDON_AT, b.ADDON_AT + len(code)), (b.BASE + SIZE - 4, b.BASE + SIZE)]
        if variant in ("i2cfix", "i2cfix-volume-holdmute"):
            r += [(0x08008eae, 0x08008eb2), (0x08007d78, 0x08007d7c)]
            r += [(a, a + 4) for a in (0x0800ad70, 0x080091d0, 0x0800b0d2, 0x0800ad78, 0x080091d8, 0x0800b0da)]
        if variant == "i2cfix-volume-holdmute":
            r += [(0x08009be4, 0x08009be6), (b.TABLE_AT, b.TABLE_AT + 56), (0x0800a0aa, 0x0800a0ae),
                  (0x080092f4, 0x080092f8)]
        if variant == "diag":
            r += [(b.BASE + 0x519a, b.BASE + 0x519b), (b.BASE + 4, b.BASE + 8), (b.BASE + 0x3c, b.BASE + 0x40),
                  (0x08007f82, 0x08007f86), (0x080086c0, 0x080086c4)]
        return r

    def test_only_intended_bytes_change(self):
        for v in ("i2cfix", "i2cfix-volume-holdmute", "diag"):
            img = b.build(self.stock, v)
            for lo, hi in changed_ranges(self.stock, img):
                self.assertTrue(any(a <= lo and hi <= z for a, z in self.allowed(v)),
                                "%s: unexpected change at 0x%08x..0x%08x" % (v, lo, hi))

    def test_crc_vectors_version(self):
        for v in ("i2cfix", "i2cfix-volume-holdmute", "diag"):
            info, warns = pf.check_image(b.build(self.stock, v))
            self.assertEqual(info["version_raw"], 0x0119, v)   # never raise the version marker
            self.assertLessEqual(info["pages"], pf.MAX_APP_PAGES)

    def test_addon_placed(self):
        for v in ("i2cfix", "i2cfix-volume-holdmute", "diag"):
            code, _ = b.load_addon(v)
            img = b.build(self.stock, v)
            self.assertEqual(img[off(b.ADDON_AT):off(b.ADDON_AT) + len(code)], code)

    def test_i2c_priority_and_hooks(self):
        for v in ("i2cfix", "i2cfix-volume-holdmute"):
            img = b.build(self.stock, v)
            _, L = b.load_addon(v)
            self.assertEqual(img[off(0x08008eae):off(0x08008eb2)], b"\x10\x21\x10\x22")
            for at in (0x0800ad70, 0x080091d0, 0x0800b0d2):
                self.assertEqual(bl_target(img, at), (L["start_hook"], True))
            for at in (0x0800ad78, 0x080091d8, 0x0800b0da):
                self.assertEqual(bl_target(img, at), (L["wait_done"], True))

    def test_holdmute_extras(self):
        img = b.build(self.stock, "i2cfix-volume-holdmute")
        _, L = b.load_addon("i2cfix-volume-holdmute")
        self.assertEqual(bl_target(img, 0x0800a0aa), (L["tone_hook"], True))
        self.assertEqual(struct.unpack_from("<I", img, off(0x080092f4))[0], L["btn_task"] | 1)

    def test_diag_hooks(self):
        img = b.build(self.stock, "diag")
        _, L = b.load_addon("diag")
        self.assertEqual(struct.unpack_from("<I", img, 4)[0], L["reset_stub"] | 1)
        self.assertEqual(struct.unpack_from("<I", img, 0x3c)[0], L["st_hook"] | 1)
        self.assertEqual(bl_target(img, 0x08007f82), (L["ml_hook"], True))
        self.assertEqual(bl_target(img, 0x080086c0), (L["diag_reply"], False))

    def test_wrong_input_refused(self):
        bad = bytearray(self.stock)
        bad[0x1000] ^= 1
        with self.assertRaisesRegex(b.BuildError, "sha256"):
            b.build(bytes(bad), "i2cfix")

    def test_thumb_bl_roundtrip(self):
        for at, target in ((0x0800ad70, 0x0800f460), (0x0800f500, 0x08004400), (0x08004000, 0x0800fffe)):
            for link in (True, False):
                self.assertEqual(decode_bl(b.thumb_bl(at, target, link)), (target - at - 4, link))


@unittest.skipUnless(os.environ.get("PODMIC_STOCK"), "set PODMIC_STOCK=<stock 1.1.9 image> to run")
class ReferenceBuildTest(unittest.TestCase):
    def test_reference_sha256(self):
        with open(os.environ["PODMIC_STOCK"], "rb") as f:
            stock = f.read()
        for v, h in b.REFERENCE_SHA256.items():
            self.assertEqual(hashlib.sha256(b.build(stock, v)).hexdigest(), h, v)


if __name__ == "__main__":
    unittest.main()
