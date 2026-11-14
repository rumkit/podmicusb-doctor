"""
Offline tests for podmic_flash.py: protocol and safety rules against a simulated bootloader.
Uses a synthetic (random) image, so no RØDE firmware and no mic are needed.

run from the repo root:  python -m unittest discover -s firmware/tests -v
"""
import os
import random
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import podmic_flash as pf  # noqa: E402


def make_image(pages=46, version=0x0119, seed=1):
    """Random image with a plausible vector table, a version at 0x20 and a valid trailing CRC32."""
    rnd = random.Random(seed)
    body = bytearray(rnd.getrandbits(8) for _ in range(pages * pf.PAGE_SIZE - 4))
    struct.pack_into("<II", body, 0, 0x20002000, pf.APP_BASE + 0x3d31)
    struct.pack_into("<H", body, 0x20, version)
    return bytes(body) + struct.pack("<I", zlib.crc32(body))


class FakeBootloader:
    """Answers like the real bootloader in the factory-restore capture."""

    def __init__(self, nak_erase=None, info=bytes.fromhex("01494101000004300000")):
        self.sent, self.queue, self.flash, self.erased = [], [], {}, set()
        self.nak_erase, self.info, self.last = nak_erase, info, None

    def flush(self):
        self.queue.clear()

    def send(self, r):
        r = bytes(r)
        self.sent.append(r)
        rid, c = r[0], r[1]
        if rid == 2 and c == ord("I"):
            self.queue.append(self.info)
        elif rid == 2 and c == ord("E"):
            assert len(r) == pf.CMD_LEN
            self.erased.add(r[2])
            self.last = r[2]
            self.queue.append(bytes([1, 0x45, 0x4E if r[2] == self.nak_erase else 0x41]) + bytes(7))
        elif rid == 3:
            assert len(r) == pf.DATA_LEN and self.last in self.erased, "write without erase"
            self.flash[self.last] = r[1:]
            self.queue.append(bytes([1, 0x50, 0x41]) + bytes(7))
        elif rid == 2 and c == ord("R"):
            pass
        else:
            raise AssertionError("unexpected report " + r.hex())

    def recv(self, timeout):
        return self.queue.pop(0) if self.queue else None


def flash(dev, data):
    pf.boot_info(dev)
    pf.write_pages(dev, pf.pages_of(data), log=lambda *_: None)
    pf.reboot(dev)


class FakeApi:
    def __init__(self, bcd):
        self.bcd = bcd

    def find(self, pid):
        return [{"pid": pf.PID_APP, "bcd": self.bcd}] if pid == pf.PID_APP else []


class ProtocolTest(unittest.TestCase):
    def test_report_stream(self):
        data = make_image()
        dev = FakeBootloader()
        flash(dev, data)
        expected = [pf.cmd("I")]
        for n, page in enumerate(pf.pages_of(data)):
            expected += [pf.cmd("E", n), bytes([3]) + page]
        expected.append(pf.cmd("R"))
        self.assertEqual(dev.sent, expected)
        self.assertEqual(len(dev.sent), 94)   # same count as RØDE Central's factory restore (46 pages)

    def test_flash_content_equals_image(self):
        data = make_image()
        dev = FakeBootloader()
        flash(dev, data)
        self.assertEqual(b"".join(dev.flash[i] for i in sorted(dev.flash)), data)

    def test_settings_pages_never_erased(self):
        dev = FakeBootloader()
        flash(dev, make_image(pages=46))
        self.assertLess(max(dev.erased), pf.SETTINGS_FIRST_PAGE)

    def test_short_image_padded_with_ff(self):
        data = make_image(pages=45)[:-4]
        data = data[:-100]
        data += struct.pack("<I", zlib.crc32(data))
        pages = pf.pages_of(data)
        self.assertEqual(len(pages), 45)
        self.assertTrue(pages[-1].endswith(b"\xff" * 100))

    def test_nak_stops_without_reboot(self):
        dev = FakeBootloader(nak_erase=5)
        pf.boot_info(dev)
        with self.assertRaisesRegex(pf.FlashError, "erase page 5"):
            pf.write_pages(dev, pf.pages_of(make_image()), log=lambda *_: None)
        self.assertFalse(any(r[0] == 2 and r[1] == ord("R") for r in dev.sent))
        self.assertNotIn(6, dev.erased)

    def test_no_reply_is_an_error(self):
        dev = FakeBootloader()
        dev.send = lambda r: None   # bootloader that never answers
        with self.assertRaisesRegex(pf.FlashError, "no reply"):
            pf.boot_info(dev)

    def test_unknown_bootloader_refused(self):
        dev = FakeBootloader(info=bytes.fromhex("01494102000004300000"))
        with self.assertRaisesRegex(pf.FlashError, "differs from the known"):
            pf.boot_info(dev)
        self.assertEqual(dev.erased, set())


class ImageCheckTest(unittest.TestCase):
    def test_good_image(self):
        info, warns = pf.check_image(make_image())
        self.assertEqual(info["version_raw"], 0x0119)
        self.assertEqual(warns, [])

    def test_bit_flip_rejected(self):
        bad = bytearray(make_image())
        bad[100] ^= 1
        with self.assertRaisesRegex(pf.FlashError, "CRC32"):
            pf.check_image(bytes(bad))

    def test_too_many_pages_rejected(self):
        with self.assertRaisesRegex(pf.FlashError, "pages"):
            pf.check_image(make_image(pages=47))

    def _with_vectors(self, sp, reset):
        body = bytearray(make_image()[:-4])
        struct.pack_into("<II", body, 0, sp, reset)
        return bytes(body) + struct.pack("<I", zlib.crc32(body))

    def test_bad_stack_pointer_rejected(self):
        with self.assertRaisesRegex(pf.FlashError, "stack pointer"):
            pf.check_image(self._with_vectors(0x08001000, pf.APP_BASE + 0x101))

    def test_reset_vector_outside_image_rejected(self):
        with self.assertRaisesRegex(pf.FlashError, "outside the image"):
            pf.check_image(self._with_vectors(0x20002000, 0x08000101))   # bootloader area = wrong base

    def test_arm_mode_reset_vector_rejected(self):
        with self.assertRaisesRegex(pf.FlashError, "Thumb"):
            pf.check_image(self._with_vectors(0x20002000, pf.APP_BASE + 0x100))


class VersionRuleTest(unittest.TestCase):
    """The flasher must refuse only a LOWER version (the bootloader rejects it and wedges)."""

    def test_lower_refused(self):
        with self.assertRaisesRegex(pf.FlashError, "LOWER"):
            pf.compare_versions(FakeApi(0x0119), 0x0118)

    def test_equal_allowed(self):
        pf.compare_versions(FakeApi(0x0119), 0x0119)

    def test_higher_allowed_with_warning(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pf.compare_versions(FakeApi(0x0119), 0x0120)
        self.assertIn("HIGHER", out.getvalue())


if __name__ == "__main__":
    unittest.main()
