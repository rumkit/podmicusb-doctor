"""
Offline tests for the wizard: stock-image sourcing and the full flash flow against a simulated mic
(app mode -> bootloader -> app). Synthetic images only; no RØDE firmware, no mic, no network.

run from the repo root:  python -m unittest discover -s wizard/tests -v
"""
import contextlib
import hashlib
import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "firmware", "tests"))
import podmicusb_doctor as wz   # noqa: E402
import test_flasher as tf       # noqa: E402  (make_image, FakeBootloader)

pf = wz.pf


class FakeMic:
    """One simulated PodMic USB that switches between app mode and the bootloader like the real one."""

    def __init__(self, bcd=0x0119, mode="app"):
        self.bcd, self.mode = bcd, mode
        self.boot = tf.FakeBootloader()
        self.app_sent = []

    # api side
    def find(self, pid):
        if pid == pf.PID_APP and self.mode == "app":
            return [{"pid": pf.PID_APP, "bcd": self.bcd, "usage_page": pf.USAGE_PAGE_VENDOR,
                     "in_len": 64, "out_len": 64}]
        if pid == pf.PID_BOOT and self.mode == "boot":
            return [{"pid": pf.PID_BOOT, "bcd": 0x0100, "usage_page": pf.USAGE_PAGE_VENDOR,
                     "in_len": pf.CMD_LEN, "out_len": pf.DATA_LEN}]
        return []

    def wait_for(self, pid, timeout=0):
        found = self.find(pid)
        if len(found) != 1:
            raise pf.FlashError("device PID 0x%04x did not appear" % pid)
        return found[0]

    def open(self, desc):
        return self

    # device side
    def flush(self):
        self.boot.flush()

    def send(self, r):
        r = bytes(r)
        if self.mode == "app":
            self.app_sent.append(r)
            if r[:2] == b"\x02R":
                self.mode = "boot"
            return
        self.boot.send(r)
        if r[:2] == b"\x02R":
            if self.boot.flash:     # new firmware: the app reports the image's version
                img = b"".join(self.boot.flash[i] for i in sorted(self.boot.flash))
                self.bcd = int.from_bytes(img[0x20:0x22], "little")
            self.mode = "app"

    def recv(self, timeout):
        return self.boot.recv(timeout)

    def close(self):
        pass


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()):
        yield


class FlashFlowTest(unittest.TestCase):
    def test_flash_writes_image_and_mic_returns(self):
        img = tf.make_image()
        mic = FakeMic()
        with quiet():
            d = wz.flash(mic, img, log=lambda *_: None)
        self.assertEqual(mic.mode, "app")
        self.assertEqual(d["bcd"], 0x0119)
        self.assertEqual(b"".join(mic.boot.flash[i] for i in sorted(mic.boot.flash)), img)
        self.assertLess(max(mic.boot.erased), pf.SETTINGS_FIRST_PAGE)

    def test_lower_version_refused_before_anything_is_sent(self):
        mic = FakeMic(bcd=0x0120)
        with quiet(), self.assertRaisesRegex(pf.FlashError, "LOWER"):
            wz.flash(mic, tf.make_image(version=0x0119))
        self.assertEqual(mic.app_sent, [])
        self.assertEqual(mic.mode, "app")

    def test_nak_leaves_mic_in_bootloader(self):
        mic = FakeMic()
        mic.boot.nak_erase = 3
        with quiet(), self.assertRaisesRegex(pf.FlashError, "erase page 3"):
            wz.flash(mic, tf.make_image(), log=lambda *_: None)
        self.assertEqual(mic.mode, "boot")
        self.assertNotIn(4, mic.boot.erased)

    def test_bad_crc_refused_before_anything_is_sent(self):
        bad = bytearray(tf.make_image())
        bad[200] ^= 1
        mic = FakeMic()
        with self.assertRaisesRegex(pf.FlashError, "CRC32"):
            wz.flash(mic, bytes(bad))
        self.assertEqual(mic.app_sent, [])

    def test_from_bootloader_mode(self):
        mic = FakeMic(mode="boot")
        with quiet():
            wz.flash(mic, tf.make_image(), log=lambda *_: None)
        self.assertEqual(mic.mode, "app")


class FindMicTest(unittest.TestCase):
    def test_none(self):
        api = mock.Mock(find=lambda pid: [])
        with self.assertRaisesRegex(wz.WizardError, "no PodMic"):
            wz.find_mic(api)

    def test_two(self):
        api = mock.Mock(find=lambda pid: [{"pid": pid}])
        with self.assertRaisesRegex(wz.WizardError, "more than one"):
            wz.find_mic(api)

    def test_app_and_boot(self):
        self.assertEqual(wz.find_mic(FakeMic())[0], "app")
        self.assertEqual(wz.find_mic(FakeMic(mode="boot"))[0], "boot")


class StockSourceTest(unittest.TestCase):
    IMG = tf.make_image(seed=7)

    def setUp(self):
        p = mock.patch.object(wz, "STOCK_SHA256", hashlib.sha256(self.IMG).hexdigest())
        p.start()
        self.addCleanup(p.stop)

    def fetch(self, mapping):
        def f(url):
            v = mapping[url]
            if isinstance(v, Exception):
                raise v
            return v
        return f

    def test_download_when_no_local_install(self):
        img = wz.get_stock(fetch=self.fetch({wz.FIRMWARE_URL: self.IMG}), find_dll=lambda p: None,
                           log=lambda *_: None)
        self.assertEqual(img, self.IMG)

    def test_changed_server_file_names_new_version(self):
        manifest = json.dumps({"podmic-usb-manifest": {"main-version": {"update-version": "1.2.0"}}}).encode()
        f = self.fetch({wz.FIRMWARE_URL: b"something else", wz.MANIFEST_URL: manifest})
        with self.assertRaisesRegex(wz.WizardError, "version 1.2.0"):
            wz.get_stock(fetch=f, find_dll=lambda p: None, log=lambda *_: None)

    def test_changed_server_file_without_manifest(self):
        f = self.fetch({wz.FIRMWARE_URL: b"x", wz.MANIFEST_URL: urllib.error.URLError("down")})
        with self.assertRaisesRegex(wz.WizardError, "no longer offers"):
            wz.get_stock(fetch=f, find_dll=lambda p: None, log=lambda *_: None)

    def test_network_error(self):
        f = self.fetch({wz.FIRMWARE_URL: urllib.error.URLError("no route")})
        with self.assertRaisesRegex(wz.WizardError, "download failed"):
            wz.get_stock(fetch=f, find_dll=lambda p: None, log=lambda *_: None)

    def test_local_install_preferred_no_network(self):
        def no_net(url):
            raise AssertionError("network used")
        with mock.patch.object(wz, "local_stock", return_value=self.IMG):
            self.assertEqual(wz.get_stock(fetch=no_net, log=lambda *_: None), self.IMG)


@unittest.skipUnless(os.environ.get("PODMIC_STOCK"), "set PODMIC_STOCK to a stock 1.1.9 image")
class RealStockTest(unittest.TestCase):
    def test_every_menu_entry_builds_the_reference_image(self):
        with open(os.environ["PODMIC_STOCK"], "rb") as f:
            stock = f.read()
        for key, _, _ in wz.VARIANTS:
            img = wz.make_image(stock, key)
            want = wz.STOCK_SHA256 if key is None else wz.build.REFERENCE_SHA256[key]
            self.assertEqual(hashlib.sha256(img).hexdigest(), want, key)
            self.assertEqual(img[0x20:0x22], b"\x19\x01")      # version marker never raised


if __name__ == "__main__":
    unittest.main()
