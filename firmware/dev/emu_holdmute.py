#!/usr/bin/env python3
"""emu_holdmute.py - mini Thumb interpreter for the holdmute add-on (tone_hook = jingle, btn_task = hold-to-mute), running
the REAL bytes of a built i2cfix-volume-holdmute image, decoded by Rizin (`rizin` on PATH). Stock callees are stubbed. Checks pitch,
level, length, register/stack preservation, no stray writes, button timings, and writes WAV previews of the jingles.
Only needed when changing i2cfix-volume-holdmute.s. (Originally written for the fix2 test image; the code is the same.)
usage: python3 emu_holdmute.py podmicusb-1.1.9-i2cfix-volume-holdmute.bin [out_dir]"""
import json, re, struct, subprocess, sys, wave, os

IMG = sys.argv[1]; OUT = sys.argv[2] if len(sys.argv) > 2 else "."
BASE = 0x08004000
flash = bytearray(open(IMG, "rb").read())
ram = bytearray(0x2000)
M32 = 0xFFFFFFFF
S32 = lambda x: x - (1 << 32) if x & 0x80000000 else x
labels = {k: int(v, 16) for k, v in json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "..", "patches", "i2cfix-volume-holdmute.json")))["labels"].items()}
DIS_AT = min(labels["tone_hook"], labels["btn_task"])   # tone_hook + btn_task = 0xe8 bytes

dis = json.loads(subprocess.run(["rizin", "-q", "-e", "scr.color=0", "-e", "asm.cpu=cortexm", "-a", "arm", "-b", "16",
                                 "-m", hex(BASE), "-c", "s %#x; pDj 0xe8" % DIS_AT, IMG], capture_output=True, text=True).stdout)
INS = {x["offset"]: (x["disasm"], x["size"]) for x in dis}

def rd(a, n):
    if 0x20000000 <= a < 0x20002000: b = ram[a - 0x20000000:a - 0x20000000 + n]
    elif BASE <= a < BASE + len(flash): b = flash[a - BASE:a - BASE + n]
    else: raise Exception("read %#x" % a)
    return int.from_bytes(b, "little")
def wr(a, n, v):
    if not 0x20000000 <= a < 0x20002000: raise Exception("write outside RAM %#x" % a)
    ram[a - 0x20000000:a - 0x20000000 + n] = (v & ((1 << 8 * n) - 1)).to_bytes(n, "little")
WRITES = set()

ALIAS = {"ip": 12, "sb": 9, "sl": 10, "fp": 11, "sp": 13, "lr": 14, "pc": 15}
def rn(t):
    t = t.strip(); return ALIAS[t] if t in ALIAS else int(t[1:])
def imm(t): return int(t.strip().lstrip("#"), 0)

class CPU:
    def __init__(s): s.r = [0] * 16; s.Z = False
    def run(s, entry, stubs, maxsteps=100000):
        s.r[14] = 0xFFFFFFF0; pc = entry; n = 0
        while True:
            n += 1
            if n > maxsteps: raise Exception("runaway")
            if pc == 0xFFFFFFF0: return
            if pc in stubs:
                ret = stubs[pc](s)
                pc = ret if ret is not None else s.r[14]; continue
            text, size = INS[pc]; nxt = pc + size
            op, _, args = text.partition(" "); a = [x.strip() for x in re.split(r",(?![^{]*})(?![^\[]*\])", args)] if args else []
            r = s.r
            def mem(t):   # [rn, x] -> address
                inner = t.strip()[1:-1].split(",")
                base = r[rn(inner[0])]
                if len(inner) == 1: return base
                x = inner[1].strip()
                if x.startswith("r") or x in ALIAS:
                    v = r[rn(x)]
                    if len(inner) == 3: v <<= int(inner[2].split()[1])
                    return (base + v) & M32
                return (base + imm(x)) & M32
            if op in ("push", "push.w"):
                regs = [rn(x) for x in a[0].strip("{}").split(",")]
                r[13] -= 4 * len(regs)
                for i, q in enumerate(sorted(regs)): wr(r[13] + 4 * i, 4, r[q])
            elif op in ("pop", "pop.w"):
                regs = sorted(rn(x) for x in a[0].strip("{}").split(","))
                for i, q in enumerate(regs): r[q] = rd(r[13] + 4 * i, 4)
                r[13] += 4 * len(regs)
                if 15 in regs: pc = r[15]; continue
            elif op == "movw": r[rn(a[0])] = imm(a[1])
            elif op == "movt": r[rn(a[0])] = (r[rn(a[0])] & 0xFFFF) | (imm(a[1]) << 16)
            elif op == "movs": r[rn(a[0])] = imm(a[1]); s.Z = r[rn(a[0])] == 0
            elif op == "mov": r[rn(a[0])] = r[rn(a[1])]
            elif op in ("ldrb",): r[rn(a[0])] = rd(mem(a[1]), 1)
            elif op in ("ldr", "ldr.w"): r[rn(a[0])] = rd(mem(a[1]), 4)
            elif op == "strb": WRITES.add(mem(a[1])); wr(mem(a[1]), 1, r[rn(a[0])])
            elif op == "strh": WRITES.add(mem(a[1])); wr(mem(a[1]), 2, r[rn(a[0])])
            elif op in ("str", "str.w"): WRITES.add(mem(a[1])); wr(mem(a[1]), 4, r[rn(a[0])])
            elif op == "ldrd":
                ad = mem(a[2]); r[rn(a[0])] = rd(ad, 4); r[rn(a[1])] = rd(ad + 4, 4)
            elif op == "strd":
                ad = mem(a[2]); wr(ad, 4, r[rn(a[0])]); wr(ad + 4, 4, r[rn(a[1])])
                if len(a) == 4: b = rn(a[2].strip("[]")); r[b] = (r[b] + imm(a[3])) & M32
            elif op == "ldm":
                b = r[rn(a[0])]
                for i, q in enumerate(sorted(rn(x) for x in a[1].strip("{}").split(","))): r[q] = rd(b + 4 * i, 4)
            elif op in ("adds", "subs", "cmp", "addw", "subw", "add.w", "orr.w", "lsls", "eors"):
                if op == "cmp": d, x, y = None, r[rn(a[0])], a[1]
                elif len(a) == 2: d, x, y = rn(a[0]), r[rn(a[0])], a[1]
                else: d, x, y = rn(a[0]), r[rn(a[1])], a[2]
                if y.startswith("r") or y in ALIAS: yv = r[rn(y)]
                else: yv = imm(y)
                if len(a) == 4:
                    k, sh = a[3].split()
                    yv = (yv << int(sh)) & M32 if k == "lsl" else yv >> int(sh)
                if op in ("adds", "addw", "add.w"): v = (x + yv) & M32
                elif op in ("subs", "cmp", "subw"): v = (x - yv) & M32
                elif op == "orr.w": v = x | yv
                elif op == "eors": v = x ^ yv
                elif op == "lsls": v = (x << yv) & M32
                if op in ("adds", "subs", "cmp", "lsls", "eors"): s.Z = v == 0
                if d is not None: r[d] = v
            elif op == "smull":
                p = S32(r[rn(a[2])]) * S32(r[rn(a[3])]); r[rn(a[0])] = p & M32; r[rn(a[1])] = (p >> 32) & M32
            elif op == "qadd":
                v = S32(r[rn(a[1])]) + S32(r[rn(a[2])]); v = max(-2**31, min(2**31 - 1, v)); r[rn(a[0])] = v & M32
            elif op in ("b", "b.w"): pc = int(a[0], 0); continue
            elif op == "beq":
                if s.Z: pc = int(a[0], 0); continue
            elif op == "bne":
                if not s.Z: pc = int(a[0], 0); continue
            elif op in ("cbz", "cbnz"):
                if (r[rn(a[0])] == 0) == (op == "cbz"): pc = int(a[1], 0); continue
            elif op == "bl": r[14] = nxt; pc = int(a[0], 0); continue
            else: raise Exception("unhandled %s @%#x" % (text, pc))
            pc = nxt

MUTE = 0x200001c5; D7C = 0x20000d7c; D7D = 0x20000d7d; OUTFLAG = 0x20001648; BUF = 0x20000e00
events = []
def stub_setmute(c):            # stock 0x08008de8(new): struct+1 = new (+LED); returns to lr
    wr(MUTE, 1, c.r[0]); events.append(("setmute", c.r[0]))
def stub_click(c):              # stock 0x080092a2 tail: short-click action, ends with pop {r3, pc}
    events.append(("click",)); c.r[13] += 8; return rd(c.r[13] - 4, 4)
STUBS = {0x08008de8: stub_setmute, 0x080092a2: stub_click}

cpu = CPU(); SP0 = 0x20001f00
def call(entry, regs=None):
    cpu.r = [0x1111 * i for i in range(16)]
    if regs: 
        for k, v in regs.items(): cpu.r[k] = v
    cpu.r[13] = SP0
    before = list(cpu.r)
    cpu.run(entry, STUBS)
    assert cpu.r[13] == SP0, "sp not balanced"
    for q in (4, 5, 6, 7, 8, 9, 10, 11): assert cpu.r[q] == before[q], "r%d clobbered" % q
    return cpu.r

wr(OUTFLAG, 1, 0)              # stock 'output not blocked' byte (any value; must come back in r2)
samples = []
def block():
    for i in range(32): wr(BUF + 4 * i, 4, 0x01000000 * (i % 3) )   # some "music" to check saturating add
    r = call(labels["tone_hook"], {5: BUF})
    assert r[3] == OUTFLAG and r[2] == rd(OUTFLAG, 1), "r2/r3 outputs wrong"
    out = [S32(rd(BUF + 4 * i, 4)) - 0x01000000 * (i % 3) for i in range(32)]
    for i in range(0, 32, 2): assert out[i] == out[i + 1], "L != R"
    samples.extend(out[0::2])

# --- tone: first observation must be silent, then mute -> falling, unmute -> rising
for _ in range(50): block()
assert not any(samples), "tone at boot / first observation"
def jingle(newmute, nblocks=800):
    start = len(samples); wr(MUTE, 1, newmute)
    for _ in range(nblocks): block()
    return samples[start:]
def segs(x):
    out, cur, z = [], None, 0
    i = 0; nz = [k for k, v in enumerate(x) if v]
    return nz[0] if nz else None, nz[-1] if nz else None
mute_s = jingle(1); unmute_s = jingle(0)
FS = 48000
for name, s in (("mute", mute_s), ("unmute", unmute_s)):
    first, last = segs(s)
    peak = max(abs(v) for v in s)
    # zero crossings per half to estimate frequency of each note
    def freq(part):
        zc = sum(1 for i in range(1, len(part)) if (part[i-1] < 0) != (part[i] < 0))
        return zc / 2 / (len(part) / FS)
    a = s[first:first + 3840]; b = s[first + 3840 + 1920:first + 3840 + 1920 + 3840]
    print("%-6s: starts sample %d (%.2f ms), sound span %.1f ms, peak %.4f FS (%.1f dBFS), "
          "note1 ~%.0f Hz, note2 ~%.0f Hz, first sample %d, last nonzero |%d|"
          % (name, first, first / 48, (last - first) / 48, peak / 2**31, 20 * __import__('math').log10(peak / 2**31),
             freq(a), freq(b), s[first], abs(s[last])))
# --- re-toggle in the middle of a jingle
mid = len(samples); wr(MUTE, 1, 1)
for _ in range(100): block()
wr(MUTE, 1, 0)
for _ in range(800): block()
print("retrigger mid-jingle: ok, %d samples" % (len(samples) - mid))

# --- button: ticks of 4 ms; level 0 = pressed. Stock sampler sets D7D on 1->0 edge.
def tick(level):
    prev = rd(D7C, 1)
    if prev == 1 and level == 0: wr(D7D, 1, 1)
    wr(D7C, 1, level)
    call(labels["btn_task"])
wr(D7C, 1, 1); wr(MUTE, 1, 0); events.clear()
def press(ticks, idle=20):
    for _ in range(ticks): tick(0)
    for _ in range(idle): tick(1)
    e = list(events); events.clear(); return e
print("short 40 ms  :", press(10))
print("hold 0.66 s  :", press(165))
print("hold 0.70 s  :", press(175), "mute byte", rd(MUTE, 1))
print("hold 3 s     :", press(750), "mute byte", rd(MUTE, 1))
print("two clicks   :", press(5) + press(5))
print("idle ticks   :", press(0, 100))
bad = [hex(a) for a in WRITES if not (0x20001af4 <= a < 0x20001b10 or a in (MUTE, D7D) or SP0 - 64 <= a < SP0
                                      or BUF <= a < BUF + 128)]
print("writes outside TS/BT/mute/edge/stack/buffer:", bad or "none")

# --- WAV preview (mono 16-bit), jingles only
os.makedirs(OUT, exist_ok=True)
for name, s in (("jingle_mute", mute_s), ("jingle_unmute", unmute_s)):
    with wave.open(os.path.join(OUT, name + ".wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(FS)
        w.writeframes(b"".join(struct.pack("<h", v >> 16) for v in [0] * 4800 + s[:20000] + [0] * 4800))
print("wrote", OUT)
