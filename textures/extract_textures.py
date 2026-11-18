#!/usr/bin/env python3
"""
Extracts all clean 1x GIM textures directly from the game's archives:
  - PSP_GAME/USRDIR/pack/P2PT_ALL.cpk  (CRILAYLA, gzip, Atlus 0x0101 RLE)
  - PSP_GAME/USRDIR/pack/datapack.bin
  - PSP_GAME/SYSDIR/EBOOT.OLD (or EBOOT.BIN)

Computes exact PPSSPP XXH64 texture replacement hashes (reduceHash = True),
deduplicates identical textures across containers, filters out ambiguous hash
collisions, and exports categorized 1x PNGs alongside manifest.json for
repacking with repack_textures.py.
"""

import argparse
import hashlib
import json
import re
import shutil
import struct
import zlib
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np


# Pure-Python XXH64 & XXH32 (exact PPSSPP seeds: 0xBACD7814 / 0xC0108888)
P64_1 = 0x9E3779B185EBCA87
P64_2 = 0xC2B2AE3D27D4EB4F
P64_3 = 0x165667B19E3779F9
P64_4 = 0x85EBCA77C2B2AE63
P64_5 = 0x27D4EB2F165667C5
M64 = 0xFFFFFFFFFFFFFFFF

P32_1 = 0x9E3779B1
P32_2 = 0x85EBCA77
P32_3 = 0xC2B2AE3D
P32_4 = 0x27D4EB2F
P32_5 = 0x165667B1
M32 = 0xFFFFFFFF


def _rotl64(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & M64


def xxh64(data: bytes, seed: int = 0) -> int:
    n = len(data)
    p = 0
    if n >= 32:
        v1 = (seed + P64_1 + P64_2) & M64
        v2 = (seed + P64_2) & M64
        v3 = seed & M64
        v4 = (seed - P64_1) & M64
        limit = n - 32
        while p <= limit:
            k1, k2, k3, k4 = struct.unpack_from("<4Q", data, p)
            v1 = (_rotl64((v1 + k1 * P64_2) & M64, 31) * P64_1) & M64
            v2 = (_rotl64((v2 + k2 * P64_2) & M64, 31) * P64_1) & M64
            v3 = (_rotl64((v3 + k3 * P64_2) & M64, 31) * P64_1) & M64
            v4 = (_rotl64((v4 + k4 * P64_2) & M64, 31) * P64_1) & M64
            p += 32
        h = (_rotl64(v1, 1) + _rotl64(v2, 7) + _rotl64(v3, 12) + _rotl64(v4, 18)) & M64
        for v in (v1, v2, v3, v4):
            v = (_rotl64((v * P64_2) & M64, 31) * P64_1) & M64
            h = ((h ^ v) * P64_1 + P64_4) & M64
    else:
        h = (seed + P64_5) & M64
    h = (h + n) & M64
    while p + 8 <= n:
        k1 = (_rotl64((struct.unpack_from("<Q", data, p)[0] * P64_2) & M64, 31) * P64_1) & M64
        h = (_rotl64(h ^ k1, 27) * P64_1 + P64_4) & M64
        p += 8
    while p + 4 <= n:
        k1 = (struct.unpack_from("<I", data, p)[0] * P64_1) & M64
        h = (_rotl64(h ^ k1, 23) * P64_2 + P64_3) & M64
        p += 4
    while p < n:
        h = (_rotl64(h ^ ((data[p] * P64_5) & M64), 11) * P64_1) & M64
        p += 1
    h ^= h >> 33
    h = (h * P64_2) & M64
    h ^= h >> 29
    h = (h * P64_3) & M64
    h ^= h >> 32
    return h


def _rotl32(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & M32


def xxh32(data: bytes, seed: int = 0) -> int:
    n = len(data)
    p = 0
    if n >= 16:
        v1 = (seed + P32_1 + P32_2) & M32
        v2 = (seed + P32_2) & M32
        v3 = seed & M32
        v4 = (seed - P32_1) & M32
        limit = n - 16
        while p <= limit:
            k1, k2, k3, k4 = struct.unpack_from("<4I", data, p)
            v1 = (_rotl32((v1 + k1 * P32_2) & M32, 13) * P32_1) & M32
            v2 = (_rotl32((v2 + k2 * P32_2) & M32, 13) * P32_1) & M32
            v3 = (_rotl32((v3 + k3 * P32_2) & M32, 13) * P32_1) & M32
            v4 = (_rotl32((v4 + k4 * P32_2) & M32, 13) * P32_1) & M32
            p += 16
        h = (_rotl32(v1, 1) + _rotl32(v2, 7) + _rotl32(v3, 12) + _rotl32(v4, 18)) & M32
    else:
        h = (seed + P32_5) & M32
    h = (h + n) & M32
    while p + 4 <= n:
        h = (_rotl32((h + struct.unpack_from("<I", data, p)[0] * P32_3) & M32, 17) * P32_4) & M32
        p += 4
    while p < n:
        h = (_rotl32((h + data[p] * P32_5) & M32, 11) * P32_1) & M32
        p += 1
    h ^= h >> 15
    h = (h * P32_2) & M32
    h ^= h >> 13
    h = (h * P32_3) & M32
    h ^= h >> 16
    return h


# CRI CPK (@UTF + CRILAYLA) Parser
def _read_utf_val(buf: bytes, p: int, ftype: int, get_str, dbase: int):
    if ftype in (0, 1):
        return buf[p], p + 1
    if ftype in (2, 3):
        return struct.unpack_from(">H", buf, p)[0], p + 2
    if ftype in (4, 5):
        return struct.unpack_from(">I", buf, p)[0], p + 4
    if ftype in (6, 7):
        return struct.unpack_from(">Q", buf, p)[0], p + 8
    if ftype == 8:
        return struct.unpack_from(">f", buf, p)[0], p + 4
    if ftype == 9:
        return struct.unpack_from(">d", buf, p)[0], p + 8
    if ftype == 0xA:
        return get_str(struct.unpack_from(">I", buf, p)[0]), p + 4
    if ftype == 0xB:
        off, sz = struct.unpack_from(">II", buf, p)
        return buf[dbase + off : dbase + off + sz], p + 8
    raise ValueError(f"Unsupported @UTF field type {ftype:#x}")


def parse_utf(buf: bytes, base: int):
    assert buf[base : base + 4] == b"@UTF"
    t = base + 8
    rows_off, str_off, data_off, _, num_fields, row_size, num_rows = struct.unpack_from(">IIIIHHI", buf, t)
    sbase = t + str_off
    dbase = t + data_off
    rbase = t + rows_off
    get_str = lambda off: buf[sbase + off : buf.index(b"\x00", sbase + off)].decode("utf-8", errors="replace")

    p = t + 24
    fields = []
    for _ in range(num_fields):
        fl = buf[p]
        p += 1
        name = ""
        if fl & 0x10:
            name = get_str(struct.unpack_from(">I", buf, p)[0])
            p += 4
        storage = fl & 0xE0
        ftype = fl & 0x0F
        const_val = None
        if storage == 0x20:
            const_val, p = _read_utf_val(buf, p, ftype, get_str, dbase)
        fields.append((name, storage, ftype, const_val))

    rows = []
    for r in range(num_rows):
        rp = rbase + r * row_size
        row = {}
        for name, storage, ftype, const_val in fields:
            if storage == 0x40:
                row[name], rp = _read_utf_val(buf, rp, ftype, get_str, dbase)
            elif storage == 0x20:
                row[name] = const_val
            else:
                row[name] = 0
        rows.append(row)
    return rows


def decompress_crilayla(buf: bytes) -> bytes:
    if buf[:8] != b"CRILAYLA":
        return buf
    uncomp_size, header_offset = struct.unpack_from("<II", buf, 8)
    raw_tail = buf[16 + header_offset : 16 + header_offset + 0x100]
    src = buf[16 : 16 + header_offset]
    out = bytearray(uncomp_size + 0x100)
    out[:0x100] = raw_tail

    bit_pos = len(src) * 8
    cur_byte = 0
    bits_left = 0

    def get_bits(n: int) -> int:
        nonlocal bit_pos, cur_byte, bits_left
        res = 0
        while n > 0:
            if bits_left == 0:
                bit_pos -= 8
                cur_byte = src[bit_pos // 8]
                bits_left = 8
            take = n if n < bits_left else bits_left
            res <<= take
            bits_left -= take
            res |= (cur_byte >> bits_left) & ((1 << take) - 1)
            n -= take
        return res

    out_end = len(out)
    out_cursor = 0
    vle_lens = (2, 3, 5, 8)
    while out_cursor < uncomp_size:
        if get_bits(1) == 0:
            out[out_end - 1 - out_cursor] = get_bits(8)
            out_cursor += 1
        else:
            back_off = get_bits(13) + 3
            match_len = 3
            for l in vle_lens:
                v = get_bits(l)
                match_len += v
                if v != (1 << l) - 1:
                    break
            else:
                while True:
                    v = get_bits(8)
                    match_len += v
                    if v != 0xFF:
                        break
            for _ in range(match_len):
                out[out_end - 1 - out_cursor] = out[out_end - 1 - out_cursor + back_off]
                out_cursor += 1
    return bytes(out)


# GIM Parser, Decoder & PPSSPP Hash Computation
def next_pow2_and_log(n: int):
    p = 1
    k = 0
    while p < n:
        p <<= 1
        k += 1
    return p, k


def parse_gim(buf: bytes, start: int):
    if buf[start : start + 12] != b"MIG.00.1PSP\x00":
        return None
    if start + 32 > len(buf):
        return None
    tag, _, rcs, _, rdo = struct.unpack_from("<HHIII", buf, start + 16)
    if tag != 2 or rcs < 16 or start + 16 + rcs > len(buf):
        return None
    tend = start + 16 + rcs
    imgs = []
    pals = []

    def walk(s: int, e: int):
        while s + 16 <= e:
            t, _, csz, noff, doff = struct.unpack_from("<HHIII", buf, s)
            if csz < 16 or noff < 16 or s + csz > e:
                break
            if t == 3:
                walk(s + doff, s + csz)
            elif t == 4:
                imgs.append((s + doff, s + csz))
            elif t == 5:
                pals.append((s + doff, s + csz))
            s += noff

    walk(start + 16 + rdo, tend)
    if not imgs:
        return None

    ih, ie = imgs[0]
    if ih + 0x20 > ie:
        return None
    _, _, fmt, ord_, w, h, bpp, pa, ha = struct.unpack_from("<HHHHHHHHH", buf, ih)
    loff = struct.unpack_from("<I", buf, ih + 0x1C)[0]
    if ih + loff > ie or w == 0 or h == 0:
        return None

    pal = None
    if pals:
        ph, pe = pals[0]
        _, _, pf, po, pw_p, ph_p, pbpp = struct.unpack_from("<HHHHHHH", buf, ph)
        ploff = struct.unpack_from("<I", buf, ph + 0x1C)[0]
        pal = {
            "fmt": pf,
            "order": po,
            "colors": pw_p,
            "bpp": pbpp,
            "raw": buf[ph + ploff : pe],
        }

    return {
        "end": tend,
        "w": w,
        "h": h,
        "img_fmt": fmt,
        "img_order": ord_,
        "bpp": bpp,
        "pix_raw": buf[ih + loff : ie],
        "pal": pal,
    }


def decode_gim(g: dict) -> np.ndarray:
    """Decode a parsed GIM dict to an (H, W, 4) uint8 RGBA image."""
    w, h, fmt, order = g["w"], g["h"], g["img_fmt"], g["img_order"]
    raw = g["pix_raw"]
    if fmt == 4:
        row_bytes = (w + 1) // 2
    elif fmt == 5:
        row_bytes = w
    elif fmt == 3:
        row_bytes = w * 4
    elif fmt in (0, 1, 2):
        row_bytes = w * 2
    else:
        raise ValueError(f"Unsupported GIM img_fmt {fmt}")

    pad_rb = ((row_bytes + 15) // 16) * 16
    if order == 1:
        pad_h = ((h + 7) // 8) * 8
        need = pad_rb * pad_h
        b = raw[:need] + (b"\x00" * max(0, need - len(raw)))
        unswz = (
            np.frombuffer(b, np.uint8)
            .reshape((pad_h // 8, pad_rb // 16, 8, 16))
            .transpose(0, 2, 1, 3)
            .reshape((pad_h, pad_rb))[:h, :row_bytes]
        )
    else:
        need = row_bytes * h
        b = raw[:need] + (b"\x00" * max(0, need - len(raw)))
        unswz = np.frombuffer(b, np.uint8).reshape((h, row_bytes))

    if fmt in (4, 5):
        if g["pal"] is None:
            pal_rgba = np.zeros((256, 4), np.uint8)
            pal_rgba[:, :3] = np.arange(256, dtype=np.uint8)[:, None]
            pal_rgba[:, 3] = 255
        else:
            pf = g["pal"]["fmt"]
            pw_p = g["pal"]["colors"]
            praw = g["pal"]["raw"]
            if pf == 3:
                pal_rgba = np.frombuffer(praw[: pw_p * 4], np.uint8).reshape((-1, 4)).copy()
            elif pf == 2:
                u16 = np.frombuffer(praw[: pw_p * 2], np.uint16)
                pal_rgba = np.stack(
                    [
                        (u16 & 0xF) * 17,
                        ((u16 >> 4) & 0xF) * 17,
                        ((u16 >> 8) & 0xF) * 17,
                        ((u16 >> 12) & 0xF) * 17,
                    ],
                    axis=-1,
                ).astype(np.uint8)
            elif pf == 1:
                u16 = np.frombuffer(praw[: pw_p * 2], np.uint16)
                r = (u16 & 0x1F) * 255 // 31
                gr = ((u16 >> 5) & 0x1F) * 255 // 31
                b_ = ((u16 >> 10) & 0x1F) * 255 // 31
                a = ((u16 >> 15) & 1) * 255
                pal_rgba = np.stack([r, gr, b_, a], axis=-1).astype(np.uint8)
            else:
                raise ValueError(f"Unsupported GIM pal_fmt {pf}")

        if fmt == 4:
            idx = np.empty((h, row_bytes * 2), np.uint8)
            idx[:, 0::2] = unswz & 0x0F
            idx[:, 1::2] = (unswz >> 4) & 0x0F
            idx = idx[:, :w].copy()
        else:
            idx = unswz[:, :w].copy()
        if len(pal_rgba) <= int(idx.max()):
            pad = np.zeros((int(idx.max()) + 1, 4), np.uint8)
            pad[: len(pal_rgba)] = pal_rgba
            pal_rgba = pad
        return pal_rgba[idx]

    if fmt == 3:
        return unswz.reshape((h, w, 4)).copy()
    if fmt == 2:
        u16 = unswz.view(np.uint16).reshape((h, w))
        return np.stack(
            [
                (u16 & 0xF) * 17,
                ((u16 >> 4) & 0xF) * 17,
                ((u16 >> 8) & 0xF) * 17,
                ((u16 >> 12) & 0xF) * 17,
            ],
            axis=-1,
        ).astype(np.uint8)
    raise ValueError(f"Unhandled fmt {fmt}")


def _strided_xxh64(pix: bytes, pw: int, hh: int, bpp: int, bw: int):
    bpl = int((bpp * pw) // 8 * 0.5)
    stride = (bpp * bw) // 8
    if bpl <= 0 or hh <= 0 or (hh - 1) * stride + bpl > len(pix):
        return None
    res = 0
    for y in range(hh):
        rh = xxh64(pix[y * stride : y * stride + bpl], 0xBACD7814) & 0xFFFFFFFF
        res = ((res * 11) ^ rh) & 0xFFFFFFFF
    return res


def compute_ppsspp_hashes(g: dict, include_inverted_clut: bool = False) -> dict:
    """Compute exact PPSSPP XXH64 (reduceHash=True) keys."""
    w, h, bpp, pix = g["w"], g["h"], g["bpp"], g["pix_raw"]
    pw, logw = next_pow2_and_log(w)
    ph, logh = next_pow2_and_log(h)
    dim = (logh << 8) | logw

    rb = (w * bpp + 7) // 8
    prb = ((rb + 15) // 16) * 16
    bw_gim = (prb * 8) // bpp

    h_cands = [ph]
    if ph == 512:
        for ch in (h, 272):
            if ch not in h_cands:
                h_cands.append(ch)

    data_hashes = []
    seen_dh = set()
    for hh in h_cands:
        for bw in (bw_gim, pw):
            if bw <= pw:
                tp = bw * hh + (pw - bw)
                L = int((bpp * tp) // 8 * 0.5)
                if 0 < L <= len(pix):
                    dh = f"{xxh64(pix[:L], 0xBACD7814) & 0xFFFFFFFF:08x}"
                    if dh not in seen_dh:
                        seen_dh.add(dh)
                        data_hashes.append(dh)
            else:
                sh = _strided_xxh64(pix, pw, hh, bpp, bw)
                if sh is not None:
                    dh = f"{sh:08x}"
                    if dh not in seen_dh:
                        seen_dh.add(dh)
                        data_hashes.append(dh)

    clut_hashes = []
    inverted_clut_hashes = []
    if g["img_fmt"] in (4, 5) and g["pal"] is not None:
        pf = g["pal"]["fmt"]
        xm = (0xC500FF00 | pf) ^ dim
        c32 = xxh32(g["pal"]["raw"], 0xC0108888)
        clut_hashes.append(f"{(c32 ^ xm) & 0xFFFFFFFF:08x}")

        if include_inverted_clut and pf == 3:
            a = np.frombuffer(g["pal"]["raw"], np.uint8).reshape(-1, 4)
            # In-game flashback/negative portrait CLUT inversion modes
            c1 = a.copy()
            if len(c1) > 1:
                c1[1:, :3] = 255 - c1[1:, :3]
            c2 = np.where(
                a[:, 3:4] > 0,
                np.stack([255 - a[:, 0], 255 - a[:, 1], 255 - a[:, 2], a[:, 3]], axis=1),
                a,
            ).astype(np.uint8)
            for cand in (c1, c2):
                ih = f"{(xxh32(cand.tobytes(), 0xC0108888) ^ xm) & 0xFFFFFFFF:08x}"
                if ih not in clut_hashes and ih not in inverted_clut_hashes:
                    inverted_clut_hashes.append(ih)
    else:
        clut_hashes.append(f"{dim:08x}")

    full_keys = [f"00000000{c}{d}" for c in clut_hashes for d in data_hashes]
    inv_keys = [f"00000000{c}{d}" for c in inverted_clut_hashes for d in data_hashes]
    return {
        "pow2_w": pw,
        "pow2_h": ph,
        "full_keys": full_keys,
        "inverted_keys": inv_keys,
    }


# Container Scanners (raw + gzip + Atlus 0x0101 PackBits RLE)
def extract_0101_blocks(buf: bytes):
    blocks = []
    n = len(buf)
    i = 0
    while i + 12 <= n:
        tag = struct.unpack_from("<I", buf, i)[0]
        if (tag & 0xFFFF) == 0x0101 and (tag >> 16) < 0x100:
            csz, usz = struct.unpack_from("<II", buf, i + 4)
            if 12 < csz <= n - i and 0 < usz <= 16 * 1024 * 1024:
                src = buf[i + 12 : i + csz]
                out = bytearray()
                p = 0
                slen = len(src)
                ok = True
                while len(out) < usz and p < slen:
                    c = src[p]
                    p += 1
                    if c < 0x80:
                        cnt = c + 1
                        if p + cnt > slen or len(out) + cnt > usz:
                            ok = False
                            break
                        out.extend(src[p : p + cnt])
                        p += cnt
                    else:
                        cnt = (c & 0x7F) + 3
                        if p >= slen or len(out) + cnt > usz:
                            ok = False
                            break
                        out.extend(bytes([src[p]]) * cnt)
                        p += 1
                if ok and len(out) == usz and slen - p <= 16:
                    blocks.append((i, bytes(out)))
                    i = (i + csz + 3) & ~3
                    continue
        i += 4
    return blocks


def scan_blob_for_gims(data: bytes, enable_0101: bool = True):
    found = []

    # Raw GIM
    pos = 0
    while True:
        idx = data.find(b"MIG.00.1PSP\x00", pos)
        if idx < 0:
            break
        g = parse_gim(data, idx)
        if g:
            found.append(g)
            pos = g["end"]
        else:
            pos = idx + 12

    # Embedded gzip streams
    pos = 0
    while True:
        idx = data.find(b"\x1f\x8b\x08", pos)
        if idx < 0:
            break
        if idx + 10 <= len(data) and data[idx + 3] in (0x00, 0x08):
            try:
                dec = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data[idx:])
                if b"MIG.00.1PSP\x00" in dec:
                    mp = 0
                    while True:
                        mi = dec.find(b"MIG.00.1PSP\x00", mp)
                        if mi < 0:
                            break
                        g = parse_gim(dec, mi)
                        if g:
                            found.append(g)
                            mp = g["end"]
                        else:
                            mp = mi + 12
            except Exception:
                pass
        pos = idx + 3

    #Atlus 0x0101 RLE blocks
    if enable_0101:
        for _, blk in extract_0101_blocks(data):
            mp = 0
            while True:
                mi = blk.find(b"MIG.00.1PSP\x00", mp)
                if mi < 0:
                    break
                g = parse_gim(blk, mi)
                if g:
                    found.append(g)
                    mp = g["end"]
                else:
                    mp = mi + 12

    return found


# Categorization & Image Helpers
CONTAINER_PRIORITY = {
    "syscg.bin": 0,
    "titlecg.bin": 1,
    "EBOOT.OLD": 2,
    "datapack.bin": 3,
    "gallerycg.bin": 4,
    "img_bunner.bin": 5,
    "img_shop.bin": 6,
    "img_velvet.bin": 7,
    "bstup.bin": 8,
}


def category_for_container(container: str) -> str:
    if container in ("bstup.bin", "cut_in.bin"):
        return "portraits"
    if container.startswith("card.bin"):
        return "cards"
    if container in ("EVTBG.BNP", "gallerycg.bin"):
        return "events"
    if container.startswith("MMAP") or container in ("map_w.bin", "LMAP.BNP"):
        return "maps"
    if container in ("EVTUNIT.BNP", "EVTUNIT2.BNP", "MCHAR.BNP"):
        return "sprites_overworld"
    if container in (
        "P2P_DPK.BNP",
        "P2P_DPF.BNP",
        "P2P_BPLY.BNP",
        "SMN_EFFE.BNP",
        "MUT_EFFE.BNP",
        "m_ef00.gim",
        "m_ef00a.gim",
    ):
        return "sprites_battle"
    if container.startswith("DUN") or container in ("DNGCOM.BNP", "dunchar.bin"):
        return "dungeons"
    if container in (
        "bj.bin",
        "bingo.bin",
        "poker.bin",
        "slot.bin",
        "ca_face.bin",
        "ca_cmn.bin",
        "ca_hlp.bin",
        "edit_floor.bin",
        "edit.bin",
    ):
        return "minigames"
    return "ui"


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


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.replace("/", "_").replace("#", "_"))


def resolve_psp_game_dir(game_dir: Path) -> Path:
    if (game_dir / "PSP_GAME").is_dir():
        return game_dir / "PSP_GAME"
    if (game_dir / "USRDIR").is_dir():
        return game_dir
    raise FileNotFoundError(
        f"Could not find PSP_GAME/USRDIR inside '{game_dir}'. "
        "Pass the extracted ISO root or the PSP_GAME folder."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract clean 1x textures and PPSSPP hashes from Persona 2: Innocent Sin (ULUS10584)."
    )
    parser.add_argument(
        "--game-dir",
        type=Path,
        default=Path("./extracted_iso"),
        help="Path to extracted ISO root (containing PSP_GAME) or PSP_GAME folder (default: ./extracted_iso)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./textures_work"),
        help="Output workspace directory for extracted PNGs and manifest.json (default: ./textures_work)",
    )
    parser.add_argument(
        "--padded",
        action="store_true",
        help="Save textures padded to full PSP power-of-two canvases instead of clean cropped native sizes",
    )
    args = parser.parse_args()

    psp_game = resolve_psp_game_dir(args.game_dir)
    cpk_path = psp_game / "USRDIR" / "pack" / "P2PT_ALL.cpk"
    datapack_path = psp_game / "USRDIR" / "pack" / "datapack.bin"
    eboot_path = psp_game / "SYSDIR" / "EBOOT.OLD"
    if not eboot_path.exists():
        eboot_path = psp_game / "SYSDIR" / "EBOOT.BIN"

    if not cpk_path.exists():
        raise FileNotFoundError(f"Missing P2PT_ALL.cpk at {cpk_path}")

    tex_root = args.out_dir / ("padded" if args.padded else "unpadded")
    manifest_path = args.out_dir / "manifest.json"
    if tex_root.exists():
        shutil.rmtree(tex_root)
    tex_root.mkdir(parents=True, exist_ok=True)

    print("Reading game archives...")
    cpk = cpk_path.read_bytes()
    hdr = parse_utf(cpk, 16)[0]
    add_off = min(hdr["TocOffset"], hdr["ContentOffset"])
    rows = parse_utf(cpk, hdr["TocOffset"] + 16)

    name_counts = Counter(
        f"{r.get('DirName', '')}/{r.get('FileName', '')}".strip("/") for r in rows
    )

    containers = []
    for r in rows:
        base_name = f"{r.get('DirName', '')}/{r.get('FileName', '')}".strip("/")
        cname = f"{base_name}#id{r['ID']}" if name_counts[base_name] > 1 else base_name
        raw = cpk[add_off + r["FileOffset"] : add_off + r["FileOffset"] + r["FileSize"]]
        containers.append((cname, decompress_crilayla(raw), True))

    if datapack_path.exists():
        containers.append(("datapack.bin", datapack_path.read_bytes(), False))
    if eboot_path.exists():
        containers.append((eboot_path.name, eboot_path.read_bytes(), False))

    print(f"Scanning {len(containers)} containers and decoding textures...")
    decoded_entries = []
    for cname, blob, use_rle in containers:
        gims = scan_blob_for_gims(blob, enable_0101=use_rle)
        if not gims:
            continue
        is_portrait_bin = cname == "bstup.bin"
        for idx, g in enumerate(gims):
            rgba = decode_gim(g)
            hashes = compute_ppsspp_hashes(g, include_inverted_clut=is_portrait_bin)
            rgba_sha = hashlib.sha256(
                rgba.tobytes() + struct.pack("<II", g["w"], g["h"])
            ).hexdigest()
            decoded_entries.append({
                "container": cname,
                "idx": idx,
                "w": g["w"],
                "h": g["h"],
                "pow2_w": hashes["pow2_w"],
                "pow2_h": hashes["pow2_h"],
                "full_keys": hashes["full_keys"],
                "inverted_keys": hashes["inverted_keys"],
                "rgba_sha256": rgba_sha,
                "rgba": rgba,
            })

    print(f"   Decoded {len(decoded_entries)} raw texture instances. Resolving duplicates and hashes...")

    # Check every full_key across all decoded entries to exclude ambiguous collisions
    fk_entries = defaultdict(list)
    for e in decoded_entries:
        for fk in e["full_keys"]:
            fk_entries[fk].append(e)

    def pick_canonical(items: list[dict]) -> dict:
        return min(
            items,
            key=lambda e: (
                CONTAINER_PRIORITY.get(e["container"], 100),
                -e["h"],
                e["container"],
                e["idx"],
            ),
        )

    ambiguous_keys = set()
    for fk, items in fk_entries.items():
        uniq_shas = {it["rgba_sha256"] for it in items}
        if len(uniq_shas) == 1:
            continue
        # Allow height-padding differences (e.g. 250px vs 256px) if overlapping pixels match
        by_sha_map = {it["rgba_sha256"]: it for it in items}
        vals = list(by_sha_map.values())
        w0 = vals[0]["w"]
        if all(v["w"] == w0 for v in vals):
            mh = min(v["h"] for v in vals)
            base_im = vals[0]["rgba"][:mh].astype(np.float32)
            if all(float(np.abs(v["rgba"][:mh].astype(np.float32) - base_im).mean()) < 0.01 for v in vals[1:]):
                continue
        ambiguous_keys.add(fk)

    # Group by unique RGBA content and write one clean PNG per unique texture
    by_sha = defaultdict(list)
    for e in decoded_entries:
        by_sha[e["rgba_sha256"]].append(e)

    manifest_items = []
    used_keys = set()
    cat_counts = Counter()
    total_keys = 0
    total_inv_keys = 0

    for _, group in sorted(
        by_sha.items(),
        key=lambda kv: (pick_canonical(kv[1])["container"], pick_canonical(kv[1])["idx"]),
    ):
        rep = pick_canonical(group)
        valid_keys = sorted(
            {
                fk
                for it in group
                for fk in it["full_keys"]
                if fk not in ambiguous_keys and fk not in used_keys
            }
        )
        if not valid_keys:
            continue

        valid_inv_keys = sorted(
            {
                ik
                for it in group
                for ik in it["inverted_keys"]
                if ik not in ambiguous_keys and ik not in used_keys and ik not in valid_keys
            }
        )

        cat = category_for_container(rep["container"])
        stem = safe_name(Path(rep["container"]).stem)
        fname = f"{stem}_{rep['idx']:04d}_{rep['w']}x{rep['h']}.png"
        rel_path = f"{cat}/{fname}"

        out_img = (
            place_on_pow2_canvas(rep["rgba"], rep["pow2_w"], rep["pow2_h"])
            if args.padded
            else rep["rgba"]
        )
        write_rgba_png(tex_root / rel_path, out_img)

        used_keys.update(valid_keys)
        used_keys.update(valid_inv_keys)
        total_keys += len(valid_keys)
        total_inv_keys += len(valid_inv_keys)
        cat_counts[cat] += 1

        entry = {
            "rel_path": rel_path,
            "category": cat,
            "w": rep["w"],
            "h": rep["h"],
            "pow2_w": rep["pow2_w"],
            "pow2_h": rep["pow2_h"],
            "keys": valid_keys,
        }
        if valid_inv_keys:
            entry["inverted_keys"] = valid_inv_keys
        manifest_items.append(entry)

    manifest = {
        "game_id": "ULUS10584",
        "padded": bool(args.padded),
        "textures_dir": tex_root.name,
        "total_textures": len(manifest_items),
        "total_keys": total_keys,
        "total_inverted_keys": total_inv_keys,
        "categories": dict(sorted(cat_counts.items())),
        "items": manifest_items,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"\nDone! Extracted {len(manifest_items)} unique textures ({total_keys} PPSSPP hashes).")
    if total_inv_keys:
        print(f"   (+ {total_inv_keys} inverted-palette portrait hashes tracked for automatic flashback support)")
    print(f"   Images:   {tex_root}")
    print(f"   Manifest: {manifest_path}")
    print("\nTextures by category:")
    for cat, cnt in sorted(cat_counts.items()):
        print(f"   {cat:20s}: {cnt}")


if __name__ == "__main__":
    main()
