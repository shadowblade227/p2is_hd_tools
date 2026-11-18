#!/usr/bin/env python3
"""
PMF Cutscene Extractor for Persona 2: Innocent Sin (PSP, ULUS10584).

Extracts the game's Sony PMF movies into lossless PNG frame sequences (or MP4
videos), uncompressed 44.1kHz stereo WAV audio (decoded from Sony's embedded
ATRAC3+ Private Stream 1 track), and JSON metadata.

Requires: ffmpeg and ffprobe installed on system PATH.
"""

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

PACK_SIZE = 2048
SID_AUDIO = 0xBD
SID_PADDING = 0xBE
ATX_PACKET_SIZE = 752


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def extract_pmf_atrac_to_wav(pmf_path: Path, wav_path: Path) -> bool:
    """Demux Sony's Private Stream 1 (0xBD) ATRAC3+ audio from a PMF and decode to WAV."""
    data = pmf_path.read_bytes()
    if data[:8] != b"PSMF0015":
        return False
    strm_off = struct.unpack_from(">I", data, 8)[0]

    raw_audio = bytearray()
    pos = strm_off
    n = len(data)
    while pos + PACK_SIZE <= n:
        pk = data[pos : pos + PACK_SIZE]
        p = 14
        while p + 6 <= PACK_SIZE and pk[p] == 0 and pk[p + 1] == 0 and pk[p + 2] == 1:
            sid = pk[p + 3]
            ln = (pk[p + 4] << 8) | pk[p + 5]
            if sid == SID_AUDIO:
                hlen = pk[p + 8]
                payload_start = p + 9 + hlen
                if payload_start + 4 <= p + 6 + ln and pk[payload_start] == 0x00:
                    raw_audio.extend(pk[payload_start + 4 : p + 6 + ln])
            if sid == SID_PADDING:
                break
            p += 6 + ln
        pos += PACK_SIZE

    if len(raw_audio) < ATX_PACKET_SIZE or len(raw_audio) % ATX_PACKET_SIZE != 0:
        return False

    # Strip per-packet 8-byte Sony 0x0FD0 framing headers and wrap in a 96-byte EA3 (OMA) header
    cfg = bytes(raw_audio[2:4])
    frames = bytearray()
    for i in range(0, len(raw_audio), ATX_PACKET_SIZE):
        if raw_audio[i : i + 2] != b"\x0f\xd0":
            return False
        frames.extend(raw_audio[i + 8 : i + ATX_PACKET_SIZE])

    ea3 = bytearray(96)
    ea3[0:4] = b"EA3\x01"
    ea3[4:6] = b"\x00\x60"
    ea3[6:8] = b"\xff\xff"
    ea3[32] = 0x01  # ATRAC3+
    ea3[34:36] = cfg

    with tempfile.NamedTemporaryFile(suffix=".oma", delete=False) as tf:
        tf.write(ea3)
        tf.write(frames)
        tmp_oma = Path(tf.name)

    try:
        res = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(tmp_oma),
                "-acodec", "pcm_s16le",
                str(wav_path),
            ],
            capture_output=True,
        )
        return res.returncode == 0 and wav_path.exists() and wav_path.stat().st_size > 44
    finally:
        tmp_oma.unlink(missing_ok=True)


def get_video_info(pmf_path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(pmf_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        return {"source_pmf": pmf_path.name}
    try:
        data = json.loads(res.stdout)
        vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        return {
            "source_pmf": pmf_path.name,
            "width": int(vstream.get("width", 480)),
            "height": int(vstream.get("height", 272)),
            "fps": vstream.get("r_frame_rate", "30000/1001"),
            "video_codec": vstream.get("codec_name", "h264"),
            "audio_codec": "atrac3p",
            "duration_sec": float(data.get("format", {}).get("duration", 0.0)),
        }
    except Exception:
        return {"source_pmf": pmf_path.name}


def extract_pmf(pmf_path: Path, out_dir: Path, fmt: str) -> bool:
    clip_name = pmf_path.stem
    clip_dir = out_dir / clip_name
    clip_dir.mkdir(parents=True, exist_ok=True)

    meta = get_video_info(pmf_path)
    (clip_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    if fmt == "png":
        frames_dir = clip_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        cmd_vid = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(pmf_path),
            "-vf", "setpts=N/FRAME_RATE/TB",
            "-fps_mode", "passthrough",
            str(frames_dir / "frame_%06d.png"),
        ]
        res = subprocess.run(cmd_vid)
        if res.returncode != 0:
            print(f"[-] Failed extracting frames for {pmf_path.name}")
            return False
        count = len(list(frames_dir.glob("*.png")))
        detail = f"{count} PNG frames"
    else:
        out_mp4 = clip_dir / f"{clip_name}.mp4"
        cmd_vid = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(pmf_path),
            "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-fps_mode", "passthrough",
            "-an",
            str(out_mp4),
        ]
        res = subprocess.run(cmd_vid)
        if res.returncode != 0:
            print(f"[-] Failed extracting MP4 for {pmf_path.name}")
            return False
        detail = out_mp4.name

    audio_wav = clip_dir / "audio.wav"
    has_audio = extract_pmf_atrac_to_wav(pmf_path, audio_wav)
    print(f"[+] {pmf_path.name}: {meta.get('width', '?')}x{meta.get('height', '?')} -> {detail}, audio={'yes' if has_audio else 'no'}")
    return True


def find_movie_dir(game_dir: Path) -> Path | None:
    for candidate in (
        game_dir / "PSP_GAME" / "USRDIR" / "pack" / "movie",
        game_dir / "USRDIR" / "pack" / "movie",
        game_dir,
    ):
        if candidate.is_dir() and any(candidate.glob("*.pmf")):
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract PMF cutscenes from Persona 2: Innocent Sin."
    )
    parser.add_argument(
        "--game-dir",
        type=Path,
        default=Path("./extracted_iso"),
        help="Path to extracted ISO root, PSP_GAME, or movie folder (default: ./extracted_iso)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./cutscenes_raw"),
        help="Output directory for extracted frames/videos and audio (default: ./cutscenes_raw)",
    )
    parser.add_argument(
        "--format",
        choices=("png", "mp4"),
        default="png",
        help="Output video format: lossless 'png' frame sequence or lossless 'mp4' (default: png)",
    )
    parser.add_argument(
        "videos",
        nargs="*",
        help="Optional movie IDs to extract (e.g. F0141 F0142). Default: all PMF files.",
    )
    args = parser.parse_args()

    if not check_ffmpeg():
        print("[-] Error: ffmpeg and ffprobe must be installed and available on system PATH.")
        sys.exit(1)

    movie_dir = find_movie_dir(args.game_dir)
    if movie_dir is None:
        print(f"[-] No .pmf files found inside {args.game_dir}")
        sys.exit(1)

    pmf_files = sorted(movie_dir.glob("*.pmf"))
    if args.videos:
        wanted = {v.upper().removesuffix(".PMF") for v in args.videos}
        pmf_files = [p for p in pmf_files if p.stem.upper() in wanted]

    if not pmf_files:
        print("[-] No matching .pmf files found.")
        sys.exit(1)

    print(f"Extracting {len(pmf_files)} cutscenes to {args.out_dir} (format={args.format})...")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    ok = sum(1 for p in pmf_files if extract_pmf(p, args.out_dir, args.format))
    print(f"\nDone! Extracted {ok}/{len(pmf_files)} cutscenes.")


if __name__ == "__main__":
    main()
