"""
podmic_hid_mac.py - macOS HID transport for podmic_flash.py (standard library only: ctypes -> IOKit,
CoreFoundation). Same interface as WinHid / WinHidDevice in podmic_flash.py; all protocol and safety logic
stays there.

NOT YET TESTED WITH A REAL MIC. It follows what hidapi's macOS backend does:
  - output reports go out with IOHIDDeviceSetReport, the buffer starting with the report ID byte, NOT padded
    (Windows needs padding to the longest output report; IOKit sends exactly the given length, which is what
    RØDE Central sent: 10 bytes for report 2, 1025 for report 3);
  - input reports arrive through an input-report callback while the run loop is pumped in recv(); returned
    bytes start with the report ID, like on Windows;
  - Max{Input,Output}ReportSize include the report ID byte, like Windows' report lengths.
"""
import collections
import ctypes
import time

import podmic_flash as pf

kCFStringEncodingUTF8 = 0x08000100
kCFNumberSInt32Type = 3
kIOHIDReportTypeOutput = 1
kIOHIDOptionsTypeNone = 0
kIOReturnSuccess = 0
kIOReturnNotPermitted = 0xE00002E2 - (1 << 32)   # IOReturn is a signed 32-bit int
RUNLOOP_SLICE_S = 0.01

# void (*IOHIDReportCallback)(void *context, IOReturn result, void *sender, IOHIDReportType type,
#                             uint32_t reportID, uint8_t *report, CFIndex reportLength)
REPORT_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_int,
                                   ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_long)


def load_frameworks():
    """IOKit + CoreFoundation with every function used here declared. Returns (cf, iokit, constants)."""
    c = ctypes
    vp, idx = c.c_void_p, c.c_long          # CF objects are pointers; CFIndex is a signed long
    cf = c.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    io = c.cdll.LoadLibrary("/System/Library/Frameworks/IOKit.framework/IOKit")

    def decl(lib, name, restype, *argtypes):
        f = getattr(lib, name)
        f.restype, f.argtypes = restype, list(argtypes)

    decl(cf, "CFRelease", None, vp)
    decl(cf, "CFRetain", vp, vp)
    decl(cf, "CFGetTypeID", c.c_ulong, vp)
    decl(cf, "CFNumberGetTypeID", c.c_ulong)
    decl(cf, "CFNumberCreate", vp, vp, idx, vp)
    decl(cf, "CFNumberGetValue", c.c_bool, vp, idx, vp)
    decl(cf, "CFStringCreateWithCString", vp, vp, c.c_char_p, c.c_uint32)
    decl(cf, "CFDictionaryCreateMutable", vp, vp, idx, vp, vp)
    decl(cf, "CFDictionarySetValue", None, vp, vp, vp)
    decl(cf, "CFSetGetCount", idx, vp)
    decl(cf, "CFSetGetValues", None, vp, c.POINTER(vp))
    decl(cf, "CFRunLoopGetCurrent", vp)
    decl(cf, "CFRunLoopRunInMode", c.c_int32, vp, c.c_double, c.c_bool)
    decl(io, "IOHIDManagerCreate", vp, vp, c.c_uint32)
    decl(io, "IOHIDManagerSetDeviceMatching", None, vp, vp)
    decl(io, "IOHIDManagerScheduleWithRunLoop", None, vp, vp, vp)
    decl(io, "IOHIDManagerCopyDevices", vp, vp)
    decl(io, "IOHIDDeviceGetProperty", vp, vp, vp)
    decl(io, "IOHIDDeviceOpen", c.c_int32, vp, c.c_uint32)
    decl(io, "IOHIDDeviceClose", c.c_int32, vp, c.c_uint32)
    decl(io, "IOHIDDeviceScheduleWithRunLoop", None, vp, vp, vp)
    decl(io, "IOHIDDeviceUnscheduleFromRunLoop", None, vp, vp, vp)
    decl(io, "IOHIDDeviceSetReport", c.c_int32, vp, c.c_int, idx, c.c_char_p, idx)
    decl(io, "IOHIDDeviceRegisterInputReportCallback", None, vp, vp, idx, REPORT_CALLBACK, vp)

    consts = {
        "run_loop_mode": c.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode").value,
        # the callback structs themselves are passed by address
        "key_callbacks": c.addressof(c.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")),
        "value_callbacks": c.addressof(c.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")),
    }
    return cf, io, consts


def input_report(report_id, data):
    """Report bytes as the protocol code expects them: starting with the report ID. IOKit should already
    deliver them that way (as hidapi assumes); if a macOS version strips the ID, put it back. Safe for this
    protocol: replies are report 1 and their first data byte is a command letter, never 0x01."""
    data = bytes(data)
    if report_id and (not data or data[0] != report_id):
        return bytes([report_id]) + data
    return data


class MacHid(pf.HidApiBase):
    """Device enumeration + I/O through IOHIDManager. One manager for the program's lifetime, scheduled on
    the current thread's run loop (like hidapi), so re-enumeration (app <-> bootloader) is seen."""

    def __init__(self, frameworks=None):
        self.cf, self.io, self.k = frameworks or load_frameworks()
        self.mgr = self.io.IOHIDManagerCreate(None, kIOHIDOptionsTypeNone)
        if not self.mgr:
            raise pf.FlashError("IOHIDManagerCreate failed")
        self.io.IOHIDManagerScheduleWithRunLoop(self.mgr, self.cf.CFRunLoopGetCurrent(), self.k["run_loop_mode"])
        self._keys = {}
        self.matching = self.cf.CFDictionaryCreateMutable(None, 0, self.k["key_callbacks"], self.k["value_callbacks"])
        vid = self._number(pf.VID)
        self.cf.CFDictionarySetValue(self.matching, self._key("VendorID"), vid)
        self.cf.CFRelease(vid)

    def _key(self, name):
        """CFString for a property name, created once and kept."""
        if name not in self._keys:
            self._keys[name] = self.cf.CFStringCreateWithCString(None, name.encode(), kCFStringEncodingUTF8)
        return self._keys[name]

    def _number(self, n):
        v = ctypes.c_int32(n)
        return self.cf.CFNumberCreate(None, kCFNumberSInt32Type, ctypes.byref(v))

    def _int_property(self, dev, name):
        ref = self.io.IOHIDDeviceGetProperty(dev, self._key(name))     # "Get" rule: not ours to release
        if not ref or self.cf.CFGetTypeID(ref) != self.cf.CFNumberGetTypeID():
            return None
        v = ctypes.c_int32()
        if not self.cf.CFNumberGetValue(ref, kCFNumberSInt32Type, ctypes.byref(v)):
            return None
        return v.value

    def pump(self, seconds=0.0):
        self.cf.CFRunLoopRunInMode(self.k["run_loop_mode"], seconds, True)

    def enumerate(self):
        """Dicts for every RØDE HID interface (VID 0x19F7), with the same keys WinHid uses."""
        self.pump()                                                    # let the manager see (dis)connections
        self.io.IOHIDManagerSetDeviceMatching(self.mgr, self.matching)
        devset = self.io.IOHIDManagerCopyDevices(self.mgr)
        if not devset:
            return []
        try:
            n = self.cf.CFSetGetCount(devset)
            refs = (ctypes.c_void_p * n)()
            self.cf.CFSetGetValues(devset, refs)
            out = []
            for ref in refs:
                d = self._describe(ref)
                if d["vid"] == pf.VID:
                    # retained for the descriptor's lifetime; never released (a few per poll, the program is short-lived)
                    d["ref"] = self.cf.CFRetain(ref)
                    out.append(d)
            return out
        finally:
            self.cf.CFRelease(devset)

    def _describe(self, ref):
        p = lambda name: self._int_property(ref, name)                 # noqa: E731
        return {"path": "IOHIDDevice 0x%x" % ref, "vid": p("VendorID"), "pid": p("ProductID"),
                "bcd": p("VersionNumber"), "usage_page": p("PrimaryUsagePage"),
                "in_len": p("MaxInputReportSize"), "out_len": p("MaxOutputReportSize")}

    def open(self, desc):
        return MacHidDevice(self, desc)


class MacHidDevice:
    def __init__(self, api, desc):
        self.api, self.desc, self.ref = api, desc, desc["ref"]
        self.queue = collections.deque(maxlen=64)
        self.scheduled = False
        rc = api.io.IOHIDDeviceOpen(self.ref, kIOHIDOptionsTypeNone)
        if rc == kIOReturnNotPermitted:
            raise pf.FlashError("macOS denied access to the mic (IOHIDDeviceOpen: not permitted). If macOS asked "
                                "for Input Monitoring, allow it for your Terminal app and run this again")
        if rc != kIOReturnSuccess:
            raise pf.FlashError("cannot open the HID device (IOHIDDeviceOpen error 0x%08x) - is RØDE Central "
                                "still running?" % (rc & 0xFFFFFFFF))
        self.opened = True
        self.buf = (ctypes.c_uint8 * max(desc["in_len"] or 64, 64))()
        self.callback = REPORT_CALLBACK(self._on_report)                # keep a reference: IOKit calls it later
        api.io.IOHIDDeviceRegisterInputReportCallback(self.ref, self.buf, len(self.buf), self.callback, None)
        api.io.IOHIDDeviceScheduleWithRunLoop(self.ref, api.cf.CFRunLoopGetCurrent(), api.k["run_loop_mode"])
        self.scheduled = True

    def _on_report(self, context, result, sender, rtype, report_id, report, length):
        if result == kIOReturnSuccess:
            self.queue.append(input_report(report_id, ctypes.string_at(report, length)))

    def flush(self):
        self.api.pump()
        self.queue.clear()

    def send(self, report):
        report = bytes(report)
        rc = self.api.io.IOHIDDeviceSetReport(self.ref, kIOHIDReportTypeOutput, report[0], report, len(report))
        if rc != kIOReturnSuccess:
            raise pf.FlashError("IOHIDDeviceSetReport(report %d, %d bytes) failed, error 0x%08x"
                                % (report[0], len(report), rc & 0xFFFFFFFF))

    def recv(self, timeout):
        end = time.monotonic() + timeout
        while not self.queue:
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.api.pump(min(RUNLOOP_SLICE_S, left))
        return self.queue.popleft()

    def close(self):
        api = self.api
        if self.scheduled:
            api.io.IOHIDDeviceRegisterInputReportCallback(self.ref, self.buf, len(self.buf), REPORT_CALLBACK(), None)
            api.io.IOHIDDeviceUnscheduleFromRunLoop(self.ref, api.cf.CFRunLoopGetCurrent(), api.k["run_loop_mode"])
            self.scheduled = False
        if getattr(self, "opened", False):
            api.io.IOHIDDeviceClose(self.ref, kIOHIDOptionsTypeNone)    # fails harmlessly if the mic left the bus
            self.opened = False
