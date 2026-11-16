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


class ProgressTest(unittest.TestCase):
    class Out(io.StringIO):
        def __init__(self, tty):
            super().__init__()
            self.tty = tty

        def isatty(self):
            return self.tty

    def test_console_redraws_one_line(self):
        out = self.Out(True)
        step = wz.page_progress(4, out)
        for i in range(4):
            step("page %d" % i)
        text = out.getvalue()
        self.assertEqual(text.count("\r"), 4)
        self.assertEqual(text.count("\n"), 1)
        self.assertTrue(text.endswith("[" + "#" * 30 + "] page  4/4\n"))

    def test_redirected_output_gets_one_line_per_page(self):
        out = self.Out(False)
        step = wz.page_progress(3, out)
        for i in range(3):
            step("page %d ok" % i)
        self.assertEqual(out.getvalue(), "page 0 ok\npage 1 ok\npage 2 ok\n")


class MenuTest(unittest.TestCase):
    """Piped/redirected input (as under unittest): one answer per line, first character counts."""

    def choose(self, *lines):
        answers = iter(lines)

        def fake_input(prompt=""):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError
        with mock.patch.object(wz, "console_keys", return_value=False), \
                mock.patch("builtins.input", fake_input), quiet():
            return wz.choose_variant(wz.VARIANTS)

    def test_number_picks_variant(self):
        self.assertEqual(self.choose("2")[0], "i2cfix-volume-holdmute")

    def test_invalid_answers_are_ignored(self):
        self.assertEqual(self.choose("9", "x", "", "1")[0], "i2cfix")

    def test_q_aborts(self):
        with self.assertRaises(wz.Abort):
            self.choose("q")

    def test_end_of_input_aborts(self):
        with self.assertRaises(wz.Abort):
            self.choose()

    def test_console_single_key(self):
        keys = iter(["x", "", "3"])
        with mock.patch.object(wz, "console_keys", return_value=True), \
                mock.patch.object(wz, "getkey", side_effect=lambda: next(keys)), quiet():
            self.assertEqual(wz.choose_variant(wz.VARIANTS)[0], "diag")

    def test_console_esc_aborts(self):
        with mock.patch.object(wz, "console_keys", return_value=True), \
                mock.patch.object(wz, "getkey", return_value="esc"), quiet(), self.assertRaises(wz.Abort):
            wz.choose_variant(wz.VARIANTS)


class RunTest(unittest.TestCase):
    """The whole wizard against the simulated mic. Keyboard input is always the piped fallback (console_keys
    mocked False), so nothing can block on the real console."""
    IMG = tf.make_image(seed=3)

    def setUp(self):
        for p in (mock.patch.object(wz, "console_keys", return_value=False),
                  mock.patch.object(pf, "central_running", return_value=False),
                  mock.patch.object(pf, "SETTLE_S", 0),
                  mock.patch.object(wz, "get_stock", return_value=self.IMG),
                  mock.patch.object(wz, "make_image", side_effect=self.fake_make_image)):
            p.start()
            self.addCleanup(p.stop)
        self.built = []

    def fake_make_image(self, stock, key):
        self.built.append(key)
        return self.IMG

    def run_wizard(self, mic, *answers):
        it = iter(answers)

        def fake_input(prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        out = io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(out):
            try:
                rc = wz.run(mic)
            except (wz.WizardError, wz.Abort) as e:
                rc = e
        return rc, out.getvalue()

    def assert_untouched(self, mic):
        self.assertEqual(mic.app_sent, [])
        self.assertEqual(mic.boot.sent, [])
        self.assertEqual(mic.mode, "app")

    def test_newer_firmware_refused(self):
        mic = FakeMic(bcd=0x0120)
        rc, _ = self.run_wizard(mic, "1", "yes")
        self.assertIsInstance(rc, wz.WizardError)
        self.assertIn("newer", str(rc))
        self.assert_untouched(mic)
        wz.get_stock.assert_not_called()

    def test_older_firmware_refused(self):
        mic = FakeMic(bcd=0x0118)
        rc, _ = self.run_wizard(mic, "1", "yes")
        self.assertIn("older", str(rc))
        self.assert_untouched(mic)

    def test_anything_but_yes_flashes_nothing(self):
        mic = FakeMic()
        rc, out = self.run_wizard(mic, "1", "y")
        self.assertEqual(rc, 0)
        self.assertIn("Cancelled", out)
        self.assert_untouched(mic)

    def test_yes_flashes_chosen_variant(self):
        mic = FakeMic()
        rc, out = self.run_wizard(mic, "2", "YES")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.built, ["i2cfix-volume-holdmute"])
        self.assertEqual(b"".join(mic.boot.flash[i] for i in sorted(mic.boot.flash)), self.IMG)
        self.assertEqual(mic.mode, "app")
        self.assertIn(hashlib.sha256(self.IMG).hexdigest(), out)   # shown before confirming

    def test_bootloader_mode_offers_only_stock(self):
        mic = FakeMic(mode="boot")
        rc, out = self.run_wizard(mic, "2", "1", "yes")    # "2" is not offered, ignored
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.built, [None])

    def test_ctrl_c_while_writing_leaves_bootloader_and_shows_recovery(self):
        mic = FakeMic()
        with mock.patch.object(pf, "write_pages", side_effect=KeyboardInterrupt):
            rc, out = self.run_wizard(mic, "1", "yes")
        self.assertEqual(rc, 1)
        self.assertIn("UPDATE INTERRUPTED", out)
        self.assertIn("Unplug the mic", out)
        self.assertEqual(mic.mode, "boot")
        self.assertFalse(any(r[:2] == b"\x02R" for r in mic.boot.sent))

    def test_unexpected_error_while_flashing_shows_recovery(self):
        mic = FakeMic()
        with mock.patch.object(pf, "write_pages", side_effect=OSError("device gone")):
            rc, out = self.run_wizard(mic, "1", "yes")
        self.assertEqual(rc, 1)
        self.assertIn("UPDATE FAILED (unexpected error): OSError: device gone", out)
        self.assertIn("Unplug the mic", out)


class SelfTestTest(unittest.TestCase):
    IMG = tf.make_image(seed=7)

    def setUp(self):
        for p in (mock.patch.object(wz, "STOCK_SHA256", hashlib.sha256(self.IMG).hexdigest()),
                  mock.patch.object(wz, "make_image", return_value=b"built")):
            p.start()
            self.addCleanup(p.stop)

    def self_test(self, fetch, local):
        out = io.StringIO()
        with mock.patch.object(wz, "local_stock", return_value=local), contextlib.redirect_stdout(out):
            rc = wz.self_test(fetch=fetch, find_dll=lambda p: "rode_bridge.dll", verbose=False)
        return rc, out.getvalue()

    def test_rode_central_without_119_is_not_a_failure(self):
        rc, out = self.self_test(lambda url: self.IMG, None)
        self.assertEqual(rc, 0, out)
        self.assertIn("not found in this RØDE Central version", out)

    def test_download_failure_fails_even_with_local_copy(self):
        def offline(url):
            raise urllib.error.URLError("offline")
        rc, out = self.self_test(offline, self.IMG)
        self.assertEqual(rc, 1, out)
        self.assertIn("from RØDE Central: OK", out)


class VersionTest(unittest.TestCase):
    def test_banner(self):
        self.assertEqual(wz.program_name("1.1.0"), "podmicusb-doctor v1.1.0")
        self.assertEqual(wz.program_name("dev-abc1234"), "podmicusb-doctor dev-abc1234")

    def test_unbuilt_source_is_dev(self):
        if not os.path.exists(os.path.join(HERE, "..", "_version.py")):
            self.assertEqual(wz.VERSION, "dev")


class MainTest(unittest.TestCase):
    def test_unexpected_error_is_shown_not_raised(self):
        out = io.StringIO()
        with mock.patch.object(wz, "self_test", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(out):
            self.assertEqual(wz.main(["--self-test"]), 1)
        self.assertIn("UNEXPECTED ERROR: RuntimeError: boom", out.getvalue())

    def test_truncated_download_becomes_wizard_error(self):
        import http.client

        def f(url):
            raise http.client.IncompleteRead(b"abc", 100)
        with self.assertRaisesRegex(wz.WizardError, "IncompleteRead"):
            wz.download_stock(fetch=f, log=lambda *_: None)


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

    def test_certificate_error_explained(self):
        import ssl
        f = self.fetch({wz.FIRMWARE_URL: urllib.error.URLError(ssl.SSLCertVerificationError("unable to get issuer"))})
        with self.assertRaisesRegex(wz.WizardError, "certificate could not be verified"):
            wz.get_stock(fetch=f, find_dll=lambda p: None, log=lambda *_: None)

    def test_tls_context_verifies(self):
        import ssl
        ctx = wz.tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_prefer_download_uses_download_even_with_local_copy(self):
        other = tf.make_image(seed=8)     # local copy must not be used
        with mock.patch.object(wz, "local_stock", return_value=other) as loc:
            img = wz.get_stock(fetch=self.fetch({wz.FIRMWARE_URL: self.IMG}), log=lambda *_: None,
                               prefer_download=True)
        self.assertEqual(img, self.IMG)
        loc.assert_not_called()

    def test_prefer_download_falls_back_to_local(self):
        f = self.fetch({wz.FIRMWARE_URL: urllib.error.URLError("offline")})
        with mock.patch.object(wz, "local_stock", return_value=self.IMG):
            self.assertEqual(wz.get_stock(fetch=f, log=lambda *_: None, prefer_download=True), self.IMG)

    def test_prefer_download_both_fail(self):
        f = self.fetch({wz.FIRMWARE_URL: urllib.error.URLError("offline")})
        with mock.patch.object(wz, "local_stock", return_value=None), \
                self.assertRaisesRegex(wz.WizardError, "download failed"):
            wz.get_stock(fetch=f, log=lambda *_: None, prefer_download=True)

    def test_local_install_preferred_no_network(self):
        def no_net(url):
            raise AssertionError("network used")
        with mock.patch.object(wz, "local_stock", return_value=self.IMG):
            self.assertEqual(wz.get_stock(fetch=no_net, log=lambda *_: None), self.IMG)


class ArgsTest(unittest.TestCase):
    def test_flags(self):
        a = wz.parse_args([])
        self.assertFalse(a.self_test or a.verbose or a.download)
        a = wz.parse_args(["--self-test", "--verbose", "--download"])
        self.assertTrue(a.self_test and a.verbose and a.download)

    def test_unknown_flag_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            wz.parse_args(["--details"])


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
