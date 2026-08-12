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
ch10_to_tmns.py - wrap IRIG-106 Chapter 10/11 packets into a TmNS recording.

Reads a Chapter 10 recording (a .ch10/.c10 file of Chapter 11 packets) and
writes a TmNS recording (.tmns + .index) by applying the RCC 106-24 Chapter 24
**Appendix 24-A** mapping of Chapter 11 data types into TmNS messages:

    Chapter 11 field         -> TmNS field
    -----------------------    ----------------------------------------
    Channel ID (16b)           lower 16 bits of MDID
    Data Type (8b)             bits 15..8 of PDID
    Data Type Version (8b)     bits  7..0 of PDID
    Packet Flags (8b)          PackageStatusFlags
    secondary-header time      MessageTimestamp (IEEE-1588 lower 64 bits)
    Packet Body                TmNS Package payload (MeasurementData)

A body larger than a 16-bit PackageLength is split across multiple standard
Packages (Appendix 24-A A.1.d(2)).  The resulting .tmns/.index can be served
by tmns_rtsp_server.py.

Usage:
    python3 ch10_to_tmns.py recording.ch10                 # -> recording.tmns/.index
    python3 ch10_to_tmns.py recording.ch10 -o out/flight1  # -> out/flight1.tmns/.index
"""

import argparse
import os

import chapter11
import tmns_message as tm


def convert(ch10_path, out_base, mdid_upper=0, max_msg_bytes=60000,
            playback=True, verbose=False):
    seqs = {}
    packets = 0
    messages = 0
    by_pdid = {}
    writer = tm.RecordWriter(out_base + ".tmns")
    with open(ch10_path, "rb") as f:
        for pkt in chapter11.iter_packets(f):
            packets += 1
            mdid, pdid, body = chapter11.map_to_tmns(pkt, mdid_upper)
            ts, _absolute = chapter11.message_timestamp(pkt)
            pkgs = tm.split_packages(pdid, body, pkt.flags & 0xFF,
                                     pkt.rtc & 0xFFFFFFFF, max_msg_bytes)
            for msg in tm.messages_from_packages(mdid, pkgs, seqs, ts,
                                                 playback, max_msg_bytes):
                writer.write(msg)
                messages += 1
            by_pdid[pdid] = by_pdid.get(pdid, 0) + 1
            if verbose:
                print(f"  ch=0x{pkt.channel_id:04x} "
                      f"dtype={chapter11.data_type_name(pkt.data_type)} "
                      f"seq={pkt.sequence} -> mdid={mdid} pdid=0x{pdid:04x} "
                      f"body={len(body)}B")
    writer.close()
    tmns_size = os.path.getsize(out_base + ".tmns")
    tm.write_index(out_base + ".index", writer.entries, tmns_size)
    return packets, messages, writer, by_pdid


def main():
    ap = argparse.ArgumentParser(
        description="Wrap Chapter 10/11 packets into a TmNS .tmns recording "
                    "(Chapter 24 Appendix 24-A).")
    ap.add_argument("ch10", help="input .ch10/.c10 recording")
    ap.add_argument("-o", "--output",
                    help="output base name (default: input path without extension)")
    ap.add_argument("--mdid-upper", type=int, default=0,
                    help="user-defined upper 16 bits of the MDID (default 0)")
    ap.add_argument("--max-msg-bytes", type=int, default=60000,
                    help="cap each TmNSDataMessage; larger bodies split across "
                         "Packages/messages (default 60000)")
    ap.add_argument("--no-playback", action="store_true",
                    help="do not set the PlaybackDataFlag on wrapped messages")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    out = args.output or os.path.splitext(args.ch10)[0]
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    packets, messages, writer, by_pdid = convert(
        args.ch10, out, args.mdid_upper, args.max_msg_bytes,
        not args.no_playback, args.verbose)

    first = writer.entries[0][0] if writer.entries else 0
    last = writer.entries[-1][0] if writer.entries else 0
    mdids = sorted({e[2] for e in writer.entries})
    print(f"Wrapped {packets} Chapter 11 packet(s) -> {messages} "
          f"TmNSDataMessage(s)")
    print(f"  wrote {out}.tmns and {out}.index")
    print(f"  MDIDs {mdids}, PDIDs "
          f"{[f'0x{p:04x}' for p in sorted(by_pdid)]}")
    if writer.entries:
        print(f"  time span (PTP s): {tm.ts_to_seconds(first):.3f} .. "
              f"{tm.ts_to_seconds(last):.3f}")


if __name__ == "__main__":
    main()
