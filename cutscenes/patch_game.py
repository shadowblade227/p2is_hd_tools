#!/usr/bin/env python3
"""
Applies the patches required to play custom/high-resolution PMF cutscenes and
load resized files from a rebuilt ISO:
  1. PARAM.SFO : Enables 64 MB extended memory mode (MEMSIZE = 1).
  2. EBOOT.OLD :
     - Expands module heap (14 MB -> 24 MB) and movie decode buffers (9 MB).
     - Bypasses hardcoded UMD LBN sector offsets so resized PMFs load by path.
     - Updates movie decoder source dimensions, framebuffer stride, and GU
       texture size for the configured cutscene resolution (up to 720p).
     - Scales 4:3 cutscene display rectangles to full vertical 272px height.
     - Validates every MIPS .rel.text relocation entry before and after patching.
"""

import argparse
import hashlib
import struct
from pathlib import Path

VA_TO_FILE_OFF = 0xC0
REL_TEXT_OFF = 0x49E4E0
REL_TEXT_SIZE = 0x146848


def next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p <<= 1
    return p


def patch_sfo(src_path: Path, dst_path: Path) -> None:
    data = src_path.read_bytes()
    magic, ver, kt_off, dt_off, count = struct.unpack_from("<4sIIII", data, 0)
    assert magic == b"\x00PSF", f"Unexpected SFO magic: {magic!r}"

    entries = []
    for i in range(count):
        k_off, fmt, used, total, d_off = struct.unpack_from("<HHIII", data, 20 + i * 16)
        k_end = data.find(b"\x00", kt_off + k_off)
        key = data[kt_off + k_off : k_end].decode("ascii")
        val = data[dt_off + d_off : dt_off + d_off + total]
        entries.append([key, fmt, used, total, val])

    entry_map = {e[0]: e for e in entries}
    assert entry_map["DISC_ID"][4].startswith(b"ULUS10584\x00"), "Not a ULUS10584 PARAM.SFO"

    if "MEMSIZE" in entry_map:
        entry_map["MEMSIZE"][1] = 0x0404
        entry_map["MEMSIZE"][2] = 4
        entry_map["MEMSIZE"][3] = 4
        entry_map["MEMSIZE"][4] = struct.pack("<I", 1)
    else:
        entries.append(["MEMSIZE", 0x0404, 4, 4, struct.pack("<I", 1)])

    entries.sort(key=lambda e: e[0].encode("ascii"))

    key_blob = bytearray()
    data_blob = bytearray()
    index_blob = bytearray()

    for key, fmt, used, total, val in entries:
        k_off = len(key_blob)
        key_blob.extend(key.encode("ascii") + b"\x00")

        while len(data_blob) % 4 != 0:
            data_blob.append(0)
        d_off = len(data_blob)
        padded_val = val[:total].ljust(total, b"\x00")
        data_blob.extend(padded_val)

        index_blob.extend(struct.pack("<HHIII", k_off, fmt, used, total, d_off))

    new_kt_off = 20 + len(index_blob)
    while (new_kt_off + len(key_blob)) % 4 != 0:
        key_blob.append(0)
    new_dt_off = new_kt_off + len(key_blob)

    header = struct.pack("<4sIIII", b"\x00PSF", ver, new_kt_off, new_dt_off, len(entries))
    out = bytes(header) + bytes(index_blob) + bytes(key_blob) + bytes(data_blob)

    # Round-trip verification
    m2, _, kt2, dt2, cnt2 = struct.unpack_from("<4sIIII", out, 0)
    assert m2 == b"\x00PSF" and cnt2 == len(entries)
    parsed = {}
    for i in range(cnt2):
        ko, fm, us, tot, do = struct.unpack_from("<HHIII", out, 20 + i * 16)
        ke = out.find(b"\x00", kt2 + ko)
        k = out[kt2 + ko : ke].decode("ascii")
        parsed[k] = (fm, us, tot, out[dt2 + do : dt2 + do + us])
    assert parsed["MEMSIZE"] == (0x0404, 4, 4, b"\x01\x00\x00\x00")
    assert parsed["DISC_ID"][3] == b"ULUS10584\x00"

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(out)
    print(f"[SFO]   Wrote {dst_path} ({len(out)} bytes, MEMSIZE=1)")


def write_u32_va(buf: bytearray, va: int, expected: int, new_val: int, label: str) -> None:
    off = va + VA_TO_FILE_OFF
    actual = struct.unpack_from("<I", buf, off)[0]
    if actual != expected:
        raise ValueError(
            f"Mismatch at VA 0x{va:06x} ({label}): expected 0x{expected:08x}, got 0x{actual:08x}"
        )
    struct.pack_into("<I", buf, off, new_val)


def validate_rel_text(buf: bytes) -> None:
    """Validate MIPS HI16/LO16 relocation pairing and R_MIPS_26 jump targets."""
    num_rels = REL_TEXT_SIZE // 8
    rels = [struct.unpack_from("<II", buf, REL_TEXT_OFF + i * 8) for i in range(num_rels)]

    R_MIPS_NONE = 0
    R_MIPS_26 = 4
    R_MIPS_HI16 = 5
    R_MIPS_LO16 = 6
    R_MIPS_16 = 7

    def seg_loaded(seg: int) -> bool:
        return seg in (0, 1)

    next_lo = [-1] * (num_rels + 1)
    for t in range(num_rels - 1, -1, -1):
        _, r_info = rels[t]
        t_type = r_info & 0xF
        t_rw = (r_info >> 8) & 0xFF
        if t_type in (R_MIPS_LO16, R_MIPS_16) and seg_loaded(t_rw):
            next_lo[t] = t
        else:
            next_lo[t] = next_lo[t + 1]

    logged_up_to = 0
    errors = []
    for r, (r_off, r_info) in enumerate(rels):
        r_type = r_info & 0xF
        rw = (r_info >> 8) & 0xFF
        if not seg_loaded(rw):
            errors.append(f"rel[{r}] bad segment {rw}")
            continue
        if r_type == R_MIPS_HI16:
            t = next_lo[r + 1]
            skip_end = num_rels if t < 0 else t
            for s in range(max(r + 1, logged_up_to), skip_end):
                s_off, s_info = rels[s]
                s_type = s_info & 0xF
                s_rw = (s_info >> 8) & 0xFF
                if s_type == R_MIPS_HI16 or not seg_loaded(s_rw):
                    continue
                if s_type not in (R_MIPS_LO16, R_MIPS_16):
                    errors.append(
                        f"HI16 at rel[{r}] va=0x{r_off:06x} skipped non-LO16 rel[{s}] type={s_type} va=0x{s_off:06x}"
                    )
            logged_up_to = max(logged_up_to, skip_end)
            if t < 0:
                errors.append(f"HI16 at rel[{r}] va=0x{r_off:06x} has no LO16")

    for r, (r_off, r_info) in enumerate(rels):
        r_type = r_info & 0xF
        rw = (r_info >> 8) & 0xFF
        if r_type == R_MIPS_NONE or rw != 0:
            continue
        op = struct.unpack_from("<I", buf, r_off + VA_TO_FILE_OFF)[0]
        if r_type == R_MIPS_26:
            opcode = op >> 26
            if opcode not in (2, 3):
                errors.append(f"rel[{r}] va=0x{r_off:06x} R_MIPS_26 points to non-j/jal op=0x{op:08x}")

    if errors:
        raise RuntimeError("Relocation validation failed:\n" + "\n".join(errors[:20]))
    print(f"[RELOC] Validated {num_rels} .rel.text entries: 0 errors")


def patch_eboot(
    src_path: Path,
    dst_path: Path,
    w16x9: int = 1280,
    w4x3: int = 960,
    height: int = 720,
) -> None:
    for name, val in (("w16x9", w16x9), ("w4x3", w4x3), ("height", height)):
        if val <= 0 or val % 16 != 0:
            raise ValueError(f"--{name} must be a positive multiple of 16 (got {val})")

    stride = ((max(w16x9, w4x3) + 63) // 64) * 64
    tbw = next_pow2(stride)
    tbh = next_pow2(height)
    frame_bytes = stride * height * 4
    if tbw > 2048 or tbh > 1024 or frame_bytes > 0x400000:
        raise ValueError(
            f"Resolution {w16x9}x{height} (stride={stride}, GU tex {tbw}x{tbh}, "
            f"{frame_bytes / 1048576:.2f} MB/frame) exceeds the 4 MB double-buffered "
            f"PSP movie framebuffer limit (maximum supported: 1280x720)."
        )

    raw = src_path.read_bytes()
    if raw[:4] != b"\x7fELF":
        raise ValueError(
            f"{src_path} is not a decrypted ELF executable (starts with {raw[:4]!r}). "
            "Use the decrypted EBOOT.OLD from the game dump."
        )
    buf = bytearray(raw)

    validate_rel_text(buf)

    # 1. Expand module heap: 0x3800 (14 MB) -> 0x6000 (24 MB)
    write_u32_va(buf, 0x3D1BCC, 0x00003800, 0x00006000, "Module heap KB")

    # 2. Expand movie decode buffers (9 MB alloc / 8 MB double-buffer memset)
    write_u32_va(buf, 0x000C4C, 0x3C040011, 0x3C040090, "Boot alloc 1 -> 9MB")
    write_u32_va(buf, 0x00135C, 0x3C040011, 0x3C040090, "Boot alloc 2 -> 9MB")
    write_u32_va(buf, 0x010B80, 0x3C060011, 0x3C060080, "Movie memset -> 8MB")

    # 3. Bypass sce_lbn sector-offset reads so resized files load by filesystem path
    write_u32_va(buf, 0x004F68, 0x27BDFFE0, 0x080D4B70, "lbn Open -> j sceIoOpen")
    write_u32_va(buf, 0x004F6C, 0x3C020000, 0x00000000, "lbn Open delay nop")

    write_u32_va(buf, 0x004FD0, 0x27BDFFE0, 0x080D4B66, "lbn OpenAsync -> j sceIoOpenAsync")
    write_u32_va(buf, 0x004FD4, 0x3C020000, 0x00000000, "lbn OpenAsync delay nop")

    write_u32_va(buf, 0x005064, 0x8E040000, 0x10000005, "lbn Stat -> b 0x00507c")
    write_u32_va(buf, 0x005068, 0x0C0CB79A, 0x02202021, "lbn Stat -> addu a0, s1, zr")

    write_u32_va(buf, 0x005A18, 0x3C030000, 0x10000007, "func_5a lbn -> b 0x005a38")
    write_u32_va(buf, 0x005A1C, 0x247001F4, 0x03A02021, "func_5a lbn delay -> addu a0, sp, zr")

    write_u32_va(buf, 0x005FC0, 0x3C030000, 0x10000007, "func_5f lbn -> b 0x005fe0")
    write_u32_va(buf, 0x005FC4, 0x247001F4, 0x03A02021, "func_5f lbn delay -> addu a0, sp, zr")

    rel_patches = [
        (1352, 0x004F6C, 0x00010005, 0x00000000, 0x00000000, "clear lbn1 HI16"),
        (1353, 0x004F8C, 0x00010006, 0x00000000, 0x00000000, "clear lbn1 LO16"),
        (1354, 0x004F98, 0x00000004, 0x00000000, 0x00000000, "clear lbn1 helper jal"),
        (1355, 0x004FC8, 0x00000004, 0x004F68, 0x00000004, "move j sceIoOpen -> 0x4f68"),
        (1356, 0x004FD4, 0x00010005, 0x00000000, 0x00000000, "clear lbn2 HI16"),
        (1357, 0x004FF4, 0x00010006, 0x00000000, 0x00000000, "clear lbn2 LO16"),
        (1358, 0x005000, 0x00000004, 0x004FE0, 0x00000004, "move j sceMpegAvcQueryYCbCrSize -> 0x4fe0"),
        (1359, 0x005030, 0x00000004, 0x004FD0, 0x00000004, "move j sceIoOpenAsync -> 0x4fd0"),
        (1362, 0x005068, 0x00000004, 0x00000000, 0x00000000, "clear lbn3 helper jal"),
        (1633, 0x005A18, 0x00010005, 0x00000000, 0x00000000, "clear func_5a lbn HI16"),
        (1634, 0x005A1C, 0x00010006, 0x00000000, 0x00000000, "clear func_5a lbn LO16"),
        (1635, 0x005A24, 0x00000004, 0x00000000, 0x00000000, "clear func_5a lbn strcmp jal"),
        (1724, 0x005FC0, 0x00010005, 0x00000000, 0x00000000, "clear func_5f lbn HI16"),
        (1725, 0x005FC4, 0x00010006, 0x00000000, 0x00000000, "clear func_5f lbn LO16"),
        (1726, 0x005FCC, 0x00000004, 0x00000000, 0x00000000, "clear func_5f lbn strcmp jal"),
    ]
    for idx, exp_off, exp_info, new_off, new_info, desc in rel_patches:
        pos = REL_TEXT_OFF + idx * 8
        act_off, act_info = struct.unpack_from("<II", buf, pos)
        if (act_off, act_info) != (exp_off, exp_info):
            raise ValueError(
                f"Reloc [{idx}] mismatch ({desc}): expected ({exp_off:06x}, {exp_info:08x}), got ({act_off:06x}, {act_info:08x})"
            )
        struct.pack_into("<II", buf, pos, new_off, new_info)

    # 4. Codecave 1: Dynamic W_src (w16x9 if table w==480 else w4x3), H_src=height
    write_u32_va(buf, 0x010ACC, 0x27BDFFE0, 0x1000D128, "open_and_play_pmf -> b 0x004f70")
    write_u32_va(buf, 0x010AD0, 0xAFB3000C, 0x27BDFFE0, "open_and_play_pmf delay addiu sp,-32")
    write_u32_va(buf, 0x004F70, 0xAFB3000C, 0xAFB3000C, "cave1 sw s3, 0xc(sp)")
    write_u32_va(buf, 0x004F74, 0x00C09821, 0x240201E0, "cave1 addiu v0, zr, 480")
    write_u32_va(buf, 0x004F78, 0xAFB20008, 0x24030000 | w16x9, f"cave1 addiu v1, zr, {w16x9}")
    write_u32_va(buf, 0x004F7C, 0x00A09021, 0x240A0000 | height, f"cave1 addiu t2, zr, {height}")
    write_u32_va(buf, 0x004F80, 0xAFB10004, 0x11220002, "cave1 beq t1, v0, 0x004f8c")
    write_u32_va(buf, 0x004F84, 0x00808821, 0x00604821, "cave1 addu t1, v1, zr (delay)")
    write_u32_va(buf, 0x004F88, 0xAFB00000, 0x24090000 | w4x3, f"cave1 addiu t1, zr, {w4x3}")
    write_u32_va(buf, 0x004F8C, 0x245001F4, 0x10002ED2, "cave1 b 0x010ad8")
    write_u32_va(buf, 0x004F90, 0xAFBF0010, 0x00809821, "cave1 addu s3, a0, zr (delay)")

    # 5. Codecave 2: sceMpegAvcQueryYCbCrSize(mpeg, 1, 480, 272, &size)
    write_u32_va(buf, 0x00FEC8, 0x0C0D4C54, 0x0C0013F6, "jal sceMpegAvcQueryYCbCrSize -> cave2")
    write_u32_va(buf, 0x004FD8, 0xAFB3000C, 0x240601E0, "cave2 addiu a2, zr, 480")
    write_u32_va(buf, 0x004FDC, 0x00C09821, 0x24070110, "cave2 addiu a3, zr, 272")
    write_u32_va(buf, 0x004FE0, 0xAFB20008, 0x080D4C54, "cave2 j sceMpegAvcQueryYCbCrSize")
    write_u32_va(buf, 0x004FE4, 0x00A09021, 0x24050001, "cave2 addiu a1, zr, 1")

    # 6. Double-buffer stride 0x400000 (4 MB/frame), frameWidth, GU tex dimensions
    write_u32_va(buf, 0x0089C8, 0x3C080008, 0x3C080040, "gu_draw1 lui t0, 0x40")
    write_u32_va(buf, 0x0089D0, 0x35088000, 0x00000000, "gu_draw1 nop")
    write_u32_va(buf, 0x0089E0, 0x24050200, 0x24050000 | tbw, f"gu_draw1 tbw={tbw}")
    write_u32_va(buf, 0x0089E4, 0x24060200, 0x24060000 | tbh, f"gu_draw1 tbh={tbh}")
    write_u32_va(buf, 0x0089EC, 0x24070200, 0x24070000 | stride, f"gu_draw1 tbw_stride={stride}")

    write_u32_va(buf, 0x0100C4, 0x00081CC0, 0x00084580, "dec1 sll t0, t0, 22")
    write_u32_va(buf, 0x0100C8, 0x000843C0, 0x00000000, "dec1 nop")
    write_u32_va(buf, 0x0100CC, 0x01034021, 0x00000000, "dec1 nop")
    write_u32_va(buf, 0x0100E0, 0x24070200, 0x24070000 | stride, f"dec1 frameWidth={stride}")

    write_u32_va(buf, 0x011CEC, 0x24070200, 0x24070000 | stride, f"dec2 frameWidth={stride}")
    write_u32_va(buf, 0x011D2C, 0x00081CC0, 0x00084580, "dec2 sll t0, t0, 22")
    write_u32_va(buf, 0x011D30, 0x000843C0, 0x00000000, "dec2 nop")
    write_u32_va(buf, 0x011D3C, 0x01034021, 0x00000000, "dec2 nop")
    write_u32_va(buf, 0x011D58, 0x24070200, 0x24070000 | stride, f"dec2b frameWidth={stride}")

    write_u32_va(buf, 0x011DE0, 0x00081CC0, 0x00084580, "dec3 sll t0, t0, 22")
    write_u32_va(buf, 0x011DE4, 0x000843C0, 0x00000000, "dec3 nop")
    write_u32_va(buf, 0x011DF0, 0x01034021, 0x00000000, "dec3 nop")

    write_u32_va(buf, 0x013340, 0x24080200, 0x24080000 | stride, f"sceMpegCreate frameWidth={stride}")

    # 7. Scale 4:3 cutscene display rects (F0142..F0159) from (80,16,320,240) to full 272px height (59,0,362,272)
    for table_name, base_va in (("Table A", 0x3DEBDC), ("Table B", 0x49B310)):
        for i in range(18):
            entry_va = base_va + i * 0x1C
            write_u32_va(buf, entry_va + 12, 80, 59, f"{table_name}[{i}].x")
            write_u32_va(buf, entry_va + 16, 16, 0, f"{table_name}[{i}].y")
            write_u32_va(buf, entry_va + 20, 320, 362, f"{table_name}[{i}].w")
            write_u32_va(buf, entry_va + 24, 240, 272, f"{table_name}[{i}].h")

    validate_rel_text(buf)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(buf)
    sha = hashlib.sha256(buf).hexdigest()[:16]
    print(
        f"[EBOOT] Wrote {dst_path} ({len(buf)} bytes, sha256={sha}, "
        f"16:9={w16x9}x{height}, 4:3={w4x3}x{height}, stride={stride})"
    )


def resolve_psp_game_dir(path: Path) -> Path:
    if (path / "PSP_GAME").is_dir():
        return path / "PSP_GAME"
    return path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Patch Persona 2: Innocent Sin EBOOT.OLD and PARAM.SFO for custom/HD PMF cutscenes."
    )
    parser.add_argument(
        "--src-dir",
        type=Path,
        default=Path("./extracted_iso/PSP_GAME"),
        help="Path to clean PSP_GAME directory or extracted ISO root (default: ./extracted_iso/PSP_GAME)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./repack_work/PSP_GAME"),
        help="Output PSP_GAME directory for patched files (default: ./repack_work/PSP_GAME)",
    )
    parser.add_argument(
        "--w16x9",
        type=int,
        default=1280,
        help="Width for 16:9 movies (F0141), multiple of 16, max 1280 (default: 1280)",
    )
    parser.add_argument(
        "--w4x3",
        type=int,
        default=960,
        help="Width for 4:3 movies (F0142..F0159), multiple of 16, max 1280 (default: 960)",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=720,
        help="Movie height, multiple of 16, max 720 (default: 720)",
    )
    args = parser.parse_args()

    src_game = resolve_psp_game_dir(args.src_dir)
    src_sfo = src_game / "PARAM.SFO"
    src_eboot = src_game / "SYSDIR" / "EBOOT.OLD"
    if not src_eboot.exists():
        src_eboot = src_game / "SYSDIR" / "EBOOT.BIN"

    patch_sfo(src_sfo, args.out_dir / "PARAM.SFO")
    patch_eboot(
        src_eboot,
        args.out_dir / "SYSDIR" / "EBOOT.OLD",
        w16x9=args.w16x9,
        w4x3=args.w4x3,
        height=args.height,
    )


if __name__ == "__main__":
    main()
