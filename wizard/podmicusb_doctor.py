#!/usr/bin/env python3
"""
podmicusb-doctor - guided installer for the RØDE PodMic USB firmware fix (Windows; macOS: not yet tested
with a real mic).

One program instead of extract_stock.py + build.py + podmic_flash.py:
  1. finds the mic and checks it runs firmware 1.1.9,
  2. gets the stock 1.1.9 image: from your RØDE Central install if there is one, otherwise from RØDE's own
     update server (the same file RØDE Central downloads), and checks its sha256,
  3. lets you pick a variant, builds it on your machine and checks it against the reference sha256,
  4. flashes it after you confirm.
Nothing is saved to disk. No RØDE firmware is shipped with this program.

usage:  py podmicusb_doctor.py [options]       (Windows; or double-click podmicusb-doctor.exe)
        python3 podmicusb_doctor.py [options]  (macOS)
  --self-test  no mic needed, changes nothing: downloads and verifies the stock image (and checks the local
               RØDE Central copy if installed), builds every variant and checks it against its reference sha256
  --verbose    also show where the program loaded its own code from, which certificate list it uses, and the
               stock image's sha256
  --download   get the stock image from RØDE's server even if RØDE Central is installed (the local copy is
               used only if the download fails)

All safety rules are the flasher's (firmware/tools/podmic_flash.py): CRC/vector checks, settings pages never
written, only a lower image version is refused, every step must be acknowledged.
"""
import hashlib
import http.client
import json
import os
import ssl
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

try:
    from _version import VERSION     # written by the CI build from the git tag (v1.1.0 -> "1.1.0"); not committed
except ImportError:
    VERSION = "dev"


def program_name(version):
    return "podmicusb-doctor " + ("v" + version if version[:1].isdigit() else version)


PROGRAM = program_name(VERSION)
REPO_URL = "https://github.com/rumkit/podmicusb-doctor"
MACOS = sys.platform == "darwin"
MACOS_CA_FILE = "/etc/ssl/cert.pem"     # macOS' own CA bundle; python.org's Python doesn't use it by default
FIRMWARE_URL = "https://update.rode.com/podmicusb/firmware.bin"
MANIFEST_URL = "https://update.rode.com/rode-devices-manifest.json"
USER_AGENT = "podmicusb-doctor/" + VERSION
MAX_DOWNLOAD = 1 << 20
SUPPORTED_BCD = 0x0119
STOCK_SHA256 = build.STOCK_SHA256

# (key, menu title, one-line explanation); key None = the unmodified stock image
VARIANTS = (
    ("i2cfix", "Fix only",
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


def drop_pending_keys():
    """Discard keys typed ahead (e.g. an Enter pressed after a single-key menu choice), so they can't answer
    the next line prompt."""
    if not console_keys():
        return
    try:
        import msvcrt
    except ImportError:
        import termios
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        return
    while msvcrt.kbhit():
        msvcrt.getwch()


def ask(prompt):
    drop_pending_keys()
    try:
        return input(prompt)
    except EOFError:
        raise Abort()


def console_keys():
    """True when single keypresses can be read (a real console/terminal, not piped/redirected input)."""
    if not sys.stdin or not sys.stdin.isatty():
        return False
    for mod in ("msvcrt", "termios"):        # Windows console / macOS (POSIX) terminal
        try:
            __import__(mod)
            return True
        except ImportError:
            pass
    return False


def getkey():
    """One keypress, no Enter. Ctrl+C raises KeyboardInterrupt, Esc returns 'esc', arrow/function keys ''."""
    try:
        import msvcrt
    except ImportError:
        return getkey_posix()
    ch = msvcrt.getwch()
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch in ("\x00", "\xe0"):        # arrow/function keys arrive as two codes
        msvcrt.getwch()
        return ""
    return "esc" if ch == "\x1b" else ch.lower()


def getkey_posix(fd=None):
    """getkey() in a POSIX terminal: cbreak mode (no echo, no Enter; Ctrl+C still raises KeyboardInterrupt).
    Esc alone -> 'esc'; an escape sequence (arrow/function key) -> ''."""
    import select
    import termios
    import tty
    fd = sys.stdin.fileno() if fd is None else fd
    old = termios.tcgetattr(fd)
    try:
        # TCSANOW, not tty's default TCSAFLUSH: keep a key typed just before the prompt (like msvcrt on Windows)
        tty.setcbreak(fd, termios.TCSANOW)
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            if not select.select([fd], [], [], 0.05)[0]:
                return "esc"
            while select.select([fd], [], [], 0.01)[0]:   # rest of the sequence
                os.read(fd, 16)
            return ""
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)       # no drain wait: on a macOS pty it can block forever
    return ch.decode("ascii", "replace").lower()


def pick(prompt, valid):
    """Wait for one of the keys in `valid` and return it. In a console a single keypress is enough (other keys
    are ignored); with piped input the first character of each line is used."""
    if console_keys():
        print(prompt, end="", flush=True)
        while True:
            k = getkey()
            if k in valid:
                print(k.upper() if k != "esc" else "")
                return k
    while True:
        k = ask(prompt).strip().lower()[:1]
        if k in valid:
            return k


def press_any_key(prompt):
    if console_keys():
        drop_pending_keys()
        print(prompt, end="", flush=True)
        getkey()
        print()
    else:
        ask(prompt)


# --------------------------------------------------------------------------
# Stock image
# --------------------------------------------------------------------------
def tls_context():
    """The system's trusted roots plus certifi's CA list if available (bundled in the exe).

    Windows: Python only sees the roots already in the Windows store. A fresh Windows has few and downloads the
    rest only when Windows' own HTTPS stack asks; Python's doesn't. RØDE's server chains to a root that may be
    missing (seen in Windows Sandbox). certifi fills the gap.
    macOS: python.org's Python has no CA list until "Install Certificates.command" is run, so macOS' own bundle
    is loaded too. Certificate checking stays on either way."""
    ctx = ssl.create_default_context()
    if MACOS and os.path.isfile(MACOS_CA_FILE):
        ctx.load_verify_locations(MACOS_CA_FILE)
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


def tls_source():
    system = ("Python default + " + MACOS_CA_FILE) if MACOS else "Windows store"
    try:
        import certifi
        return "%s + certifi %s" % (system, certifi.__version__)
    except ImportError:
        return "%s only (certifi not installed)" % system


def http_get(url, limit=MAX_DOWNLOAD):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30, context=tls_context()) as r:
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


def get_stock(fetch=http_get, find_dll=extract_stock.find_dll, log=print, prefer_download=False):
    """Stock 1.1.9 image, sha256-checked. Default: local RØDE Central install first, then download.
    prefer_download: download first, fall back to the local install only if the download fails."""
    def local():
        img = local_stock(find_dll)
        if img is not None and hashlib.sha256(img).hexdigest() == STOCK_SHA256:
            log("Found original firmware 1.1.9 in your RØDE Central install.")
            return img
        return None

    if not prefer_download:
        return local() or download_stock(fetch, log)
    try:
        return download_stock(fetch, log)
    except WizardError as e:
        img = local()
        if img is None:
            raise
        log("(download failed: %s)" % str(e).split("\n")[0])
        return img


def download_stock(fetch=http_get, log=print):
    log("Downloading original firmware from RØDE's update server...")
    try:
        img = fetch(FIRMWARE_URL)
    except http.client.HTTPException as e:       # e.g. IncompleteRead: connection dropped mid-download
        raise WizardError("download failed (%s: %s).\nCheck your internet connection and try again."
                          % (type(e).__name__, e))
    except (urllib.error.URLError, OSError) as e:
        if isinstance(getattr(e, "reason", e), ssl.SSLCertVerificationError):
            raise WizardError("download failed: the server's certificate could not be verified (%s).\n"
                              "This can happen behind a company proxy or with a wrong system clock. Install RØDE "
                              "Central (this program can take the firmware from there) and try again." % e)
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


def page_progress(total, out=None):
    """Per-page callback for pf.write_pages: a progress bar redrawn with \\r in a console, one line per page
    when the output goes to a file or pipe."""
    out = out or sys.stdout
    tty = out.isatty()
    count = [0]

    def step(msg):
        count[0] += 1
        if not tty:
            out.write(msg + "\n")
            return
        n = count[0]
        bar = "#" * (n * 30 // total)
        out.write("\r  [%-30s] page %2d/%d" % (bar, n, total))
        if n == total:
            out.write("\n")
        out.flush()
    return step


def flash(api, image, log=print, progress=page_progress):
    """Flash image to the one connected mic. Same steps and checks as podmic_flash.py flash, minus the prompt."""
    info, _ = pf.check_image(image)
    pf.compare_versions(api, info["version_raw"])
    dev = api.open(pf.enter_bootloader(api))
    try:
        pf.boot_info(dev)
        log("Writing firmware - DO NOT UNPLUG THE MIC")
        pages = pf.pages_of(image)
        pf.write_pages(dev, pages, log=progress(len(pages)))
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
    numbers = [str(i) for i in range(1, len(options) + 1)]
    k = pick("Press %s (Q to quit): " % ("1" if len(numbers) == 1 else "1-%d" % len(numbers)),
             numbers + ["q", "esc"])
    if k in ("q", "esc"):
        raise Abort()
    return options[int(k) - 1]


def wait_central_closed():
    where = "quit it (RØDE Central menu -> Quit)" if MACOS else "close it (also check the system tray)"
    while pf.central_running():
        press_any_key("RØDE Central is running. Please %s, then press any key..." % where)


def connect_mic(api):
    while True:
        try:
            return find_mic(api)
        except WizardError as e:
            press_any_key("%s\nPress any key to look again..." % e)


def print_details():
    """--verbose: where the program runs from and loaded its own code from, which certificate list it uses."""
    print("program:  %s%s" % (sys.executable if FROZEN else os.path.abspath(__file__), " (exe)" if FROZEN else ""))
    for m in (build, extract_stock, pf):
        print("module:   %-16s %s" % (m.__name__, getattr(m, "__file__", "?")))
    print("patches:  %s" % os.path.abspath(build.PATCHES))
    print("certs:    %s\n" % tls_source())


def run(api, verbose=False, prefer_download=False):
    print("%s - firmware fix for the RØDE PodMic USB\n%s\n" % (PROGRAM, REPO_URL))
    print("Not affiliated with or endorsed by RØDE. Flashing modified firmware is at your own risk.\n")
    if MACOS:
        print("macOS support is new and NOT YET TESTED with a real mic. If you try it, please report what\n"
              "happened (also if it worked) at %s/issues\n" % REPO_URL)
    if verbose:
        print_details()
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
    stock = get_stock(prefer_download=prefer_download)
    if verbose:
        print("stock sha256 %s" % hashlib.sha256(stock).hexdigest())
    key, title, _ = choose_variant(options)
    image = make_image(stock, key)
    print("\nReady to install: %s" % title)
    print("sha256 %s (same as the published reference build)" % hashlib.sha256(image).hexdigest())
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
    except Exception as e:                       # anything unexpected while the mic may be mid-update
        print("\nUPDATE FAILED (unexpected error): %s: %s\n\n%s" % (type(e).__name__, e, RECOVERY))
        return 1
    print("\nDone. The mic restarted with: %s" % title)
    return 0


def self_test(fetch=http_get, find_dll=extract_stock.find_dll, verbose=False):
    """No mic needed, nothing is written: download + verify the stock image (and the local RØDE Central copy if
    installed), build every variant and check it against its reference sha256."""
    print("%s self-test (no mic needed, nothing is changed)\n" % PROGRAM)
    if verbose:
        print_details()
    ok = True
    stock = None
    try:
        stock = download_stock(fetch)
        print("stock 1.1.9 from download:     OK  sha256 " + STOCK_SHA256)
    except WizardError as e:
        print("  download: %s" % e)
        print("stock 1.1.9 from download:     FAILED")
        ok = False
    # The local copy is optional: an older or newer RØDE Central may bundle another firmware version, in which
    # case the installer simply downloads. Report it, but it isn't a failure.
    if find_dll(None):
        local = local_stock(find_dll)
        if local is not None and hashlib.sha256(local).hexdigest() == STOCK_SHA256:
            print("stock 1.1.9 from RØDE Central: OK  sha256 " + STOCK_SHA256)
            stock = stock or local
        else:
            print("stock 1.1.9 from RØDE Central: not found in this RØDE Central version (the installer downloads "
                  "instead)")
    else:
        print("stock 1.1.9 from RØDE Central: not installed (the installer downloads instead)")
    if stock is None:
        print("\nSELF-TEST FAILED: no stock image")
        return 1
    print("\nBuilt images (each checked against the published reference sha256):")
    for key, title, _ in VARIANTS:
        try:
            img = make_image(stock, key)
            print("  %-38s OK  sha256 %s" % (title, hashlib.sha256(img).hexdigest()))
        except (WizardError, build.BuildError, OSError) as e:
            print("  %-38s FAILED: %s" % (title, e))
            ok = False
    print("\nSELF-TEST %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def parse_args(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="podmicusb-doctor", description="Firmware fix for the RØDE PodMic USB. "
                                 "Run without options for the guided installer.")
    ap.add_argument("--self-test", action="store_true",
                    help="no mic needed, changes nothing: get the stock image, build and check every variant")
    ap.add_argument("--verbose", action="store_true",
                    help="also show where the program loaded its code from, the certificate list and the stock sha256")
    ap.add_argument("--download", action="store_true",
                    help="get the stock image from RØDE's server even if RØDE Central is installed "
                         "(the local copy is used only if the download fails)")
    return ap.parse_args(argv)


def pause():
    if FROZEN:                   # double-clicked: keep the window open
        try:
            drop_pending_keys()
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass


def main(argv=None):
    a = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if a.self_test:
            return self_test(verbose=a.verbose)
        if sys.platform not in ("win32", "darwin"):
            print("This program runs on Windows and macOS only.")
            return 1
        return run(pf.hid_api(), verbose=a.verbose, prefer_download=a.download)
    except Abort:
        print("\nCancelled. Nothing was changed.")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1
    except (WizardError, pf.FlashError, build.BuildError) as e:
        print("\nERROR: %s" % e)
        return 1
    except Exception as e:                       # show it instead of closing the window on a traceback
        print("\nUNEXPECTED ERROR: %s: %s\nPlease report this at %s/issues" % (type(e).__name__, e, REPO_URL))
        return 1
    finally:
        pause()


if __name__ == "__main__":
    sys.exit(main())
