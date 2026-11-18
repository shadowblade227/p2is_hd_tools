# Persona 2: Innocent Sin (PSP) — Modding & Remaster Tools

Standalone Python tools for extracting and repacking textures and PMF cutscenes for Shin Megami Tensei: Persona 2: Innocent Sin (USA, ULUS10584).

These scripts handle archive extraction, format conversion, PPSSPP texture hashing, PMF multiplexing, executable patching, and ISO rebuilding. They do not bundle any AI upscalers or models — edit or upscale the exported images and videos with whatever software you prefer.

The functionality of the repackaged versions has been tested at:
- https://github.com/hrydgard/ppsspp
- https://github.com/NaGaa95/ppsspp-nx
- ISO from Rutracker.org

## Layout

```text
p2is_hd_tools/
├── requirements.txt
├── textures/
│   ├── extract_textures.py   # Extract 1x PNGs + PPSSPP hashes directly from game files
│   └── repack_textures.py    # Pack edited/upscaled PNGs into a PPSSPP texture pack
└── cutscenes/
    ├── extract_cutscenes.py  # Demux PMF movies into PNG frames (or MP4) + WAV audio
    ├── repack_cutscenes.py   # Encode & mux videos or PNG folders back into Sony PMF
    ├── patch_game.py         # Patch EBOOT.OLD & PARAM.SFO for HD/custom PMF playback
    └── build_iso.py          # Rebuild a bootable PSP ISO with patched files
```

## Requirements

- Python 3.10+
  ```bash
  pip install -r requirements.txt
  ```
- FFmpeg & FFprobe on your system `PATH` (only needed for the cutscene tools).
- An extracted copy of **Persona 2: Innocent Sin (USA, `ULUS10584`)** ISO containing `PSP_GAME/` and `UMD_DATA.BIN` (with decrypted `PSP_GAME/SYSDIR/EBOOT.OLD`).

## 1. Textures (PPSSPP Texture Replacement)

PSP hardware requires textures to sit on power-of-two canvases (`64`, `128`, `256`, `512`), so many native assets (like `192×192` portraits or `480×272` UI screens) are stored with empty transparent borders. `extract_textures.py` crops those borders out by default so upscalers don't create dark halo artifacts along the transparent edge and don't waste compute. `repack_textures.py` places your upscaled images back onto properly scaled power-of-two canvases with a matching `textures.ini`.

### Extract

```bash
python textures/extract_textures.py --game-dir ./extracted_iso --out-dir ./textures_work
```

This scans `P2PT_ALL.cpk`, `datapack.bin`, and `EBOOT.OLD`, computes exact PPSSPP `XXH64` hashes directly from the game data, deduplicates identical assets, and writes:

- `textures_work/unpadded/` — 1x PNGs grouped into folders (`portraits`, `ui`, `cards`, `events`, `maps`, `sprites_overworld`, `sprites_battle`, `dungeons`, `minigames`).
- `textures_work/manifest.json` — canvas dimensions and hash mappings used during repacking.

*(Pass `--padded` if you prefer full power-of-two canvases instead of cropped PNGs.)*

### Edit / Upscale

Upscale or edit any PNGs from `textures_work/unpadded/` and save the results to `./textures_upscaled/` keeping the same subfolder names and filenames.

### Repack

**Full pack:**
```bash
python textures/repack_textures.py \
  --input ./textures_upscaled \
  --manifest ./textures_work/manifest.json \
  --output ./ULUS10584
```

**Partial pack** (for example, if you only upscaled `portraits/` and `ui/`):
```bash
python textures/repack_textures.py \
  --input ./textures_upscaled \
  --manifest ./textures_work/manifest.json \
  --output ./ULUS10584 \
  --partial
```

Optional flags:
- `--scale 4` — verify every image is an exact integer `4x` upscale (otherwise detected automatically per image).
- `--no-inverted` — skip auto-generating color-inverted portrait variants used during in-game flashback effects.

### Install in PPSSPP

Copy the generated `ULUS10584/` folder into your PPSSPP `PSP/TEXTURES/` directory and enable **Settings → Tools → Developer tools → Replace textures**.

## 2. Cutscenes (PMF Extraction, Repacking & ISO Build)

### Extract

Export all 19 `.pmf` cutscenes (`F0141`–`F0159`) to lossless PNG frames and WAV audio:
```bash
python cutscenes/extract_cutscenes.py --game-dir ./extracted_iso --out-dir ./cutscenes_raw
```

Pass `--format mp4` if you prefer lossless `.mp4` files instead of PNG sequences, or pass specific movie IDs (e.g. `F0141 F0142`).

### Upscale / Edit

Process the extracted frames or videos at your desired resolution (multiple of 16, up to `1280×720` for the 16:9 opening `F0141` and `960×720` for the 4:3 cutscenes `F0142`–`F0159`) and place the outputs in `./cutscenes_upscaled/`.

`repack_cutscenes.py` accepts either:
- Video files (`cutscenes_upscaled/F0141.mp4`, `.mkv`, etc.), or
- PNG frame folders (`cutscenes_upscaled/F0141/frames/*.png` or `cutscenes_upscaled/F0141/*.png`).

### Repack PMFs

```bash
python cutscenes/repack_cutscenes.py \
  --orig-movies ./extracted_iso/PSP_GAME/USRDIR/pack/movie \
  --input-dir ./cutscenes_upscaled \
  --out-dir ./repack_work/PSP_GAME/USRDIR/pack/movie
```

Original ATRAC3+ audio tracks are preserved bit-for-bit, and resolution/bitrate are detected automatically (or override with `--bitrate <kbps>`).

### Patch `EBOOT.OLD` & `PARAM.SFO`

Patch the game executable and `PARAM.SFO` to unlock 64 MB RAM, bypass hardcoded UMD sector offsets, and configure the movie decoder buffers for your target resolution (defaults to `1280×720` for 16:9 and `960×720` for 4:3):

```bash
python cutscenes/patch_game.py \
  --src-dir ./extracted_iso/PSP_GAME \
  --out-dir ./repack_work/PSP_GAME
```

If you encoded at a different resolution (e.g. `960×544` / `720×544`), pass `--w16x9`, `--w4x3`, and `--height` to match.

### Rebuild ISO

```bash
python cutscenes/build_iso.py \
  --base-dir ./extracted_iso \
  --patch-dir ./repack_work \
  --orig-iso "./Persona 2 - Innocent Sin USA.ISO" \
  --out-iso "./Persona 2 - Innocent Sin USA [Remastered].iso"
```

## Disclaimer

This repository contains only tools and source code. No copyrighted game assets or ROMs are included.
