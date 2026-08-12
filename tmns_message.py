#!/usr/bin/env python3
#
# Copyright 2026 James Y
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
tmns_message.py - TmNS (IRIG-106 / RCC 106-24) message builders and a simple
record (.tmns) + index (.index) container for recording/playback.

Message content follows RCC 106-24 Chapter 24:
  TmNSMessageHeader (24 bytes, big-endian):
    word0: MessageVersion(4) OptionWordCount(4) Reserved(4) MessageType(4)
           MessageFlags(16)
    word1: MessageDefinitionID(32)
    word2: MessageDefinitionSequenceNumber(32)
    word3: MessageLength(32)                  (whole message, bytes)
    word4-5: MessageTimestamp(64)             (IEEE-1588 lower 64 bits:
                                               seconds<<32 | nanoseconds)
  followed by ApplicationDefinedFields (OptionWordCount*4 bytes) and one or
  more Packages (standard PackageHeader, 12 bytes: PackageDefinitionID(32),
  PackageLength(16), Reserved(8), PackageStatusFlags(8), PackageTimeDelta(32)).

The .tmns / .index container below is a purpose-built format for this project
(there is no standardized TmNS record-file format in IRIG-106; recordings are
normally carried via Chapter 10/11).  It stores raw Chapter-24 TmNSDataMessages
plus a time-ordered index for fast Range search during RTSP playback (Ch.26).

  .tmns:  16-byte header  magic b"TMNSREC\\x01" | version u16 | reserved(6)
          then a sequence of TmNSDataMessages, each framed by its MessageLength.
  .index: 24-byte header  magic b"TMNSIDX\\x01" | version u16 | entry_count u32
                          | tmns_size u64 | reserved(2)
          then entry_count fixed 24-byte, time-ordered entries:
            MessageTimestamp u64 | file_offset u64 | MDID u32 | MessageLength u32

  tmns_size links the index to its .tmns file (the .tmns byte size at index
  time), so a stale/mismatched index can be detected — every .tmns has exactly
  one corresponding .index.
"""

import bisect
import struct
import time
from typing import BinaryIO, Iterator, List, Optional, Tuple

# ----- Chapter 24 message constants ---------------------------------------
DATAMSG_HEADER_LEN = 24
STD_PKG_HEADER_LEN = 12
FLAG_END_OF_DATA = 0x0001
FLAG_PLAYBACK = 0x0040
FLAG_STANDARD_PKG_HEADER = 0x0080

# ----- container constants ------------------------------------------------
REC_MAGIC = b"TMNSREC\x01"
REC_VERSION = 1
REC_HEADER_LEN = 16
IDX_MAGIC = b"TMNSIDX\x01"
IDX_VERSION = 1
IDX_HEADER_LEN = 24
IDX_ENTRY_LEN = 24


# ----- timestamps ---------------------------------------------------------

def tmns_timestamp(seconds: int, nanoseconds: int) -> int:
    """Pack into a MessageTimestamp (IEEE-1588 lower 64 bits, Ch.24 24.2.1.9)."""
    return ((seconds & 0xFFFFFFFF) << 32) | (nanoseconds & 0xFFFFFFFF)


def now_timestamp() -> int:
    t = time.time()
    return tmns_timestamp(int(t), int((t % 1) * 1e9))


def ts_to_seconds(ts: int) -> float:
    """MessageTimestamp -> float seconds (for pacing/printing)."""
    return (ts >> 32) + (ts & 0xFFFFFFFF) / 1e9


# ----- message / package builders (Chapter 24) ----------------------------

def build_options(package_count: int) -> bytes:
    """One ApplicationDefinedFields option: PackageCount (kind 0x87)."""
    opt = bytes([0x87, 6]) + struct.pack(">I", package_count)
    return opt + b"\x00" * ((-len(opt)) % 4)


def build_package(pdid: int, measdata: bytes, status: int = 0,
                  time_delta: int = 0) -> bytes:
    """Standard PackageHeader (12B) + MeasurementData, padded to 32 bits."""
    plen = STD_PKG_HEADER_LEN + len(measdata)
    hdr = struct.pack(">IHBBI", pdid, plen, 0, status, time_delta)
    pkg = hdr + measdata
    return pkg + b"\x00" * ((-len(pkg)) % 4)


def build_datamsg(mdid: int, seq: int, packages=None, options: bytes = b"",
                  end_of_data: bool = False, playback: bool = False,
                  ts: Optional[int] = None) -> bytes:
    version = 1
    if end_of_data:
        # empty End-of-Data indicator (Ch.26 26.4.2.2)
        word0 = (version << 28) | FLAG_END_OF_DATA
        return struct.pack(">IIIIQ", word0, 0, 0, DATAMSG_HEADER_LEN, 0)
    flags = 0
    if playback:
        flags |= FLAG_PLAYBACK
    payload = b""
    if packages:
        flags |= FLAG_STANDARD_PKG_HEADER
        payload = b"".join(packages)
    owc = len(options) // 4
    word0 = (version << 28) | (owc << 24) | (0 << 16) | (flags & 0xFFFF)
    length = DATAMSG_HEADER_LEN + len(options) + len(payload)
    if ts is None:
        ts = now_timestamp()
    return struct.pack(">IIIIQ", word0, mdid, seq, length, ts) + options + payload


def parse_header(buf: bytes) -> dict:
    """Parse the 24-byte TmNSMessageHeader into a dict."""
    word0, mdid, seq, length, ts = struct.unpack(">IIIIQ", buf[:DATAMSG_HEADER_LEN])
    return {
        "version": (word0 >> 28) & 0xF,
        "owc": (word0 >> 24) & 0xF,
        "mtype": (word0 >> 16) & 0xF,
        "flags": word0 & 0xFFFF,
        "mdid": mdid,
        "seq": seq,
        "length": length,
        "timestamp": ts,
    }


# ----- helpers for wrapping a payload into Packages/messages ---------------
MAX_PACKAGE_LEN = 0xFFFF          # PackageLength is 16 bits (Ch.24 24.2.2.1.1)
OPTIONS_LEN = 8                   # length of build_options() output (PackageCount)


def split_packages(pdid: int, body: bytes, status: int = 0, time_delta: int = 0,
                   max_msg_bytes: int = 60000) -> List[bytes]:
    """Split a payload into standard Packages (Appendix 24-A A.1.d(2)).

    Each Package's payload fits the 16-bit PackageLength and keeps a message of
    packages under max_msg_bytes.
    """
    max_body = min(MAX_PACKAGE_LEN - STD_PKG_HEADER_LEN,
                   max_msg_bytes - DATAMSG_HEADER_LEN - OPTIONS_LEN
                   - STD_PKG_HEADER_LEN)
    max_body = max(1, max_body)
    chunks = [body[i:i + max_body] for i in range(0, len(body), max_body)] or [b""]
    return [build_package(pdid, c, status, time_delta) for c in chunks]


def messages_from_packages(mdid: int, packages: List[bytes], seqs: dict, ts: int,
                           playback: bool = False,
                           max_msg_bytes: int = 60000) -> List[bytes]:
    """Group Packages into TmNSDataMessages within max_msg_bytes.

    seqs is a {mdid: next_seq} dict maintaining a monotonic per-MDID
    MessageDefinitionSequenceNumber (Ch.26 26.5.1).  All messages share ts.
    """
    out: List[bytes] = []
    group, size = [], DATAMSG_HEADER_LEN + OPTIONS_LEN
    for pkg in packages:
        if group and size + len(pkg) > max_msg_bytes:
            seq = seqs.get(mdid, 0); seqs[mdid] = seq + 1
            out.append(build_datamsg(mdid, seq, packages=group,
                                     options=build_options(len(group)),
                                     playback=playback, ts=ts))
            group, size = [], DATAMSG_HEADER_LEN + OPTIONS_LEN
        group.append(pkg); size += len(pkg)
    if group:
        seq = seqs.get(mdid, 0); seqs[mdid] = seq + 1
        out.append(build_datamsg(mdid, seq, packages=group,
                                 options=build_options(len(group)),
                                 playback=playback, ts=ts))
    return out


# ----- .tmns record writer / reader ---------------------------------------

class RecordWriter:
    """Write TmNSDataMessages to a .tmns file, collecting index entries."""

    def __init__(self, path: str):
        self.f = open(path, "wb")
        self.f.write(REC_MAGIC + struct.pack(">H6x", REC_VERSION))
        self.entries: List[Tuple[int, int, int, int]] = []   # ts, offset, mdid, len
        self.count = 0

    def write(self, msg: bytes) -> None:
        offset = self.f.tell()
        h = parse_header(msg)
        self.f.write(msg)
        self.entries.append((h["timestamp"], offset, h["mdid"], h["length"]))
        self.count += 1

    def close(self) -> None:
        self.f.close()


def write_index(path: str, entries: List[Tuple[int, int, int, int]],
                tmns_size: int = 0) -> None:
    """Write a time-ordered .index from (ts, offset, mdid, length) tuples.

    tmns_size is the byte size of the corresponding .tmns file, stored so the
    index can be verified against (linked to) exactly one .tmns file.
    """
    entries = sorted(entries, key=lambda e: e[0])
    with open(path, "wb") as f:
        f.write(IDX_MAGIC + struct.pack(">HIQ2x", IDX_VERSION, len(entries),
                                        tmns_size))
        for ts, off, mdid, length in entries:
            f.write(struct.pack(">QQII", ts, off, mdid, length))


def iter_messages(f: BinaryIO, start_offset: Optional[int] = None
                  ) -> Iterator[Tuple[int, dict, bytes]]:
    """Yield (offset, header_dict, raw_bytes) from a .tmns file.

    Seeks to start_offset if given, else skips the 16-byte file header.
    """
    if start_offset is not None:
        f.seek(start_offset)
    else:
        f.seek(REC_HEADER_LEN)
    while True:
        pos = f.tell()
        hdr = f.read(DATAMSG_HEADER_LEN)
        if len(hdr) < DATAMSG_HEADER_LEN:
            return
        h = parse_header(hdr)
        rest = f.read(h["length"] - DATAMSG_HEADER_LEN)
        if len(rest) < h["length"] - DATAMSG_HEADER_LEN:
            return
        yield pos, h, hdr + rest


# ----- .index reader with time search -------------------------------------

class Index:
    """A loaded .index: time-ordered entries with binary-search helpers."""

    def __init__(self, path: str):
        self.path = path
        self.entries: List[Tuple[int, int, int, int]] = []   # ts, offset, mdid, len
        with open(path, "rb") as f:
            hdr = f.read(IDX_HEADER_LEN)
            if len(hdr) < IDX_HEADER_LEN or hdr[:8] != IDX_MAGIC:
                raise ValueError(f"{path}: not a TmNS .index file")
            _ver, count, self.tmns_size = struct.unpack(">HIQ", hdr[8:22])
            for _ in range(count):
                rec = f.read(IDX_ENTRY_LEN)
                if len(rec) < IDX_ENTRY_LEN:
                    break
                self.entries.append(struct.unpack(">QQII", rec))
        self._ts = [e[0] for e in self.entries]

    def verify_matches(self, tmns_path: str) -> None:
        """Confirm this index corresponds to tmns_path; raise ValueError if not.

        Checks the recorded .tmns size and that the first index entry points at
        a real message in the file with the matching timestamp/length.
        """
        import os
        actual = os.path.getsize(tmns_path)
        if self.tmns_size and self.tmns_size != actual:
            raise ValueError(
                f"{self.path} does not correspond to {tmns_path}: index expects "
                f"a {self.tmns_size}-byte file but it is {actual} bytes "
                f"(stale/mismatched index)")
        if not self.entries:
            return
        ts0, off0, _mdid0, len0 = self.entries[0]
        if off0 + DATAMSG_HEADER_LEN > actual:
            raise ValueError(f"{self.path}: first offset {off0} is past the end "
                             f"of {tmns_path}")
        with open(tmns_path, "rb") as f:
            f.seek(off0)
            h = parse_header(f.read(DATAMSG_HEADER_LEN))
        if h["timestamp"] != ts0 or h["length"] != len0:
            raise ValueError(f"{self.path} is stale for {tmns_path} "
                             f"(index/first-message mismatch)")

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def start_ts(self) -> int:
        return self.entries[0][0] if self.entries else 0

    @property
    def end_ts(self) -> int:
        return self.entries[-1][0] if self.entries else 0

    @property
    def mdids(self) -> set:
        return {e[2] for e in self.entries}

    def offset_at_or_after(self, ts: int) -> Optional[int]:
        """File offset of the first message with timestamp >= ts (or None)."""
        i = bisect.bisect_left(self._ts, ts)
        return self.entries[i][1] if i < len(self.entries) else None
