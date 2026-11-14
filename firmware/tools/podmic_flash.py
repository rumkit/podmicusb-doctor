#!/usr/bin/env python3
"""
podmic_flash.py - flash an application image to a RODE PodMic USB through its
HID bootloader, replicating exactly what RODE Central sent during a factory
restore (USB capture of a factory restore, 2026-11-11; see research/NOTES.md 3a).

Windows only, standard library only (ctypes -> hid.dll / setupapi.dll / kernel32).
CLOSE RODE CENTRAL before using this.

Commands
  check IMAGE   offline checks of the image file only; touches no device
  info          list PodMic / bootloader HID devices (read-only, sends nothing)
  probe         app -> bootloader -> 'I' (info) -> back to app. Erases/writes nothing.
  flash IMAGE   full flash. Asks you to type FLASH before the first erase.
  diag          (diag firmware only) read the hang snapshot / boot counter via app-mode '02 B'
  diag-test     (diag firmware only) diag, app -> bootloader -> app, diag: the boot counter
                must go up by one (proves the snapshot survives a reset). Erases/writes nothing.

Protocol (all reports via HID SET_REPORT(Output) on EP0; replies = input report 1
on the interrupt IN endpoint):
  app        report 2  '02 52 00..' 'R'   -> device reboots into bootloader (PID 0x004B)
  bootloader report 2  '02 49 00..' 'I'   -> '01 49 41 01 00 00 04 30 00 00'
                                             (ver 1, page size 0x400, 0x30 pages)
             report 2  '02 45 NN 00..' 'E' erase app page NN -> '01 45 41 ..'
             report 3  '03' + 1024 bytes   write that page   -> '01 50 41 ..'
             report 2  '02 52 00..' 'R'   -> reboot into the app (PID 0x004A)
Page NN is an index from 0x08004000. Settings live in pages 46-47 (0x0800F800,
0x0800FC00) and are NEVER erased by this script.
"""
import argparse
import struct
import sys
import time
import zlib

VID = 0x19F7
PID_APP = 0x004A
PID_BOOT = 0x004B
USAGE_PAGE_VENDOR = 0xFF00

APP_BASE = 0x08004000
PAGE_SIZE = 1024
SETTINGS_FIRST_PAGE = 46            # 0x0800F800: settings, never touched
MAX_APP_PAGES = SETTINGS_FIRST_PAGE  # pages 0..45 may be written
KNOWN_BOOT_INFO = (1, 0x400, 0x30)  # (version?, page size, page count) seen in capture

CMD_REPORT = 2
DATA_REPORT = 3
CMD_LEN = 10        # report id + 9 bytes
DATA_LEN = 1 + PAGE_SIZE
ACK = 0x41          # 'A'

REPLY_TIMEOUT_S = 2.0      # observed replies: <= 50 ms
ENUM_TIMEOUT_S = 15.0      # observed re-enumeration: 0.4-1.5 s
SETTLE_S = 1.0             # Central waited ~1 s after the bootloader appeared


class FlashError(Exception):
    pass


# --------------------------------------------------------------------------
# Image checks (pure, no device)
# --------------------------------------------------------------------------
def check_image(data):
    """Return (info dict, list of warnings). Raise FlashError on a hard failure."""
    warnings = []
    if len(data) < 0x40:
        raise FlashError("image too small (%d bytes)" % len(data))

    stored = struct.unpack_from("<I", data, len(data) - 4)[0]
    calc = zlib.crc32(data[:-4]) & 0xFFFFFFFF
    if stored != calc:
        raise FlashError("trailing CRC32 mismatch: stored 0x%08x, computed 0x%08x"
                         % (stored, calc))

    sp, reset = struct.unpack_from("<II", data, 0)
    if not (0x20000000 < sp <= 0x20010000 and sp % 4 == 0):
        raise FlashError("initial stack pointer 0x%08x is not in RAM" % sp)
    if not (reset & 1):
        raise FlashError("reset vector 0x%08x is not a Thumb address" % reset)
    if not (APP_BASE <= (reset & ~1) < APP_BASE + len(data)):
        raise FlashError("reset vector 0x%08x points outside the image "
                         "(0x%08x..0x%08x) - wrong file or wrong base address?"
                         % (reset, APP_BASE, APP_BASE + len(data)))

    pages = (len(data) + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > MAX_APP_PAGES:
        raise FlashError("image needs %d pages; only %d allowed (pages %d+ hold "
                         "the mic's settings)" % (pages, MAX_APP_PAGES, SETTINGS_FIRST_PAGE))
    if len(data) % PAGE_SIZE:
        warnings.append("image is not a multiple of %d bytes; last page will be "
                        "padded with 0xFF (how RODE pads is unverified)" % PAGE_SIZE)

    ver = struct.unpack_from("<H", data, 0x20)[0]
    return {"size": len(data), "crc32": stored, "sp": sp, "reset": reset,
            "pages": pages, "version_raw": ver}, warnings


def fmt_version(v):
    return "%d.%d.%d (0x%04x)" % (v >> 8, (v >> 4) & 0xF, v & 0xF, v)


def pages_of(data):
    padded = data + b"\xFF" * (-len(data) % PAGE_SIZE)
    return [padded[i:i + PAGE_SIZE] for i in range(0, len(padded), PAGE_SIZE)]


# --------------------------------------------------------------------------
# Protocol (transport-agnostic, so it can be tested without hardware)
# --------------------------------------------------------------------------
def cmd(letter, arg=0):
    b = bytearray(CMD_LEN)
    b[0] = CMD_REPORT
    b[1] = ord(letter)
    b[2] = arg
    return bytes(b)


def expect(reply, code, what):
    if reply is None:
        raise FlashError("%s: no reply within %.1f s" % (what, REPLY_TIMEOUT_S))
    if len(reply) < 3 or reply[0] != 1 or reply[1] != code or reply[2] != ACK:
        raise FlashError("%s: unexpected reply %s" % (what, reply[:10].hex(" ")))
    return reply


def transact(dev, report, code, what):
    dev.flush()
    try:
        dev.send(report)
    except FlashError as e:
        raise FlashError("%s: %s" % (what, e))
    return expect(dev.recv(REPLY_TIMEOUT_S), code, what)


def boot_info(dev):
    r = transact(dev, cmd("I"), ord("I"), "bootloader info 'I'")
    info = struct.unpack_from("<HHH", r, 3)
    if info != KNOWN_BOOT_INFO:
        raise FlashError("bootloader info %r differs from the known %r "
                         "(raw %s) - refusing to continue"
                         % (info, KNOWN_BOOT_INFO, r[:10].hex(" ")))
    return info


def write_pages(dev, pages, log=print):
    for n, page in enumerate(pages):
        if not 0 <= n < MAX_APP_PAGES:
            raise FlashError("internal: page index %d out of range" % n)
        transact(dev, cmd("E", n), ord("E"), "erase page %d" % n)
        transact(dev, bytes([DATA_REPORT]) + page, ord("P"), "write page %d" % n)
        log("  page %2d/%d  0x%08x  ok" % (n + 1, len(pages), APP_BASE + n * PAGE_SIZE))


def reboot(dev):
    """'R': no reply is expected; the device drops off the bus."""
    dev.flush()
    dev.send(cmd("R"))


# --------------------------------------------------------------------------
# Diagnostic firmware (firmware/build.py diag): app-mode '02 B' -> input report 3
#   [3..22] RTC backup regs BKP0..4, [23..26] RAM mirror (I2C tx/rx counts), [27] mirror tag, [28] reset cause
# Stock firmware answers 'B' on report 1 with a zero byte (no diag).
# --------------------------------------------------------------------------
DIAG_MAGIC = 0xD1A6
CODE_REGIONS = (  # (start, end, name) - from research/NOTES.md
    (0x08008f24, 0x08008f98, "I2C start() - bus busy / cb pending / previous transfer"),
    (0x08008f18, 0x08008f20, "I2C done() - transfer never completed"),
    (0x0800ad60, 0x0800ad80, "codec write 0x0800ad0c: start/done wait loop"),
    (0x0800ad0c, 0x0800adc4, "codec write 0x0800ad0c"),
    (0x080091c0, 0x080091e8, "I2C read addr 0x10 wait loop (init)"),
    (0x0800b0c2, 0x0800b0e4, "codec read wait loop (HID 'G')"),
    (0x0800b2f8, 0x0800b324, "HID send busy-wait (EP1 IN never completed)"),
    (0x08008fa4, 0x08009128, "I2C EV interrupt handler"),
    (0x08009128, 0x0800914c, "I2C ER interrupt handler"),
    (0x0800c1e8, 0x0800c1f6, "delay() (SysTick countdown)"),
    (0x080050b0, 0x08005a40, "USB interrupt handler"),
    (0x0800c174, 0x0800c1e8, "scheduler"),
)
ADDON_REGIONS = {  # add-on code moves between builds; keyed by the 'B' 01 marker (reply byte 10)
    0xF1: ((0x0800f642, 0x0800f67c, "fix1 wait_done (bounded done-wait)"),
           (0x0800f67c, 0x0800f6be, "fix1 start_hook (bounded start-wait)"),
           (0x0800f6be, 0x0800f74a, "fix1 i2c_recover")),
    0xF2: ((0x0800f604, 0x0800f63e, "fix2 wait_done (bounded done-wait)"),
           (0x0800f63e, 0x0800f680, "fix2 start_hook (bounded start-wait)"),
           (0x0800f680, 0x0800f70c, "fix2 i2c_recover"),
           (0x0800f70c, 0x0800f7a8, "fix2 tone_hook (jingle, DMA IRQ)"),
           (0x0800f7a8, 0x0800f7f4, "fix2 btn_task (hold-to-mute)")),
}
REC_MARKERS = {0xF1: "fix1", 0xF2: "fix2"}   # 'B' page 1 byte 7 (reply byte 10)
FW_ADDON = None     # set from the page-1 marker once known (cmd_diag / monitor), used by where()
REC_SITES = {0x0800ad74: "codec write (start)", 0x0800ad7c: "codec write (done)",
             0x080091d4: "addr-0x10 read at boot (start)", 0x080091dc: "addr-0x10 read at boot (done)",
             0x0800b0d6: "codec read (start)", 0x0800b0de: "codec read (done)"}
RESET_BITS = ((0x80, "low-power"), (0x40, "window-watchdog"), (0x20, "WATCHDOG (IWDG)"),
              (0x10, "software"), (0x08, "power-on"), (0x04, "reset pin"), (0x02, "option-byte"))


def read_diag(dev, page=0):
    dev.flush()
    dev.send(cmd("B", page))
    return dev.recv(REPLY_TIMEOUT_S)


def where(pc):
    for a, b, name in CODE_REGIONS + ADDON_REGIONS.get(FW_ADDON, ()):
        if a <= pc < b:
            return name
    return "unknown code" if 0x08004000 <= pc < 0x08010000 else "outside the app"


def exc_name(n):
    if n == 0:
        return "thread mode (main loop)"
    names = {40: "I2C EV IRQ", 50: "I2C ER IRQ", 83: "USB IRQ", 15: "SysTick", 26: "DMA ch1/2 IRQ (audio DSP)"}
    return "exception %d%s" % (n, " = " + names[n] if n in names else "")


def parse_diag(r):
    """Return a dict, or raise FlashError if the reply isn't a diag reply."""
    if r is None:
        raise FlashError("diag 'B': no reply within %.1f s" % REPLY_TIMEOUT_S)
    if len(r) >= 3 and r[0] == 1 and r[1] == ord("B"):
        raise FlashError("mic answered 'B' the stock way - this firmware has no diag add-on")
    if len(r) < 29 or r[0] != 3 or r[1] != ord("B") or r[2] != ACK:
        raise FlashError("diag 'B': unexpected reply %s" % r[:12].hex(" "))
    bkp = struct.unpack_from("<5I", r, 3)
    counts, = struct.unpack_from("<I", r, 23)
    d = {"raw": r[:29], "magic_ok": (bkp[0] >> 16) == DIAG_MAGIC,
         "boots": bkp[0] & 0xFF, "hangs": (bkp[0] >> 8) & 0xFF,
         "pc1": bkp[1], "stat0": bkp[2] & 0xFFFF, "stat1": bkp[2] >> 16,
         "ctl0": bkp[3] & 0xFFFF, "ctl1": bkp[3] >> 16,
         "pc2": (0x08000000 | (bkp[4] & 0xFFFF)) if bkp[4] & 0xFFFF else None,
         "i2c_state": (bkp[4] >> 16) & 0xFF, "ipsr": bkp[4] >> 24,
         "tx": counts & 0xFFFF, "rx": counts >> 16, "mirror_ok": r[27] == ((bkp[0] >> 8) & 0xFF),
         "reset": r[28]}
    return d


def fmt_diag(d):
    lines = []
    if not d["magic_ok"]:
        lines.append("backup registers hold no diag data (never written, or not retained) - raw %s"
                     % d["raw"].hex(" "))
        return lines
    if FW_ADDON == 0xF2:     # fix2 dropped the reset-cause byte (it always read 0: the bootloader clears RSTSCK)
        lines.append("boots %d, hangs caught %d" % (d["boots"], d["hangs"]))
    else:
        cause = ", ".join(n for bit, n in RESET_BITS if d["reset"] & bit) or "none recorded"
        lines.append("boots %d, hangs caught %d, this boot's reset cause: %s (0x%02x)"
                     % (d["boots"], d["hangs"], cause, d["reset"]))
    if d["hangs"]:
        lines.append("last hang: PC 0x%08x = %s, in %s"
                     % (d["pc1"], where(d["pc1"]), exc_name(d["ipsr"])))
        if d["pc2"] is not None and FW_ADDON != 0xF2:   # fix2 no longer samples the PC at 2 s
            lines.append("           PC 1 s later 0x%08x = %s%s" % (d["pc2"], where(d["pc2"]),
                         "  (same spot)" if abs(d["pc2"] - d["pc1"]) < 0x20 else ""))
        lines.append("           I2C STAT0 0x%04x STAT1 0x%04x CTL0 0x%04x CTL1 0x%04x  state byte %d (0 busy/1 done/2 err)"
                     % (d["stat0"], d["stat1"], d["ctl0"], d["ctl1"], d["i2c_state"]))
        lines.append("           I2C tx count %d, rx count %d%s" % (d["tx"], d["rx"],
                     "" if d["mirror_ok"] else "  (RAM copy not from this hang - ignore)"))
    return lines


def parse_rec(r):
    """fix1/fix2 firmware: 'B' page 1 = I2C timeout recoveries. Returns a dict, or None if the firmware has no page 1.
    Also sets FW_ADDON (used by where())."""
    global FW_ADDON
    if r is None or len(r) < 29 or r[0] != 3 or r[1] != ord("B") or r[2] != ACK or r[10] not in REC_MARKERS:
        return None
    FW_ADDON = r[10]
    total, done_to, start_to = struct.unpack_from("<3H", r, 3)
    caller, = struct.unpack_from("<I", r, 11)
    stat0, stat1, ctl0, ctl1 = struct.unpack_from("<4H", r, 15)
    return {"raw": r[:29], "total": total, "done_timeouts": done_to, "start_timeouts": start_to,
            "fw": REC_MARKERS[r[10]], "kind": r[9], "caller": caller & ~1, "stat0": stat0, "stat1": stat1, "ctl0": ctl0, "ctl1": ctl1,
            "state": r[23], "hdr": r[24], "data": r[25], "rx": r[26], "progress": r[27], "addr": r[28]}


def fmt_rec(d):
    if d is None:
        return ["no recovery page (not fix1/fix2 firmware)"]
    lines = ["I2C recoveries since boot: %d (done-wait timeouts %d, start-wait timeouts %d)"
             % (d["total"], d["done_timeouts"], d["start_timeouts"])]
    if d["total"]:
        lines.append("last recovery: %s timeout at %s (return addr 0x%08x)"
                     % ({1: "done-wait", 2: "start-wait"}.get(d["kind"], "kind %d" % d["kind"]),
                        REC_SITES.get(d["caller"], where(d["caller"])), d["caller"]))
        lines.append("           before reset: I2C STAT0 0x%04x STAT1 0x%04x CTL0 0x%04x CTL1 0x%04x  state %d"
                     % (d["stat0"], d["stat1"], d["ctl0"], d["ctl1"], d["state"]))
        lines.append("           remaining: header %d, data %d, rx %d; bytes moved %d; slave addr 0x%02x"
                     % (d["hdr"], d["data"], d["rx"], d["progress"], d["addr"]))
    return lines


def cmd_diag(args):
    require_windows()
    api = WinHid()
    apps = api.find(PID_APP)
    if len(apps) != 1:
        raise FlashError("expected exactly one PodMic USB in app mode, found %d" % len(apps))
    dev = api.open(apps[0])
    try:
        rec = parse_rec(read_diag(dev, 1))   # first: identifies fix1/fix2 for where()
        d = parse_diag(read_diag(dev))
    finally:
        dev.close()
    for line in fmt_diag(d):
        print(line)
    print("raw: " + d["raw"].hex(" "))
    for line in fmt_rec(rec):
        print(line)
    if rec is not None:
        print("raw page 1: " + rec["raw"].hex(" "))
    return d


def cmd_diag_test(args):
    """Prove the backup registers survive a reset + the bootloader: boots must go up by one."""
    print("reading diag:")
    before = cmd_diag(args)
    if not before["magic_ok"]:
        raise FlashError("no diag data before the test - the boot counter is not working (backup registers "
                         "not writable/readable?)")
    api = WinHid()
    dev = api.open(enter_bootloader(api))
    leave_bootloader(api, dev)
    time.sleep(SETTLE_S)
    print("reading diag again:")
    after = cmd_diag(args)
    if after["magic_ok"] and after["boots"] == (before["boots"] + 1) & 0xFF and after["hangs"] == before["hangs"]:
        print("PASS: boot counter %d -> %d across a software reset + bootloader; the snapshot will survive "
              "the watchdog reset too" % (before["boots"], after["boots"]))
    else:
        raise FlashError("FAIL: before boots %d hangs %d, after boots %d hangs %d (magic %s)"
                         % (before["boots"], before["hangs"], after["boots"], after["hangs"], after["magic_ok"]))


# --------------------------------------------------------------------------
# Windows HID transport (ctypes)
# --------------------------------------------------------------------------
class WinHid:
    """Device enumeration + I/O through the Windows HID class driver."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ct, self.w = ctypes, w
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.hid = ctypes.WinDLL("hid", use_last_error=True)
        self.sapi = ctypes.WinDLL("setupapi", use_last_error=True)

        class GUID(ctypes.Structure):
            _fields_ = [("d1", w.DWORD), ("d2", w.WORD), ("d3", w.WORD), ("d4", ctypes.c_ubyte * 8)]

        class IFDATA(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("guid", GUID), ("flags", w.DWORD),
                        ("reserved", ctypes.c_void_p)]

        class ATTR(ctypes.Structure):
            _fields_ = [("Size", w.ULONG), ("VendorID", w.USHORT), ("ProductID", w.USHORT),
                        ("VersionNumber", w.USHORT)]

        class CAPS(ctypes.Structure):
            _fields_ = [("Usage", w.USHORT), ("UsagePage", w.USHORT),
                        ("InputReportByteLength", w.USHORT), ("OutputReportByteLength", w.USHORT),
                        ("FeatureReportByteLength", w.USHORT), ("rest", w.USHORT * 27)]

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                        ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

        self.GUID, self.IFDATA, self.ATTR, self.CAPS, self.OVERLAPPED = GUID, IFDATA, ATTR, CAPS, OVERLAPPED
        k, h, s = self.k32, self.hid, self.sapi
        k.CreateFileW.restype = w.HANDLE
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CreateEventW.restype = w.HANDLE
        k.CreateEventW.argtypes = [ctypes.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
        k.ReadFile.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.GetOverlappedResult.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL]
        k.CancelIoEx.argtypes = [w.HANDLE, ctypes.c_void_p]
        k.ResetEvent.argtypes = [w.HANDLE]
        h.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
        h.HidD_GetAttributes.argtypes = [w.HANDLE, ctypes.POINTER(ATTR)]
        h.HidD_GetAttributes.restype = w.BOOLEAN
        h.HidD_GetPreparsedData.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
        h.HidD_GetPreparsedData.restype = w.BOOLEAN
        h.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        h.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(CAPS)]
        h.HidP_GetCaps.restype = ctypes.c_long
        h.HidD_SetOutputReport.argtypes = [w.HANDLE, ctypes.c_void_p, w.ULONG]
        h.HidD_SetOutputReport.restype = w.BOOLEAN
        h.HidD_FlushQueue.argtypes = [w.HANDLE]
        h.HidD_FlushQueue.restype = w.BOOLEAN
        s.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        s.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), w.LPCWSTR, w.HWND, w.DWORD]
        s.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                  w.DWORD, ctypes.POINTER(IFDATA)]
        s.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(IFDATA), ctypes.c_void_p,
                                                       w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        s.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

    def _bad(self, h):
        return not h or h == self.ct.c_void_p(-1).value  # NULL or INVALID_HANDLE_VALUE

    def _open(self, path, access, flags=0):
        GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
        acc = {"none": 0, "rw": GENERIC_READ | GENERIC_WRITE}[access]
        h = self.k32.CreateFileW(path, acc, 3, None, 3, flags, None)  # share R|W, OPEN_EXISTING
        if self._bad(h):
            return None
        return h

    def enumerate(self):
        """Yield dicts for every RODE vendor-page HID collection (VID 0x19F7)."""
        ct = self.ct
        guid = self.GUID()
        self.hid.HidD_GetHidGuid(ct.byref(guid))
        hdev = self.sapi.SetupDiGetClassDevsW(ct.byref(guid), None, None, 0x12)  # PRESENT|DEVICEINTERFACE
        if self._bad(hdev):
            return []
        out = []
        try:
            i = 0
            while True:
                ifd = self.IFDATA()
                ifd.cbSize = ct.sizeof(self.IFDATA)
                if not self.sapi.SetupDiEnumDeviceInterfaces(hdev, None, ct.byref(guid), i, ct.byref(ifd)):
                    break
                i += 1
                need = self.w.DWORD(0)
                self.sapi.SetupDiGetDeviceInterfaceDetailW(hdev, ct.byref(ifd), None, 0, ct.byref(need), None)
                buf = ct.create_string_buffer(need.value)
                struct.pack_into("<I", buf, 0, 8 if ct.sizeof(ct.c_void_p) == 8 else 6)
                if not self.sapi.SetupDiGetDeviceInterfaceDetailW(hdev, ct.byref(ifd), buf, need, None, None):
                    continue
                path = ct.wstring_at(ct.addressof(buf) + 4)
                d = self._describe(path)
                if d and d["vid"] == VID:
                    out.append(d)
        finally:
            self.sapi.SetupDiDestroyDeviceInfoList(hdev)
        return out

    def _describe(self, path):
        ct = self.ct
        h = self._open(path, "none")
        if h is None:
            return None
        try:
            a = self.ATTR()
            a.Size = ct.sizeof(self.ATTR)
            if not self.hid.HidD_GetAttributes(h, ct.byref(a)):
                return None
            d = {"path": path, "vid": a.VendorID, "pid": a.ProductID, "bcd": a.VersionNumber,
                 "usage_page": None, "in_len": None, "out_len": None}
            pp = ct.c_void_p()
            if self.hid.HidD_GetPreparsedData(h, ct.byref(pp)):
                caps = self.CAPS()
                if self.hid.HidP_GetCaps(pp, ct.byref(caps)) == 0x00110000:
                    d.update(usage_page=caps.UsagePage, in_len=caps.InputReportByteLength,
                             out_len=caps.OutputReportByteLength)
                self.hid.HidD_FreePreparsedData(pp)
            return d
        finally:
            self.k32.CloseHandle(h)

    def find(self, pid):
        return [d for d in self.enumerate() if d["pid"] == pid and d["usage_page"] == USAGE_PAGE_VENDOR]

    def wait_for(self, pid, timeout=ENUM_TIMEOUT_S):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.find(pid)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                raise FlashError("more than one device with PID 0x%04x - connect only one mic" % pid)
            time.sleep(0.1)
        raise FlashError("device PID 0x%04x did not appear within %.0f s" % (pid, timeout))

    def wait_gone(self, pid, timeout=ENUM_TIMEOUT_S):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.find(pid):
                return
            time.sleep(0.1)
        raise FlashError("device PID 0x%04x did not disconnect within %.0f s" % (pid, timeout))

    def open(self, desc):
        return WinHidDevice(self, desc)


class WinHidDevice:
    def __init__(self, api, desc):
        self.api, self.desc = api, desc
        FILE_FLAG_OVERLAPPED = 0x40000000
        self.hw = api._open(desc["path"], "rw")                       # for SET_REPORT
        self.hr = api._open(desc["path"], "rw", FILE_FLAG_OVERLAPPED)  # for reading
        if self.hw is None or self.hr is None:
            self.close()
            raise FlashError("cannot open the HID device (is RODE Central still running?)")
        self.event = api.k32.CreateEventW(None, True, False, None)

    def flush(self):
        self.api.hid.HidD_FlushQueue(self.hr)

    def send(self, report):
        # Windows wants a buffer of OutputReportByteLength (the largest output
        # report), zero-padded, like hidapi does; the HID class driver then sends
        # the length that report ID actually has (10 B for report 2, as in the capture).
        ct = self.api.ct
        n = max(len(report), self.desc["out_len"] or 0)
        buf = ct.create_string_buffer(bytes(report) + bytes(n - len(report)), n)
        if not self.api.hid.HidD_SetOutputReport(self.hw, buf, n):
            raise FlashError("HidD_SetOutputReport(report %d, %d bytes) failed, error %d"
                             % (report[0], len(report), ct.get_last_error()))

    def recv(self, timeout):
        api, ct = self.api, self.api.ct
        n = self.desc["in_len"]
        buf = ct.create_string_buffer(n)
        ov = api.OVERLAPPED()
        api.k32.ResetEvent(self.event)
        ov.hEvent = self.event
        got = api.w.DWORD(0)
        ok = api.k32.ReadFile(self.hr, buf, n, None, ct.byref(ov))
        if not ok and ct.get_last_error() != 997:  # ERROR_IO_PENDING
            raise FlashError("ReadFile failed, error %d" % ct.get_last_error())
        if api.k32.WaitForSingleObject(self.event, int(timeout * 1000)) != 0:
            api.k32.CancelIoEx(self.hr, ct.byref(ov))
            api.k32.GetOverlappedResult(self.hr, ct.byref(ov), ct.byref(got), True)
            return None
        if not api.k32.GetOverlappedResult(self.hr, ct.byref(ov), ct.byref(got), False):
            raise FlashError("read failed, error %d" % ct.get_last_error())
        return buf.raw[:got.value]

    def close(self):
        for h in (getattr(self, "hw", None), getattr(self, "hr", None), getattr(self, "event", None)):
            if h:
                self.api.k32.CloseHandle(h)
        self.hw = self.hr = self.event = None


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def central_running():
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return "rode central" in out.lower()


def require_windows():
    if sys.platform != "win32":
        raise FlashError("device commands work on Windows only")
    if central_running():
        raise FlashError("RODE Central is running - close it first (it polls the mic "
                         "and may react to the bootloader appearing)")


def describe(d):
    kind = {PID_APP: "PodMic USB (app)", PID_BOOT: "PodMic bootloader"}.get(d["pid"], "other RODE device")
    return ("%-20s PID 0x%04x  bcdDevice %s  usage page %s  in/out report len %s/%s"
            % (kind, d["pid"], fmt_version(d["bcd"]),
               "0x%04x" % d["usage_page"] if d["usage_page"] is not None else "?",
               d["in_len"], d["out_len"]))


def cmd_check(args):
    data = open(args.image, "rb").read()
    info, warns = check_image(data)
    print("image     %s" % args.image)
    print("size      %d bytes = %d pages of %d (max %d)" % (info["size"], info["pages"], PAGE_SIZE, MAX_APP_PAGES))
    print("CRC32     0x%08x  ok" % info["crc32"])
    print("vectors   SP 0x%08x, reset 0x%08x  ok" % (info["sp"], info["reset"]))
    print("version   %s  (what the mic should report after flashing)" % fmt_version(info["version_raw"]))
    for wmsg in warns:
        print("WARNING   " + wmsg)
    return data, info


def cmd_info(args):
    if sys.platform != "win32":
        raise FlashError("device commands work on Windows only")
    devs = WinHid().enumerate()
    if not devs:
        print("no RODE HID devices found")
    for d in devs:
        print(describe(d))


def enter_bootloader(api):
    """Find exactly one app-mode mic, send 'R', return the bootloader descriptor."""
    boot = api.find(PID_BOOT)
    if boot:
        print("mic is already in bootloader mode")
        return check_boot_desc(boot[0])
    apps = api.find(PID_APP)
    if len(apps) != 1:
        raise FlashError("expected exactly one PodMic USB (PID 0x%04x), found %d" % (PID_APP, len(apps)))
    print("found:  " + describe(apps[0]))
    dev = api.open(apps[0])
    try:
        print("sending 'R' to the app (reboot into bootloader)")
        reboot(dev)
    finally:
        dev.close()
    api.wait_for(PID_BOOT)
    time.sleep(SETTLE_S)
    return check_boot_desc(api.wait_for(PID_BOOT))


def check_boot_desc(d):
    print("found:  " + describe(d))
    if d["in_len"] != CMD_LEN or d["out_len"] != DATA_LEN:
        raise FlashError("bootloader report lengths in/out %s/%s, expected %d/%d"
                         % (d["in_len"], d["out_len"], CMD_LEN, DATA_LEN))
    return d


def leave_bootloader(api, dev):
    print("sending 'R' to the bootloader (reboot into the app)")
    reboot(dev)
    dev.close()
    d = api.wait_for(PID_APP)
    print("found:  " + describe(d))
    return d


def compare_versions(api, img_ver):
    """Refuse an image older than what the mic reports (the bootloader rejects it and wedges);
    warn on a newer one (anti-downgrade may then block going back)."""
    apps = api.find(PID_APP)
    if len(apps) != 1:
        print("WARNING: mic not in app mode, its installed version is unknown - cannot compare. "
              "The bootloader rejects images older than the installed version.")
        return
    dev_ver = apps[0]["bcd"]
    if img_ver < dev_ver:
        raise FlashError("image version %s is LOWER than the mic's %s - the bootloader rejects "
                         "that and stops responding (2026-11-11). Refusing."
                         % (fmt_version(img_ver), fmt_version(dev_ver)))
    if img_ver > dev_ver:
        print("WARNING: image version %s is HIGHER than the mic's %s. If the bootloader enforces "
              "anti-downgrade against the installed version, %s (and RODE Central's restore) "
              "may be rejected afterwards." % (fmt_version(img_ver), fmt_version(dev_ver),
                                               fmt_version(dev_ver)))


def cmd_probe(args):
    require_windows()
    api = WinHid()
    dev = api.open(enter_bootloader(api))
    try:
        info = boot_info(dev)
        print("bootloader info: version %d, page size %d, %d pages  (matches capture)" % info)
    finally:
        # nothing was erased, so returning to the (intact) app is always safe
        leave_bootloader(api, dev)
    print("probe done - nothing was erased or written")


def cmd_flash(args):
    data, info = cmd_check(args)
    require_windows()
    pages = pages_of(data)
    api = WinHid()
    compare_versions(api, info["version_raw"])
    if args.skip_info and not api.find(PID_BOOT):
        raise FlashError("--skip-info is only allowed when the mic is already in bootloader mode")
    dev = api.open(enter_bootloader(api))
    try:
        if args.skip_info:
            print("WARNING: --skip-info: NOT asking the bootloader 'I' (recovery attempt)")
        else:
            boot_info(dev)
            print("bootloader info matches capture")
        print()
        print("About to ERASE and WRITE pages 0..%d (0x%08x..0x%08x) with %s."
              % (len(pages) - 1, APP_BASE, APP_BASE + len(pages) * PAGE_SIZE - 1, args.image))
        print("Settings pages %d-47 are not touched. Do not unplug the mic during the ~4 s flash."
              % SETTINGS_FIRST_PAGE)
        if input("Type FLASH to continue, anything else to abort: ").strip() != "FLASH":
            print("aborted before erasing anything; rebooting back into the app")
            leave_bootloader(api, dev)
            return
        t0 = time.monotonic()
        write_pages(dev, pages)
        print("all %d pages written in %.1f s" % (len(pages), time.monotonic() - t0))
    except FlashError:
        dev.close()
        print("\nFLASH FAILED. The mic is left in bootloader mode on purpose (no 'R' sent).")
        print("If the bootloader stopped answering (as after a rejected last page), unplug the mic,")
        print("wait ~10 s, replug, then run RODE Central -> factory restore (proven 2026-11-11).")
        raise
    d = leave_bootloader(api, dev)
    if d["bcd"] == info["version_raw"]:
        print("OK: mic reports %s = the version stored in the image" % fmt_version(d["bcd"]))
    else:
        print("NOTE: mic reports %s, image says %s"
              % (fmt_version(d["bcd"]), fmt_version(info["version_raw"])))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn, needs_image in (("check", cmd_check, True), ("info", cmd_info, False),
                                  ("probe", cmd_probe, False), ("flash", cmd_flash, True),
                                  ("diag", cmd_diag, False), ("diag-test", cmd_diag_test, False)):
        sp = sub.add_parser(name)
        if needs_image:
            sp.add_argument("image")
        if name == "flash":
            sp.add_argument("--skip-info", action="store_true",
                            help="recovery only: mic already in bootloader mode but not answering 'I'; "
                                 "go straight to erase/write (each step must still be ACKed)")
        sp.set_defaults(fn=fn)
    args = p.parse_args(argv)
    try:
        args.fn(args)
    except FlashError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
