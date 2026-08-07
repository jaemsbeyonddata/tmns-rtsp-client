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
tmns_mock_server.py - A minimal mock IRIG-106 TmNS RTSPDataSource.

This is NOT a conformance reference; it is a test fixture used to exercise the
tmns_rtsp_client.py tool without a real TmNS source.  It implements just enough
of RCC/IRIG 106 Chapter 26 to be useful:

  * RTSP 1.0 control channel over TCP (default port 55554).
  * OPTIONS / SETUP / PLAY / PAUSE / TEARDOWN, with CSeq + Session handling.
  * Error paths for negative testing:
      - unknown method            -> 501 Not Implemented
      - non-TmNS Transport        -> 461 Unsupported Transport
      - malformed Range           -> 457 Invalid Range
      - unknown/expired Session   -> 454 Session Not Found
  * Parses the TmNS Transport header, opens the RTSPDataChannel:
      - UDP: sends TmNSDataMessages to the sink's client_port.
      - TCP: connects to the sink's listening client_port (source connects).
  * On PLAY, streams TmNSDataMessages (Chapter 24 header) built with a standard
    PackageHeader and ApplicationDefinedFields options.  Two data sources:
      - synthetic (default): generated MeasurementData, so the client's payload
        decoder has something to parse; a bounded Range finishes with an
        EndOfDataFlag message.
      - Chapter 11 playback (--ch10 FILE): reads an IRIG-106 Chapter 11
        recording and streams each packet body as a TmNS Package, mapping the
        Chapter 11 fields into TmNS per Chapter 24 Appendix 24-A (Channel ID ->
        MDID low 16 bits, Data Type/Version -> PDID).  End-of-Data is sent when
        the file is exhausted.

Usage:
    python3 tmns_mock_server.py [--port 55554] [--mdid 1] [--pdid 100]
                                [--rate 20] [--session-timeout 60]
    python3 tmns_mock_server.py --ch10 recording.ch10 [--mdid-upper 0] [--loop]
"""

import argparse
import re
import socket
import struct
import threading
import time

import chapter11

DATAMSG_HEADER_LEN = 24
STD_PKG_HEADER_LEN = 12
MAX_PACKAGE_LEN = 0xFFFF  # PackageLength is 16 bits (Ch.24 24.2.2.1.1)
OPTIONS_LEN = 28          # constant length of build_options() output
DEFAULT_MAX_MSG_BYTES = 60000  # keep each TmNSDataMessage under the UDP limit

RANGE_RE = re.compile(
    r"ptp-clock\s*=\s*(start|now|\d+)\s*-\s*(end|now|\d+)?\s*$", re.I)

# MDID list tokens in a TmNS_Request_Defined_URI: &N or &N-M (Ch.26 26.4.1.4)
MDID_TOKEN_RE = re.compile(r"&(\d+)(?:-(\d+))?")


def parse_requested_mdids(uri: str):
    """Return a list of (lo, hi) MDID intervals from a request URI.

    An empty list means "all MDIDs" (a request with no TmNSlist, per Ch.26
    26.4.1.4).  Intervals avoid expanding potentially huge ranges.
    """
    intervals = []
    for m in MDID_TOKEN_RE.finditer(uri):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        intervals.append((min(a, b), max(a, b)))
    return intervals


def mdid_requested(intervals, mdid: int) -> bool:
    return not intervals or any(lo <= mdid <= hi for lo, hi in intervals)


def now_timestamp() -> int:
    """Current time as a TmNS MessageTimestamp (IEEE-1588 lower 64 bits)."""
    now = time.time()
    return ((int(now) & 0xFFFFFFFF) << 32) | (int((now % 1) * 1e9) & 0xFFFFFFFF)


def _ts_option(kind: int) -> bytes:
    """32-bit TAI seconds + 32-bit nanoseconds timestamp option (0x88/0x89).

    Ch.24 Table 24-1 lists 8 bytes of timestamp data; option-length is encoded
    as total-including-header (2 + 8 = 10), consistent with the other rows.
    """
    now = time.time()
    secs, nsec = int(now), int((now % 1) * 1e9)
    return bytes([kind, 10]) + struct.pack(">II", secs, nsec)


def build_options(package_count: int) -> bytes:
    """ApplicationDefinedFields: PackageCount + Ingress + Egress timestamps.

    Exercises the 106-24 option set (0x87 PackageCount, 0x88 IngressTimestamp,
    0x89 EgressTimestamp) and is padded to a 32-bit boundary.
    """
    opts = (bytes([0x87, 6]) + struct.pack(">I", package_count)  # PackageCount
            + _ts_option(0x88)                                   # IngressTimestamp
            + _ts_option(0x89))                                  # EgressTimestamp (106-24)
    pad = (-len(opts)) % 4                                       # End-of-Options/pad
    return opts + b"\x00" * pad


def build_package(pdid: int, measdata: bytes, status: int = 0,
                  time_delta: int = 0) -> bytes:
    """Standard PackageHeader (12B) + MeasurementData, padded to 32 bits."""
    plen = STD_PKG_HEADER_LEN + len(measdata)
    hdr = struct.pack(">IHBBI", pdid, plen, 0, status, time_delta)
    pkg = hdr + measdata
    return pkg + b"\x00" * ((-len(pkg)) % 4)


def build_datamsg(mdid: int, seq: int, packages=None, options: bytes = b"",
                  end_of_data: bool = False, playback: bool = False,
                  ts: int = None) -> bytes:
    version = 1
    if end_of_data:
        # empty End-of-Data indicator (Ch.26 26.4.2.2)
        word0 = (version << 28) | 0x0001
        return struct.pack(">IIIIQ", word0, 0, 0, DATAMSG_HEADER_LEN, 0)
    flags = 0
    if playback:
        flags |= 0x0040
    payload = b""
    if packages:
        flags |= 0x0080          # StandardPackageHeaderFlag
        payload = b"".join(packages)
    owc = len(options) // 4
    word0 = (version << 28) | (owc << 24) | (0 << 16) | (flags & 0xFFFF)
    length = DATAMSG_HEADER_LEN + len(options) + len(payload)
    if ts is None:
        ts = now_timestamp()
    header = struct.pack(">IIIIQ", word0, mdid, seq, length, ts)
    return header + options + payload


class Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.lower = "UDP"
        self.dest = None
        self.client_port = None
        self.data_sock = None
        self.streaming = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.bounded = False
        self.last_activity = time.time()
        self.mdid_intervals = []      # requested MDID filter ([] = all)


class MockSource:
    def __init__(self, port, mdid, pdid, rate, session_timeout,
                 ch10_path=None, mdid_upper=0, loop=False,
                 max_msg_bytes=DEFAULT_MAX_MSG_BYTES, no_get_parameter=False):
        self.port = port
        self.mdid = mdid
        self.pdid = pdid
        self.rate = rate
        self.max_msg_bytes = max_msg_bytes
        self.session_timeout = session_timeout
        self.no_get_parameter = no_get_parameter
        self.ch10_path = ch10_path
        self.mdid_upper = mdid_upper
        self.loop = loop
        self.sessions = {}
        self._sid_counter = 0x1000

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        source = (f"Chapter 11 playback: {self.ch10_path}"
                  f"{' (looping)' if self.loop else ''}"
                  if self.ch10_path else
                  f"synthetic mdid={self.mdid} pdid={self.pdid}")
        print(f"[mock] TmNS RTSPDataSource listening on TCP :{self.port} "
              f"({source}, rate={self.rate}/s, "
              f"session_timeout={self.session_timeout}s)")
        try:
            while True:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle, args=(conn, addr),
                                 daemon=True).start()
        except KeyboardInterrupt:
            print("\n[mock] shutting down")
        finally:
            srv.close()

    def _handle(self, conn, addr):
        print(f"[mock] control connection from {addr}")
        buf = b""
        sess = None
        try:
            while True:
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise ConnectionError
                    buf += chunk
                head, _, buf = buf.partition(b"\r\n\r\n")
                lines = head.decode("iso-8859-1").split("\r\n")
                method, uri, _ = (lines[0].split(" ", 2) + ["", ""])[:3]
                headers = {}
                for l in lines[1:]:
                    if ":" in l:
                        k, v = l.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                clen = int(headers.get("content-length", "0") or "0")
                while len(buf) < clen:
                    buf += conn.recv(4096)
                buf = buf[clen:]
                sess = self._dispatch(conn, method.upper(), uri, headers, addr, sess)
        except (ConnectionError, OSError):
            pass
        finally:
            if sess:
                sess.stop.set()
            conn.close()
            print(f"[mock] control connection {addr} closed")

    def _reply(self, conn, cseq, code, reason, extra=None, body=b""):
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: {cseq}",
                 "Server: TmNS-Mock-Source/1.0"]
        if extra:
            lines += [f"{k}: {v}" for k, v in extra.items()]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    def _lookup_session(self, headers):
        """Return (session, error_code). Enforces existence and expiry."""
        sid = headers.get("session", "").split(";")[0].strip()
        if not sid or sid not in self.sessions:
            return None, 454
        sess = self.sessions[sid]
        if time.time() - sess.last_activity > self.session_timeout:
            sess.stop.set()
            self.sessions.pop(sid, None)
            print(f"[mock] session {sid} expired")
            return None, 454
        sess.last_activity = time.time()
        return sess, 0

    KNOWN_METHODS = {"OPTIONS", "DESCRIBE", "SETUP", "PLAY", "PAUSE",
                     "TEARDOWN", "GET_PARAMETER"}

    def _touch_session(self, headers):
        """Any request carrying a live Session id refreshes its timeout."""
        sid = headers.get("session", "").split(";")[0].strip()
        if sid and sid in self.sessions:
            self.sessions[sid].last_activity = time.time()

    def _dispatch(self, conn, method, uri, headers, addr, sess):
        cseq = headers.get("cseq", "0")

        known = set(self.KNOWN_METHODS)
        if self.no_get_parameter:
            known.discard("GET_PARAMETER")     # simulate a server without it

        # Unknown / unimplemented methods are rejected independently of any
        # session state (method recognition precedes session validation).
        if method not in known:
            self._reply(conn, cseq, 501, "Not Implemented")
            return sess

        # A Session-bearing request (incl. OPTIONS) refreshes the timeout.
        self._touch_session(headers)

        if method == "OPTIONS":
            public = "OPTIONS, DESCRIBE, SETUP, TEARDOWN, PLAY, PAUSE"
            if not self.no_get_parameter:
                public += ", GET_PARAMETER"
            self._reply(conn, cseq, 200, "OK", {"Public": public})
            return sess

        if method == "DESCRIBE":
            sdp = (b"v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=TmNS Mock Source\r\n"
                   b"m=application 0 TMNS/TMNSP 0\r\n"
                   b"a=control:%s\r\n" % uri.encode())
            self._reply(conn, cseq, 200, "OK",
                        {"Content-Type": "application/sdp"}, sdp)
            return sess

        if method == "SETUP":
            transport = headers.get("transport", "")
            if "TMNS/TMNSP" not in transport.upper():
                self._reply(conn, cseq, 461, "Unsupported Transport")
                return sess
            self._sid_counter += 1
            sess = Session(f"{self._sid_counter:08X}")
            m = re.search(r"TMNS/TMNSP/(TCP|UDP)", transport, re.I)
            if m:
                sess.lower = m.group(1).upper()
            m = re.search(r"client_port=(\d+)", transport)
            if m:
                sess.client_port = int(m.group(1))
            m = re.search(r"destination=([\d.]+)", transport)
            sess.dest = m.group(1) if m else addr[0]
            self.sessions[sess.sid] = sess
            self._reply(conn, cseq, 200, "OK",
                        {"Session": f"{sess.sid};timeout={self.session_timeout}",
                         "Transport": transport.split(",")[0].strip()})
            return sess

        # everything below needs a valid, unexpired session
        sess, err = self._lookup_session(headers)
        if err:
            self._reply(conn, cseq, 454, "Session Not Found")
            return None

        if method == "PLAY":
            rng = headers.get("range", "")
            if rng and not self._valid_range(rng):
                self._reply(conn, cseq, 457, "Invalid Range")
                return sess
            sess.bounded = bool(rng) and not rng.rstrip().endswith("-")
            sess.mdid_intervals = parse_requested_mdids(uri)
            if sess.mdid_intervals:
                print(f"[mock] PLAY requesting MDID intervals {sess.mdid_intervals}")
            self._start_stream(sess, addr)
            extra = {"Session": sess.sid}
            if rng:
                extra["Range"] = rng
            self._reply(conn, cseq, 200, "OK", extra)
            return sess

        if method == "PAUSE":
            sess.streaming.clear()
            self._reply(conn, cseq, 200, "OK", {"Session": sess.sid})
            return sess

        if method == "GET_PARAMETER":
            self._reply(conn, cseq, 200, "OK", {"Session": sess.sid})
            return sess

        if method == "TEARDOWN":
            sess.stop.set()
            sess.streaming.clear()
            self._reply(conn, cseq, 200, "OK", {"Session": sess.sid})
            self.sessions.pop(sess.sid, None)
            return None

        self._reply(conn, cseq, 501, "Not Implemented")
        return sess

    @staticmethod
    def _valid_range(rng: str) -> bool:
        m = RANGE_RE.match(rng.strip())
        if not m:
            return False
        start, end = m.group(1), m.group(2)
        if start and end and start.isdigit() and end and end.isdigit():
            if int(end) < int(start):
                return False
        return True

    def _start_stream(self, sess, addr):
        if sess.thread and sess.thread.is_alive():
            sess.streaming.set()
            return
        sess.streaming.set()

        def run():
            if sess.lower == "UDP":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                target = (sess.dest or addr[0], sess.client_port)
                send = lambda b: sock.sendto(b, target)
            else:  # TCP: source connects to sink's listening client_port
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.connect((sess.dest or addr[0], sess.client_port))
                except OSError as e:
                    print(f"[mock] data connect failed: {e}")
                    return
                send = lambda b: sock.sendall(b)
            sess.data_sock = sock
            print(f"[mock] data channel {sess.lower} -> "
                  f"{sess.dest or addr[0]}:{sess.client_port}")

            interval = 1.0 / self.rate if self.rate > 0 else 0.0
            source = (self._ch10_messages if self.ch10_path
                      else self._synthetic_messages)
            sent = 0
            try:
                for msg in source(sess):
                    if sess.stop.is_set():
                        break
                    while not sess.streaming.is_set() and not sess.stop.is_set():
                        time.sleep(0.02)     # paused
                    if sess.stop.is_set():
                        break
                    send(msg)
                    sent += 1
                    if interval:
                        time.sleep(interval)
                else:
                    # generator exhausted (Chapter 11 file finished) -> End-of-Data
                    if not sess.stop.is_set():
                        send(build_datamsg(0, 0, end_of_data=True))
                        print(f"[mock] End-of-Data after {sent} message(s)")
            except OSError as e:
                print(f"[mock] data send stopped: {e}")
            finally:
                sock.close()

        sess.thread = threading.Thread(target=run, daemon=True)
        sess.thread.start()

    def _synthetic_messages(self, sess):
        """Yield synthetic TmNSDataMessages (two Packages each).

        Runs until stop; a bounded Range ends the stream with End-of-Data.
        Honors the request URI's MDID filter (Ch.26 26.4.1.4).
        """
        if not mdid_requested(sess.mdid_intervals, self.mdid):
            return                       # configured MDID not requested
        seq = 0
        while not sess.stop.is_set():
            t = time.time()
            m0 = struct.pack(">Qdi", seq, t, seq * seq) + b"\xa5" * 4
            m1 = struct.pack(">Qf", seq, float(seq) / 10.0)
            pkgs = [build_package(self.pdid, m0, time_delta=seq * 1000),
                    build_package(self.pdid + 1, m1, time_delta=seq * 1000)]
            yield build_datamsg(self.mdid, seq, packages=pkgs,
                                options=build_options(len(pkgs)))
            seq += 1
            if sess.bounded and seq >= 25:
                # bounded Range: stream ends; run() emits the End-of-Data marker
                return

    def _split_packages(self, pdid, body, status, time_delta):
        """Split a Chapter 11 body into standard Packages (Appendix 24-A A.1.d(2)).

        Each Package's payload is <= the 16-bit PackageLength limit and small
        enough that a message of packages stays under max_msg_bytes.
        """
        max_body = min(MAX_PACKAGE_LEN - STD_PKG_HEADER_LEN,
                       self.max_msg_bytes - DATAMSG_HEADER_LEN - OPTIONS_LEN
                       - STD_PKG_HEADER_LEN)
        max_body = max(1, max_body)
        chunks = [body[i:i + max_body] for i in range(0, len(body), max_body)] \
            or [b""]
        return [build_package(pdid, c, status=status, time_delta=time_delta)
                for c in chunks]

    def _pack_messages(self, mdid, packages, seqs, ts):
        """Group Packages into TmNSDataMessages within max_msg_bytes; yield bytes.

        MessageDefinitionSequenceNumber is a monotonic per-MDID counter
        (Ch.26 26.5.1), which supersedes the appendix's notional
        "Ch11 seq -> low 8 bits" guidance. All messages derived from one
        Chapter 11 packet share the same MessageTimestamp.
        """
        group, size = [], DATAMSG_HEADER_LEN + OPTIONS_LEN
        for pkg in packages:
            if group and size + len(pkg) > self.max_msg_bytes:
                seq = seqs.get(mdid, 0); seqs[mdid] = seq + 1
                yield build_datamsg(mdid, seq, packages=group,
                                    options=build_options(len(group)),
                                    playback=True, ts=ts)
                group, size = [], DATAMSG_HEADER_LEN + OPTIONS_LEN
            group.append(pkg); size += len(pkg)
        if group:
            seq = seqs.get(mdid, 0); seqs[mdid] = seq + 1
            yield build_datamsg(mdid, seq, packages=group,
                                options=build_options(len(group)),
                                playback=True, ts=ts)

    def _ch10_messages(self, sess):
        """Yield TmNSDataMessages built from a Chapter 11 recording.

        Each Chapter 11 packet is mapped per Chapter 24 Appendix 24-A: the body
        becomes one or more standard Packages (split across Packages/messages
        when it exceeds the 16-bit PackageLength or max_msg_bytes), the Packet
        Flags map to PackageStatusFlags, and the secondary-header absolute time
        maps to MessageTimestamp. The request URI's MDID list filters output.
        """
        seqs = {}
        while not sess.stop.is_set():
            with open(self.ch10_path, "rb") as f:
                for pkt in chapter11.iter_packets(f):
                    if sess.stop.is_set():
                        return
                    mdid, pdid, body = chapter11.map_to_tmns(pkt, self.mdid_upper)
                    if not mdid_requested(sess.mdid_intervals, mdid):
                        continue
                    ts, _abs = chapter11.message_timestamp(pkt)
                    packages = self._split_packages(
                        pdid, body, pkt.flags & 0xFF, pkt.rtc & 0xFFFFFFFF)
                    yield from self._pack_messages(mdid, packages, seqs, ts)
            if not self.loop:
                return
            # loop: replay the file until stopped (no End-of-Data between loops)


def main():
    ap = argparse.ArgumentParser(description="Mock TmNS RTSPDataSource for testing.")
    ap.add_argument("--port", type=int, default=55554)
    ap.add_argument("--mdid", type=int, default=1,
                    help="MessageDefinitionID to emit (synthetic mode)")
    ap.add_argument("--pdid", type=int, default=100,
                    help="PackageDefinitionID to emit (synthetic mode)")
    ap.add_argument("--rate", type=float, default=20.0,
                    help="messages per second (0 = as fast as possible)")
    ap.add_argument("--session-timeout", type=int, default=60,
                    help="RTSP session timeout advertised/enforced (seconds)")
    ap.add_argument("--ch10", metavar="FILE",
                    help="play back an IRIG-106 Chapter 11 (.ch10/.c10) recording, "
                         "mapping packets to TmNS per Chapter 24 Appendix 24-A")
    ap.add_argument("--mdid-upper", type=int, default=0,
                    help="user-defined upper 16 bits of the MDID for Ch11 playback "
                         "(lower 16 bits come from the Channel ID; default 0)")
    ap.add_argument("--loop", action="store_true",
                    help="replay the Chapter 11 file continuously until stopped")
    ap.add_argument("--max-msg-bytes", type=int, default=DEFAULT_MAX_MSG_BYTES,
                    help="cap each TmNSDataMessage size; larger Chapter 11 bodies "
                         "are split across Packages/messages "
                         f"(default {DEFAULT_MAX_MSG_BYTES})")
    ap.add_argument("--no-get-parameter", action="store_true",
                    help="reject GET_PARAMETER with 501 (simulate a server that "
                         "lacks it, to test OPTIONS-based keep-alive)")
    args = ap.parse_args()
    MockSource(args.port, args.mdid, args.pdid, args.rate,
               args.session_timeout, ch10_path=args.ch10,
               mdid_upper=args.mdid_upper, loop=args.loop,
               max_msg_bytes=args.max_msg_bytes,
               no_get_parameter=args.no_get_parameter).serve()


if __name__ == "__main__":
    main()
