# Lab environment (Docker)

The Linux container the firmware was analysed in. You **don't need it to use the fix**: the tools in
[`firmware/tools/`](../firmware/tools/) run on Windows (and macOS, not yet tested with a real mic) with plain
Python. It is here so the analysis in
[`research/`](../research/) can be reproduced and the patches in [`firmware/patches/`](../firmware/patches/) rebuilt.
It's a small example of the setup that was used, not a maintained product: take what you need, or use your own image.

## What's inside

| Tool | Used for | Needed by this repo? |
|---|---|---|
| **Rizin** 0.10.0 (pinned commit) + **rz-ghidra** | disassembly / decompilation of the firmware (`af`, `pdg`); `rz-asm` assembles the patches | yes, for `firmware/dev/assemble.py` and `emu_holdmute.py` |
| **TShark** | reading the USBPcap captures (disconnect, factory restore) | for redoing the capture analysis |
| Python 3 | the repo's scripts and tests | yes |
| [Blutter](https://github.com/worawit/blutter) (pinned commit), in `/opt/blutter` | decompiling the RØDE Central Mobile Flutter app (Android arm64 `libapp.so`) for the protocol spec; Dart VMs for Central Mobile 2.0.94 and 2.2.0 prebuilt | for redoing the Central app analysis |
| Ghidra (headless), OpenJDK 21 | used by REA's Ghidra bridge (it can't load the Thumb-only raw image; Rizin did the real work) | no |
| Node.js 26, [REA](https://www.npmjs.com/package/rea-agents), Claude Code | the AI-assisted reverse-engineering setup the investigation was done with | no |
| Neovim, build tools | convenience / building Rizin | — |

For the Rizin command line and the key firmware addresses see [`AGENTS.md`](../AGENTS.md) → "Firmware facts".
The stock image comes from your own RØDE Central install via `firmware/tools/extract_stock.py` (run it on
Windows, then copy the `.bin` into the mounted folder).

## Build

1. Download a Ghidra release zip from <https://github.com/NationalSecurityAgency/ghidra/releases> and save it as
   `docker/ghidra.zip` (it is git-ignored).
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

Flashing, the monitor and USBPcap captures can't run in the container: they need the mic on the host's USB and run
on Windows (flashing also on macOS).
