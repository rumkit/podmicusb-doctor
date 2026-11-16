#!/usr/bin/env python3
"""
podmicusb-doctor - guided installer for the RØDE PodMic USB firmware fix (Windows).

One program instead of extract_stock.py + build.py + podmic_flash.py:
  1. finds the mic and checks it runs firmware 1.1.9,
  2. gets the stock 1.1.9 image: from your RØDE Central install if there is one, otherwise from RØDE's own
     update server (the same file RØDE Central downloads), and checks its sha256,
  3. lets you pick a variant, builds it on your machine and checks it against the reference sha256,
  4. flashes it after you confirm.
Nothing is saved to disk. No RØDE firmware is shipped with this program.

usage:  py podmicusb_doctor.py        (or double-click podmicusb-doctor.exe)

All safety rules are the flasher's (firmware/tools/podmic_flash.py): CRC/vector checks, settings pages never
written, only a lower image version is refused, every step must be acknowledged.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

FROZEN = getattr(sys, "frozen", False)          # True inside the PyInstaller exe
if not FROZEN:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "firmware", "tools"))

import build                    # noqa: E402
import extract_stock            # noqa: E402
import podmic_flash as pf       # noqa: E402

if FROZEN:
    build.PATCHES = os.path.join(sys._MEIPASS, "patches")   # bundled with --add-data

VERSION = "1.1.0"
REPO_URL = "https://github.com/rumkit/podmicusb-doctor"
FIRMWARE_URL = "https://update.rode.com/podmicusb/firmware.bin"
MANIFEST_URL = "https://update.rode.com/rode-devices-manifest.json"
USER_AGENT = "podmicusb-doctor/" + VERSION
MAX_DOWNLOAD = 1 << 20
SUPPORTED_BCD = 0x0119
STOCK_SHA256 = build.STOCK_SHA256

# (key, menu title, one-line explanation); key None = the unmodified stock image
VARIANTS = (
    ("i2cfix", "Fix only (recommended)",
     "fixes the random disconnects; changes nothing else"),
    ("i2cfix-volume-holdmute", "Fix + extras",
     "the fix, plus louder headphones at power-up and hold the wheel to mute (with a short jingle)"),
    ("diag", "Diagnostic (does NOT fix anything)",
     "records where the firmware hangs, to confirm your mic has this bug; see the project page"),
    (None, "Original RØDE firmware 1.1.9",
     "go back to stock"),
)

RECOVERY = """\
If the mic doesn't work after this:
  1. Unplug the mic, wait about 10 seconds, plug it back in.
  2. Run this program again and choose "Original RØDE firmware 1.1.9",
     or use RØDE Central -> factory restore.
Help: %s/blob/master/firmware/README.md#recovery""" % REPO_URL


class WizardError(Exception):
    pass


class Abort(Exception):
    pass


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        raise Abort()


# --------------------------------------------------------------------------
# Stock image
# --------------------------------------------------------------------------
def http_get(url, limit=MAX_DOWNLOAD):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise WizardError("download from %s is unexpectedly large" % url)
    return data


def server_version(fetch):
    """PodMic USB firmware version RØDE's update manifest offers, or None."""
    try:
        m = json.loads(fetch(MANIFEST_URL).decode("utf-8"))
        return str(m["podmic-usb-manifest"]["main-version"]["update-version"])
    except Exception:
        return None


def local_stock(find_dll=extract_stock.find_dll):
    """Stock image from a local RØDE Central install, or None."""
    dll = find_dll(None)
    if not dll:
        return None
    try:
        with open(dll, "rb") as f:
            return extract_stock.extract(f.read())
    except OSError:
        return None


def get_stock(fetch=http_get, find_dll=extract_stock.find_dll, log=print):
    img = local_stock(find_dll)
    if img is not None and hashlib.sha256(img).hexdigest() == STOCK_SHA256:
        log("Found original firmware 1.1.9 in your RØDE Central install.")
        return img
    log("Downloading original firmware from RØDE's update server...")
    try:
        img = fetch(FIRMWARE_URL)
    except (urllib.error.URLError, OSError) as e:
        raise WizardError("download failed (%s).\nCheck your internet connection, or install RØDE Central "
                          "(this program can take the firmware from there) and try again." % e)
    if hashlib.sha256(img).hexdigest() != STOCK_SHA256:
        v = server_version(fetch)
        raise WizardError("RØDE's server no longer offers the firmware this fix is made for (1.1.9)%s.\n"
                          "If RØDE has released an update, install it with RØDE Central first: it may fix the "
                          "problem. This program supports 1.1.9 only."
                          % (" but version %s" % v if v else ""))
    log("Downloaded and verified (sha256 matches the original 1.1.9).")
    return img


def make_image(stock, key):
    if key is None:
        return stock
    img = build.build(stock, key)
    if hashlib.sha256(img).hexdigest() != build.REFERENCE_SHA256[key]:
        raise WizardError("built image differs from the reference build - not flashing (please report this)")
    return img


# --------------------------------------------------------------------------
# Mic
# --------------------------------------------------------------------------
def find_mic(api):
    """Return ("app" | "boot", descriptor). Raise WizardError if there isn't exactly one mic."""
    apps, boots = api.find(pf.PID_APP), api.find(pf.PID_BOOT)
    if len(apps) + len(boots) > 1:
        raise WizardError("more than one PodMic USB is connected - connect only the one to update")
    if apps:
        return "app", apps[0]
    if boots:
        return "boot", boots[0]
    raise WizardError("no PodMic USB found - plug it in (directly, not through a hub if possible)")


def flash(api, image, log=print):
    """Flash image to the one connected mic. Same steps and checks as podmic_flash.py flash, minus the prompt."""
    info, _ = pf.check_image(image)
    pf.compare_versions(api, info["version_raw"])
    dev = api.open(pf.enter_bootloader(api))
    try:
        pf.boot_info(dev)
        log("Writing firmware - DO NOT UNPLUG THE MIC")
        pages = pf.pages_of(image)
        pf.write_pages(dev, pages, log=lambda *_: None)
        log("All %d pages written." % len(pages))
    except pf.FlashError:
        dev.close()
        raise
    d = pf.leave_bootloader(api, dev)
    if d["bcd"] != info["version_raw"]:
        raise WizardError("the mic came back reporting version %s, expected %s"
                          % (pf.fmt_version(d["bcd"]), pf.fmt_version(info["version_raw"])))
    return d


# --------------------------------------------------------------------------
# Wizard
# --------------------------------------------------------------------------
def choose_variant(options):
    print("\nWhat do you want to install?")
    for i, (_, title, text) in enumerate(options, 1):
        print("  %d. %s\n     %s" % (i, title, text))
    while True:
        a = ask("Enter a number (or Q to quit): ").strip().lower()
        if a == "q":
            raise Abort()
        if a.isdigit() and 1 <= int(a) <= len(options):
            return options[int(a) - 1]


def wait_central_closed():
    while pf.central_running():
        ask("RØDE Central is running. Close it (also check the system tray), then press Enter...")


def connect_mic(api):
    while True:
        try:
            return find_mic(api)
        except WizardError as e:
            ask("%s\nPress Enter to look again..." % e)


def run(api):
    print("podmicusb-doctor %s - firmware fix for the RØDE PodMic USB\n%s\n" % (VERSION, REPO_URL))
    print("Not affiliated with or endorsed by RØDE. Flashing modified firmware is at your own risk.\n")
    wait_central_closed()
    mode, desc = connect_mic(api)
    options = VARIANTS
    if mode == "boot":
        print("The mic is in update (bootloader) mode, probably after an interrupted update.\n"
              "Try first: unplug it, wait 10 s, plug it back in, run this again.\n"
              "If it keeps coming back in this mode, this program can write the original firmware to it.")
        options = VARIANTS[-1:]
    else:
        print("Found: RØDE PodMic USB, firmware %s" % pf.fmt_version(desc["bcd"]).split(" ")[0])
        if desc["bcd"] > SUPPORTED_BCD:
            raise WizardError("this mic has a newer firmware than 1.1.9. The fix is made for 1.1.9 only, "
                              "and the newer firmware may already fix the problem. Nothing was changed.")
        if desc["bcd"] < SUPPORTED_BCD:
            raise WizardError("this mic has an older firmware. Update it to 1.1.9 with RØDE Central first, "
                              "then run this again. Nothing was changed.")
    stock = get_stock()
    key, title, _ = choose_variant(options)
    image = make_image(stock, key)
    print("\nReady to install: %s" % title)
    print("The update takes about 10 seconds. Don't unplug the mic or close this window until it's done.")
    if ask("Type YES to start, anything else to cancel: ").strip().lower() != "yes":
        print("Cancelled. Nothing was changed.")
        return 0
    wait_central_closed()
    try:
        flash(api, image)
    except (pf.FlashError, WizardError) as e:
        print("\nUPDATE FAILED: %s\n\n%s" % (e, RECOVERY))
        return 1
    except KeyboardInterrupt:
        print("\nUPDATE INTERRUPTED.\n\n%s" % RECOVERY)
        return 1
    print("\nDone. The mic restarted with: %s" % title)
    return 0


def main():
    if sys.platform != "win32":
        print("This program runs on Windows only.")
        return 1
    try:
        rc = run(pf.WinHid())
    except Abort:
        print("\nCancelled. Nothing was changed.")
        rc = 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        rc = 1
    except (WizardError, pf.FlashError, build.BuildError) as e:
        print("\nERROR: %s" % e)
        rc = 1
    if FROZEN:                   # double-clicked: keep the window open
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
