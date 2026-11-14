#!/usr/bin/env python3
"""
extract_stock.py - copy the stock PodMic USB firmware 1.1.9 out of YOUR RØDE Central install.

RØDE Central bundles the PodMic USB application image inside rode_bridge.dll. This script finds it
(by its vector table), checks the sha256 against the known stock 1.1.9 image and writes it to a file.
Nothing is downloaded and nothing is sent to the mic.

usage:
  py extract_stock.py [PATH] [-o OUT]
     PATH  rode_bridge.dll, or a folder to search for it (e.g. the RØDE Central install folder).
           Default: search the Program Files folders for a RØDE Central install.
     OUT   output file (default: podmicusb-1.1.9-stock.bin in the current folder)

Checked with RØDE Central for Windows 2.0.65 (PodMic firmware 1.1.9).
"""
import argparse
import glob
import hashlib
import os
import sys

STOCK_SHA256 = "11703ba4b1f4ed1d4d59243225547b596ebcbae8fffe48c302a63fd5b70b8d4c"
STOCK_SIZE = 47104
# first 8 bytes of the image: initial SP 0x20002000, reset vector 0x08007d31
SIGNATURE = bytes.fromhex("00200020317d0008")


# Default install: C:\Program Files\RØDE Microphones\RODE Central\rode_bridge.dll
DEFAULT_SUBDIR = os.path.join("R\u00d8DE Microphones", "RODE Central")
# Fallback search below Program Files: any vendor/app folder named R*DE* (RØDE, RODE) or *Central*
SEARCH_DIRS = ("R*DE*", "*Central*")


def find_dll(path):
    if path:
        if os.path.isfile(path):
            return path
        hits = glob.glob(os.path.join(glob.escape(path), "**", "rode_bridge.dll"), recursive=True)
        return hits[0] if hits else None
    roots = [r for r in dict.fromkeys(os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")) if r]
    for root in roots:
        p = os.path.join(root, DEFAULT_SUBDIR, "rode_bridge.dll")
        if os.path.isfile(p):
            return p
    for root in roots:
        for d in SEARCH_DIRS:
            hits = glob.glob(os.path.join(glob.escape(root), d, "**", "rode_bridge.dll"), recursive=True)
            if hits:
                return hits[0]
    return None


def extract(data):
    """Return the stock image found in data, or None."""
    i = data.find(SIGNATURE)
    while i >= 0:
        img = data[i:i + STOCK_SIZE]
        if hashlib.sha256(img).hexdigest() == STOCK_SHA256:
            return img
        i = data.find(SIGNATURE, i + 1)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", nargs="?", help="rode_bridge.dll or a folder containing it")
    ap.add_argument("-o", "--out", default="podmicusb-1.1.9-stock.bin")
    a = ap.parse_args(argv)
    dll = find_dll(a.path)
    if not dll:
        print("ERROR: rode_bridge.dll not found%s. Install RØDE Central or pass the path to rode_bridge.dll."
              % (" under " + a.path if a.path else " in Program Files"), file=sys.stderr)
        return 1
    print("reading  %s" % dll)
    img = extract(open(dll, "rb").read())
    if img is None:
        print("ERROR: no stock PodMic USB 1.1.9 image in this rode_bridge.dll. A different RØDE Central version may\n"
              "bundle a different firmware version; this project's patches apply to 1.1.9 only.", file=sys.stderr)
        return 1
    with open(a.out, "wb") as f:
        f.write(img)
    print("wrote    %s  (%d bytes, sha256 %s = stock 1.1.9 OK)" % (a.out, len(img), STOCK_SHA256))
    return 0


if __name__ == "__main__":
    sys.exit(main())
