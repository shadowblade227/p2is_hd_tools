#!/usr/bin/env python3
"""
PSP ISO 9660 Builder for Persona 2: Innocent Sin (ULUS10584).

Overlays modified files (patched EBOOT.OLD, PARAM.SFO, remastered PMF movies)
onto a clean extracted game tree and builds a sector-aligned bootable PSP ISO,
using the original ISO's System Area and Primary Volume Descriptor template.
"""

import argparse
import hashlib
import os
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SECTOR_SIZE = 2048
SUF_DIR = b"\x00\x00\x00\x00\x8dUXA\x00\x00\x00\x00\x00\x00"
SUF_FILE = b"\x00\x00\x00\x00\rUXA\x00\x00\x00\x00\x00\x00"


def build_dir_record(name_bytes: bytes, lba: int, size: int, is_dir: bool, date7: bytes) -> bytes:
    nlen = len(name_bytes)
    pad = b"\x00" if (nlen % 2 == 0) else b""
    suf = SUF_DIR if is_dir else SUF_FILE
    rec_len = 33 + nlen + len(pad) + len(suf)
    assert rec_len <= 255 and rec_len % 2 == 0
    hdr = bytearray(33)
    hdr[0] = rec_len
    hdr[1] = 0
    struct.pack_into("<I", hdr, 2, lba)
    struct.pack_into(">I", hdr, 6, lba)
    struct.pack_into("<I", hdr, 10, size)
    struct.pack_into(">I", hdr, 14, size)
    hdr[18:25] = date7
    hdr[25] = 0x02 if is_dir else 0x00
    hdr[26] = 0
    hdr[27] = 0
    struct.pack_into("<H", hdr, 28, 1)
    struct.pack_into(">H", hdr, 30, 1)
    hdr[32] = nlen
    return bytes(hdr) + name_bytes + pad + suf


def pack_dir_sectors(
    entries: List[Tuple[bytes, int, int, bool]],
    self_lba: int,
    parent_lba: int,
    parent_size: int,
    date7: bytes,
) -> bytes:
    def encode_all(self_sz: int) -> bytes:
        recs = [
            build_dir_record(b"\x00", self_lba, self_sz, True, date7),
            build_dir_record(b"\x01", parent_lba, parent_size, True, date7),
        ]
        for nb, lba, sz, is_dir in entries:
            recs.append(build_dir_record(nb, lba, sz, is_dir, date7))

        out = bytearray()
        cur = bytearray()
        for r in recs:
            if len(cur) + len(r) > SECTOR_SIZE:
                cur.extend(b"\x00" * (SECTOR_SIZE - len(cur)))
                out.extend(cur)
                cur = bytearray()
            cur.extend(r)
        if cur or not out:
            cur.extend(b"\x00" * (SECTOR_SIZE - len(cur)))
            out.extend(cur)
        return bytes(out)

    pass1 = encode_all(SECTOR_SIZE)
    sz = len(pass1)
    pass2 = encode_all(sz)
    assert len(pass2) == sz
    return pass2


def collect_tree(
    base_dir: Path,
    patch_dir: Path,
    only_movies: Optional[List[str]] = None,
) -> Dict[str, Path]:
    """Map relative POSIX paths to source files, overlaying patch_dir onto base_dir."""
    files: Dict[str, Path] = {}
    for root, _, fnames in os.walk(base_dir):
        for fn in fnames:
            full = Path(root) / fn
            rel = full.relative_to(base_dir).as_posix()
            files[rel] = full

    if patch_dir.exists():
        for root, _, fnames in os.walk(patch_dir):
            for fn in fnames:
                full = Path(root) / fn
                rel = full.relative_to(patch_dir).as_posix()
                if rel.startswith("tmp_enc/"):
                    continue
                if only_movies is not None and rel.startswith("PSP_GAME/USRDIR/pack/movie/"):
                    stem = Path(rel).stem.upper()
                    if stem not in only_movies:
                        continue
                if rel in files:
                    files[rel] = full
                else:
                    raise RuntimeError(f"Unexpected extra file in patch dir not present in base ISO: {rel}")

    return files


class DirNode:
    def __init__(self, rel_path: str, name: str, parent: Optional["DirNode"] = None):
        self.rel_path = rel_path
        self.name = name
        self.parent = parent
        self.pt_index = 0
        self.lba = 0
        self.size = SECTOR_SIZE
        self.subdirs: List["DirNode"] = []
        self.files: List[Tuple[str, Path, int]] = []


def verify_iso(iso_path: Path, expected_files: Dict[str, Path], verify_all_sha: bool = True) -> None:
    iso_size = iso_path.stat().st_size
    assert iso_size % SECTOR_SIZE == 0
    with open(iso_path, "rb") as f:
        f.seek(16 * SECTOR_SIZE)
        pvd = f.read(SECTOR_SIZE)
        assert pvd[0:7] == b"\x01CD001\x01"
        vol_sectors = struct.unpack_from("<I", pvd, 80)[0]
        assert vol_sectors * SECTOR_SIZE == iso_size, f"{vol_sectors * 2048} != {iso_size}"
        rlba = struct.unpack_from("<I", pvd, 158)[0]
        rsz = struct.unpack_from("<I", pvd, 166)[0]

        found: Dict[str, Tuple[int, int]] = {}

        def walk(lba: int, sz: int, prefix: str = "") -> None:
            f.seek(lba * SECTOR_SIZE)
            buf = f.read(sz)
            pos = 0
            while pos < sz:
                rlen = buf[pos]
                if rlen == 0:
                    pos = ((pos // SECTOR_SIZE) + 1) * SECTOR_SIZE
                    continue
                rec = buf[pos : pos + rlen]
                clba = struct.unpack_from("<I", rec, 2)[0]
                csz = struct.unpack_from("<I", rec, 10)[0]
                flags = rec[25]
                nlen = rec[32]
                name = rec[33 : 33 + nlen]
                pos += rlen
                if name in (b"\x00", b"\x01"):
                    continue
                sname = name.decode("ascii")
                rel = f"{prefix}/{sname}" if prefix else sname
                if flags & 2:
                    walk(clba, csz, rel)
                else:
                    found[rel] = (clba, csz)

        walk(rlba, rsz)
        assert set(found.keys()) == set(expected_files.keys()), (
            f"ISO file set mismatch: diff={set(found.keys()) ^ set(expected_files.keys())}"
        )

        for rel, (clba, csz) in found.items():
            sp = expected_files[rel]
            assert csz == sp.stat().st_size, f"Size mismatch for {rel}: {csz} vs {sp.stat().st_size}"
            assert (clba * SECTOR_SIZE + csz) <= iso_size
            if verify_all_sha or rel in ("PSP_GAME/PARAM.SFO", "PSP_GAME/SYSDIR/EBOOT.OLD") or rel.endswith(".pmf"):
                f.seek(clba * SECTOR_SIZE)
                h_iso = hashlib.sha256()
                rem = csz
                while rem > 0:
                    chunk = f.read(min(1024 * 1024, rem))
                    h_iso.update(chunk)
                    rem -= len(chunk)
                h_disk = hashlib.sha256(sp.read_bytes()).hexdigest()
                assert h_iso.hexdigest() == h_disk, f"SHA mismatch in ISO for {rel}"

    print(f"[VERIFY] {iso_path.name}: all {len(found)} files & SHA-256 checksums verified!")


def build_iso(
    out_iso: Path,
    base_dir: Path,
    patch_dir: Path,
    orig_iso: Path,
    only_movies: Optional[List[str]] = None,
    verify_all_sha: bool = True,
) -> None:
    if not base_dir.is_dir():
        raise FileNotFoundError(f"Base extracted ISO directory not found: {base_dir}")
    if not orig_iso.is_file():
        raise FileNotFoundError(f"Original ISO file not found: {orig_iso}")

    files_map = collect_tree(base_dir, patch_dir, only_movies)
    print(f"[ISO] Collected {len(files_map)} files (overlay from {patch_dir})")

    root = DirNode("", "")
    dir_lookup: Dict[str, DirNode] = {"": root}

    def get_or_create_dir(drel: str) -> DirNode:
        if drel in dir_lookup:
            return dir_lookup[drel]
        parent_rel, _, name = drel.rpartition("/")
        parent = get_or_create_dir(parent_rel)
        node = DirNode(drel, name, parent)
        parent.subdirs.append(node)
        dir_lookup[drel] = node
        return node

    for rel, src_path in sorted(files_map.items()):
        drel, _, fname = rel.rpartition("/")
        dnode = get_or_create_dir(drel)
        dnode.files.append((fname, src_path, src_path.stat().st_size))

    # BFS order for ISO 9660 Path Table
    bfs_dirs: List[DirNode] = []
    queue: List[DirNode] = [root]
    while queue:
        nxt_q: List[DirNode] = []
        for d in queue:
            d.pt_index = len(bfs_dirs) + 1
            bfs_dirs.append(d)
            d.subdirs.sort(key=lambda c: c.name.upper())
            nxt_q.extend(d.subdirs)
        queue = nxt_q

    def build_path_tables() -> Tuple[bytes, bytes]:
        pt_l = bytearray()
        pt_m = bytearray()
        for d in bfs_dirs:
            par_idx = 1 if d.parent is None else d.parent.pt_index
            name_b = b"\x00" if d.parent is None else d.name.encode("ascii")
            nlen = len(name_b)
            pad = b"\x00" if (nlen % 2 == 1) else b""
            pt_l.extend(struct.pack("<BBIH", nlen, 0, d.lba, par_idx) + name_b + pad)
            pt_m.extend(struct.pack(">BBIH", nlen, 0, d.lba, par_idx) + name_b + pad)
        return bytes(pt_l), bytes(pt_m)

    # Read template System Area + PVD + VDST from original ISO
    with open(orig_iso, "rb") as f:
        sys_area = f.read(16 * SECTOR_SIZE)
        orig_pvd = bytearray(f.read(SECTOR_SIZE))
        orig_vdst = f.read(SECTOR_SIZE)

    assert len(sys_area) == 16 * SECTOR_SIZE
    assert orig_pvd[0:7] == b"\x01CD001\x01"
    assert orig_vdst[0:7] == b"\xffCD001\x01"
    root_date7 = bytes(orig_pvd[156 + 18 : 156 + 25])

    pt_l_dummy, _ = build_path_tables()
    pt_size = len(pt_l_dummy)
    pt_sectors = (pt_size + SECTOR_SIZE - 1) // SECTOR_SIZE
    pt_l_lba = 18
    pt_m_lba = pt_l_lba + pt_sectors
    dir_start_lba = pt_m_lba + pt_sectors

    cur_lba = dir_start_lba
    for d in bfs_dirs:
        dummy_entries = (
            [(sd.name.encode("ascii"), 0, SECTOR_SIZE, True) for sd in d.subdirs]
            + [(fn.encode("ascii"), 0, sz, False) for fn, _, sz in d.files]
        )
        dummy_entries.sort(key=lambda e: e[0].upper())
        dir_blob = pack_dir_sectors(dummy_entries, cur_lba, cur_lba, SECTOR_SIZE, root_date7)
        d.lba = cur_lba
        d.size = len(dir_blob)
        cur_lba += d.size // SECTOR_SIZE

    file_records: List[Tuple[DirNode, str, Path, int]] = []
    for d in bfs_dirs:
        for fn, sp, sz in d.files:
            file_records.append((d, fn, sp, sz))

    def file_sort_key(item: Tuple[DirNode, str, Path, int]):
        d, fn, _, _ = item
        rel = f"{d.rel_path}/{fn}" if d.rel_path else fn
        is_pmf = 1 if rel.startswith("PSP_GAME/USRDIR/pack/movie/") else 0
        return (is_pmf, rel)

    file_records.sort(key=file_sort_key)

    file_lba_map: Dict[Tuple[str, str], int] = {}
    file_extents: List[Tuple[str, Path, int, int]] = []
    for d, fn, sp, sz in file_records:
        file_lba_map[(d.rel_path, fn)] = cur_lba
        file_extents.append((f"{d.rel_path}/{fn}" if d.rel_path else fn, sp, cur_lba, sz))
        cur_lba += max(1, (sz + SECTOR_SIZE - 1) // SECTOR_SIZE)

    total_sectors = cur_lba
    total_mb = total_sectors * SECTOR_SIZE / (1024 * 1024)
    print(f"[ISO] Planned {total_sectors} sectors ({total_mb:.2f} MB), files={len(file_extents)}")

    pt_l_raw, pt_m_raw = build_path_tables()
    assert len(pt_l_raw) == pt_size
    pt_l_padded = pt_l_raw.ljust(pt_sectors * SECTOR_SIZE, b"\x00")
    pt_m_padded = pt_m_raw.ljust(pt_sectors * SECTOR_SIZE, b"\x00")

    dir_blobs: List[bytes] = []
    for d in bfs_dirs:
        entries = (
            [(sd.name.encode("ascii"), sd.lba, sd.size, True) for sd in d.subdirs]
            + [(fn.encode("ascii"), file_lba_map[(d.rel_path, fn)], sz, False) for fn, _, sz in d.files]
        )
        entries.sort(key=lambda e: e[0].upper())
        par_lba = d.parent.lba if d.parent else d.lba
        par_sz = d.parent.size if d.parent else d.size
        blob = pack_dir_sectors(entries, d.lba, par_lba, par_sz, root_date7)
        assert len(blob) == d.size
        dir_blobs.append(blob)

    pvd = bytearray(orig_pvd)
    struct.pack_into("<I", pvd, 80, total_sectors)
    struct.pack_into(">I", pvd, 84, total_sectors)
    struct.pack_into("<I", pvd, 132, pt_size)
    struct.pack_into(">I", pvd, 136, pt_size)
    struct.pack_into("<I", pvd, 140, pt_l_lba)
    struct.pack_into("<I", pvd, 144, 0)
    struct.pack_into(">I", pvd, 148, pt_m_lba)
    struct.pack_into(">I", pvd, 152, 0)
    root_rec = build_dir_record(b"\x00", root.lba, root.size, True, root_date7)[:34]
    pvd[156:190] = root_rec

    out_iso.parent.mkdir(parents=True, exist_ok=True)
    tmp_iso = out_iso.with_suffix(".iso.tmp")
    with open(tmp_iso, "wb") as out:
        out.write(sys_area)
        out.write(pvd)
        out.write(orig_vdst)
        out.write(pt_l_padded)
        out.write(pt_m_padded)
        for db in dir_blobs:
            out.write(db)

        buf_sz = 4 * 1024 * 1024
        for rel, sp, lba, sz in file_extents:
            assert out.tell() == lba * SECTOR_SIZE, f"Offset mismatch at {rel}"
            with open(sp, "rb") as inf:
                copied = 0
                while True:
                    chunk = inf.read(buf_sz)
                    if not chunk:
                        break
                    out.write(chunk)
                    copied += len(chunk)
                assert copied == sz
            rem = (SECTOR_SIZE - (sz % SECTOR_SIZE)) % SECTOR_SIZE
            if sz == 0:
                rem = SECTOR_SIZE
            if rem:
                out.write(b"\x00" * rem)
        assert out.tell() == total_sectors * SECTOR_SIZE

    tmp_iso.replace(out_iso)
    print(f"[ISO] Wrote {out_iso} ({out_iso.stat().st_size:,} bytes)")
    verify_iso(out_iso, files_map, verify_all_sha=verify_all_sha)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Build a bootable PSP ISO from extracted game files and patched/remastered overrides."
    )
    ap.add_argument(
        "--base-dir",
        type=Path,
        default=Path("./extracted_iso"),
        help="Path to clean extracted game files (default: ./extracted_iso)",
    )
    ap.add_argument(
        "--patch-dir",
        type=Path,
        default=Path("./repack_work"),
        help="Path to directory containing patched EBOOT/SFO/PMFs to overlay (default: ./repack_work)",
    )
    ap.add_argument(
        "--orig-iso",
        type=Path,
        default=Path("./Persona 2 - Innocent Sin USA.ISO"),
        help="Path to original clean ISO for boot sector/PVD template",
    )
    ap.add_argument(
        "--out-iso",
        type=Path,
        default=Path("./Persona 2 - Innocent Sin USA [Remastered].iso"),
        help="Output path for the rebuilt ISO",
    )
    ap.add_argument(
        "--only-movies",
        nargs="*",
        default=None,
        help="Optional list of movie IDs to include from --patch-dir (e.g. F0141 F0142)",
    )
    ap.add_argument(
        "--fast-verify",
        action="store_true",
        help="Only SHA-256 check patched/PMF files instead of every file in the ISO",
    )
    args = ap.parse_args()

    only_movies = [x.upper().removesuffix(".PMF") for x in args.only_movies] if args.only_movies else None
    build_iso(
        out_iso=args.out_iso,
        base_dir=args.base_dir,
        patch_dir=args.patch_dir,
        orig_iso=args.orig_iso,
        only_movies=only_movies,
        verify_all_sha=not args.fast_verify,
    )


if __name__ == "__main__":
    main()
