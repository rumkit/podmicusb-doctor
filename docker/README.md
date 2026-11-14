# Lab environment (Docker)

The Linux container the firmware was analysed in. You **don't need it to use the fix**: the tools in
[`firmware/tools/`](../firmware/tools/) run on Windows with plain Python. It is here so the analysis in
[`research/`](../research/) can be reproduced and the patches in [`firmware/patches/`](../firmware/patches/) rebuilt.

## What's inside

| Tool | Used for | Needed by this repo? |
|---|---|---|
| **Rizin** 0.10.0 (pinned commit) + **rz-ghidra** | disassembly / decompilation of the firmware (`af`, `pdg`); `rz-asm` assembles the patches | yes, for `firmware/dev/assemble.py` and `emu_holdmute.py` |
| **TShark** | reading the USBPcap captures (disconnect, factory restore) | for redoing the capture analysis |
| Python 3 | the repo's scripts and tests | yes |
| Ghidra (headless), OpenJDK 21 | used by REA's Ghidra bridge (it can't load the Thumb-only raw image; Rizin did the real work) | no |
| Node.js 26, [REA](https://www.npmjs.com/package/rea-agents), Claude Code | the AI-assisted reverse-engineering setup the investigation was done with | no |
| Neovim, build tools | convenience / building Rizin | — |

Opening the firmware: `rizin -a arm -b 16 -m 0x08004000 -e asm.cpu=cortexm podmicusb-1.1.9-stock.bin`
(see [`AGENTS.md`](../AGENTS.md) for key addresses). The stock image comes from your own RØDE Central install via
`firmware/tools/extract_stock.py` (run it on Windows, then copy the `.bin` into the mounted folder).

## Build

1. Download a Ghidra release zip from <https://github.com/NationalSecurityAgency/ghidra/releases> and save it as
   `docker/ghidra.zip` (it is git-ignored). If you don't want Ghidra, delete step 5 of the Dockerfile instead.
2. From the repo root:
   ```bash
   docker build -t podmic-re docker/
   ```
   Rizin and rz-ghidra are compiled from source, so the first build takes a while. Both are pinned through the
   build args `RIZIN_REF` / `RZ_GHIDRA_REF`; if rz-ghidra's `dev` branch has moved on and no longer builds against
   the pinned Rizin, set both to matching release tags.

## Run

```bash
docker run -it --name podmic-re -v podmic-re-home:/root -v "$PWD":/workspace podmic-re
```
- `-v "$PWD":/workspace`: your working folder (repo, stock image, captures) inside the container.
- `-v podmic-re-home:/root` (optional): keeps the home directory (shell history, Claude Code / REA login and
  settings) across container restarts. Later: `docker start -ai podmic-re`.
- Secrets (e.g. `CLAUDE_CODE_OAUTH_TOKEN`) are never baked into the image; if you need them, pass them at run
  time with `--env-file .env` and keep `.env` out of git.

The Windows-only parts (flashing, the monitor, USBPcap captures) can't run in the container: they need the mic
on the host's USB.

## Status

This Dockerfile was written to reproduce the container the investigation ran in (Ubuntu 26.04, Rizin 0.10.0
@ 46565f6, rz-ghidra, TShark 4.6.4, REA, Claude Code). **It has not itself been built and tested yet.** Floating
parts (the Ubuntu base image, NodeSource, npm packages, rz-ghidra `dev`) may also have changed since.
