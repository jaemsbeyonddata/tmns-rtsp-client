#!/usr/bin/env python3
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
  * On PLAY, streams synthetic TmNSDataMessages (Chapter 24 header) built with
    a standard PackageHeader and an ApplicationDefinedFields option, so the
    client's payload decoder has something real to parse.  A bounded Range
    finishes with an EndOfDataFlag message.

Usage:
    python3 tmns_mock_server.py [--port 55554] [--mdid 1] [--pdid 100]
                                [--rate 20] [--session-timeout 60]
"""

import argparse
import re
import socket
import struct
import threading
import time

DATAMSG_HEADER_LEN = 24
STD_PKG_HEADER_LEN = 12

RANGE_RE = re.compile(
    r"ptp-clock\s*=\s*(start|now|\d+)\s*-\s*(end|now|\d+)?\s*$", re.I)


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
                  end_of_data: bool = False, playback: bool = False) -> bytes:
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
    ts = int(time.time() * 1e9)
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


class MockSource:
    def __init__(self, port, mdid, pdid, rate, session_timeout):
        self.port = port
        self.mdid = mdid
        self.pdid = pdid
        self.rate = rate
        self.session_timeout = session_timeout
        self.sessions = {}
        self._sid_counter = 0x1000

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        print(f"[mock] TmNS RTSPDataSource listening on TCP :{self.port} "
              f"(mdid={self.mdid}, pdid={self.pdid}, rate={self.rate}/s, "
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

    def _dispatch(self, conn, method, uri, headers, addr, sess):
        cseq = headers.get("cseq", "0")

        # Unknown / unimplemented methods are rejected independently of any
        # session state (method recognition precedes session validation).
        if method not in self.KNOWN_METHODS:
            self._reply(conn, cseq, 501, "Not Implemented")
            return sess

        if method == "OPTIONS":
            self._reply(conn, cseq, 200, "OK",
                        {"Public": "OPTIONS, DESCRIBE, SETUP, TEARDOWN, "
                                   "PLAY, PAUSE, GET_PARAMETER"})
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

            seq = 0
            interval = 1.0 / self.rate if self.rate > 0 else 0.05
            sent = 0
            try:
                while not sess.stop.is_set():
                    if not sess.streaming.is_set():
                        time.sleep(0.02)
                        continue
                    # two Packages of synthetic MeasurementData
                    t = time.time()
                    m0 = struct.pack(">Qdi", seq, t, seq * seq) + b"\xa5" * 4
                    m1 = struct.pack(">Qf", seq, float(seq) / 10.0)
                    pkgs = [build_package(self.pdid, m0, time_delta=seq * 1000),
                            build_package(self.pdid + 1, m1, time_delta=seq * 1000)]
                    msg = build_datamsg(self.mdid, seq, packages=pkgs,
                                        options=build_options(len(pkgs)))
                    send(msg)
                    seq += 1
                    sent += 1
                    time.sleep(interval)
                    if sess.bounded and sent >= 25:
                        send(build_datamsg(0, 0, end_of_data=True))
                        print("[mock] sent End-of-Data indication")
                        break
            except OSError as e:
                print(f"[mock] data send stopped: {e}")
            finally:
                sock.close()

        sess.thread = threading.Thread(target=run, daemon=True)
        sess.thread.start()


def main():
    ap = argparse.ArgumentParser(description="Mock TmNS RTSPDataSource for testing.")
    ap.add_argument("--port", type=int, default=55554)
    ap.add_argument("--mdid", type=int, default=1, help="MessageDefinitionID to emit")
    ap.add_argument("--pdid", type=int, default=100, help="PackageDefinitionID to emit")
    ap.add_argument("--rate", type=float, default=20.0, help="messages per second")
    ap.add_argument("--session-timeout", type=int, default=60,
                    help="RTSP session timeout advertised/enforced (seconds)")
    args = ap.parse_args()
    MockSource(args.port, args.mdid, args.pdid, args.rate,
               args.session_timeout).serve()


if __name__ == "__main__":
    main()
