"""
Offline tests for the macOS HID backend (podmic_hid_mac.py). A fake IOKit/CoreFoundation stands in for the
frameworks, so these run on any OS, including the real ctypes input-report callback. On macOS, LiveBindingTest
also loads the real frameworks and enumerates (no mic needed: it only proves the bindings work).

run from the repo root:  python -m unittest discover -s firmware/tests -v
"""
import contextlib
import ctypes
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import podmic_flash as pf      # noqa: E402
import podmic_hid_mac as mac   # noqa: E402
import test_flasher as tf      # noqa: E402  (make_image, FakeBootloader)

APP = {"VendorID": pf.VID, "ProductID": pf.PID_APP, "VersionNumber": 0x0119, "PrimaryUsagePage": 0xFF00,
       "MaxInputReportSize": 64, "MaxOutputReportSize": 64}
BOOT = {"VendorID": pf.VID, "ProductID": pf.PID_BOOT, "VersionNumber": 0x0100, "PrimaryUsagePage": 0xFF00,
        "MaxInputReportSize": pf.CMD_LEN, "MaxOutputReportSize": pf.DATA_LEN}
APP_AUDIO = dict(APP, PrimaryUsagePage=0x000C)            # the mic's other (consumer control) HID interface
OTHER_VENDOR = dict(APP, VendorID=0x046D)


class FakeFrameworks:
    """Just enough IOKit + CoreFoundation. CF objects are small ints; numbers/strings/sets live in tables."""

    NUMBER_TYPE, STRING_TYPE = 7, 8

    def __init__(self, devices, bootloader=None, strip_report_id=False):
        self.next_id = 1000
        self.objects = {}            # id -> ("num", int) | ("str", name) | ("set", [ids]) | ("dict", {})
        self.devices = {}            # id -> props
        self.attached = []           # props currently on the bus
        self.attach(*devices)
        self.callbacks = {}          # device id -> (buffer, length, callback)
        self.opened, self.scheduled, self.released = set(), set(), []
        self.sent = []
        self.boot = bootloader
        self.strip_report_id = strip_report_id
        self.open_rc = 0
        self.cf, self.io = self, self

    def _new(self, obj):
        self.next_id += 1
        self.objects[self.next_id] = obj
        return self.next_id

    def attach(self, *props):
        for p in props:
            self.attached.append(p)

    def as_tuple(self):
        return self, self, {"run_loop_mode": 1, "key_callbacks": 2, "value_callbacks": 3}

    # CoreFoundation
    def CFRelease(self, ref):
        self.released.append(ref)

    def CFRetain(self, ref):
        return ref

    def CFGetTypeID(self, ref):
        return {"num": self.NUMBER_TYPE, "str": self.STRING_TYPE}.get(self.objects[ref][0], 0)

    def CFNumberGetTypeID(self):
        return self.NUMBER_TYPE

    def CFNumberCreate(self, alloc, kind, ptr):
        return self._new(("num", ptr._obj.value))

    def CFNumberGetValue(self, ref, kind, ptr):
        ptr._obj.value = self.objects[ref][1]
        return True

    def CFStringCreateWithCString(self, alloc, s, enc):
        return self._new(("str", s.decode()))

    def CFDictionaryCreateMutable(self, alloc, cap, kcb, vcb):
        return self._new(("dict", {}))

    def CFDictionarySetValue(self, d, k, v):
        self.objects[d][1][self.objects[k][1]] = self.objects[v][1]

    def CFSetGetCount(self, ref):
        return len(self.objects[ref][1])

    def CFSetGetValues(self, ref, out):
        for i, v in enumerate(self.objects[ref][1]):
            out[i] = v

    def CFRunLoopGetCurrent(self):
        return 99

    def CFRunLoopRunInMode(self, mode, seconds, once):
        """Deliver one pending reply from the simulated bootloader through the registered callback."""
        if self.boot is None or not self.boot.queue:
            return 0
        reply = self.boot.queue.pop(0)
        for dev, (buf, length, cb) in self.callbacks.items():
            data = reply[1:] if self.strip_report_id else reply
            ctypes.memmove(buf, data, len(data))
            cb(None, 0, dev, 0, reply[0], ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)), len(data))
        return 0

    # IOKit
    def IOHIDManagerCreate(self, alloc, opts):
        return self._new(("mgr", None))

    def IOHIDManagerScheduleWithRunLoop(self, mgr, loop, mode):
        pass

    def IOHIDManagerSetDeviceMatching(self, mgr, matching):
        self.matching = dict(self.objects[matching][1])

    def IOHIDManagerCopyDevices(self, mgr):
        ids = []
        for props in self.attached:
            if all(props.get(k) == v for k, v in self.matching.items()):
                dev = self._new(("dev", None))
                self.devices[dev] = props
                ids.append(dev)
        return self._new(("set", ids)) if ids else None

    def IOHIDDeviceGetProperty(self, dev, key):
        name = self.objects[key][1]
        if name not in self.devices[dev]:
            return None
        return self._new(("num", self.devices[dev][name]))

    def IOHIDDeviceOpen(self, dev, opts):
        if self.open_rc == 0:
            self.opened.add(dev)
        return self.open_rc

    def IOHIDDeviceClose(self, dev, opts):
        self.opened.discard(dev)
        return 0

    def IOHIDDeviceScheduleWithRunLoop(self, dev, loop, mode):
        self.scheduled.add(dev)

    def IOHIDDeviceUnscheduleFromRunLoop(self, dev, loop, mode):
        self.scheduled.discard(dev)

    def IOHIDDeviceRegisterInputReportCallback(self, dev, buf, length, cb, ctx):
        if cb:
            self.callbacks[dev] = (buf, length, cb)
        else:
            self.callbacks.pop(dev, None)

    def IOHIDDeviceSetReport(self, dev, rtype, report_id, data, length):
        data = bytes(data[:length])
        assert rtype == mac.kIOHIDReportTypeOutput and data[0] == report_id
        self.sent.append(data)
        if self.boot is not None:
            self.boot.send(data)
        return 0


def api_with(*devices, **kw):
    fw = FakeFrameworks(devices, **kw)
    return mac.MacHid(fw.as_tuple()), fw


class EnumerateTest(unittest.TestCase):
    def test_descriptor_has_the_windows_keys(self):
        api, _ = api_with(BOOT)
        d, = api.enumerate()
        for k, v in (("vid", pf.VID), ("pid", pf.PID_BOOT), ("bcd", 0x0100), ("usage_page", 0xFF00),
                     ("in_len", pf.CMD_LEN), ("out_len", pf.DATA_LEN)):
            self.assertEqual(d[k], v, k)
        with contextlib.redirect_stdout(io.StringIO()):
            pf.check_boot_desc(d)        # the flasher's length check passes unchanged

    def test_only_rode_devices_matched(self):
        api, fw = api_with(APP, OTHER_VENDOR)
        self.assertEqual([d["vid"] for d in api.enumerate()], [pf.VID])
        self.assertEqual(fw.matching, {"VendorID": pf.VID})

    def test_find_picks_vendor_interface(self):
        api, _ = api_with(APP_AUDIO, APP)
        found = api.find(pf.PID_APP)
        self.assertEqual([d["usage_page"] for d in found], [0xFF00])

    def test_nothing_attached(self):
        api, _ = api_with()
        self.assertEqual(api.enumerate(), [])

    def test_wait_for_sees_reenumeration(self):
        api, fw = api_with(APP)
        fw.attached.clear()
        with mock.patch.object(pf.time, "sleep", side_effect=lambda s: fw.attach(BOOT)):
            self.assertEqual(api.wait_for(pf.PID_BOOT, timeout=5)["pid"], pf.PID_BOOT)


class DeviceTest(unittest.TestCase):
    def open_boot(self, **kw):
        api, fw = api_with(BOOT, bootloader=tf.FakeBootloader(), **kw)
        return api.open(api.find(pf.PID_BOOT)[0]), fw

    def test_report_sent_unpadded_with_report_id(self):
        dev, fw = self.open_boot()
        dev.send(pf.cmd("I"))
        self.assertEqual(fw.sent, [pf.cmd("I")])          # 10 bytes, like RØDE Central; no padding to 1025

    def test_reply_through_callback(self):
        dev, _ = self.open_boot()
        self.assertEqual(pf.boot_info(dev), pf.KNOWN_BOOT_INFO)

    def test_reply_without_report_id_gets_it_back(self):
        dev, _ = self.open_boot(strip_report_id=True)
        self.assertEqual(pf.boot_info(dev), pf.KNOWN_BOOT_INFO)

    def test_timeout_returns_none(self):
        dev, _ = self.open_boot()
        self.assertIsNone(dev.recv(0.05))

    def test_full_flash_through_backend(self):
        img = tf.make_image()
        dev, fw = self.open_boot()
        tf.flash(dev, img)
        self.assertEqual(b"".join(fw.boot.flash[i] for i in sorted(fw.boot.flash)), img)
        self.assertEqual(len(fw.sent), 94)
        self.assertEqual({len(r) for r in fw.sent}, {pf.CMD_LEN, pf.DATA_LEN})

    def test_close_unregisters_and_closes(self):
        dev, fw = self.open_boot()
        dev.close()
        self.assertEqual((fw.opened, fw.scheduled, fw.callbacks), (set(), set(), {}))
        dev.close()                                        # twice is harmless

    def test_open_denied_explained(self):
        api, fw = api_with(BOOT)
        fw.open_rc = mac.kIOReturnNotPermitted
        with self.assertRaisesRegex(pf.FlashError, "Input Monitoring"):
            api.open(api.find(pf.PID_BOOT)[0])

    def test_open_error(self):
        api, fw = api_with(BOOT)
        fw.open_rc = -536870203                            # kIOReturnExclusiveAccess 0xe00002c5
        with self.assertRaisesRegex(pf.FlashError, "0xe00002c5"):
            api.open(api.find(pf.PID_BOOT)[0])


class InputReportTest(unittest.TestCase):
    def test_kept_when_it_starts_with_the_id(self):
        self.assertEqual(mac.input_report(1, b"\x01IA"), b"\x01IA")

    def test_id_restored(self):
        self.assertEqual(mac.input_report(1, b"IA"), b"\x01IA")

    def test_report_id_zero_untouched(self):
        self.assertEqual(mac.input_report(0, b"IA"), b"IA")


class DispatchTest(unittest.TestCase):
    def test_windows_and_mac(self):
        with mock.patch.object(pf, "WinHid", return_value="win"):
            self.assertEqual(pf.hid_api("win32"), "win")
        with mock.patch.object(mac, "MacHid", return_value="mac"):
            self.assertEqual(pf.hid_api("darwin"), "mac")

    def test_other_os_refused(self):
        with self.assertRaisesRegex(pf.FlashError, "Windows and macOS only"):
            pf.hid_api("linux")

    def test_central_detection(self):
        for platform, listing, want in (("darwin", "launchd\nRØDE Central\n", True),
                                        ("darwin", "launchd\nRODE Central\n", True),
                                        ("darwin", "launchd\nFinder\n", False),
                                        ("win32", '"RODE Central.exe","1234"\n', True)):
            with mock.patch("subprocess.run", return_value=mock.Mock(stdout=listing)) as run:
                self.assertEqual(pf.central_running(platform), want, listing)
            self.assertEqual(run.call_args[0][0][0], "ps" if platform == "darwin" else "tasklist")


@unittest.skipUnless(sys.platform == "darwin", "macOS only: loads the real IOKit/CoreFoundation")
class LiveBindingTest(unittest.TestCase):
    def test_enumerate_runs(self):
        devs = mac.MacHid().enumerate()          # usually [] (no mic on a CI runner)
        self.assertIsInstance(devs, list)
        for d in devs:
            self.assertEqual(d["vid"], pf.VID)


if __name__ == "__main__":
    unittest.main()
