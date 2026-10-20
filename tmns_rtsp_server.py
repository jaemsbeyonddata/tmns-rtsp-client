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
import bisect
import glob
import heapq
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

    @property
    def time_ordered(self):
        return self.index.time_ordered

    def byte_range(self, start_ts, end_ts):
        """(first offset, end offset, message count) for a time range, or None.

        Only for a time-ordered recording, where the range is one contiguous
        run of bytes, found by two binary searches of the index.
        """
        idx = self.index
        if not len(idx) or self.end_ts < start_ts or self.start_ts > end_ts:
            return None
        lo = bisect.bisect_left(idx._ts, start_ts)
        hi = bisect.bisect_right(idx._ts, end_ts)
        if lo >= hi:
            return None
        end = idx.entries[hi][1] if hi < len(idx) else os.path.getsize(self.tmns_path)
        return idx.entries[lo][1], end, hi - lo

    def messages(self, start_ts, end_ts, mdids):
        """Yield (timestamp, raw message) with start_ts <= timestamp <= end_ts.

        raw is a memoryview into a large read buffer (see tm.iter_raw).
        """
        if not len(self.index) or self.end_ts < start_ts or self.start_ts > end_ts:
            return
        if not self.time_ordered:
            # read in index (timestamp) order; the index's MDIDs filter
            # before anything is read
            lo, hi = self.index.span(start_ts, end_ts)
            entries = (e for e in self.index.iter_entries(lo, hi)
                       if mdid_requested(mdids, e[2]))
            with open(self.tmns_path, "rb", buffering=0) as f:
                for ts, _mdid, raw in tm.iter_indexed(f, entries):
                    yield ts, raw
            return
        rng = self.byte_range(start_ts, end_ts)
        if rng is None:
            return
        with open(self.tmns_path, "rb", buffering=0) as f:
            for ts, mdid, raw in tm.iter_raw(f, rng[0], rng[1]):
                if start_ts <= ts <= end_ts and mdid_requested(mdids, mdid):
                    yield ts, raw


class Library:
    """One or more recordings, played back in time order.

    Recordings whose time spans overlap form a group whose messages are
    merged by timestamp; groups play one after another.
    """

    def __init__(self, recordings):
        self.recordings = sorted(recordings, key=lambda r: r.start_ts)
        self.groups = []
        group_end = None
        for r in self.recordings:
            if not len(r.index):
                continue
            if self.groups and r.start_ts <= group_end:
                self.groups[-1].append(r)
                group_end = max(group_end, r.end_ts)
            else:
                self.groups.append([r])
                group_end = r.end_ts

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

    def mdid_summary(self, limit=50_000_000):
        """Sorted MDIDs, unless the index is too big to scan at start-up."""
        n = self.total_messages()
        if n > limit:
            return f"(not scanned: {n:,} index entries)"
        return str(sorted(self.mdids))

    def messages(self, start_ts, end_ts, mdids, groups=None):
        """Yield (timestamp, raw message) in timestamp order."""
        for group in self.groups if groups is None else groups:
            if len(group) == 1:
                yield from group[0].messages(start_ts, end_ts, mdids)
            else:
                yield from heapq.merge(
                    *(r.messages(start_ts, end_ts, mdids) for r in group),
                    key=lambda m: m[0])


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

    def __init__(self, library, port, session_timeout=60, speed=1.0, asap=False,
                 max_sessions=0):
        self.lib = library
        self.port = port
        self.session_timeout = session_timeout
        self.speed = speed
        self.asap = asap
        self.max_sessions = max_sessions      # 0 = unlimited
        self.sessions = {}
        self._lock = threading.Lock()         # session count + id allocation
        self._sid = 0x2000

    def serve(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        print(f"[server] TmNS RTSPDataSource on TCP :{self.port}")
        print(f"[server] {len(self.lib.recordings)} recording(s), "
              f"{self.lib.total_messages():,} messages, MDIDs {self.lib.mdid_summary()}")
        print(f"[server] time span (PTP s): {tm.ts_to_seconds(self.lib.start_ts):.3f}"
              f" .. {tm.ts_to_seconds(self.lib.end_ts):.3f}")
        # (an index without the order flag is checked here, not on PLAY)
        for r in self.lib.recordings:
            if not r.time_ordered:
                print(f"[server] {os.path.basename(r.tmns_path)}: not in time "
                      f"order; read in index order")
        merged = sum(len(g) for g in self.lib.groups if len(g) > 1)
        if merged:
            print(f"[server] {merged} recording(s) overlap in time; their "
                  f"messages are merged by timestamp")
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
        used = set()                    # sessions set up/used on this connection
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
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise ConnectionError
                    buf += chunk
                buf = buf[clen:]
                sess = self._dispatch(conn, method.upper(), uri, headers, addr, sess)
                if sess is not None:
                    used.add(sess.sid)
        except (ConnectionError, OSError):
            pass
        finally:
            # the stream stops with its control connection, so its session
            # (and any earlier one set up without a TEARDOWN) ends too
            for sid in used:
                s = self.sessions.pop(sid, None)
                if s:
                    s.stop.set()
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

    def _expire_idle(self):
        """Drop sessions idle past the timeout (caller holds self._lock)."""
        now = time.time()
        for sid, s in list(self.sessions.items()):
            if now - s.last_activity > self.session_timeout:
                s.stop.set()
                self.sessions.pop(sid, None)
                print(f"[server] session {sid} expired")

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
            if headers.get("session"):
                # SETUP on an existing session changes its transport (RFC 2326
                # 10.4); refused while its stream runs (playing or paused)
                sess, err = self._lookup(headers)
                if err:
                    self._reply(conn, cseq, 454, "Session Not Found")
                    return None
                if sess.thread and sess.thread.is_alive():
                    self._reply(conn, cseq, 455, "Method Not Valid in This State")
                    return sess
            else:
                with self._lock:
                    self._expire_idle()
                    if self.max_sessions and len(self.sessions) >= self.max_sessions:
                        print(f"[server] SETUP refused: {len(self.sessions)} "
                              f"session(s), --max-sessions {self.max_sessions}")
                        self._reply(conn, cseq, 503, "Service Unavailable")
                        return sess
                    self._sid += 1
                    sess = Session(f"{self._sid:08X}")
                    sess.speed = self.speed
                    self.sessions[sess.sid] = sess
            m = re.search(r"TMNS/TMNSP/(TCP|UDP)", transport, re.I)
            if m:
                sess.lower = m.group(1).upper()
            m = re.search(r"client_port=(\d+)", transport)
            if m:
                sess.client_port = int(m.group(1))
            m = re.search(r"destination=([\d.]+)", transport)
            sess.dest = m.group(1) if m else addr[0]
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

    SEND_CHUNK = 8 << 20        # sendfile() bytes between pause/stop checks
    TCP_BATCH = 256 << 10       # coalesced TCP bytes per send
    BATCH_AGE = 0.005           # seconds a paced TCP batch may wait to fill
    MAX_GAP = 2.0               # longest wait for a timestamp gap (seconds)
    SNDBUF = 8 << 20            # requested data-socket send buffer

    def _start_stream(self, sess, addr):
        if sess.thread and sess.thread.is_alive():
            sess.streaming.set()             # resume
            return
        sess.streaming.set()
        start_ts, end_ts = sess.range

        def run():
            target = (sess.dest or addr[0], sess.client_port)
            if sess.lower == "UDP":
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            else:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.SNDBUF)
            except OSError:
                pass
            if sess.lower != "UDP":
                try:
                    sock.connect(target)
                except OSError as e:
                    print(f"[server] data connect failed: {e}")
                    sock.close()
                    return
            print(f"[server] data {sess.lower} -> {target[0]}:{target[1]}  range "
                  f"{tm.ts_to_seconds(start_ts):.3f}..{tm.ts_to_seconds(end_ts):.3f}")

            asap = self.asap or sess.speed <= 0
            t0 = time.monotonic()
            try:
                if sess.lower == "TCP" and asap and not sess.mdids:
                    sent, nbytes, done = self._send_tcp_asap(sess, sock, start_ts, end_ts)
                else:
                    sent, nbytes, done = self._send_messages(
                        sess, sock, target,
                        self.lib.messages(start_ts, end_ts, sess.mdids), asap)
                if done:
                    eod = tm.build_datamsg(0, 0, end_of_data=True)
                    if sess.lower == "UDP":
                        sock.sendto(eod, target)
                    else:
                        sock.sendall(eod)
                    dt = time.monotonic() - t0
                    print(f"[server] End-of-Data after {sent:,} message(s), "
                          f"{nbytes / 1e6:,.1f} MB in {dt:.1f}s "
                          f"({nbytes / 1e6 / dt if dt > 0 else 0:,.1f} MB/s)")
            except OSError as e:
                print(f"[server] data send stopped: {e}")
            finally:
                sock.close()

        sess.thread = threading.Thread(target=run, daemon=True)
        sess.thread.start()

    @staticmethod
    def _wait_playing(sess):
        """Block while paused; False once the session is stopped."""
        while not sess.streaming.wait(0.05):
            if sess.stop.is_set():
                return False
        return not sess.stop.is_set()

    def _send_tcp_asap(self, sess, sock, start_ts, end_ts):
        """Unfiltered TCP as fast as possible.  A time-ordered recording that
        overlaps no other is copied straight to the socket with sendfile()
        (no per-message work in Python); other groups are merged and sent
        message by message."""
        sent = nbytes = 0
        for group in self.lib.groups:
            rec = group[0]
            if len(group) > 1 or not rec.time_ordered:
                n, b, done = self._send_messages(
                    sess, sock, None,
                    self.lib.messages(start_ts, end_ts, [], [group]), True)
                sent, nbytes = sent + n, nbytes + b
                if not done:
                    return sent, nbytes, False
                continue
            rng = rec.byte_range(start_ts, end_ts)
            if rng is None:
                continue
            off, end, count = rng
            with open(rec.tmns_path, "rb") as f:
                while off < end:
                    if not self._wait_playing(sess):
                        return sent, nbytes, False
                    n = sock.sendfile(f, off, min(self.SEND_CHUNK, end - off))
                    if n == 0:
                        raise OSError("sendfile sent nothing")
                    off += n
                    nbytes += n
            sent += count
        return sent, nbytes, True

    def _send_messages(self, sess, sock, target, msgs, asap):
        """Send (timestamp, message) pairs one by one: UDP (one datagram
        each), MDID-filtered or merged TCP, or real-time pacing.  TCP writes
        are coalesced into batches."""
        udp = sess.lower == "UDP"
        speed = sess.speed if sess.speed > 0 else 1.0
        sendto = sock.sendto
        batch, size, batch_t = [], 0, 0.0
        sent = nbytes = 0
        base = None                          # (wall time, message time) anchor
        mono, sleep = time.monotonic, time.sleep

        def flush():
            nonlocal batch, size
            if batch:
                sock.sendall(b"".join(batch))
                batch, size = [], 0

        for ts, raw in msgs:
            if not sess.streaming.is_set():
                flush()
                if not self._wait_playing(sess):
                    return sent, nbytes, False
                base = None                  # resume paced from here
            if sess.stop.is_set():
                return sent, nbytes, False
            if not asap:
                # a fixed schedule (not per-gap sleeps) so timing errors
                # don't accumulate over hours of playback
                t = (ts >> 32) + (ts & 0xFFFFFFFF) * 1e-9
                now = mono()
                if base is None:
                    base = (now, t)
                wait = base[0] + (t - base[1]) / speed - now
                if wait > self.MAX_GAP:      # cap long gaps in the recording
                    base = (base[0] - (wait - self.MAX_GAP), base[1])
                    wait = self.MAX_GAP
                if wait > 0.0005:
                    flush()
                    sleep(wait)
                elif batch and now - batch_t > self.BATCH_AGE:
                    flush()
            if udp:
                sendto(raw, target)
            else:
                if not batch:
                    batch_t = mono() if not asap else 0.0
                batch.append(raw)
                size += len(raw)
                if size >= self.TCP_BATCH:
                    flush()
            sent += 1
            nbytes += len(raw)
        flush()
        return sent, nbytes, True


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
    ap.add_argument("--max-sessions", type=int, default=0,
                    help="most RTSP sessions open at once; a SETUP beyond it gets "
                         "503 Service Unavailable (default 0 = unlimited)")
    ap.add_argument("--asap", action="store_true",
                    help="ignore timestamps and send as fast as possible")
    args = ap.parse_args()

    library = Library(load_recordings(args))
    TmnsRtspServer(library, args.port, args.session_timeout,
                   args.speed, args.asap, args.max_sessions).serve()


if __name__ == "__main__":
    main()
