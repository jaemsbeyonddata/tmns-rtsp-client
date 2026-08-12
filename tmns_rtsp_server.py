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
tmns_rtsp_server.py - a TmNS RTSPDataSource that plays back .tmns recordings.

Implements the RCC/IRIG 106 Chapter 26 RTSPControlChannel (OPTIONS, SETUP,
PLAY, PAUSE, TEARDOWN; also DESCRIBE, GET_PARAMETER) and streams recorded
TmNSDataMessages over the RTSPDataChannel (UDP or a separate TCP channel).

On PLAY it reads the Range header (ptp-clock=start-end), uses the .index file
to binary-search the start offset in the matching .tmns file, and streams the
messages whose MessageTimestamp falls in the range (filtered by the request
URI's MDID list), ending with an End-of-Data indication.

Two modes:
  * single pair:   --tmns sample.tmns --index sample.index
  * folder:        --tmns-dir DIR [--index-dir DIR2]
                   (each X.tmns is paired with X.index; recordings are played
                    back in time order across files)

Usage:
    python3 tmns_rtsp_server.py --tmns sample.tmns --index sample.index
    python3 tmns_rtsp_server.py --tmns-dir recs/ --index-dir idx/ --port 55554
"""

import argparse
import glob
import os
import re
import socket
import struct
import threading
import time

import tmns_message as tm

RANGE_RE = re.compile(
    r"ptp-clock\s*=\s*(start|now|\d+)\s*-\s*(end|now|\d+)?\s*$", re.I)
MDID_TOKEN_RE = re.compile(r"&(\d+)(?:-(\d+))?")


def parse_requested_mdids(uri):
    """List of (lo, hi) MDID intervals from a request URI ([] = all)."""
    out = []
    for m in MDID_TOKEN_RE.finditer(uri):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        out.append((min(a, b), max(a, b)))
    return out


def mdid_requested(intervals, mdid):
    return not intervals or any(lo <= mdid <= hi for lo, hi in intervals)


# ----- recordings ---------------------------------------------------------

class Recording:
    def __init__(self, tmns_path, index_path):
        self.tmns_path = tmns_path
        self.index_path = index_path
        if not os.path.exists(tmns_path):
            raise ValueError(f"{tmns_path}: no such .tmns file")
        if not os.path.exists(index_path):
            raise ValueError(f"{tmns_path}: no corresponding index {index_path}")
        self.index = tm.Index(index_path)
        self.index.verify_matches(tmns_path)   # one .tmns <-> one .index

    @property
    def start_ts(self):
        return self.index.start_ts

    @property
    def end_ts(self):
        return self.index.end_ts

    def messages(self, start_ts, end_ts, mdids):
        """Yield raw message bytes with start_ts <= timestamp <= end_ts."""
        if not len(self.index) or self.end_ts < start_ts or self.start_ts > end_ts:
            return
        off = self.index.offset_at_or_after(start_ts)
        if off is None:
            return
        with open(self.tmns_path, "rb") as f:
            for _pos, h, raw in tm.iter_messages(f, off):
                ts = h["timestamp"]
                if ts > end_ts:
                    return
                if ts < start_ts:
                    continue
                if mdid_requested(mdids, h["mdid"]):
                    yield raw


class Library:
    """One or more recordings, played back in time order."""

    def __init__(self, recordings):
        self.recordings = sorted(recordings, key=lambda r: r.start_ts)

    @property
    def start_ts(self):
        return min((r.start_ts for r in self.recordings), default=0)

    @property
    def end_ts(self):
        return max((r.end_ts for r in self.recordings), default=0)

    @property
    def mdids(self):
        s = set()
        for r in self.recordings:
            s |= r.index.mdids
        return s

    def total_messages(self):
        return sum(len(r.index) for r in self.recordings)

    def messages(self, start_ts, end_ts, mdids):
        for r in self.recordings:               # already time-ordered
            yield from r.messages(start_ts, end_ts, mdids)


def parse_range(rng, lib):
    """Return (start_ts, end_ts) for a ptp-clock range, or None if invalid."""
    m = RANGE_RE.match(rng.strip())
    if not m:
        return None
    s, e = m.group(1), (m.group(2) or "")

    def resolve(tok, default):
        tok = tok.lower()
        if tok == "":
            return default
        if tok == "start":
            return lib.start_ts
        if tok == "end":
            return lib.end_ts
        if tok == "now":
            return tm.now_timestamp()
        return int(tok)

    start = resolve(s, lib.start_ts)
    end = resolve(e, lib.end_ts)
    if end < start:
        return None
    return start, end


# ----- RTSP session + server ----------------------------------------------

class Session:
    def __init__(self, sid):
        self.sid = sid
        self.lower = "UDP"
        self.dest = None
        self.client_port = None
        self.streaming = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.last_activity = time.time()
        self.mdids = []
        self.range = None          # (start_ts, end_ts)
        self.speed = 1.0


class TmnsRtspServer:
    KNOWN_METHODS = {"OPTIONS", "DESCRIBE", "SETUP", "PLAY", "PAUSE",
                     "TEARDOWN", "GET_PARAMETER"}

    def __init__(self, library, port, session_timeout=60, speed=1.0, asap=False):
        self.lib = library
        self.port = port
        self.session_timeout = session_timeout
        self.speed = speed
        self.asap = asap
        self.sessions = {}
        self._sid = 0x2000

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        print(f"[server] TmNS RTSPDataSource on TCP :{self.port}")
        print(f"[server] {len(self.lib.recordings)} recording(s), "
              f"{self.lib.total_messages()} messages, MDIDs {sorted(self.lib.mdids)}")
        print(f"[server] time span (PTP s): {tm.ts_to_seconds(self.lib.start_ts):.3f}"
              f" .. {tm.ts_to_seconds(self.lib.end_ts):.3f}")
        try:
            while True:
                conn, addr = srv.accept()
                threading.Thread(target=self._handle, args=(conn, addr),
                                 daemon=True).start()
        except KeyboardInterrupt:
            print("\n[server] shutting down")
        finally:
            srv.close()

    def _handle(self, conn, addr):
        print(f"[server] control connection from {addr}")
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
            print(f"[server] control connection {addr} closed")

    def _reply(self, conn, cseq, code, reason, extra=None, body=b""):
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: {cseq}",
                 "Server: TmNS-RTSP-Server/1.0"]
        if extra:
            lines += [f"{k}: {v}" for k, v in extra.items()]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        conn.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)

    def _touch(self, headers):
        sid = headers.get("session", "").split(";")[0].strip()
        if sid in self.sessions:
            self.sessions[sid].last_activity = time.time()

    def _lookup(self, headers):
        sid = headers.get("session", "").split(";")[0].strip()
        if not sid or sid not in self.sessions:
            return None, 454
        sess = self.sessions[sid]
        if time.time() - sess.last_activity > self.session_timeout:
            sess.stop.set()
            self.sessions.pop(sid, None)
            print(f"[server] session {sid} expired")
            return None, 454
        sess.last_activity = time.time()
        return sess, 0

    def _dispatch(self, conn, method, uri, headers, addr, sess):
        cseq = headers.get("cseq", "0")
        if method not in self.KNOWN_METHODS:
            self._reply(conn, cseq, 501, "Not Implemented")
            return sess
        self._touch(headers)

        if method == "OPTIONS":
            self._reply(conn, cseq, 200, "OK",
                        {"Public": "OPTIONS, DESCRIBE, SETUP, TEARDOWN, "
                                   "PLAY, PAUSE, GET_PARAMETER"})
            return sess

        if method == "DESCRIBE":
            sdp = (b"v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=TmNS Recording Playback\r\n"
                   b"m=application 0 TMNS/TMNSP 0\r\na=control:%s\r\n" % uri.encode())
            self._reply(conn, cseq, 200, "OK",
                        {"Content-Type": "application/sdp"}, sdp)
            return sess

        if method == "SETUP":
            transport = headers.get("transport", "")
            if "TMNS/TMNSP" not in transport.upper():
                self._reply(conn, cseq, 461, "Unsupported Transport")
                return sess
            self._sid += 1
            sess = Session(f"{self._sid:08X}")
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

        sess, err = self._lookup(headers)
        if err:
            self._reply(conn, cseq, 454, "Session Not Found")
            return None

        if method == "PLAY":
            rng = headers.get("range", "")
            if rng:
                parsed = parse_range(rng, self.lib)
                if parsed is None:
                    self._reply(conn, cseq, 457, "Invalid Range")
                    return sess
                sess.range = parsed
            elif sess.range is None:
                sess.range = (self.lib.start_ts, self.lib.end_ts)
            sess.mdids = parse_requested_mdids(uri)
            sp = headers.get("speed", "")
            if sp:
                try:
                    sess.speed = float(sp)
                except ValueError:
                    pass
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

    def _start_stream(self, sess, addr):
        if sess.thread and sess.thread.is_alive():
            sess.streaming.set()             # resume
            return
        sess.streaming.set()
        start_ts, end_ts = sess.range

        def run():
            if sess.lower == "UDP":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                target = (sess.dest or addr[0], sess.client_port)
                send = lambda b: sock.sendto(b, target)
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.connect((sess.dest or addr[0], sess.client_port))
                except OSError as e:
                    print(f"[server] data connect failed: {e}")
                    return
                send = lambda b: sock.sendall(b)
            print(f"[server] data {sess.lower} -> {sess.dest or addr[0]}:"
                  f"{sess.client_port}  range {tm.ts_to_seconds(start_ts):.3f}.."
                  f"{tm.ts_to_seconds(end_ts):.3f}")

            asap = self.asap or sess.speed <= 0
            sent = 0
            prev_ts = None
            try:
                for raw in self.lib.messages(start_ts, end_ts, sess.mdids):
                    while not sess.streaming.is_set() and not sess.stop.is_set():
                        time.sleep(0.02)         # paused
                    if sess.stop.is_set():
                        break
                    if not asap and prev_ts is not None:
                        dt = (tm.ts_to_seconds(tm.parse_header(raw)["timestamp"])
                              - tm.ts_to_seconds(prev_ts)) / sess.speed
                        if dt > 0:
                            time.sleep(min(dt, 2.0))   # cap to avoid long stalls
                    send(raw)
                    prev_ts = tm.parse_header(raw)["timestamp"]
                    sent += 1
                else:
                    if not sess.stop.is_set():
                        send(tm.build_datamsg(0, 0, end_of_data=True))
                        print(f"[server] End-of-Data after {sent} message(s)")
            except OSError as e:
                print(f"[server] data send stopped: {e}")
            finally:
                sock.close()

        sess.thread = threading.Thread(target=run, daemon=True)
        sess.thread.start()


# ----- recording discovery + CLI ------------------------------------------

def load_recordings(args):
    recs = []
    if args.tmns:
        index = args.index or (os.path.splitext(args.tmns)[0] + ".index")
        try:
            recs.append(Recording(args.tmns, index))
        except (ValueError, OSError) as e:
            raise SystemExit(f"error: {e}")
    elif args.tmns_dir:
        index_dir = args.index_dir or args.tmns_dir
        tmns_files = sorted(glob.glob(os.path.join(args.tmns_dir, "*.tmns")))
        # one .tmns must map to exactly one .index; verify and pair strictly
        paired_indexes = set()
        for path in tmns_files:
            base = os.path.splitext(os.path.basename(path))[0]
            idx = os.path.join(index_dir, base + ".index")
            try:
                recs.append(Recording(path, idx))
                paired_indexes.add(os.path.abspath(idx))
            except (ValueError, OSError) as e:
                print(f"[server] skip {path}: {e}")
        # warn about orphan .index files that pair with no .tmns
        for idx in sorted(glob.glob(os.path.join(index_dir, "*.index"))):
            if os.path.abspath(idx) not in paired_indexes:
                base = os.path.splitext(os.path.basename(idx))[0]
                if not os.path.exists(os.path.join(args.tmns_dir, base + ".tmns")):
                    print(f"[server] warning: orphan index {idx} (no matching "
                          f"{base}.tmns in {args.tmns_dir})")
    if not recs:
        raise SystemExit("no valid recordings found (use --tmns/--index or "
                         "--tmns-dir; each .tmns needs its matching .index)")
    print(f"[server] loaded {len(recs)} recording(s), each with a verified index")
    return recs


def main():
    ap = argparse.ArgumentParser(
        description="TmNS RTSP server that plays back .tmns recordings.")
    ap.add_argument("--tmns", help="a single .tmns recording file")
    ap.add_argument("--index", help="its .index (default: <tmns basename>.index)")
    ap.add_argument("--tmns-dir", help="folder of .tmns files to play back")
    ap.add_argument("--index-dir", help="folder of matching .index files "
                    "(default: same as --tmns-dir)")
    ap.add_argument("--port", type=int, default=55554,
                    help="RTSP control port (default 55554)")
    ap.add_argument("--session-timeout", type=int, default=60)
    ap.add_argument("--speed", type=float, default=1.0,
                    help="playback speed multiplier for real-time pacing "
                         "(default 1.0; a client Speed header overrides it)")
    ap.add_argument("--asap", action="store_true",
                    help="ignore timestamps and send as fast as possible")
    args = ap.parse_args()

    library = Library(load_recordings(args))
    TmnsRtspServer(library, args.port, args.session_timeout,
                   args.speed, args.asap).serve()


if __name__ == "__main__":
    main()
