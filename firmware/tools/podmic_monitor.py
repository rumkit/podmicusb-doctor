#!/usr/bin/env python3
"""
podmic_monitor.py - watch a RODE PodMic USB on Windows and log every stall / disconnect, with timing.
Works with ANY PodMic USB firmware (stock or modified). Windows, Python 3 standard library only; needs
podmic_flash.py next to it (HID layer). CLOSE RODE CENTRAL first (its own polls would mix with ours).

Two modes:
  basic (default)  discovery + liveness only. Sends nothing but the status request RODE Central itself sends
                   every 0.5 s:  SET_REPORT report 8 '08 08 01 00..'  ->  input report 7 '07 08 41 ..'
  --advanced       basic + the hang record of this project's diag image, read at start and after every
                   re-enumeration (hidden service command 02 'B'). On other firmware 'B' is a harmless no-op;
                   the monitor notes that once and carries on in basic mode. (It also reads the I2C-recovery
                   counter of the unpublished fix1/fix2 test images, see research/NOTES.md.)
                   The published i2cfix images keep no record: I2C failures caught by their timeout are
                   invisible here, only disconnects show up.

What it logs (console + log file):
  STALL    a poll went unanswered (mic main loop not responding)
  RECOVER  answers resumed without a disconnect          -> transient stall, duration
  GONE     the mic left USB; silent time before that      -> what reset it:
             ~1.1-2.2 s stock / ~9-17 s with the diag image (long watchdog)  => watchdog reset (main loop hung)
             ~0 s (answers right up to the drop)                       => instant reset (fault/NMI) or unplug
  BACK     the mic re-enumerated, and how long that took
  ALIVE    heartbeat every 5 min with the poll count
  advanced mode only:
  DIAG     the diag image's hang record, read back at FOUND and after every BACK
  RECOVER-I2C  (fix1/fix2 test images only) the mic caught an I2C hang and recovered without disconnecting

usage:  py podmic_monitor.py [--advanced] [--interval 1.0] [--rec-every 30] [--log podmic_monitor.log]
Stop with Ctrl+C.
"""
import argparse
import datetime
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import podmic_flash as pf  # noqa: E402

POLL = bytes([8, 0x08, 0x01]) + bytes(25)   # report 8, 28 bytes incl. report id
REPLY_TIMEOUT_S = 0.5                       # normal replies: 1-17 ms
HEARTBEAT_S = 300


def stamp(t):
    return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Monitor:
    """State machine, separated from Windows I/O so it can be tested offline."""

    def __init__(self, log):
        self.log = log
        self.last_ok = None       # wall time of last answered poll
        self.stall_start = None   # wall time of first unanswered poll in the current stall
        self.missed = 0
        self.gone_at = None
        self.polls = 0

    def answered(self, t):
        self.polls += 1
        if self.stall_start is not None:
            self.log("RECOVER  answers resumed after %.3f s silent (%d missed polls), no disconnect"
                     % (t - self.stall_start, self.missed))
        self.stall_start, self.missed, self.last_ok = None, 0, t

    def unanswered(self, t_sent):
        self.polls += 1
        if self.stall_start is None:
            self.stall_start = t_sent
            self.log("STALL    poll sent at %s not answered (last answer %s)"
                     % (stamp(t_sent), stamp(self.last_ok) if self.last_ok else "never"))
        self.missed += 1

    def gone(self, t, why, pending_since=None):
        """pending_since: send time of a poll still waiting for its reply when the device vanished.
        Replies normally take 1-17 ms, so a poll pending > 50 ms counts as unanswered."""
        self.gone_at = t
        if self.stall_start is None and pending_since is not None and t - pending_since > 0.05:
            self.stall_start, self.missed = pending_since, 1
        if self.stall_start is not None:
            self.log("GONE     mic left USB. SILENT BEFORE DROP: %.3f s (%d missed polls; last answer %.3f s "
                     "before drop) [%s]" % (t - self.stall_start, self.missed,
                                            t - self.last_ok if self.last_ok else -1, why))
        else:
            self.log("GONE     mic left USB while still answering: last answer %.3f s before drop "
                     "=> instant reset (no stall seen) [%s]" % (t - self.last_ok if self.last_ok else -1, why))
        self.stall_start, self.missed = None, 0

    def back(self, t, desc):
        self.log("BACK     re-enumerated %.3f s after drop (bcdDevice %s)"
                 % (t - self.gone_at, pf.fmt_version(desc["bcd"])))
        self.gone_at, self.last_ok = None, None


def read_rec(dev):
    """fix1/fix2 firmware: recovery page, or None (other firmware / no answer)."""
    dev.flush()
    dev.send(pf.cmd("B", 1))
    return pf.parse_rec(dev.recv(REPLY_TIMEOUT_S))


def log_rec(rec, log, tag="DIAG    "):
    for line in pf.fmt_rec(rec):
        log("%s %s" % (tag, line))
    if rec is not None and rec["total"]:
        log("%s raw page 1 %s" % (tag, rec["raw"].hex(" ")))


def read_fw_reports(dev, log):
    """--advanced: log what the modified firmware reports. Returns the I2C-recovery total, or None if this
    firmware has no recovery page (stock / diag-only) -> no periodic checks."""
    try:
        rec = read_rec(dev)          # first: tells pf which add-on (fix1/fix2) is running
    except pf.FlashError:
        rec = None
    try:
        d = pf.parse_diag(pf.read_diag(dev))
    except pf.FlashError:
        d = None
    if d is None and rec is None:
        log("DIAG     this firmware has no diagnostic reports (stock?) - monitoring disconnects only")
        return None
    if d is not None:
        for line in pf.fmt_diag(d):
            log("DIAG     " + line)
        log("DIAG     raw " + d["raw"].hex(" "))
    log_rec(rec, log)
    return rec["total"] if rec is not None else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--advanced", action="store_true",
                    help="also read the hang record of the diag image")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between polls (default 1.0)")
    ap.add_argument("--rec-every", type=float, default=30.0,
                    help="--advanced: seconds between I2C-recovery checks (fix1/fix2 test images only; default 30)")
    ap.add_argument("--log", default="podmic_monitor.log", help="log file (appended)")
    args = ap.parse_args(argv)

    if sys.platform != "win32":
        print("ERROR: Windows only", file=sys.stderr)
        return 1
    if pf.central_running():
        print("ERROR: RODE Central is running - close it first (its polls would mix with ours)", file=sys.stderr)
        return 1

    logf = open(args.log, "a", encoding="utf-8")

    def log(msg):
        line = "%s  %s" % (stamp(time.time()), msg)
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    api = pf.WinHid()
    mon = Monitor(log)
    log("START    %s mode, polling every %.2f s, reply timeout %.2f s"
        % ("advanced" if args.advanced else "basic", args.interval, REPLY_TIMEOUT_S))
    dev = None
    next_beat = time.monotonic() + HEARTBEAT_S
    rec_total = None          # None = firmware has no recovery page (or not read yet)
    next_rec = 0.0
    try:
        while True:
            if dev is None:
                found = api.find(pf.PID_APP)
                if len(found) != 1:
                    time.sleep(0.05)
                    continue
                if mon.gone_at is not None:
                    mon.back(time.time(), found[0])
                    time.sleep(pf.SETTLE_S)          # let the firmware finish booting
                else:
                    log("FOUND    %s" % pf.describe(found[0]))
                try:
                    dev = api.open(found[0])
                except pf.FlashError as e:
                    log("WARN     %s" % e)
                    time.sleep(1)
                    continue
                if args.advanced:
                    rec_total = read_fw_reports(dev, log)
                    next_rec = time.monotonic() + args.rec_every

            t0 = time.monotonic()
            t_sent = None
            try:
                dev.flush()
                dev.send(POLL)
                t_sent = time.time()
                r = dev.recv(REPLY_TIMEOUT_S)
                if r is not None and len(r) >= 3 and r[0] == 7 and r[1] == 0x08 and r[2] == 0x41:
                    mon.answered(time.time())
                elif r is None:
                    mon.unanswered(t_sent)
                else:
                    log("ODD      unexpected reply %s" % r[:8].hex(" "))
            except pf.FlashError as e:
                mon.gone(time.time(), str(e)[:80], t_sent)
                dev.close()
                dev = None
                continue

            if rec_total is not None and time.monotonic() >= next_rec:
                next_rec = time.monotonic() + args.rec_every
                try:
                    rec = read_rec(dev)
                except pf.FlashError:
                    rec = None          # a real disconnect shows up on the next poll
                if rec is not None and rec["total"] != rec_total:
                    log("RECOVER-I2C  %d new I2C recover%s (mic stayed connected)"
                        % (rec["total"] - rec_total, "y" if rec["total"] - rec_total == 1 else "ies"))
                    log_rec(rec, log, "RECOVER-I2C ")
                    rec_total = rec["total"]

            if time.monotonic() >= next_beat:
                log("ALIVE    %d polls so far" % mon.polls)
                next_beat += HEARTBEAT_S
            time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        log("STOP     %d polls" % mon.polls)
    finally:
        if dev is not None:
            dev.close()
        logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
