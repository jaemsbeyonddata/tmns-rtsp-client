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
                          | tmns_size u64 | flags u8 | reserved(1)
          then entry_count fixed 24-byte, time-ordered entries:
            MessageTimestamp u64 | file_offset u64 | MDID u32 | MessageLength u32

  entry_count holds the count modulo 2**32; a reader takes the real count
  from the .index size, so an index can hold more than 4G entries.

  flags says whether the .tmns itself is in time order (its messages appear
  in the same order as the index entries): IDX_FLAG_ORDERED or
  IDX_FLAG_UNORDERED.  An index with neither (written before the flag
  existed) is checked once when opened.

  tmns_size links the index to its .tmns file (the .tmns byte size at index
  time), so a stale/mismatched index can be detected — every .tmns has exactly
  one corresponding .index.
"""

import bisect
import heapq
import mmap
import os
import shutil
import struct
import tempfile
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
IDX_FLAG_ORDERED = 0x01          # .tmns messages are in timestamp order
IDX_FLAG_UNORDERED = 0x02        # .tmns messages are not in timestamp order


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

_MSG_HDR = struct.Struct(">IIIIQ")     # word0, MDID, seq, MessageLength, ts
_IDX_ENTRY = struct.Struct(">QQII")    # ts, offset, MDID, MessageLength
_IDX_TS = struct.Struct(">Q")
READ_CHUNK = 4 << 20                   # bytes per read when streaming a .tmns

class RecordWriter:
    """Write TmNSDataMessages to a .tmns file and its .index.

    Index entries are streamed to a temporary file beside the .index as
    messages are written, so memory stays flat however large the recording.
    close() then writes the .index: entries already in time order are copied
    straight across; otherwise they are merge-sorted in bounded runs.
    """

    BUFFERED = 65536            # entries packed in memory before a flush
    SORT_RUN = 1 << 20          # entries sorted in memory per run
    FAN_IN = 64                 # runs merged at once (bounds open files)

    def __init__(self, path: str, index_path: Optional[str] = None):
        self.path = path
        self.index_path = index_path or os.path.splitext(path)[0] + ".index"
        self.f = open(path, "wb")
        self.f.write(REC_MAGIC + struct.pack(">H6x", REC_VERSION))
        self.offset = REC_HEADER_LEN
        self._tmp = tempfile.NamedTemporaryFile(
            dir=os.path.dirname(os.path.abspath(self.index_path)),
            prefix=".index-", suffix=".part", delete=False)
        self._buf: List[bytes] = []
        self.count = 0
        self.start_ts: Optional[int] = None      # earliest MessageTimestamp
        self.end_ts: Optional[int] = None        # latest MessageTimestamp
        self.mdids: set = set()
        self.time_ordered = True                 # timestamps never went back
        self._last_ts = 0

    def write(self, msg: bytes) -> None:
        _w0, mdid, _seq, length, ts = _MSG_HDR.unpack_from(msg)
        self.f.write(msg)
        self._buf.append(_IDX_ENTRY.pack(ts, self.offset, mdid, length))
        if len(self._buf) >= self.BUFFERED:
            self._flush()
        self.offset += len(msg)
        self.count += 1
        self.mdids.add(mdid)
        if self.start_ts is None:
            self.start_ts = self.end_ts = ts
        else:
            if ts < self._last_ts:
                self.time_ordered = False
            self.start_ts = min(self.start_ts, ts)
            self.end_ts = max(self.end_ts, ts)
        self._last_ts = ts

    def _flush(self) -> None:
        self._tmp.write(b"".join(self._buf))
        self._buf = []

    def close(self) -> None:
        """Finish the .tmns, then write its .index."""
        if self.f.closed:
            return
        self.f.close()
        self._flush()
        self._tmp.close()
        try:
            with open(self.index_path, "wb") as out:
                flags = (IDX_FLAG_ORDERED if self.time_ordered
                         else IDX_FLAG_UNORDERED)
                out.write(IDX_MAGIC + struct.pack(
                    ">HIQBx", IDX_VERSION, self.count & 0xFFFFFFFF,
                    self.offset, flags))
                if self.time_ordered:
                    with open(self._tmp.name, "rb") as src:
                        shutil.copyfileobj(src, out, 16 << 20)
                else:
                    self._sort_into(out)
        finally:
            os.remove(self._tmp.name)

    def _sort_into(self, out: BinaryIO) -> None:
        """External merge sort of the temporary entries by (ts, offset).

        Sorting on the offset too keeps messages with equal timestamps in
        file order, as a stable sort would.
        """
        runs = []
        try:
            with open(self._tmp.name, "rb") as src:
                while True:
                    block = src.read(self.SORT_RUN * IDX_ENTRY_LEN)
                    if not block:
                        break
                    run = sorted(_IDX_ENTRY.iter_unpack(block))
                    if not runs and len(block) < self.SORT_RUN * IDX_ENTRY_LEN:
                        _write_entries(out, run)     # fits in one run
                        return
                    runs.append(self._spill(run))
            # merge at most FAN_IN runs at a time, in passes, so a huge
            # unordered recording never needs thousands of open files
            while len(runs) > self.FAN_IN:
                group, runs = runs[:self.FAN_IN], runs[self.FAN_IN:]
                runs.append(self._spill(
                    heapq.merge(*(_read_entries(r) for r in group))))
                for r in group:
                    r.close()
            _write_entries(out, heapq.merge(*(_read_entries(r) for r in runs)))
        finally:
            for r in runs:
                r.close()

    def _spill(self, entries) -> BinaryIO:
        """Write sorted entries to an anonymous temp file, rewound to read."""
        tmp = tempfile.TemporaryFile(
            dir=os.path.dirname(os.path.abspath(self.index_path)))
        _write_entries(tmp, entries)
        tmp.seek(0)
        return tmp


def _write_entries(f: BinaryIO, entries) -> None:
    pack, chunk = _IDX_ENTRY.pack, []
    for e in entries:
        chunk.append(pack(*e))
        if len(chunk) >= 65536:
            f.write(b"".join(chunk))
            chunk = []
    f.write(b"".join(chunk))


def _read_entries(f: BinaryIO) -> Iterator[Tuple[int, int, int, int]]:
    while True:
        block = f.read(65536 * IDX_ENTRY_LEN)
        if not block:
            return
        yield from _IDX_ENTRY.iter_unpack(block)


def write_index(path: str, entries: List[Tuple[int, int, int, int]],
                tmns_size: int = 0) -> None:
    """Write a time-ordered .index from an in-memory list of
    (ts, offset, mdid, length) tuples (RecordWriter streams its own).

    tmns_size is the byte size of the corresponding .tmns file, stored so the
    index can be verified against (linked to) exactly one .tmns file.
    """
    if any(entries[i][0] > entries[i + 1][0] for i in range(len(entries) - 1)):
        entries = sorted(entries, key=lambda e: e[0])
    ordered = all(entries[i][1] < entries[i + 1][1]
                  for i in range(len(entries) - 1))
    with open(path, "wb") as f:
        f.write(IDX_MAGIC + struct.pack(
            ">HIQBx", IDX_VERSION, len(entries) & 0xFFFFFFFF, tmns_size,
            IDX_FLAG_ORDERED if ordered else IDX_FLAG_UNORDERED))
        _write_entries(f, entries)


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
        if h["length"] < DATAMSG_HEADER_LEN:
            return                       # corrupt MessageLength; stop here
        rest = f.read(h["length"] - DATAMSG_HEADER_LEN)
        if len(rest) < h["length"] - DATAMSG_HEADER_LEN:
            return
        yield pos, h, hdr + rest


def iter_raw(f: BinaryIO, start_offset: int, end_offset: Optional[int] = None,
             chunk: int = READ_CHUNK) -> Iterator[Tuple[int, int, memoryview]]:
    """Yield (timestamp, mdid, message) from a .tmns, from start_offset on.

    The fast reader for playback: it reads the file in large chunks and
    yields each message as a memoryview into its chunk (no copy, no header
    dict), so sending it costs one syscall and no Python-level buffering.
    Stops before end_offset if given, or at a short/corrupt message.
    """
    unpack = _MSG_HDR.unpack_from
    f.seek(start_offset)
    pos = start_offset
    buf = memoryview(b"")
    off = 0
    while end_offset is None or pos < end_offset:
        if len(buf) - off < DATAMSG_HEADER_LEN:
            want = chunk if end_offset is None else min(chunk, end_offset - pos)
            more = f.read(max(want, DATAMSG_HEADER_LEN))
            buf = memoryview(bytes(buf[off:]) + more)
            off = 0
            if len(buf) < DATAMSG_HEADER_LEN:
                return
        _w0, mdid, _seq, length, ts = unpack(buf, off)
        if length < DATAMSG_HEADER_LEN:
            return                       # corrupt MessageLength; stop here
        if len(buf) - off < length:
            more = f.read(max(chunk, length - (len(buf) - off)))
            buf = memoryview(bytes(buf[off:]) + more)
            off = 0
            if len(buf) < length:
                return
        yield ts, mdid, buf[off:off + length]
        off += length
        pos += length


def iter_indexed(f: BinaryIO, entries, chunk: int = READ_CHUNK
                 ) -> Iterator[Tuple[int, int, memoryview]]:
    """Yield (timestamp, mdid, message) in the order of index entries.

    For a .tmns whose messages are not in time order: each message is read
    at its indexed offset, but entries that sit back to back in the file are
    fetched with one read (up to chunk bytes), so stretches that are in
    order still read sequentially.
    """
    run: List[Tuple[int, int, int, int]] = []
    start = end = 0

    def fetch():
        f.seek(start)
        buf = memoryview(f.read(end - start))
        for ts, off, mdid, length in run:
            o = off - start
            if o + length > len(buf):
                return                   # file shorter than its index
            yield ts, mdid, buf[o:o + length]

    for e in entries:
        off, length = e[1], e[3]
        if run and (off != end or end - start + length > chunk):
            yield from fetch()
            run = []
        if not run:
            start = end = off
        run.append(e)
        end = off + length
    if run:
        yield from fetch()


# ----- .index reader with time search -------------------------------------

class _Entries:
    """Read-only sequence view of the index entries in a mapped .index.

    Entries are unpacked on access, so a huge index costs no memory; this is
    what keeps load time and RAM flat for very large recordings.
    """

    def __init__(self, buf, count: int, item):
        self._buf, self._n, self._item = buf, count, item

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i):
        if i < 0:
            i += self._n
        if not 0 <= i < self._n:
            raise IndexError("index entry out of range")
        return self._item(self._buf, IDX_HEADER_LEN + i * IDX_ENTRY_LEN)


class Index:
    """A .index mapped from disk: time-ordered entries with binary search.

    The file is memory-mapped rather than read into a list, so opening an
    index of any size is instant and costs no memory beyond the page cache.
    """

    def __init__(self, path: str):
        self.path = path
        self._mm = None
        with open(path, "rb") as f:
            hdr = f.read(IDX_HEADER_LEN)
            if len(hdr) < IDX_HEADER_LEN or hdr[:8] != IDX_MAGIC:
                raise ValueError(f"{path}: not a TmNS .index file")
            _ver, count, self.tmns_size, self.flags = struct.unpack(
                ">HIQB", hdr[8:23])
            size = os.fstat(f.fileno()).st_size
            n = (size - IDX_HEADER_LEN) // IDX_ENTRY_LEN
            # entry_count is the count modulo 2**32 (> 4G entries wraps it);
            # a shorter-than-declared file is cut to its whole entries
            if n & 0xFFFFFFFF != count:
                n = min(n, count)
            buf = b""
            if n:
                self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                buf = self._mm
        self.entries = _Entries(buf, n, _IDX_ENTRY.unpack_from)
        self._ts = _Entries(buf, n, lambda b, o: _IDX_TS.unpack_from(b, o)[0])
        self._ordered: Optional[bool] = None

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
            self._mm = None

    def verify_matches(self, tmns_path: str) -> None:
        """Confirm this index corresponds to tmns_path; raise ValueError if not.

        Checks the recorded .tmns size and that the first index entry points at
        a real message in the file with the matching timestamp/length.
        """
        actual = os.path.getsize(tmns_path)
        if self.tmns_size and self.tmns_size != actual:
            raise ValueError(
                f"{self.path} does not correspond to {tmns_path}: index expects "
                f"a {self.tmns_size}-byte file but it is {actual} bytes "
                f"(stale/mismatched index)")
        if not len(self.entries):
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
        return self.entries[0][0] if len(self.entries) else 0

    @property
    def end_ts(self) -> int:
        return self.entries[-1][0] if len(self.entries) else 0

    @property
    def mdids(self) -> set:
        """Every MDID in the index (one pass over the whole file)."""
        n = len(self.entries)
        if not n:
            return set()
        try:
            import numpy as np
        except ImportError:
            np = None
        out = set()
        step = 1 << 20                   # entries per pass
        for i in range(0, n, step):
            lo = IDX_HEADER_LEN + i * IDX_ENTRY_LEN
            hi = lo + min(step, n - i) * IDX_ENTRY_LEN
            part = self._mm[lo:hi]
            if np is not None:
                rec = np.frombuffer(part, dtype=">u4").reshape(-1, 6)
                out.update(np.unique(rec[:, 4]).tolist())
            else:
                out.update(m for (m,) in struct.iter_unpack(">16xI4x", part))
        return out

    @property
    def time_ordered(self) -> bool:
        """True if the .tmns holds its messages in timestamp order.

        Taken from the header flag; an index without one is checked once
        (file offsets must rise through the time-ordered entries).
        """
        if self._ordered is None:
            if self.flags & IDX_FLAG_ORDERED:
                self._ordered = True
            elif self.flags & IDX_FLAG_UNORDERED:
                self._ordered = False
            else:
                self._ordered = self._offsets_rise()
        return self._ordered

    def _offsets_rise(self) -> bool:
        prev = -1
        for i in range(0, len(self.entries), 1 << 20):
            hi = min(i + (1 << 20), len(self.entries))
            block = self._mm[IDX_HEADER_LEN + i * IDX_ENTRY_LEN:
                             IDX_HEADER_LEN + hi * IDX_ENTRY_LEN]
            offs = [o for (o,) in struct.iter_unpack(">8xQ8x", block)]
            if offs[0] <= prev or any(a >= b for a, b in zip(offs, offs[1:])):
                return False
            prev = offs[-1]
        return True

    def span(self, start_ts: int, end_ts: int) -> Tuple[int, int]:
        """Entry numbers [lo, hi) with start_ts <= timestamp <= end_ts."""
        return (bisect.bisect_left(self._ts, start_ts),
                bisect.bisect_right(self._ts, end_ts))

    def iter_entries(self, lo: int, hi: int
                     ) -> Iterator[Tuple[int, int, int, int]]:
        """Yield (ts, offset, mdid, length) for entries lo..hi-1."""
        step = 65536
        for i in range(lo, hi, step):
            j = min(i + step, hi)
            yield from _IDX_ENTRY.iter_unpack(
                self._mm[IDX_HEADER_LEN + i * IDX_ENTRY_LEN:
                         IDX_HEADER_LEN + j * IDX_ENTRY_LEN])

    def offset_at_or_after(self, ts: int) -> Optional[int]:
        """File offset of the first message with timestamp >= ts (or None)."""
        i = bisect.bisect_left(self._ts, ts)
        return self.entries[i][1] if i < len(self.entries) else None

    def offset_after(self, ts: int) -> Optional[int]:
        """File offset of the first message with timestamp > ts (or None)."""
        i = bisect.bisect_right(self._ts, ts)
        return self.entries[i][1] if i < len(self.entries) else None
