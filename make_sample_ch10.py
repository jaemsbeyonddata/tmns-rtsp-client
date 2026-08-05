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
make_sample_ch10.py - generate a small, valid IRIG-106 Chapter 11 recording
for exercising the Chapter 11 playback path of tmns_mock_server.py.

This writes well-formed Chapter 11 packets (correct sync, lengths, 48-bit RTC,
optional secondary header, and valid header/secondary checksums) across a few
data types and channels.  It is a test fixture, not a full Chapter 11 recorder.

Usage:
    python3 make_sample_ch10.py [-o sample.ch10] [-n 12]
"""

import argparse
import struct
import time

CH11_SYNC = 0xEB25
SECONDARY_FLAG = 0x80

# (channel_id, data_type, data_type_version, body-builder)
CHANNELS = [
    (0x0007, 0x09, 0x01, "PCM F1"),          # -> MDID 0x0007, PDID 0x0901
    (0x0021, 0x40, 0x00, "Video F0"),        # -> MDID 0x0021, PDID 0x4000
    (0x0100, 0x19, 0x01, "MIL-STD-1553 F1"), # -> MDID 0x0100, PDID 0x1901
]


def _checksum16(data: bytes) -> int:
    """16-bit arithmetic sum of 16-bit little-endian words."""
    total = 0
    for i in range(0, len(data) - 1, 2):
        total += data[i] | (data[i + 1] << 8)
    return total & 0xFFFF


def build_packet(channel_id, data_type, dtv, seq, body, rtc, secondary=True):
    flags = SECONDARY_FLAG if secondary else 0x00
    sec = b""
    if secondary:
        # secondary header: 8-byte time + 2 reserved + 2 checksum
        sectime = struct.pack("<Q", int(time.time() * 1e7))  # 100ns ticks (arbitrary)
        sec_no_ck = sectime + b"\x00\x00"
        sec = sec_no_ck + struct.pack("<H", _checksum16(sec_no_ck))

    data_len = len(body)
    total = 24 + len(sec) + data_len
    pad = (-total) % 4
    packet_len = total + pad

    # primary header, bytes 0..21 (checksum goes at 22)
    hdr = struct.pack("<HHIIBBBB",
                      CH11_SYNC, channel_id, packet_len, data_len,
                      dtv, seq, flags, data_type)
    hdr += struct.pack("<I", rtc & 0xFFFFFFFF)          # RTC low 32
    hdr += struct.pack("<H", (rtc >> 32) & 0xFFFF)      # RTC high 16 (48-bit total)
    hdr += struct.pack("<H", _checksum16(hdr))          # header checksum

    return hdr + sec + body + b"\x00" * pad


def main():
    ap = argparse.ArgumentParser(description="Generate a sample Chapter 11 file.")
    ap.add_argument("-o", "--output", default="sample.ch10")
    ap.add_argument("-n", "--packets", type=int, default=12,
                    help="total number of packets to write (default 12)")
    args = ap.parse_args()

    seqs = {}
    rtc = 0
    written = 0
    with open(args.output, "wb") as f:
        for i in range(args.packets):
            channel_id, data_type, dtv, label = CHANNELS[i % len(CHANNELS)]
            seq = seqs.get(channel_id, 0)
            seqs[channel_id] = (seq + 1) & 0xFF
            # a recognizable body: ASCII label + counters
            body = (f"{label} #{i}".encode("ascii")
                    + struct.pack("<II", i, channel_id))
            rtc += 100000  # advance the 10 MHz relative time counter
            f.write(build_packet(channel_id, data_type, dtv, seq, body, rtc))
            written += 1

    print(f"Wrote {written} Chapter 11 packets to {args.output}")
    print("Channels/data types:")
    for cid, dt, dtv, label in CHANNELS:
        print(f"  Channel 0x{cid:04x} -> MDID 0x{cid:04x} ({cid}); "
              f"{label} (0x{dt:02x}/0x{dtv:02x}) -> PDID 0x{(dt << 8) | dtv:04x} "
              f"({(dt << 8) | dtv})")


if __name__ == "__main__":
    main()
