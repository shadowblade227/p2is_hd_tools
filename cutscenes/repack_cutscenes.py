#!/usr/bin/env python3
"""
Encodes edited or upscaled cutscene videos (MP4/MKV files or PNG frame
sequences) at any resolution into Sony PSP-compliant H.264/AVC streams and
multiplexes them with the original bit-exact ATRAC3+ audio tracks into valid
PSMF0015 (.pmf) containers.

Requires: ffmpeg and ffprobe installed on system PATH.
"""

import argparse
import bisect
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

PACK_SIZE = 2048
MUX_RATE = 25_000
CLOCK_27M = 27_000_000
PACK_DUR_DEN = MUX_RATE * 50
PACK_DUR_NUM = PACK_SIZE * CLOCK_27M
PACK_DUR_BASE = PACK_DUR_NUM // PACK_DUR_DEN
PACK_DUR_REM = PACK_DUR_NUM % PACK_DUR_DEN
PACK_DUR = (PACK_DUR_NUM + PACK_DUR_DEN - 1) // PACK_DUR_DEN
PACK_DUR_SEED_REM = 1_000_000
PACK_DUR_STARTUP_SEED_REM = 250_000

PTS_VIDEO_START = 90_000
PTS_AUDIO_START = 85_069
PTS_STAMP_INTERVAL = 16
PTS_STAMP_INTERVAL_FINAL = 18
FRAME_DUR_90K = 3003
DEEP_CONT_START_FRAME = 3_770
LATE_TS_GATE_START_FRAME = 5_000
TAIL_TS_GATE_MAX_FRAMES_REM = 100
DEEP_CONT_MIN_BYTES_TO_TARGET = 256

SCR_LEAD_VIDEO_SEG = 89_850 * 300
SCR_LEAD_VIDEO_MID = 90_000 * 300
SCR_LEAD_VIDEO_CROSS = 86_958 * 300
SCR_LEAD_VIDEO_STARTUP = 10_782_564
SCR_LEAD_VIDEO_INITIAL_STARTUP_TAIL = 22_470_000
SCR_LEAD_VIDEO_STARTUP_TAIL = 77_920 * 300
SCR_LEAD_AUDIO_BOOT0 = 74_775 * 300
SCR_LEAD_AUDIO_BOOT1 = 24_468_692
SCR_LEAD_AUDIO_NORM = 16_719 * 300
SCR_LEAD_AUDIO_SHORT = 12_537 * 300
AUDIO_PLAIN_RACE_TOL_27M = 8_192
VIDEO_BF_WAIT_TOL_27M = AUDIO_PLAIN_RACE_TOL_27M
DEEP_CONT_TOL_27M = AUDIO_PLAIN_RACE_TOL_27M * 3 + 2_048

SCR_INITIAL_27M = 3_044_236
SCR_INITIAL_STARTUP_27M = 15_342_067

VIDEO_PAYLOAD = PACK_SIZE - 14 - 9
AUDIO_PAYLOAD_FIRST = PACK_SIZE - 14 - 6 - 3 - 8 - 4
AUDIO_PAYLOAD_NORM = PACK_SIZE - 14 - 6 - 3 - 5 - 4

SID_VIDEO = 0xE0
SID_AUDIO = 0xBD
SID_SYSTEM = 0xBB
SID_PRIV2 = 0xBF
SID_PADDING = 0xBE

NO_DEEP_CONT_GATE = -1
DEEP_CONT_MAX_BYTES_BY_OFFSET = [
    NO_DEEP_CONT_GATE,
    1776, 4027, 13420, 20594, 30036, 36910, 41698, 48372,
    53035, 57432, 62351, 67627, 69754, 71660, 76598, 76547,
    76773, 75166, 69190, 63319, 56996, 50960, 44752, 34977,
    28118, 23452, 16838, 8010, 1799, 1107, 1338, NO_DEEP_CONT_GATE, 1331,
    NO_DEEP_CONT_GATE, 878, 61, 1375, 1657, 1706, 612, NO_DEEP_CONT_GATE, 322, 1400,
    1832, 1718, 385, 1626, NO_DEEP_CONT_GATE, 1862, 1889, 1191, 702,
    796, 1057, 1470, 940, 65, 1147,
]

ATX_PACKET_SIZE = 752
ATX_BLOCK_ALIGN = 744
ATX_SAMPLE_RATE = 44_100


def extract_atrac_and_header(orig_pmf: Path) -> Tuple[bytes, bytes]:
    data = orig_pmf.read_bytes()
    if data[:8] != b"PSMF0015":
        raise ValueError(f"{orig_pmf} invalid magic: {data[:8]!r}")
    strm_off = struct.unpack_from(">I", data, 8)[0]
    if strm_off != 2048:
        raise ValueError(f"{orig_pmf} unexpected stream offset {strm_off}")

    header = data[:2048]
    audio = bytearray()
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
                sub_id = pk[payload_start]
                if sub_id != 0x00:
                    raise ValueError(f"{orig_pmf} unexpected audio track sub_id={sub_id}")
                audio.extend(pk[payload_start + 4 : p + 6 + ln])
            if sid == SID_PADDING:
                break
            p += 6 + ln
        pos += PACK_SIZE

    if len(audio) % ATX_PACKET_SIZE != 0:
        raise ValueError(f"{orig_pmf} ATRAC size {len(audio)} not multiple of {ATX_PACKET_SIZE}")
    frame_cnt = len(audio) // ATX_PACKET_SIZE
    for i in range(frame_cnt):
        if audio[i * ATX_PACKET_SIZE : i * ATX_PACKET_SIZE + 2] != b"\x0f\xd0":
            raise ValueError(f"{orig_pmf} bad ATRAC sync at frame {i}")
    return header, bytes(audio)


def split_frames_annexb(data: bytes) -> List[bytes]:
    sc: List[Tuple[int, int]] = []
    n = len(data)
    search_from = 0
    prev_end = -1
    while search_from < n:
        idx = data.find(b"\x00\x00\x01", search_from)
        if idx < 0:
            break
        if idx > prev_end and idx > 0 and data[idx - 1] == 0:
            sc.append((idx - 1, 4))
            prev_end = idx + 3
        else:
            sc.append((idx, 3))
            prev_end = idx + 3
        search_from = idx + 3

    aus: List[List[bytes]] = []
    pend: List[bytes] = []
    mv = memoryview(data)
    for i, (s, pfx) in enumerate(sc):
        e = sc[i + 1][0] if i + 1 < len(sc) else n
        if e <= s:
            continue
        nal = mv[s:e]
        t = (nal[pfx] & 0x1F) if len(nal) > pfx else 0
        if t in (1, 5):
            pend.append(bytes(nal))
            aus.append(pend)
            pend = []
        else:
            pend.append(bytes(nal))

    if pend and aus:
        aus[-1].extend(pend)

    return [b"".join(au) for au in aus]


def is_sync_access_unit(au: bytes) -> bool:
    has_idr = False
    has_sps = False
    has_pps = False
    n = len(au)
    pos = 0
    while pos < n:
        idx = au.find(b"\x00\x00\x01", pos)
        if idx < 0:
            break
        nal_off = idx + 3
        if nal_off >= n:
            break
        nal_type = au[nal_off] & 0x1F
        if nal_type == 5:
            has_idr = True
        elif nal_type == 7:
            has_sps = True
        elif nal_type == 8:
            has_pps = True
        if has_idr and has_sps and has_pps:
            return True
        pos = nal_off
    return False


def build_audio_end_ticks(au_count: int, sample_rate: int = ATX_SAMPLE_RATE) -> List[int]:
    ticks: List[int] = []
    if sample_rate == 44_100:
        tick_base = 11_145
        tick_frac = 25_500
        carry = 21_300
    else:
        tick_num = 240_000 * 2_048
        tick_base = tick_num // sample_rate
        tick_frac = tick_num % sample_rate
        carry = sample_rate // 2

    tick = 0
    for i in range(au_count):
        if i > 0:
            carry += tick_frac
            tick += tick_base + (carry // sample_rate)
            carry %= sample_rate
        ticks.append(tick)
    return ticks


def count_audio_unit_starts(cursor: int, payload_size: int, packet_size: int) -> int:
    if payload_size <= 0 or packet_size <= 0:
        return 0
    range_end = cursor + payload_size
    first = ((cursor + packet_size - 1) // packet_size) * packet_size
    if first >= range_end:
        return 0
    return ((range_end - 1 - first) // packet_size) + 1


def atx_ext16(cursor: int, block_align: int) -> int:
    rem = cursor % block_align
    first_au_bytes = (block_align - rem) % block_align
    global_frame = (cursor + first_au_bytes) // block_align
    period = block_align + 8
    return ((first_au_bytes + global_frame * 8) % period) & 0xFFFF


def video_pts_dts(frame_idx: int) -> Tuple[int, int]:
    pts = PTS_VIDEO_START + frame_idx * FRAME_DUR_90K
    return pts, pts - FRAME_DUR_90K


def encode_ts(ts: int, marker: int) -> bytes:
    ts &= (1 << 33) - 1
    return bytes([
        marker | (((ts >> 30) & 0x07) << 1) | 0x01,
        (ts >> 22) & 0xFF,
        (((ts >> 15) & 0x7F) << 1) | 0x01,
        (ts >> 7) & 0xFF,
        ((ts & 0x7F) << 1) | 0x01,
    ])


def encode_pts_dts(pts: int, dts: int) -> bytes:
    return encode_ts(pts, 0x31) + encode_ts(dts, 0x11)


def build_pack_header(scr27: int) -> bytes:
    scr_base = scr27 // 300
    scr_ext = scr27 % 300
    return bytes([
        0x00, 0x00, 0x01, 0xBA,
        0x44 | (((scr_base >> 30) & 0x07) << 3) | ((scr_base >> 28) & 0x03),
        (scr_base >> 20) & 0xFF,
        (((scr_base >> 15) & 0x1F) << 3) | 0x04 | ((scr_base >> 13) & 0x03),
        (scr_base >> 5) & 0xFF,
        ((scr_base & 0x1F) << 3) | 0x04 | ((scr_ext >> 7) & 0x03),
        ((scr_ext & 0x7F) << 1) | 0x01,
        (MUX_RATE >> 14) & 0xFF,
        (MUX_RATE >> 6) & 0xFF,
        ((MUX_RATE & 0x3F) << 2) | 0x03,
        0xF8,
    ])


SYS_HDR_AUDIO = bytes([
    0x00, 0x00, 0x01, SID_SYSTEM, 0x00, 0x0C,
    0x80, 0xC3, 0x51, 0x80, 0xF0, 0x7F, 0xB9, 0xE0, 0xFB, 0xBD, 0xE0, 0x08,
])


def build_bf_pes(seg: List[int]) -> bytes:
    table_size = 2 + len(seg) * 4
    buf = bytearray(24 + len(seg) * 4)
    pay_len = len(buf) - 6
    buf[0:6] = bytes([0x00, 0x00, 0x01, SID_PRIV2, (pay_len >> 8) & 0xFF, pay_len & 0xFF])
    buf[6] = 0x01
    buf[7] = SID_VIDEO
    # buf[8:20] zero (12 bytes preTable, includes 4x2B RPI fields at buf[8:16])
    buf[20] = (table_size >> 8) & 0xFF
    buf[21] = table_size & 0xFF
    buf[22] = (len(seg) >> 8) & 0xFF
    buf[23] = len(seg) & 0xFF
    off = 24
    last_i = len(seg) - 1
    for i, s in enumerate(seg):
        s_clamped = s if s <= 0xFFFF else 0xFFFF
        buf[off] = 0x04
        buf[off + 1] = 0x00 if i == last_i else 0x80
        buf[off + 2] = (s_clamped >> 8) & 0xFF
        buf[off + 3] = s_clamped & 0xFF
        off += 4
    return bytes(buf)


def build_padding(size: int) -> bytes:
    if size < 6:
        return b"\x00" * size
    pay = size - 6
    return bytes([0x00, 0x00, 0x01, SID_PADDING, (pay >> 8) & 0xFF, pay & 0xFF]) + (b"\xFF" * pay)


def build_video_nots_pes(payload: bytes) -> bytes:
    pes_len = 3 + len(payload)
    return bytes([0x00, 0x00, 0x01, SID_VIDEO, (pes_len >> 8) & 0xFF, pes_len & 0xFF, 0x81, 0x00, 0x00]) + payload


def build_video_ts_pes(payload: bytes, pts: int, dts: int) -> bytes:
    pes_len = 3 + 10 + len(payload)
    return (
        bytes([0x00, 0x00, 0x01, SID_VIDEO, (pes_len >> 8) & 0xFF, pes_len & 0xFF, 0x81, 0xC0, 0x0A])
        + encode_pts_dts(pts, dts)
        + payload
    )


def build_video_seg_pes(payload: bytes, pts: int, dts: int) -> bytes:
    pes_len = 3 + 10 + 3 + len(payload)
    return (
        bytes([0x00, 0x00, 0x01, SID_VIDEO, (pes_len >> 8) & 0xFF, pes_len & 0xFF, 0x81, 0xC1, 0x0D])
        + encode_pts_dts(pts, dts)
        + bytes([0x1E, 0x60, 0xFB])
        + payload
    )


def build_audio_pes_first(payload: bytes, pts: int, ext16: int, sub_id: int = 0) -> bytes:
    pes_len = 3 + 8 + 4 + len(payload)
    return (
        bytes([0x00, 0x00, 0x01, SID_AUDIO, (pes_len >> 8) & 0xFF, pes_len & 0xFF, 0x81, 0x81, 0x08])
        + encode_ts(pts, 0x21)
        + bytes([0x1E, 0x60, 0x04, sub_id, 0x00, (ext16 >> 8) & 0xFF, ext16 & 0xFF])
        + payload
    )


def build_audio_pes_norm(payload: bytes, pts: int, ext16: int, sub_id: int = 0) -> bytes:
    pes_len = 3 + 5 + 4 + len(payload)
    return (
        bytes([0x00, 0x00, 0x01, SID_AUDIO, (pes_len >> 8) & 0xFF, pes_len & 0xFF, 0x81, 0x80, 0x05])
        + encode_ts(pts, 0x21)
        + bytes([sub_id, 0x00, (ext16 >> 8) & 0xFF, ext16 & 0xFF])
        + payload
    )


def mux_mps_single_track(video_units: List[bytes], audio_payload: bytes) -> bytes:
    num_frames = len(video_units)
    frame_sizes = [len(u) for u in video_units]
    is_sync = [is_sync_access_unit(u) for u in video_units]
    frame_byte_start: List[int] = []
    off = 0
    for sz in frame_sizes:
        frame_byte_start.append(off)
        off += sz
    total_video_bytes = off
    video_payload = b"".join(video_units)

    audio_len = len(audio_payload)
    atx_count = audio_len // ATX_PACKET_SIZE
    audio_end_ticks = build_audio_end_ticks(atx_count, ATX_SAMPLE_RATE)

    # Build BF segments starting at sync frames (plus frame 0)
    sync_frames = [0] + [i for i in range(1, num_frames) if is_sync[i]]
    bf_frame_starts: List[int] = []
    bf_segments: List[List[int]] = []
    for i, s in enumerate(sync_frames):
        e = sync_frames[i + 1] if i + 1 < len(sync_frames) else num_frames
        if e <= s:
            continue
        bf_frame_starts.append(s)
        bf_segments.append(frame_sizes[s:e])

    bf_byte_bounds: List[int] = []
    bf_end = 0
    for seg in bf_segments:
        bf_end += sum(seg)
        bf_byte_bounds.append(bf_end)

    initial_startup_mode = bool(bf_segments and sum(bf_segments[0]) <= VIDEO_PAYLOAD * 2)
    scr27 = SCR_INITIAL_STARTUP_27M if initial_startup_mode else SCR_INITIAL_27M
    pack_dur_carry = PACK_DUR_STARTUP_SEED_REM if initial_startup_mode else PACK_DUR_SEED_REM

    audio_cursor = 0
    audio_pack_idx = 0
    audio_units_consumed = 0
    audio_first_pack = True

    packs: List[bytearray] = []

    def advance_scr() -> None:
        nonlocal scr27, pack_dur_carry
        pack_dur_carry += PACK_DUR_REM
        scr27 += PACK_DUR_BASE + (pack_dur_carry // PACK_DUR_DEN)
        pack_dur_carry %= PACK_DUR_DEN

    def wait_scr(target: int) -> None:
        nonlocal scr27, pack_dur_carry
        if target <= scr27:
            return
        while True:
            nc = pack_dur_carry + PACK_DUR_REM
            ns = scr27 + PACK_DUR_BASE + (nc // PACK_DUR_DEN)
            ncn = nc % PACK_DUR_DEN
            scr27 = ns
            pack_dur_carry = ncn
            if target <= ns:
                return

    def snap_wait_target(target: int) -> int:
        if target <= scr27:
            return scr27
        ps, pc = scr27, pack_dur_carry
        while True:
            nc = pc + PACK_DUR_REM
            ns = ps + PACK_DUR_BASE + (nc // PACK_DUR_DEN)
            ncn = nc % PACK_DUR_DEN
            if target <= ns:
                return ns
            ps, pc = ns, ncn

    def emit_pack(packets: List[bytes]) -> bytearray:
        hdr = build_pack_header(scr27)
        pack = bytearray(PACK_SIZE)
        wo = 0
        pack[wo : wo + 14] = hdr
        wo += 14
        for pkt in packets:
            pack[wo : wo + len(pkt)] = pkt
            wo += len(pkt)
        pad_sz = PACK_SIZE - wo
        if pad_sz >= 6:
            pack[wo : wo + pad_sz] = build_padding(pad_sz)
        advance_scr()
        return pack

    def frame_at(pos: int) -> int:
        idx = bisect.bisect_right(frame_byte_start, pos) - 1
        return max(0, idx)

    def audio_lead_for_pack(pidx: int, au_starts: int, ext16: int) -> int:
        if pidx == 0:
            return SCR_LEAD_AUDIO_BOOT0
        if pidx == 1:
            return SCR_LEAD_AUDIO_BOOT1
        return SCR_LEAD_AUDIO_SHORT if (au_starts >= 3 and ext16 <= 175) else SCR_LEAD_AUDIO_NORM

    def audio_scr_target() -> int:
        if audio_cursor >= audio_len:
            return scr27 + PACK_DUR * 1_000_000
        lim = AUDIO_PAYLOAD_FIRST if audio_first_pack else AUDIO_PAYLOAD_NORM
        psz = min(lim, audio_len - audio_cursor)
        au_ptr = atx_ext16(audio_cursor, ATX_BLOCK_ALIGN)
        u_starts = count_audio_unit_starts(audio_cursor, psz, ATX_PACKET_SIZE)
        lead = audio_lead_for_pack(audio_pack_idx, u_starts, au_ptr)
        if audio_pack_idx > 1 and psz < lim and u_starts < 2:
            lead += SCR_LEAD_AUDIO_NORM - SCR_LEAD_AUDIO_SHORT
        aidx = min(audio_units_consumed, len(audio_end_ticks) - 1) if audio_end_ticks else 0
        tick240k = audio_end_ticks[aidx] if audio_end_ticks else 0
        pts27m = (PTS_AUDIO_START * 600 + tick240k * 225 + 1) // 2
        target = max(0, pts27m - lead)
        if audio_pack_idx > 1 and u_starts >= 3 and au_ptr <= 175 and target >= 42:
            target -= 42
        return target

    def audio_has_three_unit_starts() -> bool:
        if audio_cursor >= audio_len:
            return False
        lim = AUDIO_PAYLOAD_FIRST if audio_first_pack else AUDIO_PAYLOAD_NORM
        psz = min(lim, audio_len - audio_cursor)
        return count_audio_unit_starts(audio_cursor, psz, ATX_PACKET_SIZE) >= 3

    def emit_audio_pack() -> Optional[bytearray]:
        nonlocal audio_first_pack, audio_cursor, audio_pack_idx, audio_units_consumed
        lim = AUDIO_PAYLOAD_FIRST if audio_first_pack else AUDIO_PAYLOAD_NORM
        psz = min(lim, audio_len - audio_cursor)
        if psz <= 0:
            return None
        payload = audio_payload[audio_cursor : audio_cursor + psz]
        aidx = 0 if audio_units_consumed <= 0 else min(audio_units_consumed, len(audio_end_ticks) - 1)
        pts = PTS_AUDIO_START if audio_units_consumed <= 0 else PTS_AUDIO_START + ((audio_end_ticks[aidx] * 3 + 4) // 8)
        au_ptr = atx_ext16(audio_cursor, ATX_BLOCK_ALIGN)
        u_starts = count_audio_unit_starts(audio_cursor, psz, ATX_PACKET_SIZE)
        pes = build_audio_pes_first(payload, pts, au_ptr, 0) if audio_first_pack else build_audio_pes_norm(payload, pts, au_ptr, 0)
        audio_first_pack = False
        audio_cursor += psz
        audio_pack_idx += 1
        audio_units_consumed += u_starts

        hdr = build_pack_header(scr27)
        pack = bytearray(PACK_SIZE)
        pack[0:14] = hdr
        pack[14 : 14 + len(pes)] = pes
        wo = 14 + len(pes)
        pad_sz = PACK_SIZE - wo
        if pad_sz >= 6:
            pack[wo : wo + pad_sz] = build_padding(pad_sz)
        advance_scr()
        return pack

    def seg_start_frame(seg_idx: int) -> int:
        if not bf_frame_starts or seg_idx <= 0:
            return 0
        if seg_idx >= len(bf_frame_starts):
            return bf_frame_starts[-1]
        return bf_frame_starts[seg_idx]

    def seg_end_frame(seg_idx: int) -> int:
        return bf_frame_starts[seg_idx + 1] if seg_idx + 1 < len(bf_frame_starts) else num_frames

    def ts_step_for_segment(seg_idx: int) -> int:
        cnt = len(bf_frame_starts)
        if initial_startup_mode and seg_idx < 2:
            return PTS_STAMP_INTERVAL_FINAL
        if seg_idx == cnt - 2 and seg_idx >= 0:
            return PTS_STAMP_INTERVAL
        return PTS_STAMP_INTERVAL_FINAL if seg_idx == cnt - 1 else PTS_STAMP_INTERVAL

    def video_seg_lead(seg_idx: int) -> int:
        cnt = len(bf_frame_starts)
        if initial_startup_mode and seg_idx < 2:
            return SCR_LEAD_VIDEO_STARTUP
        return SCR_LEAD_VIDEO_STARTUP if seg_idx == cnt - 1 else SCR_LEAD_VIDEO_SEG

    def video_ts_lead(seg_idx: int, sframe: int, stamped_frame: int, is_tail_ts: bool) -> int:
        cnt = len(bf_frame_starts)
        if initial_startup_mode and seg_idx == 0:
            return SCR_LEAD_VIDEO_INITIAL_STARTUP_TAIL if is_tail_ts else SCR_LEAD_VIDEO_STARTUP
        if initial_startup_mode and seg_idx == 1 and stamped_frame <= sframe + PTS_STAMP_INTERVAL_FINAL:
            return SCR_LEAD_VIDEO_STARTUP
        if seg_idx == cnt - 1:
            return SCR_LEAD_VIDEO_STARTUP_TAIL if is_tail_ts else SCR_LEAD_VIDEO_STARTUP
        if is_tail_ts and seg_idx == cnt - 2:
            return SCR_LEAD_VIDEO_CROSS
        return SCR_LEAD_VIDEO_MID

    def bf_target_for_seg(bf_idx: int) -> int:
        _, dts = video_pts_dts(bf_frame_starts[bf_idx])
        lead = video_seg_lead(bf_idx)
        return max(0, dts * 300 - lead)

    def try_next_ts_target(next_pts_frame: int, cur_seg_idx: int, cur_ts_step: int) -> Optional[int]:
        if next_pts_frame < 0 or next_pts_frame >= num_frames:
            return None
        send = seg_end_frame(cur_seg_idx)
        if next_pts_frame >= send:
            return None
        sstart = seg_start_frame(cur_seg_idx)
        is_tail = next_pts_frame + cur_ts_step >= send
        lead = video_ts_lead(cur_seg_idx, sstart, next_pts_frame, is_tail)
        _, dts = video_pts_dts(next_pts_frame)
        return max(0, dts * 300 - lead)

    def effective_ts_wait_target(cur_seg_idx: int, cur_ts_step: int, stamped_frame: int, following_pts_frame: int, ts_target: int) -> int:
        if cur_ts_step != PTS_STAMP_INTERVAL_FINAL or cur_seg_idx < 0 or cur_seg_idx >= len(bf_frame_starts):
            return ts_target
        if seg_start_frame(cur_seg_idx) < LATE_TS_GATE_START_FRAME:
            return ts_target
        send = seg_end_frame(cur_seg_idx)
        if following_pts_frame < send and ts_target > scr27:
            return ts_target
        wframe = following_pts_frame if following_pts_frame < send else send
        if wframe <= stamped_frame or wframe >= num_frames:
            return ts_target
        _, dts = video_pts_dts(wframe)
        return max(0, dts * 300 - SCR_LEAD_VIDEO_MID)

    def get_plain_len(v_cursor: int, bf_idx: int, next_bf_thresh: int) -> int:
        if v_cursor >= total_video_bytes:
            return 0
        cap = VIDEO_PAYLOAD
        if bf_idx < len(bf_frame_starts):
            cap = min(cap, next_bf_thresh - v_cursor)
        return min(cap, total_video_bytes - v_cursor)

    def is_mid_frame_cont(v_cursor: int, pay_len: int) -> bool:
        if pay_len <= 0 or v_cursor >= total_video_bytes:
            return False
        sf = frame_at(v_cursor)
        if frame_byte_start[sf] == v_cursor:
            return False
        ef = frame_at(min(total_video_bytes - 1, v_cursor + pay_len - 1))
        return sf == ef

    def compute_plain_target(v_cursor: int, pay_len: int, bf_idx: int, next_bf_thresh: int) -> int:
        if pay_len <= 0 or v_cursor >= total_video_bytes:
            return 0
        if bf_idx < len(bf_frame_starts) and v_cursor + pay_len >= next_bf_thresh:
            _, dts = video_pts_dts(bf_frame_starts[bf_idx])
            return max(0, dts * 300 - SCR_LEAD_VIDEO_MID)
        end_pos = min(total_video_bytes - 1, v_cursor + pay_len - 1)
        ef = frame_at(end_pos)
        _, dts = video_pts_dts(ef)
        return max(0, dts * 300 - SCR_LEAD_VIDEO_MID)

    def try_deep_cont_target(v_cursor: int, pay_len: int, sstart: int, priority_mode: bool) -> Tuple[Optional[int], int, int]:
        if pay_len <= 0 or v_cursor >= total_video_bytes:
            return None, NO_DEEP_CONT_GATE, (1 << 60)
        fr = frame_at(v_cursor)
        if frame_byte_start[fr] == v_cursor or fr <= sstart:
            return None, NO_DEEP_CONT_GATE, (1 << 60)
        foff = v_cursor - frame_byte_start[fr]
        offset_ready = True
        if fr <= sstart + 1:
            if foff < VIDEO_PAYLOAD * 4:
                offset_ready = False
        elif fr <= sstart + 3 and VIDEO_PAYLOAD <= foff < VIDEO_PAYLOAD * 3:
            offset_ready = False
        elif not priority_mode and 1024 < foff < VIDEO_PAYLOAD:
            offset_ready = False

        first_target_frame = fr + 1
        if sstart > DEEP_CONT_START_FRAME:
            pef = frame_at(min(total_video_bytes - 1, v_cursor + pay_len - 1))
            if pef > fr:
                first_target_frame = pef

        for tframe in range(first_target_frame, num_frames):
            _, dts = video_pts_dts(tframe)
            tgt = max(0, dts * 300 - SCR_LEAD_VIDEO_MID)
            if tgt > scr27 + DEEP_CONT_TOL_27M:
                tfrom_seg = tframe - sstart
                btt = frame_byte_start[tframe] - v_cursor
                if sstart > DEEP_CONT_START_FRAME:
                    if btt < DEEP_CONT_MIN_BYTES_TO_TARGET:
                        continue
                    if btt >= VIDEO_PAYLOAD:
                        return None, NO_DEEP_CONT_GATE, (1 << 60)
                if 0 < tfrom_seg < len(DEEP_CONT_MAX_BYTES_BY_OFFSET):
                    max_b = DEEP_CONT_MAX_BYTES_BY_OFFSET[tfrom_seg]
                    if max_b == NO_DEEP_CONT_GATE or btt > max_b:
                        return None, max_b, btt
                    return tgt, max_b, btt
                elif not offset_ready:
                    return None, NO_DEEP_CONT_GATE, (1 << 60)
                return tgt, NO_DEEP_CONT_GATE, (1 << 60)
        return None, NO_DEEP_CONT_GATE, (1 << 60)

    def should_reserve_ts(v_cursor: int, next_pts_frame: int, cur_seg_idx: int, cur_ts_step: int) -> bool:
        nvt = try_next_ts_target(next_pts_frame, cur_seg_idx, cur_ts_step)
        if nvt is None or v_cursor >= total_video_bytes:
            return False
        packs_until = ((nvt - scr27) // PACK_DUR) + 1 if nvt > scr27 else 1
        ts_bound = frame_byte_start[next_pts_frame]
        ts_pay = VIDEO_PAYLOAD - 10
        ts_win_start = max(0, ts_bound - ts_pay)
        bytes_before = max(0, ts_win_start - v_cursor)
        req_packs = ((bytes_before + VIDEO_PAYLOAD - 1) // VIDEO_PAYLOAD) + 1
        return req_packs > packs_until

    def should_reserve_bf(v_cursor: int, next_bf_thresh: int, bf_idx: int, next_pts_frame: int, cur_ts_step: int) -> bool:
        if bf_idx >= len(bf_frame_starts) or v_cursor >= next_bf_thresh:
            return False
        nsync = bf_frame_starts[bf_idx]
        bf_tgt = bf_target_for_seg(bf_idx)
        if bf_tgt <= scr27:
            return True
        packs_until = ((bf_tgt - scr27) // PACK_DUR) + 1
        if packs_until == 0:
            return True
        if cur_ts_step <= 0 or next_pts_frame >= nsync:
            rem_ts = 0
        else:
            rem_ts = ((nsync - 1 - next_pts_frame) // cur_ts_step) + 1
        rem_bytes = next_bf_thresh - v_cursor
        residual = max(0, rem_bytes - rem_ts * (VIDEO_PAYLOAD - 10))
        req_packs = rem_ts + ((residual + VIDEO_PAYLOAD - 1) // VIDEO_PAYLOAD)
        return req_packs >= packs_until

    def try_next_video_action_target(v_cursor: int, next_bf_thresh: int, bf_idx: int, next_pts_frame: int, cur_seg_idx: int, cur_ts_step: int) -> Optional[int]:
        nxt = None
        is_deep_reg = seg_start_frame(cur_seg_idx) >= DEEP_CONT_START_FRAME
        if bf_idx < len(bf_frame_starts):
            bft = bf_target_for_seg(bf_idx)
            if not is_deep_reg or bft > scr27:
                nxt = bft if nxt is None else min(nxt, bft)
        tst = try_next_ts_target(next_pts_frame, cur_seg_idx, cur_ts_step)
        if tst is not None:
            eff_tst = effective_ts_wait_target(cur_seg_idx, cur_ts_step, next_pts_frame, next_pts_frame + cur_ts_step, tst)
            if not is_deep_reg or eff_tst > scr27:
                nxt = eff_tst if nxt is None else min(nxt, eff_tst)
        plen = get_plain_len(v_cursor, bf_idx, next_bf_thresh)
        if plen > 0:
            is_tail_bf = bf_idx < len(bf_frame_starts) and v_cursor + plen >= next_bf_thresh and plen < VIDEO_PAYLOAD
            if is_tail_bf:
                pt = compute_plain_target(v_cursor, plen, bf_idx, next_bf_thresh)
            elif is_mid_frame_cont(v_cursor, plen):
                pt = scr27
            else:
                pt = compute_plain_target(v_cursor, plen, bf_idx, next_bf_thresh)
            if is_deep_reg:
                dt, _, _ = try_deep_cont_target(v_cursor, plen, seg_start_frame(cur_seg_idx), True)
                if dt is not None:
                    pt = dt
            nxt = pt if nxt is None else min(nxt, pt)
        return nxt

    def should_wait_for_audio(v_cursor: int, next_bf_thresh: int, bf_idx: int, next_pts_frame: int, cur_seg_idx: int, cur_ts_step: int, next_aud_tgt: int, next_aud_three: bool) -> bool:
        nvt = try_next_video_action_target(v_cursor, next_bf_thresh, bf_idx, next_pts_frame, cur_seg_idx, cur_ts_step)
        if nvt is None:
            return True
        asnap = snap_wait_target(next_aud_tgt)
        vsnap = snap_wait_target(nvt)
        if asnap != vsnap:
            return asnap < vsnap
        if (
            not next_aud_three
            and next_aud_tgt > nvt
            and next_aud_tgt - nvt <= AUDIO_PLAIN_RACE_TOL_27M * 2
            and asnap - next_aud_tgt <= AUDIO_PLAIN_RACE_TOL_27M
        ):
            return False
        return True

    if not bf_segments:
        return b""

    # Initial pack (sys_hdr + bf0 + first video seg)
    bf_idx = 0
    next_bf_thresh = bf_byte_bounds[0]
    bf0 = build_bf_pes(bf_segments[0])
    fixed0 = 14 + len(SYS_HDR_AUDIO) + len(bf0) + (6 + 3 + 13)
    ts_step0 = ts_step_for_segment(0)
    next_pts_frame = min(bf_frame_starts[0] + ts_step0, num_frames)
    next_ts_byte0 = frame_byte_start[next_pts_frame] if next_pts_frame < num_frames else total_video_bytes
    chunk0_len = min(max(0, PACK_SIZE - fixed0), next_ts_byte0, total_video_bytes)
    chunk0 = video_payload[0:chunk0_len]
    v_cursor = chunk0_len
    pts0, dts0 = video_pts_dts(0)
    packs.append(emit_pack([SYS_HDR_AUDIO, bf0, build_video_seg_pes(chunk0, pts0, dts0)]))

    bf_idx = 1
    cur_seg_idx = 0
    cur_ts_step = ts_step0

    while v_cursor < total_video_bytes or audio_cursor < audio_len:
        send = seg_end_frame(cur_seg_idx)
        while next_pts_frame < send and next_pts_frame < num_frames and v_cursor > frame_byte_start[next_pts_frame]:
            next_pts_frame += 1

        while True:
            if bf_idx < len(bf_segments) and v_cursor >= next_bf_thresh:
                break

            has_pending_audio = audio_cursor < audio_len
            pending_aud_tgt = audio_scr_target() if has_pending_audio else (1 << 60)
            pending_aud_three = audio_has_three_unit_starts() if has_pending_audio else False

            res_ts = should_reserve_ts(v_cursor, next_pts_frame, cur_seg_idx, cur_ts_step)
            res_bf = should_reserve_bf(v_cursor, next_bf_thresh, bf_idx, next_pts_frame, cur_ts_step)
            res_vid = res_ts or res_bf
            before_bf = bf_idx >= len(bf_segments) or v_cursor < next_bf_thresh

            if (
                before_bf
                and res_ts
                and not res_bf
                and has_pending_audio
                and pending_aud_tgt > scr27
                and should_wait_for_audio(v_cursor, next_bf_thresh, bf_idx, next_pts_frame, cur_seg_idx, cur_ts_step, pending_aud_tgt, pending_aud_three)
            ):
                wait_scr(pending_aud_tgt)
                continue

            if before_bf and res_ts and not res_bf and has_pending_audio and pending_aud_tgt <= scr27:
                res_vid = False

            if (
                before_bf
                and res_bf
                and not res_ts
                and has_pending_audio
                and pending_aud_tgt <= scr27
                and bf_idx < len(bf_segments)
                and pending_aud_tgt <= bf_target_for_seg(bf_idx)
            ):
                res_vid = False

            if res_vid:
                break

            audio_emitted = False
            if has_pending_audio and scr27 >= pending_aud_tgt:
                ap = emit_audio_pack()
                if ap is not None:
                    packs.append(ap)
                    audio_emitted = True
            if audio_emitted:
                continue

            if (
                has_pending_audio
                and pending_aud_tgt > scr27
                and should_wait_for_audio(v_cursor, next_bf_thresh, bf_idx, next_pts_frame, cur_seg_idx, cur_ts_step, pending_aud_tgt, pending_aud_three)
            ):
                wait_scr(pending_aud_tgt)
                continue

            break

        if bf_idx < len(bf_segments) and v_cursor >= next_bf_thresh:
            bf_tgt = bf_target_for_seg(bf_idx)
            bf_drain_tgt = bf_tgt
            bf_wait_tgt = bf_tgt

            use_penult = False
            if bf_idx < len(bf_frame_starts) - 1 and bf_frame_starts[bf_idx] >= LATE_TS_GATE_START_FRAME and (num_frames - bf_frame_starts[bf_idx] <= TAIL_TS_GATE_MAX_FRAMES_REM):
                npf_pen = min(bf_frame_starts[bf_idx] + PTS_STAMP_INTERVAL_FINAL, num_frames)
                ptgt = try_next_ts_target(npf_pen, bf_idx, PTS_STAMP_INTERVAL_FINAL)
                if ptgt is not None:
                    use_penult = True
                    bf_drain_tgt = ptgt
                    bf_wait_tgt = ptgt

            use_final = False
            if not use_penult and bf_idx == len(bf_frame_starts) - 1 and bf_frame_starts[bf_idx] >= LATE_TS_GATE_START_FRAME and num_frames > 0:
                use_final = True
                _, dts_last = video_pts_dts(num_frames - 1)
                ftgt = max(0, dts_last * 300 - SCR_LEAD_VIDEO_MID)
                bf_drain_tgt = ftgt
                bf_wait_tgt = ftgt

            if audio_cursor < audio_len:
                nat = audio_scr_target()
                if nat <= bf_drain_tgt + PACK_DUR:
                    if nat > scr27:
                        if nat <= bf_drain_tgt:
                            wait_scr(nat)
                            continue
                    else:
                        ap = emit_audio_pack()
                        if ap is not None:
                            packs.append(ap)
                            continue

            bf = build_bf_pes(bf_segments[bf_idx])
            next_bf_thresh = bf_byte_bounds[bf_idx] if bf_idx < len(bf_byte_bounds) else total_video_bytes + 1
            bf_idx += 1

            if v_cursor < total_video_bytes:
                fi2 = frame_at(v_cursor)
                pts_v, dts_v = video_pts_dts(fi2)
                cur_seg_idx = bf_idx - 1
                cur_ts_step = PTS_STAMP_INTERVAL_FINAL if use_penult else ts_step_for_segment(cur_seg_idx)
                next_pts_frame = min(bf_frame_starts[cur_seg_idx] + cur_ts_step, num_frames)
                tol = 0 if use_final else VIDEO_BF_WAIT_TOL_27M
                wait_scr(max(0, bf_wait_tgt - tol))
                fixed_b = 14 + len(SYS_HDR_AUDIO) + len(bf) + (6 + 3 + 13)
                next_ts_byte = frame_byte_start[next_pts_frame] if next_pts_frame < num_frames else total_video_bytes
                avail_b = min(
                    max(0, PACK_SIZE - fixed_b),
                    next_bf_thresh - v_cursor,
                    max(0, next_ts_byte - v_cursor),
                    total_video_bytes - v_cursor,
                )
                chunk_b = video_payload[v_cursor : v_cursor + avail_b]
                v_cursor += avail_b
                audio_first_pack = True
                packs.append(emit_pack([SYS_HDR_AUDIO, bf, build_video_seg_pes(chunk_b, pts_v, dts_v)]))
            else:
                audio_first_pack = True
                packs.append(emit_pack([SYS_HDR_AUDIO, bf]))
            continue

        if v_cursor < total_video_bytes:
            send_ts = seg_end_frame(cur_seg_idx)
            can_emit_ts = next_pts_frame < send_ts
            ts_bound = frame_byte_start[next_pts_frame] if next_pts_frame < num_frames else total_video_bytes + 1
            ts_pay = VIDEO_PAYLOAD - 10
            if bf_idx < len(bf_segments):
                ts_pay = min(ts_pay, next_bf_thresh - v_cursor)
            ts_pay = min(ts_pay, total_video_bytes - v_cursor)

            if can_emit_ts and ts_pay > 0 and v_cursor <= ts_bound < v_cursor + ts_pay:
                stamped_frame = next_pts_frame
                following_pts_frame = stamped_frame + cur_ts_step
                next_chunk_bound = frame_byte_start[following_pts_frame] if following_pts_frame < num_frames else total_video_bytes
                ts_pay = min(ts_pay, max(0, next_chunk_bound - v_cursor))

                pts_v, dts_v = video_pts_dts(stamped_frame)
                next_pts_frame = following_pts_frame
                sstart = seg_start_frame(cur_seg_idx)
                send2 = seg_end_frame(cur_seg_idx)
                is_tail_ts = following_pts_frame >= send2
                ts_lead = video_ts_lead(cur_seg_idx, sstart, stamped_frame, is_tail_ts)
                ts_tgt = max(0, dts_v * 300 - ts_lead)
                ts_wait = effective_ts_wait_target(cur_seg_idx, cur_ts_step, stamped_frame, following_pts_frame, ts_tgt)
                wait_scr(ts_wait)
                chunk_ts = video_payload[v_cursor : v_cursor + ts_pay]
                v_cursor += ts_pay
                packs.append(emit_pack([build_video_ts_pes(chunk_ts, pts_v, dts_v)]))
                continue

        if v_cursor < total_video_bytes:
            len3 = get_plain_len(v_cursor, bf_idx, next_bf_thresh)
            is_tail_bf = bf_idx < len(bf_segments) and v_cursor + len3 >= next_bf_thresh and len3 < VIDEO_PAYLOAD
            mid_cont = is_mid_frame_cont(v_cursor, len3)
            waited_deep = False
            if len3 > 0 and seg_start_frame(cur_seg_idx) >= DEEP_CONT_START_FRAME:
                dt, max_b, btt = try_deep_cont_target(v_cursor, len3, seg_start_frame(cur_seg_idx), False)
                if dt is not None and (audio_cursor >= audio_len or audio_scr_target() >= dt):
                    if max_b > VIDEO_PAYLOAD and btt > VIDEO_PAYLOAD:
                        wait_scr(max(0, dt - DEEP_CONT_TOL_27M))
                    else:
                        wait_scr(dt)
                    waited_deep = True

            if len3 > 0 and not waited_deep and (not mid_cont or is_tail_bf):
                wait_scr(compute_plain_target(v_cursor, len3, bf_idx, next_bf_thresh))

            chunk_v = video_payload[v_cursor : v_cursor + len3]
            v_cursor += len3
            packs.append(emit_pack([build_video_nots_pes(chunk_v)]))
        elif audio_cursor < audio_len:
            ap = emit_audio_pack()
            if ap is not None:
                packs.append(ap)
        else:
            break

    # PatchBfRpiFields
    frame_pack = [-1] * num_frames
    vcur_scan = 0
    nxt_fr = 0
    for pk_idx, pack in enumerate(packs):
        pos = 14
        while pos + 6 <= len(pack) and pack[pos] == 0 and pack[pos + 1] == 0 and pack[pos + 2] == 1:
            sid = pack[pos + 3]
            ln = (pack[pos + 4] << 8) | pack[pos + 5]
            nxt = pos + 6 + ln
            if sid == SID_VIDEO and pos + 9 <= len(pack):
                hlen = pack[pos + 8]
                plen = ln - 3 - hlen
                if plen > 0:
                    pstart = vcur_scan
                    pend = vcur_scan + plen
                    while nxt_fr < num_frames and frame_byte_start[nxt_fr] < pend:
                        if frame_byte_start[nxt_fr] >= pstart:
                            frame_pack[nxt_fr] = pk_idx
                        nxt_fr += 1
                    vcur_scan = pend
            if sid == SID_PADDING:
                break
            pos = nxt

    bf_rec = 0
    for pk_idx, pack in enumerate(packs):
        if bf_rec >= len(bf_frame_starts):
            break
        pos = 14
        while pos + 6 <= len(pack) and pack[pos] == 0 and pack[pos + 1] == 0 and pack[pos + 2] == 1:
            sid = pack[pos + 3]
            ln = (pack[pos + 4] << 8) | pack[pos + 5]
            if sid == SID_PRIV2:
                payload_off = pos + 6
                seg_start = bf_frame_starts[bf_rec]
                bf_rec += 1
                for i in range(4):
                    fr = seg_start + i + 1
                    rel_pk = (frame_pack[fr] - pk_idx) if (fr < num_frames and frame_pack[fr] >= pk_idx) else 0
                    fld = payload_off + 2 + i * 2
                    pack[fld] = (rel_pk >> 8) & 0xFF
                    pack[fld + 1] = rel_pk & 0xFF
                break
            if sid == SID_PADDING:
                break
            pos += 6 + ln

    return b"".join(packs)



def find_video_input(input_dir: Path, vid_id: str) -> Tuple[str, Path]:
    """
    Locate input video for vid_id inside input_dir.
    Returns ("video", file_path) for MP4/MKV/MOV/AVI or ("frames", frames_dir) for PNG sequences.
    """
    exts = (".mp4", ".mkv", ".mov", ".avi")
    # 1. Exact or prefix-matched video file in input_dir
    for ext in exts:
        exact = input_dir / f"{vid_id}{ext}"
        if exact.is_file():
            return "video", exact
    for ext in exts:
        matches = sorted(input_dir.glob(f"{vid_id}*{ext}"))
        if matches:
            return "video", matches[0]

    # 2. Subfolder <input_dir>/<vid_id>/ (either PNG frames or a video file inside)
    sub = input_dir / vid_id
    if sub.is_dir():
        frames_sub = sub / "frames"
        if frames_sub.is_dir() and any(frames_sub.glob("*.png")):
            return "frames", frames_sub
        if any(sub.glob("*.png")):
            return "frames", sub
        for ext in exts:
            matches = sorted(sub.glob(f"*{ext}"))
            if matches:
                return "video", matches[0]

    raise FileNotFoundError(
        f"No video file or PNG frame folder found for '{vid_id}' in {input_dir}"
    )


def probe_input(kind: str, src: Path) -> Tuple[int, int, int, Optional[str]]:
    """Return (width, height, frame_count, ffmpeg_frame_pattern_or_None)."""
    if kind == "frames":
        pngs = sorted(src.glob("*.png"))
        if not pngs:
            raise FileNotFoundError(f"No PNG frames found in {src}")
        probe = subprocess.run(
            [
                FFPROBE, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                str(pngs[0]),
            ],
            capture_output=True, text=True, check=True,
        )
        st = json.loads(probe.stdout)["streams"][0]
        w, h = int(st["width"]), int(st["height"])
        # Detect frame_%06d.png pattern or fallback to sorted concat file
        first_name = pngs[0].name
        if first_name.startswith("frame_") and first_name.endswith(".png") and len(first_name) == 16:
            return w, h, len(pngs), str(src / "frame_%06d.png")
        return w, h, len(pngs), None

    probe = subprocess.run(
        [
            FFPROBE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,nb_frames",
            "-of", "json",
            str(src),
        ],
        capture_output=True, text=True, check=True,
    )
    st = json.loads(probe.stdout)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    nb = st.get("nb_frames")
    if nb is None or nb == "N/A":
        # Fallback: count packets quickly
        p2 = subprocess.run(
            [
                FFPROBE, "-v", "error",
                "-count_packets", "-select_streams", "v:0",
                "-show_entries", "stream=nb_read_packets",
                "-of", "default=nw=1:nk=1",
                str(src),
            ],
            capture_output=True, text=True, check=True,
        )
        nb = p2.stdout.strip()
    return w, h, int(nb), None


def encode_h264_annexb(
    kind: str,
    src: Path,
    frame_pattern: Optional[str],
    out_264: Path,
    tmp_dir: Path,
    target_w: int,
    target_h: int,
    avg_kbps: int,
    max_kbps: int,
    buf_kbps: int,
) -> None:
    tmp_dir.mkdir(parents=True, exist_ok=True)
    passlog = tmp_dir / f"{out_264.stem}_passlog"
    concat_file: Optional[Path] = None

    if kind == "frames":
        if frame_pattern is not None:
            input_args = ["-framerate", "30000/1001", "-i", frame_pattern]
        else:
            concat_file = tmp_dir / f"{out_264.stem}_frames.txt"
            pngs = sorted(src.glob("*.png"))
            lines = [f"file '{p.resolve().as_posix()}'\nduration 0.0333667" for p in pngs]
            concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
            input_args = ["-f", "concat", "-safe", "0", "-i", str(concat_file)]
    else:
        input_args = ["-i", str(src)]

    vf = f"scale={target_w}:{target_h}:flags=lanczos,fps=30000/1001"
    x264_params = (
        f"slices=1:weightp=1:weightb=0:8x8dct=0:cabac=1:nal-hrd=vbr:"
        f"vbv-maxrate={max_kbps}:vbv-bufsize={buf_kbps}:aq-mode=1:"
        f"aq-strength=0.7:psy-rd=0.8:rc-lookahead=10:repeat-headers=1:aud=1"
    )
    common = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        *input_args,
        "-c:v", "libx264", "-preset", "slow",
        "-profile:v", "main", "-level", "3.1",
        "-b:v", f"{avg_kbps}k", "-maxrate", f"{max_kbps}k", "-bufsize", f"{buf_kbps}k",
        "-refs", "1", "-bf", "0",
        "-g", "30", "-keyint_min", "15", "-sc_threshold", "40",
        "-vf", vf, "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
        "-x264-params", x264_params,
        "-an",
    ]
    p1 = common + ["-pass", "1", "-passlogfile", str(passlog), "-f", "null", os.devnull]
    subprocess.run(p1, check=True)
    p2 = common + ["-pass", "2", "-passlogfile", str(passlog), "-f", "h264", str(out_264)]
    subprocess.run(p2, check=True)

    for f in tmp_dir.glob(f"{passlog.name}*"):
        f.unlink(missing_ok=True)
    if concat_file is not None:
        concat_file.unlink(missing_ok=True)


def verify_pmf(
    pmf_path: Path,
    expected_w: int,
    expected_h: int,
    expected_audio: Optional[bytes] = None,
) -> None:
    sz = pmf_path.stat().st_size
    assert sz % PACK_SIZE == 0 and sz > 2048, f"{pmf_path} invalid size {sz}"
    hdr, extracted_audio = extract_atrac_and_header(pmf_path)
    assert hdr[:8] == b"PSMF0015"
    assert hdr[142] * 16 == expected_w and hdr[143] * 16 == expected_h
    if expected_audio is not None:
        assert extracted_audio == expected_audio, f"{pmf_path} ATRAC audio round-trip mismatch"

    res = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(pmf_path)],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(res.stdout)
    vstreams = [s for s in info["streams"] if s["codec_type"] == "video"]
    assert vstreams and vstreams[0]["width"] == expected_w and vstreams[0]["height"] == expected_h, vstreams


def process_one(
    vid_id: str,
    orig_movie_dir: Path,
    input_dir: Path,
    out_movie_dir: Path,
    tmp_dir: Path,
    bitrate_override: Optional[int] = None,
    force: bool = False,
) -> Path:
    orig_pmf = orig_movie_dir / f"{vid_id}.pmf"
    if not orig_pmf.exists():
        raise FileNotFoundError(f"Original PMF not found: {orig_pmf}")

    out_pmf = out_movie_dir / f"{vid_id}.pmf"
    kind, src = find_video_input(input_dir, vid_id)
    raw_w, raw_h, expected_frames, frame_pattern = probe_input(kind, src)

    # Sony PSMF dimensions are stored in 16x16 macroblocks (round up, e.g. 1272 -> 1280)
    tw = max(16, ((raw_w + 15) // 16) * 16)
    th = max(16, ((raw_h + 15) // 16) * 16)

    if bitrate_override is not None:
        avg_k = bitrate_override
    else:
        res_factor = (tw * th) / (1280 * 720)
        avg_k = int(max(1200, min(8000, 3800 * res_factor)))
    max_k = int(avg_k * 1.55)
    buf_k = int(avg_k * 1.1)

    out_movie_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    orig_hdr, audio_raw = extract_atrac_and_header(orig_pmf)
    if out_pmf.exists() and not force:
        try:
            verify_pmf(out_pmf, tw, th, expected_audio=audio_raw)
            print(f"[SKIP] {vid_id}.pmf already exists & verified ({out_pmf.stat().st_size / 1048576:.2f} MB)")
            return out_pmf
        except Exception:
            pass

    out_264 = tmp_dir / f"{vid_id}.264"
    video_units = None
    if out_264.exists() and not force:
        cand = split_frames_annexb(out_264.read_bytes())
        if len(cand) == expected_frames:
            video_units = cand
            print(f"[REUSE] {vid_id}: reusing {out_264.name} ({len(video_units)} frames)")

    if video_units is None:
        print(f"[ENC]  {vid_id}: {src.name} -> {tw}x{th} ({expected_frames} frames, {avg_k}k/{max_k}k)...")
        encode_h264_annexb(kind, src, frame_pattern, out_264, tmp_dir, tw, th, avg_k, max_k, buf_k)
        video_units = split_frames_annexb(out_264.read_bytes())

    assert len(video_units) == expected_frames, (
        f"{vid_id}: encoded {len(video_units)} frames vs expected {expected_frames}"
    )
    max_au = max(len(u) for u in video_units)
    print(f"[MUX]  {vid_id}: {len(video_units)} frames (max_au={max_au} B), {len(audio_raw) // ATX_PACKET_SIZE} ATRAC frames...")

    mps_bytes = mux_mps_single_track(video_units, audio_raw)
    assert len(mps_bytes) % PACK_SIZE == 0

    hdr = bytearray(orig_hdr)
    last_ts = PTS_VIDEO_START + len(video_units) * FRAME_DUR_90K
    struct.pack_into(">I", hdr, 12, len(mps_bytes))
    hdr[0x5A:0x60] = last_ts.to_bytes(6, "big")
    hdr[0x74:0x7A] = last_ts.to_bytes(6, "big")
    hdr[142] = tw // 16
    hdr[143] = th // 16

    with open(out_pmf, "wb") as f:
        f.write(hdr)
        f.write(mps_bytes)

    verify_pmf(out_pmf, tw, th, expected_audio=audio_raw)
    out_264.unlink(missing_ok=True)
    print(f"[DONE] {vid_id}.pmf -> {out_pmf.stat().st_size / 1048576:.2f} MB")
    return out_pmf


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Encode and multiplex custom cutscene videos or PNG frame folders back into Sony PMF format."
    )
    ap.add_argument(
        "--orig-movies",
        type=Path,
        default=Path("./extracted_iso/PSP_GAME/USRDIR/pack/movie"),
        help="Directory containing original .pmf files (default: ./extracted_iso/PSP_GAME/USRDIR/pack/movie)",
    )
    ap.add_argument(
        "--input-dir",
        type=Path,
        default=Path("./cutscenes_upscaled"),
        help="Directory containing edited/upscaled videos (.mp4/.mkv) or frame subfolders (default: ./cutscenes_upscaled)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./repack_work/PSP_GAME/USRDIR/pack/movie"),
        help="Output directory for remuxed .pmf files (default: ./repack_work/PSP_GAME/USRDIR/pack/movie)",
    )
    ap.add_argument(
        "--tmp-dir",
        type=Path,
        default=Path("./repack_work/tmp_enc"),
        help="Temporary directory for intermediate 2-pass H.264 streams (default: ./repack_work/tmp_enc)",
    )
    ap.add_argument(
        "--bitrate",
        type=int,
        default=None,
        help="Optional target average video bitrate in kbps (default: auto-scaled by resolution)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Force re-encoding even if a verified output PMF already exists",
    )
    ap.add_argument(
        "videos",
        nargs="*",
        help="Optional movie IDs to process (e.g. F0141 F0142). Default: all matching movies in --input-dir.",
    )
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("Error: ffmpeg and ffprobe must be installed and available on system PATH.")
        sys.exit(1)

    if args.videos:
        targets = [v.upper().removesuffix(".PMF") for v in args.videos]
    else:
        all_orig = [p.stem for p in sorted(args.orig_movies.glob("*.pmf"))]
        targets = []
        for vid in all_orig:
            try:
                find_video_input(args.input_dir, vid)
                targets.append(vid)
            except FileNotFoundError:
                pass
        if not targets:
            print(f"No matching cutscene inputs found in {args.input_dir}")
            sys.exit(1)

    for vid in targets:
        process_one(
            vid_id=vid,
            orig_movie_dir=args.orig_movies,
            input_dir=args.input_dir,
            out_movie_dir=args.out_dir,
            tmp_dir=args.tmp_dir,
            bitrate_override=args.bitrate,
            force=args.force,
        )


if __name__ == "__main__":
    main()
