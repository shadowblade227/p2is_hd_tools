#!/usr/bin/env python3
"""
Takes upscaled or edited PNGs matching the directory layout created by
extract_textures.py, verifies dimensions, places each image onto its exact
scaled PSP power-of-two canvas, and generates a ready-to-use PPSSPP texture
replacement pack (ULUS10584/textures.ini + PNGs).
"""

import argparse
import json
import shutil
from collections import Counter
from pathlib import Path

import cv2
import numpy as np


def read_as_rgba(path: Path) -> np.ndarray:
    arr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if arr is None:
        raise ValueError(f"Failed to read image: {path}")
    if arr.ndim == 2:
        return np.stack([arr, arr, arr, np.full_like(arr, 255)], axis=-1)
    if arr.shape[2] == 3:
        rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
        alpha = np.full((arr.shape[0], arr.shape[1], 1), 255, dtype=np.uint8)
        return np.concatenate([rgb, alpha], axis=-1)
    if arr.shape[2] == 4:
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2RGBA)
    raise ValueError(f"Unsupported image channel layout {arr.shape} in {path}")


def place_on_pow2_canvas(rgba: np.ndarray, pw: int, ph: int) -> np.ndarray:
    h, w = rgba.shape[:2]
    if w == pw and h == ph:
        return rgba
    canvas = np.zeros((ph, pw, 4), dtype=np.uint8)
    cw, ch = min(w, pw), min(h, ph)
    canvas[:ch, :cw] = rgba[:ch, :cw]
    return canvas


def write_rgba_png(path: Path, rgba: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    cv2.imwrite(str(path), bgra)


def invert_rgb_preserve_alpha(rgba: np.ndarray) -> np.ndarray:
    out = rgba.copy()
    mask = out[:, :, 3] > 0
    out[mask, 0] = 255 - out[mask, 0]
    out[mask, 1] = 255 - out[mask, 1]
    out[mask, 2] = 255 - out[mask, 2]
    return out


def repack(
    input_dir: Path,
    output_dir: Path,
    manifest_path: Path,
    expected_scale: int | None = None,
    partial: bool = False,
    include_inverted: bool = True,
) -> None:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    if not input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ini_lines = []
    used_keys = set()
    scale_counts = Counter()
    packed_count = 0
    inverted_count = 0
    missing_count = 0

    for item in manifest["items"]:
        rel_path = item["rel_path"]
        keys = item.get("keys", [])
        if not keys:
            continue

        src = input_dir / rel_path
        if not src.exists():
            missing_count += 1
            if partial:
                continue
            raise FileNotFoundError(
                f"Missing texture: {src}\n"
                "Re-run with --partial if you only want to pack a subset of textures."
            )

        w, h = item["w"], item["h"]
        pw, ph = item["pow2_w"], item["pow2_h"]
        rgba = read_as_rgba(src)
        uh, uw = rgba.shape[:2]

        # Accept either unpadded (S*w, S*h) or already-padded (S*pw, S*ph) input
        if uw % w == 0 and uh % h == 0 and (uw // w) == (uh // h):
            scale = uw // w
            is_already_padded = False
        elif uw % pw == 0 and uh % ph == 0 and (uw // pw) == (uh // ph):
            scale = uw // pw
            is_already_padded = True
        else:
            raise ValueError(
                f"Dimension mismatch for {rel_path}: got {uw}x{uh}, "
                f"expected integer multiple of {w}x{h} (unpadded) or {pw}x{ph} (padded)."
            )

        if expected_scale is not None and scale != expected_scale:
            raise ValueError(
                f"Scale mismatch for {rel_path}: detected {scale}x ({uw}x{uh}), expected {expected_scale}x."
            )

        canvas = rgba if is_already_padded else place_on_pow2_canvas(rgba, pw * scale, ph * scale)
        write_rgba_png(output_dir / rel_path, canvas)
        packed_count += 1
        scale_counts[scale] += 1

        for k in keys:
            if k not in used_keys:
                ini_lines.append(f"{k} = {rel_path}")
                used_keys.add(k)

        inv_keys = item.get("inverted_keys", [])
        if include_inverted and inv_keys:
            inv_rel = f"portraits_inverted/{Path(rel_path).name}"
            inv_rgba = invert_rgb_preserve_alpha(rgba)
            inv_canvas = inv_rgba if is_already_padded else place_on_pow2_canvas(inv_rgba, pw * scale, ph * scale)
            write_rgba_png(output_dir / inv_rel, inv_canvas)
            inverted_count += 1
            for ik in inv_keys:
                if ik not in used_keys:
                    ini_lines.append(f"{ik} = {inv_rel}")
                    used_keys.add(ik)

    header = [
        "# Persona 2: Innocent Sin (ULUS10584) Texture Pack",
        "[options]",
        "version = 1",
        "hash = xxh64",
        "ignoreAddress = true",
        "reduceHash = True",
        "ignoreMipmap = true",
        "",
        "[games]",
        "ULUS10584 = textures.ini",
        "",
        "[hashes]",
    ]
    (output_dir / "textures.ini").write_text("\n".join(header + ini_lines) + "\n", encoding="utf-8")

    print(f"Done! Built PPSSPP texture pack at: {output_dir}")
    print(f"   Textures packed:   {packed_count} (scales: {dict(scale_counts)})")
    if inverted_count:
        print(f"   Inverted variants: {inverted_count}")
    if partial and missing_count:
        print(f"   Skipped (partial): {missing_count}")
    print(f"   Total hash keys:   {len(ini_lines)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repack upscaled/edited textures into a ready-to-use PPSSPP texture pack (ULUS10584)."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("./textures_upscaled"),
        help="Directory containing your upscaled/edited PNGs (default: ./textures_upscaled)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("./textures_work/manifest.json"),
        help="Path to manifest.json generated by extract_textures.py (default: ./textures_work/manifest.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./ULUS10584"),
        help="Output PPSSPP texture pack directory (default: ./ULUS10584)",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=None,
        help="Optionally enforce a specific integer upscale factor (e.g. 2 or 4)",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="Allow repacking only the subset of textures present in --input (e.g. UI/portraits only)",
    )
    parser.add_argument(
        "--no-inverted",
        action="store_true",
        help="Do not auto-generate color-inverted flashback portrait variants",
    )
    args = parser.parse_args()

    repack(
        input_dir=args.input,
        output_dir=args.output,
        manifest_path=args.manifest,
        expected_scale=args.scale,
        partial=args.partial,
        include_inverted=not args.no_inverted,
    )


if __name__ == "__main__":
    main()
