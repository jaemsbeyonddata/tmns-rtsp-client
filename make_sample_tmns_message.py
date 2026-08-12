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
make_sample_tmns_message.py - generate a TmNS recording of PCM data.

Builds RCC 106-24 Chapter 24 TmNSDataMessages whose Packages carry synthetic
IRIG-106 Chapter 4 PCM minor frames, and writes:

  <base>.tmns   - the recorded TmNSDataMessages (see tmns_message.py)
  <base>.index  - a time-ordered index for fast Range search on playback

Each PCM minor frame (16-bit big-endian words) is:
    [0xFE6B, 0x2840]   frame sync pattern (IRIG-106 Ch.4 32-bit 0xFE6B2840)
    [SFID]             subframe ID counter
    [data words ...]   sampled measurements (sine + ramp + counter)

Usage:
    python3 make_sample_tmns_message.py -o sample -n 500 --rate 100 \
        --mdid 7 --pdid 2305 --words 16 --subframes 4
"""

import argparse
import math
import struct

import tmns_message as tm

PCM_SYNC_WORDS = (0xFE6B, 0x2840)   # IRIG-106 Ch.4 32-bit frame sync 0xFE6B2840


def build_pcm_minor_frame(sfid: int, data_words) -> bytes:
    """One PCM minor frame as big-endian 16-bit words."""
    words = list(PCM_SYNC_WORDS) + [sfid & 0xFFFF] + [w & 0xFFFF for w in data_words]
    return b"".join(struct.pack(">H", w) for w in words)


def pcm_data_words(frame_i: int, num_words: int):
    """Synthetic 12-bit-ish measurements packed in 16-bit words."""
    words = []
    for k in range(num_words):
        # a sine per channel, a ramp, and a frame counter for recognizability
        val = 2048 + int(2000 * math.sin(2 * math.pi * (frame_i * 0.01 + k / num_words)))
        words.append(val & 0x0FFF)
    if num_words >= 1:
        words[-1] = frame_i & 0xFFFF     # last word = frame counter
    return words


def main():
    ap = argparse.ArgumentParser(description="Generate a TmNS PCM recording.")
    ap.add_argument("-o", "--output", default="sample",
                    help="output base name -> <base>.tmns and <base>.index")
    ap.add_argument("-n", "--messages", type=int, default=500,
                    help="number of TmNSDataMessages to write (default 500)")
    ap.add_argument("--rate", type=float, default=100.0,
                    help="messages per second (sets timestamp spacing; default 100)")
    ap.add_argument("--mdid", type=int, default=7, help="MessageDefinitionID")
    ap.add_argument("--pdid", type=int, default=2305,
                    help="PackageDefinitionID (default 2305 = PCM F1 0x0901)")
    ap.add_argument("--words", type=int, default=16,
                    help="PCM data words per minor frame (default 16)")
    ap.add_argument("--subframes", type=int, default=4,
                    help="minor frames per major frame / SFID modulus (default 4)")
    ap.add_argument("--start-time", type=float, default=None,
                    help="start time in TAI/PTP seconds (default: now)")
    args = ap.parse_args()

    import time as _t
    base_sec = int(args.start_time if args.start_time is not None else _t.time())
    step_ns = int(1e9 / args.rate) if args.rate > 0 else 0

    writer = tm.RecordWriter(f"{args.output}.tmns")
    for i in range(args.messages):
        total_ns = i * step_ns
        sec = base_sec + total_ns // 1_000_000_000
        nsec = total_ns % 1_000_000_000
        ts = tm.tmns_timestamp(sec, nsec)

        frame = build_pcm_minor_frame(i % args.subframes, pcm_data_words(i, args.words))
        pkg = tm.build_package(args.pdid, frame, time_delta=0)
        msg = tm.build_datamsg(args.mdid, i, packages=[pkg],
                               options=tm.build_options(1), playback=True, ts=ts)
        writer.write(msg)
    writer.close()

    import os
    tmns_size = os.path.getsize(f"{args.output}.tmns")
    tm.write_index(f"{args.output}.index", writer.entries, tmns_size)

    first = writer.entries[0][0] if writer.entries else 0
    last = writer.entries[-1][0] if writer.entries else 0
    print(f"Wrote {writer.count} TmNSDataMessages to {args.output}.tmns")
    print(f"Wrote {writer.count} index entries to {args.output}.index")
    print(f"  MDID {args.mdid}, PDID {args.pdid} (0x{args.pdid:04x}), "
          f"{args.words} PCM words/frame")
    print(f"  time span: {tm.ts_to_seconds(first):.3f} .. "
          f"{tm.ts_to_seconds(last):.3f} s (PTP)")
    print(f"  play a sub-range with e.g. Range: ptp-clock={first}-{last}")


if __name__ == "__main__":
    main()
