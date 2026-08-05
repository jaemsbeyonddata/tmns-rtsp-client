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
chapter11.py - IRIG-106 Chapter 11 packet reader and the Chapter 24
Appendix 24-A mapping of Chapter 11 data types into TmNS fields.

Chapter 11 primary packet header (24 bytes, little-endian):

    off 0  : Packet Sync Pattern (u16) = 0xEB25
    off 2  : Channel ID          (u16)
    off 4  : Packet Length       (u32)  total packet bytes incl header/trailer
    off 8  : Data Length         (u32)  bytes in the packet body
    off 12 : Data Type Version   (u8)
    off 13 : Sequence Number     (u8)
    off 14 : Packet Flags        (u8)   bit7 = secondary header present
    off 15 : Data Type           (u8)
    off 16 : Relative Time Ctr   (u48)
    off 22 : Header Checksum      (u16)

If Packet Flags bit 7 is set, a 12-byte secondary header (8-byte time +
2 reserved + 2 checksum) follows before the body.  The next packet begins
Packet Length bytes after the start of this one.

Appendix 24-A field mapping (RCC 106-24 Chapter 24):

    Chapter 11 field            -> TmNS field
    ----------------------------  ----------------------------------------
    Channel ID (16b)              lower 16 bits of MDID
    Data Type (8b)                bits 15..8 of PDID
    Data Type Version (8b)        bits  7..0 of PDID
    Packet Body                   TmNS Package payload (MeasurementData)

This module has no third-party dependencies.
"""

import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Optional

CH11_SYNC = 0xEB25
CH11_SYNC_LE = b"\x25\xeb"
CH11_PRIMARY_HEADER_LEN = 24
CH11_SECONDARY_HEADER_LEN = 12
CH11_SECONDARY_HEADER_FLAG = 0x80


@dataclass
class Ch11Packet:
    channel_id: int
    data_type: int
    data_type_version: int
    sequence: int
    flags: int
    rtc: int                        # 48-bit Relative Time Counter
    packet_length: int
    data_length: int
    secondary_time: Optional[bytes] # raw 8-byte secondary-header time, if present
    body: bytes                     # packet body (Data Length bytes)


def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


def _u48(b: bytes, o: int) -> int:
    return _u32(b, o) | (_u16(b, o + 4) << 32)


def iter_packets(f: BinaryIO) -> Iterator[Ch11Packet]:
    """Stream Chapter 11 packets from a binary file object.

    Reads one packet at a time (O(1) memory, suitable for very large
    recordings). On a bad sync word it resynchronizes by scanning forward
    one byte at a time.
    """
    while True:
        start = f.tell()
        hdr = f.read(CH11_PRIMARY_HEADER_LEN)
        if len(hdr) < CH11_PRIMARY_HEADER_LEN:
            return
        if _u16(hdr, 0) != CH11_SYNC:
            f.seek(start + 1)           # resync
            continue

        channel_id = _u16(hdr, 2)
        packet_len = _u32(hdr, 4)
        data_len = _u32(hdr, 8)
        dtv = hdr[12]
        seq = hdr[13]
        flags = hdr[14]
        dtype = hdr[15]
        rtc = _u48(hdr, 16)

        if packet_len < CH11_PRIMARY_HEADER_LEN:
            f.seek(start + 1)           # invalid length; resync
            continue

        rest = f.read(packet_len - CH11_PRIMARY_HEADER_LEN)
        if len(rest) < packet_len - CH11_PRIMARY_HEADER_LEN:
            return                      # truncated file

        off = 0
        sec_time = None
        if flags & CH11_SECONDARY_HEADER_FLAG:
            sec_time = rest[0:8]
            off = CH11_SECONDARY_HEADER_LEN

        body = rest[off:off + data_len]
        yield Ch11Packet(channel_id, dtype, dtv, seq, flags, rtc,
                         packet_len, data_len, sec_time, body)


def map_to_tmns(pkt: Ch11Packet, mdid_upper: int = 0):
    """Apply the Appendix 24-A mapping. Returns (mdid, pdid, body).

    mdid_upper populates the user-defined upper 16 bits of the MDID
    (aircraft/box/etc.); the lower 16 bits are the Chapter 11 Channel ID.
    """
    mdid = ((mdid_upper & 0xFFFF) << 16) | (pkt.channel_id & 0xFFFF)
    pdid = ((pkt.data_type & 0xFF) << 8) | (pkt.data_type_version & 0xFF)
    return mdid, pdid, pkt.body


def tmns_timestamp(seconds: int, nanoseconds: int) -> int:
    """Pack seconds/nanoseconds into a TmNS MessageTimestamp.

    Per Ch.24 24.2.1.9 the MessageTimestamp is the lower 64 bits of the
    IEEE 1588-2008 time structure: upper 32 bits = seconds, lower 32 = ns.
    """
    return ((seconds & 0xFFFFFFFF) << 32) | (nanoseconds & 0xFFFFFFFF)


def message_timestamp(pkt: Ch11Packet):
    """Best-effort MessageTimestamp for a Chapter 11 packet.

    Returns (timestamp, absolute).  Per Appendix 24-A A.1.c(1)d the Chapter 11
    secondary-header absolute (PTP/1588) time maps to the MessageTimestamp; we
    read it as u32 LE seconds followed by u32 LE nanoseconds (the IEEE-1588
    lower-64 layout in Chapter 11's little-endian byte order).  When no
    secondary header is present there is no absolute time, so we derive a
    relative timestamp from the 10 MHz Relative Time Counter (100 ns/tick).
    """
    if pkt.secondary_time and len(pkt.secondary_time) >= 8:
        seconds = _u32(pkt.secondary_time, 0)
        nanoseconds = _u32(pkt.secondary_time, 4)
        return tmns_timestamp(seconds, nanoseconds), True
    ns = pkt.rtc * 100
    return tmns_timestamp(ns // 1_000_000_000, ns % 1_000_000_000), False


# Convenience: a few common Chapter 11 data-type names for logging.
CH11_DATA_TYPE_NAMES = {
    0x00: "Computer-Generated F0 (setup/TMATS)",
    0x01: "Computer-Generated F1 (events)",
    0x02: "Computer-Generated F2 (index)",
    0x03: "Computer-Generated F3 (streaming config)",
    0x09: "PCM F1",
    0x11: "Time Data F1",
    0x19: "MIL-STD-1553 F1",
    0x21: "Analog F1",
    0x29: "Discrete F1",
    0x30: "Message F0",
    0x38: "ARINC-429 F0",
    0x40: "Video F0",
    0x48: "Image F0",
    0x50: "UART F0",
    0x60: "Ethernet F0",
    0x70: "TSPI/CTS F0",
}


def data_type_name(dtype: int) -> str:
    return CH11_DATA_TYPE_NAMES.get(dtype, f"0x{dtype:02x}")
