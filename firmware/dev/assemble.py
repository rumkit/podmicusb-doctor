#!/usr/bin/env python3
"""
assemble.py - assemble a patch source (firmware/patches/<name>.s) into firmware/patches/<name>.json,
which build.py reads. Only needed when you CHANGE a .s file; the .json files are committed.

Needs rz-asm from Rizin (https://rizin.re) on PATH. Runs on Linux/macOS/Windows.

Source format (one instruction per line, ';' comments):
  .org 0x0800f460        first line: load address of the add-on
  label:                 labels; may be used as operands (resolved iteratively)
  MRS_R12_MSP etc.       a few instructions rz-asm can't encode are given as raw bytes (MANUAL below)

usage: python3 assemble.py ../patches/rel1.s [--list]
"""
import json
import os
import re
import subprocess
import sys

MANUAL = {"MRS_R12_MSP": bytes.fromhex("eff3088c"), "MRS_R12_PSP": bytes.fromhex("eff3098c"),
          "CPSID_I": bytes.fromhex("72b6"), "CPSIE_I": bytes.fromhex("62b6")}


def asm(ins, addr):
    if ins in MANUAL:
        return MANUAL[ins]
    out = subprocess.run(["rz-asm", "-a", "arm", "-b", "16", "-o", hex(addr), ins],
                         capture_output=True, text=True).stdout.strip()
    if not out or "ERROR" in out:
        sys.exit("cannot assemble %r @%#x: %s" % (ins, addr, out))
    return bytes.fromhex(out)


def assemble(src):
    lines = [l.split(";")[0].strip() for l in open(src, encoding="utf-8")]
    lines = [l for l in lines if l]
    if not lines[0].startswith(".org"):
        sys.exit("first line must be .org ADDRESS")
    org = int(lines[0].split()[1], 16)
    body = lines[1:]
    names = [x[:-1] for x in body if x.endswith(":")]
    labels, prev = {}, None
    for _ in range(8):  # iterate until label addresses (and so instruction sizes) are stable
        addr, code, listing = org, b"", []
        for l in body:
            if l.endswith(":"):
                labels[l[:-1]] = addr
                continue
            ins = l
            for k, v in labels.items():
                ins = re.sub(r"\b%s\b" % k, hex(v), ins)
            for k in re.findall(r"\b[a-z_][a-z0-9_]*\b", l):
                if k in names and k not in labels:
                    ins = ins.replace(k, hex(addr))  # forward ref, first pass: placeholder
            b = asm(ins, addr)
            listing.append((addr, b, l))
            code += b
            addr += len(b)
        if code == prev:
            break
        prev = code
    else:
        sys.exit("labels did not converge")
    return org, code, labels, listing


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = sys.argv[1]
    org, code, labels, listing = assemble(src)
    if "--list" in sys.argv:
        for a, b, l in listing:
            print("%08x  %-10s %s" % (a, b.hex(), l))
    out = os.path.splitext(src)[0] + ".json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"source": os.path.basename(src), "org": "0x%08x" % org, "length": len(code),
                   "labels": {k: "0x%08x" % v for k, v in labels.items()},
                   "code": [code[i:i + 32].hex() for i in range(0, len(code), 32)]}, f, indent=1)
        f.write("\n")
    print("wrote %s: %d bytes at 0x%08x..0x%08x" % (out, len(code), org, org + len(code)))


if __name__ == "__main__":
    main()
