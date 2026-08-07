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
tmns_rtsp_client.py - RTSP client / conformance tester for IRIG-106 TmNS
RTSP servers (RTSPDataSources).

Implements the RTSPControlChannel defined in RCC/IRIG 106 Chapter 26
("TmNS Data Message Transport"), which layers a small set of conventions on
top of RTSP 1.0 (RFC 2326):

  * Control channel runs over TCP.  RTSPDataSink = client, RTSPDataSource =
    server.  Default server port 55554 (MDL ListeningPort).
  * Required methods: OPTIONS, SETUP, TEARDOWN, PLAY, PAUSE.
  * Transport header uses the TmNS profile:
        TMNS/TMNSP/UDP;unicast;destination=<ip>;client_port=<p>[-<p>]
        TMNS/TMNSP/TCP;unicast;client_port=<p>
    NOTE: unlike RFC 2326, lower-transport "TCP" means a *separate* TCP data
    channel (no interleaving); the sink listens and the source connects.
  * Range header supports the PTP form:  ptp-clock=<start>-<end>
    where start/end are "start"|"now"|<ptp-timestamp> and "end"|"now".
  * PLAY may carry Speed or Bandwidth (mutually exclusive per spec).
  * The request URI is the TmNS_Request_Defined_URI encoding MDID/PDID/MeasID
    lists, destination, playback and time options, and delivery DSCP.

Data is delivered as TmNSDataMessages (Chapter 24).  The fixed 24-byte
TmNSMessageHeader (big-endian) is:

    word0: MessageVersion(4) OptionWordCount(4) Reserved(4) MessageType(4)
           MessageFlags(16)
    word1: MessageDefinitionID(32)
    word2: MessageDefinitionSequenceNumber(32)
    word3: MessageLength(32)                 (bytes, whole message incl header)
    word4-5: MessageTimestamp(64)

MessageFlags bit 0 = EndOfDataFlag, bit 6 = PlaybackDataFlag.

This tool has no third-party dependencies (Python 3.7+ stdlib only) and runs
on Linux.

Author: generated for TmNS RTSP interoperability testing.
"""

import argparse
import errno
import os
import re
import selectors
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

DEFAULT_PORT = 55554  # RCC 106 Ch.26: default RTSPControlChannel TCP port
HISTORY_FILE = os.path.expanduser("~/.tmns_rtsp_history")
DATAMSG_HEADER_LEN = 24
USER_AGENT = "TmNS-RTSP-Client/1.0"

# ----- ANSI helpers -------------------------------------------------------

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    BLUE = "\033[34m"; CYAN = "\033[36m"; MAGENTA = "\033[35m"

    enabled = sys.stdout.isatty()

    @classmethod
    def wrap(cls, s, color):
        if not cls.enabled:
            return s
        return f"{color}{s}{cls.RESET}"


# ----- optional file logging ----------------------------------------------
# Everything printed via cprint (wire dumps, status, warnings) is also written
# to the log file when one is open, without ANSI colors and with a timestamp.
_log_fh = None
_log_path = None
_log_lock = threading.Lock()


def open_logfile(path: Optional[str] = None) -> str:
    """Open (or switch to) a log file; returns the path used."""
    global _log_fh, _log_path
    close_logfile()
    if not path:
        import datetime as _dt
        path = _dt.datetime.now().strftime("rtsp_log_%Y%m%d_%H%M%S.txt")
    fh = open(path, "a", encoding="utf-8")
    import datetime as _dt
    fh.write(f"# TmNS RTSP client log — started {_dt.datetime.now().isoformat()}\n")
    fh.flush()
    with _log_lock:
        _log_fh, _log_path = fh, path
    return path


def close_logfile() -> Optional[str]:
    """Close the current log file; returns its path (or None)."""
    global _log_fh, _log_path
    with _log_lock:
        fh, path = _log_fh, _log_path
        _log_fh = _log_path = None
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass
        return path
    return None


def log_path() -> Optional[str]:
    return _log_path


def _log_write(s: str) -> None:
    if _log_fh is None:
        return
    import datetime as _dt
    ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    with _log_lock:
        if _log_fh is None:
            return
        try:
            for line in str(s).split("\n"):
                _log_fh.write(f"[{ts}] {line}\n")
            _log_fh.flush()
        except (OSError, ValueError):
            pass


def cprint(s, color=C.RESET):
    print(C.wrap(s, color))
    _log_write(s)


def human_bytes(n: float) -> str:
    """Human-readable byte count (binary units)."""
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    f = float(n)
    i = 0
    while f >= 1024.0 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{int(n)} B" if i == 0 else f"{f:.2f} {units[i]}"


def fmt_hms(seconds: float) -> str:
    """Format a duration as H:MM:SS."""
    s = int(seconds)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def compact_ranges(nums) -> str:
    """Collapse a set of ints into compact ranges, e.g. [1,2,3,7,9,10] -> '1-3,7,9-10'."""
    nums = sorted(set(nums))
    if not nums:
        return "-"
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            out.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    out.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(out)


# ----- RTSP response model ------------------------------------------------

@dataclass
class RTSPResponse:
    status_code: int
    reason: str
    version: str
    headers: Dict[str, str]           # lowercased header name -> value
    body: bytes
    raw: bytes

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name.lower(), default)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


# ----- TmNS Request-Defined URI builder -----------------------------------

def build_tmns_uri(
    host: str,
    port: Optional[int] = None,
    version: str = "1.0",
    mdids: Optional[List[str]] = None,
    pdids: Optional[List[str]] = None,
    measids: Optional[List[str]] = None,
    delivery_mdid: Optional[int] = None,
    delivery_pdid: Optional[int] = None,
    dest_ip: Optional[str] = None,
    dest_port: Optional[int] = None,
    playback: Optional[str] = None,   # "l" | "p"
    timeopt: Optional[str] = None,    # "o" | "c"
    delivery_dscp: Optional[int] = None,
) -> str:
    """Assemble a TmNS_Request_Defined_URI per Ch.26 26.4.1.4.

    Only the simple/common encodings are produced here (MDID list, or
    PDID list ">" delivery_mdid, or MeasID list ">" delivery_mdid "<"
    delivery_pdid).  Callers who need exotic forms can pass a full URI via
    --uri on the command line instead.
    """
    base = f"rtsp://{host}"
    if port:
        base += f":{port}"
    parts = [base, "TmNS", version]

    tmns_list = None
    if measids:
        if delivery_mdid is None or delivery_pdid is None:
            raise ValueError("MeasurementID request needs delivery_mdid and delivery_pdid")
        lst = "".join(f"#{m}" for m in measids)
        # measids are appended onto a pdid list; require at least one pdid
        pd = "".join(f"@{p}" for p in (pdids or []))
        md = "".join(f"&{m}" for m in (mdids or []))
        tmns_list = f"{md}{pd}{lst}>{delivery_mdid}<{delivery_pdid}"
    elif pdids:
        if delivery_mdid is None:
            raise ValueError("PackageDefinitionID request needs delivery_mdid")
        md = "".join(f"&{m}" for m in (mdids or []))
        pd = "".join(f"@{p}" for p in pdids)
        tmns_list = f"{md}{pd}>{delivery_mdid}"
    elif mdids:
        tmns_list = "".join(f"&{m}" for m in mdids)

    if tmns_list:
        parts.append(tmns_list)

    if dest_ip:
        parts.append(f"{dest_ip}:{dest_port}" if dest_port else dest_ip)
    if playback:
        parts.append(f"-{playback}")
    if timeopt:
        parts.append(f"-{timeopt}")

    uri = "/".join(parts)
    if delivery_dscp is not None:
        uri += f"//{delivery_dscp}"
    return uri


def build_transport(
    lower: str = "UDP",
    cast: str = "unicast",
    destination: Optional[str] = None,
    ttl: Optional[int] = None,
    client_port: Optional[int] = None,
    client_port_hi: Optional[int] = None,
) -> str:
    """Build a TmNS Transport header value (Ch.26 26.4.1.1)."""
    spec = f"TMNS/TMNSP/{lower.upper()};{cast}"
    if destination:
        spec += f";destination={destination}"
    if ttl is not None:
        spec += f";ttl={ttl}"
    if client_port is not None:
        if client_port_hi is not None:
            spec += f";client_port={client_port}-{client_port_hi}"
        else:
            spec += f";client_port={client_port}"
    return spec


def is_multicast(ip: Optional[str]) -> bool:
    """True if ip is an IPv4 multicast address (224.0.0.0 - 239.255.255.255)."""
    try:
        return 224 <= int(ip.split(".")[0]) <= 239
    except (ValueError, AttributeError, IndexError):
        return False


def _low_port(portspec: Optional[str]) -> Optional[int]:
    """First port of a 'N' or 'N-M' port spec."""
    if not portspec:
        return None
    try:
        return int(portspec.split("-")[0])
    except ValueError:
        return None


def parse_transport(header: Optional[str]) -> Dict[str, str]:
    """Parse an RTSP Transport header value into a dict.

    Handles the first transport-spec: protocol/profile/lower plus the
    semicolon parameters (unicast|multicast, destination, source, client_port,
    server_port, ttl, ...).
    """
    d: Dict[str, str] = {}
    if not header:
        return d
    spec = header.split(",")[0].strip()          # first transport-spec
    fields = spec.split(";")
    proto = fields[0].split("/")
    d["protocol"] = proto[0].strip() if proto else ""
    if len(proto) > 1:
        d["profile"] = proto[1].strip()
    if len(proto) > 2:
        d["lower"] = proto[2].strip().upper()
    for f in fields[1:]:
        f = f.strip()
        if f in ("unicast", "multicast"):
            d["cast"] = f
        elif "=" in f:
            k, v = f.split("=", 1)
            d[k.strip().lower()] = v.strip()
    return d


# ----- RTSP client --------------------------------------------------------

class RTSPError(Exception):
    pass


class RTSPClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT,
                 timeout: float = 10.0, verbose: bool = False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.sock: Optional[socket.socket] = None
        self.cseq = 0
        self.session: Optional[str] = None
        self.session_timeout: Optional[int] = None
        self._buf = b""
        # serializes control-channel access (REPL command vs background
        # keep-alive share one socket)
        self._lock = threading.Lock()
        # negotiated data-channel info from the last SETUP transport response
        self.server_transport: Optional[str] = None

    # -- connection --
    def connect(self) -> None:
        if self.verbose:
            cprint(f"* Connecting to {self.host}:{self.port} (TCP) ...", C.DIM)
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def reconnect(self) -> None:
        """Re-establish the control channel after a drop.

        Closes the old socket, clears any partial buffer, and drops the
        session (a dropped control connection invalidates its RTSP session).
        The CSeq counter keeps advancing so requests stay unique.
        """
        self.close()
        self._buf = b""
        self.session = None
        self.session_timeout = None
        self.connect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- request/response --
    def request(self, method: str, uri: str,
                headers: Optional[Dict[str, str]] = None,
                body: bytes = b"", quiet: bool = False) -> RTSPResponse:
        # One request (send + read) at a time on the shared control socket,
        # so a REPL command and a background keep-alive don't interleave.
        # quiet suppresses the wire dump (used for background keep-alives).
        with self._lock:
            if self.sock is None:
                raise RTSPError("not connected")
            self.cseq += 1
            hdrs: Dict[str, str] = {}
            hdrs["CSeq"] = str(self.cseq)
            hdrs["User-Agent"] = USER_AGENT
            if self.session:
                hdrs["Session"] = self.session
            if headers:
                hdrs.update(headers)
            if body:
                hdrs["Content-Length"] = str(len(body))

            lines = [f"{method} {uri} RTSP/1.0"]
            for k, v in hdrs.items():
                lines.append(f"{k}: {v}")
            raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

            if self.verbose and not quiet:
                self._dump(raw, outgoing=True)

            self.sock.sendall(raw)
            resp = self._read_response()

            if self.verbose and not quiet:
                self._dump(resp.raw, outgoing=False)

            # capture session id from any response that carries it
            sess = resp.header("session")
            if sess:
                sid = sess.split(";")[0].strip()
                self.session = sid
                m = re.search(r"timeout\s*=\s*(\d+)", sess)
                if m:
                    self.session_timeout = int(m.group(1))

            # validate CSeq echo
            echoed = resp.header("cseq")
            if echoed is not None and echoed.strip() != str(self.cseq):
                shown = echoed.strip() or "<empty>"
                msg = (f"! CSeq mismatch: sent {self.cseq}, got {shown!r} "
                       f"({method} {resp.status_code})")
                if quiet:
                    _log_write(msg)      # keep background keep-alive off-screen
                else:
                    cprint(msg, C.YELLOW)
            return resp

    def _read_response(self) -> RTSPResponse:
        # read headers up to CRLFCRLF
        while b"\r\n\r\n" not in self._buf:
            chunk = self._recv()
            if not chunk:
                raise RTSPError("connection closed while reading response headers")
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest

        header_text = head.decode("iso-8859-1")
        status_line, *header_lines = header_text.split("\r\n")
        m = re.match(r"(RTSP/\d\.\d)\s+(\d+)\s*(.*)", status_line)
        if not m:
            raise RTSPError(f"malformed status line: {status_line!r}")
        version, code, reason = m.group(1), int(m.group(2)), m.group(3)

        headers: Dict[str, str] = {}
        for line in header_lines:
            if not line.strip():
                continue
            if ":" not in line:
                continue
            name, val = line.split(":", 1)
            headers[name.strip().lower()] = val.strip()

        body = b""
        clen = int(headers.get("content-length", "0") or "0")
        if clen:
            while len(self._buf) < clen:
                chunk = self._recv()
                if not chunk:
                    raise RTSPError("connection closed while reading body")
                self._buf += chunk
            body = self._buf[:clen]
            self._buf = self._buf[clen:]

        raw = head + b"\r\n\r\n" + body
        return RTSPResponse(code, reason, version, headers, body, raw)

    def _recv(self) -> bytes:
        try:
            return self.sock.recv(65536)
        except socket.timeout:
            raise RTSPError(f"timeout after {self.timeout}s waiting for response")

    def _dump(self, raw: bytes, outgoing: bool) -> None:
        arrow = ">>>" if outgoing else "<<<"
        color = C.CYAN if outgoing else C.GREEN
        text = raw.split(b"\r\n\r\n")[0].decode("iso-8859-1")
        cprint(f"{arrow} " + f"\n{arrow} ".join(text.splitlines()), color)
        # show body length only (bodies here are SDP-ish text or empty)
        bodylen = len(raw) - len(raw.split(b"\r\n\r\n")[0]) - 4
        if bodylen > 0:
            body = raw.split(b"\r\n\r\n", 1)[1]
            cprint(f"{arrow} [body {bodylen} bytes]", C.DIM)
            try:
                for l in body.decode("iso-8859-1").splitlines():
                    cprint(f"{arrow}   {l}", C.DIM)
            except Exception:
                pass
        print()

    # -- convenience methods --
    def options(self, uri: Optional[str] = None) -> RTSPResponse:
        return self.request("OPTIONS", uri or f"rtsp://{self.host}:{self.port}")

    def describe(self, uri: str) -> RTSPResponse:
        return self.request("DESCRIBE", uri, {"Accept": "application/sdp"})

    def setup(self, uri: str, transport: str) -> RTSPResponse:
        resp = self.request("SETUP", uri, {"Transport": transport})
        self.server_transport = resp.header("transport")
        return resp

    def play(self, uri: str, prange: Optional[str] = None,
             speed: Optional[float] = None,
             bandwidth: Optional[int] = None) -> RTSPResponse:
        hdrs: Dict[str, str] = {}
        if prange:
            hdrs["Range"] = prange
        if speed is not None and bandwidth is not None:
            raise ValueError("Speed and Bandwidth are mutually exclusive (Ch.26 26.4.1.3)")
        if speed is not None:
            hdrs["Speed"] = f"{speed:g}"
        if bandwidth is not None:
            hdrs["Bandwidth"] = str(bandwidth)
        return self.request("PLAY", uri, hdrs)

    def pause(self, uri: str) -> RTSPResponse:
        return self.request("PAUSE", uri)

    def teardown(self, uri: str) -> RTSPResponse:
        resp = self.request("TEARDOWN", uri)
        self.session = None
        return resp

    def get_parameter(self, uri: str, params: Optional[List[str]] = None) -> RTSPResponse:
        body = ("\r\n".join(params) + "\r\n").encode() if params else b""
        hdrs = {"Content-Type": "text/parameters"} if body else {}
        return self.request("GET_PARAMETER", uri, hdrs, body)

    def set_parameter(self, uri: str, params: Dict[str, str]) -> RTSPResponse:
        body = ("\r\n".join(f"{k}: {v}" for k, v in params.items()) + "\r\n").encode()
        return self.request("SET_PARAMETER", uri,
                            {"Content-Type": "text/parameters"}, body)

    def announce(self, uri: str, sdp: bytes) -> RTSPResponse:
        return self.request("ANNOUNCE", uri,
                            {"Content-Type": "application/sdp"}, sdp)

    def record(self, uri: str, prange: Optional[str] = None) -> RTSPResponse:
        hdrs = {"Range": prange} if prange else {}
        return self.request("RECORD", uri, hdrs)

    def redirect(self, uri: str) -> RTSPResponse:
        return self.request("REDIRECT", uri)


# ----- TmNS data-channel receiver -----------------------------------------

@dataclass
class DataMessage:
    version: int
    option_word_count: int
    msg_type: int
    flags: int
    mdid: int
    seq: int
    length: int
    timestamp: int
    payload_len: int

    @property
    def end_of_data(self) -> bool:
        return bool(self.flags & 0x0001)

    @property
    def playback(self) -> bool:
        return bool(self.flags & 0x0040)


def parse_datamsg_header(buf: bytes) -> Optional[DataMessage]:
    if len(buf) < DATAMSG_HEADER_LEN:
        return None
    word0, mdid, seq, length, ts = struct.unpack(">IIIIQ", buf[:DATAMSG_HEADER_LEN])
    version = (word0 >> 28) & 0xF
    owc = (word0 >> 24) & 0xF
    mtype = (word0 >> 16) & 0xF
    flags = word0 & 0xFFFF
    return DataMessage(version, owc, mtype, flags, mdid, seq, length, ts,
                       max(0, length - DATAMSG_HEADER_LEN))


# MessageFlags bit masks (Ch.24 24.2.1.5)
FLAG_END_OF_DATA = 0x0001
FLAG_PLAYBACK = 0x0040
FLAG_STANDARD_PKG_HEADER = 0x0080

STANDARD_PKG_HEADER_LEN = 12  # Ch.24 24.2.2.1.1

# ApplicationDefinedFields option-kind names (Ch.24 Table 24-1, 106-24)
OPTION_KINDS = {
    0x00: "End-of-Options", 0x01: "NOP",
    0x82: "DataSourceConfig", 0x83: "DataSourceError",
    0x85: "DestinationAddress", 0x86: "FragmentByteOffset",
    0x87: "PackageCount", 0x88: "IngressTimestamp",
    0x89: "EgressTimestamp",     # added in RCC 106-24
}

# option-kinds whose data is a 32-bit TAI seconds + 32-bit nanoseconds stamp
_TIMESTAMP_OPTS = (0x88, 0x89)


@dataclass
class AppOption:
    kind: int
    length: int          # total field length in bytes
    data: bytes

    @property
    def name(self) -> str:
        return OPTION_KINDS.get(self.kind, f"0x{self.kind:02x}")

    def describe(self) -> str:
        if self.kind == 0x85 and len(self.data) == 4:
            return f"{self.name}={'.'.join(str(b) for b in self.data)}"
        if self.kind in (0x86, 0x87) and len(self.data) >= 4:
            return f"{self.name}={struct.unpack('>I', self.data[:4])[0]}"
        if self.kind in _TIMESTAMP_OPTS and len(self.data) >= 8:
            secs, nsec = struct.unpack(">II", self.data[:8])
            return f"{self.name}={secs}.{nsec:09d} TAI"
        if self.data:
            return f"{self.name}[{len(self.data)}B]=0x{self.data.hex()}"
        return self.name


@dataclass
class Package:
    pdid: int
    length: int          # PackageLength: whole package (header+payload), no padding
    status_flags: int
    time_delta: int      # ns relative to MessageTimestamp
    payload: bytes       # MeasurementData bytes


@dataclass
class DecodedMessage:
    header: DataMessage
    options: List[AppOption]
    packages: List[Package]
    standard_pkg: bool
    undecoded_payload: bytes    # payload region when non-standard PackageHeaders
    truncated: bool             # captured bytes < MessageLength


def parse_app_options(buf: bytes, owc: int) -> Tuple[List[AppOption], int]:
    """Parse ApplicationDefinedFields; return (options, payload_offset)."""
    n = owc * 4
    region = buf[DATAMSG_HEADER_LEN:DATAMSG_HEADER_LEN + n]
    opts: List[AppOption] = []
    i = 0
    while i < len(region):
        kind = region[i]
        if kind == 0x00:            # End-of-Options / padding
            break
        if kind <= 0x7F:            # single-byte option (no length/data)
            opts.append(AppOption(kind, 1, b""))
            i += 1
            continue
        if i + 1 >= len(region):
            break
        length = region[i + 1]      # includes the kind + length octets
        if length < 2 or i + length > len(region):
            break
        opts.append(AppOption(kind, length, region[i + 2:i + length]))
        i += length
    return opts, DATAMSG_HEADER_LEN + n


def parse_packages(buf: bytes, start: int, msg_len: int) -> List[Package]:
    """Parse standard-PackageHeader Packages from the payload region."""
    end = min(len(buf), msg_len)
    pkgs: List[Package] = []
    off = start
    while off + STANDARD_PKG_HEADER_LEN <= end:
        pdid, plen, _resv, status, tdelta = struct.unpack(
            ">IHBBI", buf[off:off + STANDARD_PKG_HEADER_LEN])
        if plen < STANDARD_PKG_HEADER_LEN:
            break
        payload = buf[off + STANDARD_PKG_HEADER_LEN:off + plen]
        pkgs.append(Package(pdid, plen, status, tdelta, payload))
        nxt = (off + plen + 3) & ~3     # pad to next 32-bit boundary
        if nxt <= off:
            break
        off = nxt
    return pkgs


def decode_datamsg(buf: bytes) -> Optional[DecodedMessage]:
    """Fully decode one TmNSDataMessage: header, options, and Packages.

    Packages are only decoded when the StandardPackageHeaderFlag is set;
    otherwise the payload uses MDL-described custom headers that cannot be
    parsed without the MDL instance document, and is returned raw.
    """
    h = parse_datamsg_header(buf)
    if h is None:
        return None
    standard = bool(h.flags & FLAG_STANDARD_PKG_HEADER)
    opts, payload_off = parse_app_options(buf, h.option_word_count)
    truncated = len(buf) < h.length
    if h.end_of_data or h.length <= payload_off:
        return DecodedMessage(h, opts, [], standard, b"", truncated)
    if standard:
        pkgs = parse_packages(buf, payload_off, h.length)
        return DecodedMessage(h, opts, pkgs, standard, b"", truncated)
    undecoded = buf[payload_off:min(len(buf), h.length)]
    return DecodedMessage(h, opts, [], standard, undecoded, truncated)


def _hexdump(data: bytes, limit: int = 64, indent: str = "        ") -> List[str]:
    lines = []
    view = data[:limit]
    for off in range(0, len(view), 16):
        chunk = view[off:off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{indent}{off:04x}  {hexpart:<47}  {asc}")
    if len(data) > limit:
        lines.append(f"{indent}... ({len(data) - limit} more bytes)")
    return lines


def print_decoded(dm: DecodedMessage, hexdump: bool = False) -> None:
    h = dm.header
    tag = " [END-OF-DATA]" if h.end_of_data else ""
    pb = "playback" if h.playback else "live"
    trunc = " [TRUNCATED]" if dm.truncated else ""
    # MessageTimestamp is the lower 64 bits of the IEEE-1588 structure:
    # upper 32 bits = seconds, lower 32 = nanoseconds (Ch.24 24.2.1.9).
    ts_sec = h.timestamp >> 32
    ts_nsec = h.timestamp & 0xFFFFFFFF
    cprint(f"  msg mdid={h.mdid} seq={h.seq} len={h.length} v={h.version} "
           f"flags=0x{h.flags:04x} ({pb}) ts={ts_sec}.{ts_nsec:09d}"
           f"{tag}{trunc}", C.MAGENTA)
    if dm.options:
        cprint("    options: " + ", ".join(o.describe() for o in dm.options), C.DIM)
    if h.end_of_data:
        return
    if not dm.standard_pkg:
        cprint(f"    payload: {len(dm.undecoded_payload)}B non-standard/"
               f"MDL-described PackageHeader (needs MDL to decode)", C.YELLOW)
        if hexdump and dm.undecoded_payload:
            for l in _hexdump(dm.undecoded_payload):
                cprint(l, C.DIM)
        return
    for i, pkg in enumerate(dm.packages):
        cprint(f"    package[{i}] pdid={pkg.pdid} len={pkg.length} "
               f"status=0x{pkg.status_flags:02x} time_delta={pkg.time_delta}ns "
               f"measdata={len(pkg.payload)}B", C.CYAN)
        if hexdump and pkg.payload:
            for l in _hexdump(pkg.payload):
                cprint(l, C.DIM)


@dataclass
class DataStats:
    messages: int = 0
    bytes: int = 0
    end_of_data: bool = False
    gaps: int = 0
    lost: int = 0                    # estimated missing messages (from seq deltas)
    dupes: int = 0                   # duplicate/reordered sequence numbers
    packages: int = 0
    elapsed: float = 0.0
    by_mdid: Dict[int, int] = field(default_factory=dict)
    by_pdid: Dict[int, int] = field(default_factory=dict)
    by_mdid_gaps: Dict[int, int] = field(default_factory=dict)   # gaps per MDID
    by_mdid_lost: Dict[int, int] = field(default_factory=dict)   # lost per MDID
    # per-MDID last sequence number (MessageDefinitionSequenceNumber is
    # assigned per-MDID, RCC 106 Ch.26 26.5.1)
    _last_seq: Dict[int, int] = field(default_factory=dict)

    def note(self, dm: DecodedMessage) -> None:
        m = dm.header
        self.messages += 1
        self.bytes += m.length
        self.by_mdid[m.mdid] = self.by_mdid.get(m.mdid, 0) + 1
        self.packages += len(dm.packages)
        for pkg in dm.packages:
            self.by_pdid[pkg.pdid] = self.by_pdid.get(pkg.pdid, 0) + 1
        # The empty End-of-Data indicator carries mdid=0/seq=0 and is not part
        # of any data sequence, so it never counts toward gap detection.
        if m.end_of_data:
            self.end_of_data = True
            return
        prev = self._last_seq.get(m.mdid)
        if prev is not None:
            diff = (m.seq - prev) & 0xFFFFFFFF     # forward distance (wrap-safe)
            if diff == 0 or diff > 0x7FFFFFFF:
                # same seq (duplicate) or a backward jump (reordering)
                self.dupes += 1
            elif diff > 1:
                # forward gap: (diff - 1) messages missing
                missing = diff - 1
                self.gaps += 1
                self.lost += missing
                self.by_mdid_gaps[m.mdid] = self.by_mdid_gaps.get(m.mdid, 0) + 1
                self.by_mdid_lost[m.mdid] = self.by_mdid_lost.get(m.mdid, 0) + missing
        self._last_seq[m.mdid] = m.seq


def print_play_summary(stats: DataStats, title: str = "PLAY summary") -> None:
    """Print the final statistics of a completed (or interrupted) PLAY."""
    el = stats.elapsed or 0.0
    avg_rate = stats.messages / el if el > 0 else 0.0
    avg_bw = stats.bytes / el if el > 0 else 0.0
    cprint(f"\n==== {title} ====", C.BOLD)
    cprint(f"  duration      : {fmt_hms(el)}  ({el:.1f} s)")
    cprint(f"  messages      : {stats.messages:,}  (avg {avg_rate:,.1f} msg/s)")
    cprint(f"  data          : {human_bytes(stats.bytes)}  "
           f"(avg {human_bytes(avg_bw)}/s)")
    cprint(f"  packages      : {stats.packages:,}")
    cprint(f"  MDIDs         : {compact_ranges(stats.by_mdid)}  "
           f"({len(stats.by_mdid)} distinct)")
    if stats.by_pdid:
        cprint(f"  PDIDs         : {compact_ranges(stats.by_pdid)}  "
               f"({len(stats.by_pdid)} distinct)")
    cprint(f"  sequence gaps : {stats.gaps}  (est. {stats.lost:,} message(s) lost)",
           C.YELLOW if stats.gaps else C.RESET)
    if stats.by_mdid_gaps:
        breakdown = ", ".join(
            f"{mdid}={stats.by_mdid_gaps[mdid]} gap(s)/"
            f"{stats.by_mdid_lost.get(mdid, 0)} lost"
            for mdid in sorted(stats.by_mdid_gaps))
        cprint(f"  gaps by MDID  : {breakdown}", C.YELLOW)
    if stats.dupes:
        cprint(f"  duplicates/reorders : {stats.dupes}", C.YELLOW)
    cprint(f"  end-of-data   : {'yes' if stats.end_of_data else 'no'}",
           C.GREEN if stats.end_of_data else C.YELLOW)


class DataChannel:
    """Receives TmNSDataMessages for the duration of a PLAY.

    UDP: bind a local UDP socket to client_port and receive datagrams.
    TCP: listen on client_port (sink listens, source connects per 26.4.2.1),
         accept one connection, read a length-framed stream.
    """

    def __init__(self, lower: str, client_port: int, verbose: bool = False,
                 decode: bool = False, hexdump: bool = False,
                 decode_limit: int = 10, group: Optional[str] = None,
                 interface: Optional[str] = None):
        self.lower = lower.upper()
        self.client_port = client_port
        self.verbose = verbose
        self.decode = decode or hexdump
        self.hexdump = hexdump
        self.decode_limit = decode_limit
        # multicast group to join for UDP reception (None = plain unicast)
        self.group = group if is_multicast(group) else None
        self.interface = interface       # local IP for the multicast join/bind
        self._decoded_shown = 0
        self.sock: Optional[socket.socket] = None
        self.conn: Optional[socket.socket] = None

    def open(self) -> None:
        if self.lower == "UDP":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
            self.sock.bind(("", self.client_port))
            if self.group:
                self._join_multicast()
                if self.verbose:
                    cprint(f"* Data channel: UDP joined multicast {self.group} "
                           f"on :{self.client_port}"
                           + (f" (if {self.interface})" if self.interface else ""),
                           C.DIM)
            elif self.verbose:
                cprint(f"* Data channel: UDP bound on :{self.client_port}", C.DIM)
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(("0.0.0.0", self.client_port))
            self.sock.listen(1)
            if self.verbose:
                cprint(f"* Data channel: TCP listening on :{self.client_port}", C.DIM)

    def _join_multicast(self) -> None:
        """Join the multicast group so the kernel delivers its datagrams here."""
        grp = socket.inet_aton(self.group)
        ifc = socket.inet_aton(self.interface) if self.interface \
            else socket.inet_aton("0.0.0.0")
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             grp + ifc)          # struct ip_mreq

    def reconfigure(self, group: Optional[str] = None,
                    client_port: Optional[int] = None) -> bool:
        """Adopt a server-specified group/port (UDP). Call before PLAY.

        Returns True if the data channel was re-opened with new settings.
        """
        if self.lower != "UDP":
            return False
        new_group = group if is_multicast(group) else self.group
        new_port = client_port or self.client_port
        if new_group == self.group and new_port == self.client_port:
            return False
        self.close()
        self.group = new_group
        self.client_port = new_port
        self.open()
        return True

    def receive(self, duration: Optional[float] = None,
                stats_interval: float = 0.0,
                keepalive=None, keepalive_interval: Optional[float] = None,
                stop_event=None, stats: Optional[DataStats] = None
                ) -> DataStats:
        """Receive TmNSDataMessages until a deadline or End-of-Data.

        duration           max seconds to receive; None/0 = until End-of-Data
                           (or the peer closes a TCP data channel, or Ctrl-C).
        stats_interval     if > 0, print a running statistics line every N sec.
        keepalive          optional zero-arg callable invoked every
                           keepalive_interval seconds (e.g. an RTSP
                           GET_PARAMETER) to keep the session alive on a long
                           PLAY; failures are reported but do not stop receive.
        stop_event         optional threading.Event; when set, receive returns
                           (used to stop a background receiver).
        stats              optional external DataStats to fill (so a caller can
                           read running totals while receive runs in a thread).
        """
        if stats is None:
            stats = DataStats()
        start = time.time()
        hard_deadline = start + duration if duration and duration > 0 else None
        sel = selectors.DefaultSelector()
        if self.lower == "UDP":
            sel.register(self.sock, selectors.EVENT_READ, data="udp")
        else:  # TCP: wait for the source to connect, then read the stream
            sel.register(self.sock, selectors.EVENT_READ, data="listen")
        streambuf = b""
        last_stats = start
        last_ka = start
        prev = (start, 0, 0)   # (t, messages, bytes) snapshot for interval rate
        try:
            while not stats.end_of_data:
                now = time.time()
                if hard_deadline and now >= hard_deadline:
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                timeout = self._select_timeout(now, hard_deadline, last_stats,
                                               stats_interval, last_ka,
                                               keepalive, keepalive_interval)
                for key, _ in sel.select(timeout=timeout):
                    tag = key.data
                    if tag == "udp":
                        data, _addr = self.sock.recvfrom(65536)
                        self._consume_datagram(data, stats)
                    elif tag == "listen":
                        self.conn, addr = self.sock.accept()
                        self.conn.setblocking(False)
                        sel.unregister(self.sock)
                        sel.register(self.conn, selectors.EVENT_READ, data="tcp")
                        if self.verbose:
                            cprint(f"* Data source connected from {addr}", C.DIM)
                    elif tag == "tcp":
                        chunk = self.conn.recv(65536)
                        if not chunk:
                            stats.end_of_data = True
                            break
                        streambuf += chunk
                        streambuf = self._consume_stream(streambuf, stats)

                now = time.time()
                if stats_interval > 0 and now - last_stats >= stats_interval:
                    self._print_progress(stats, start, now, prev)
                    prev = (now, stats.messages, stats.bytes)
                    last_stats = now
                if (keepalive and keepalive_interval
                        and now - last_ka >= keepalive_interval):
                    try:
                        keepalive()
                    except (RTSPError, OSError) as e:
                        cprint(f"! keep-alive failed: {e}", C.YELLOW)
                    last_ka = now
        except KeyboardInterrupt:
            cprint("\n* interrupted -- stopping data reception", C.YELLOW)
        finally:
            sel.close()
        stats.elapsed = time.time() - start
        return stats

    @staticmethod
    def _select_timeout(now, hard_deadline, last_stats, stats_interval,
                        last_ka, keepalive, keepalive_interval) -> float:
        """Wake up in time for the next deadline/stats/keep-alive event."""
        waits = [1.0]
        if hard_deadline:
            waits.append(hard_deadline - now)
        if stats_interval > 0:
            waits.append(last_stats + stats_interval - now)
        if keepalive and keepalive_interval:
            waits.append(last_ka + keepalive_interval - now)
        return max(0.0, min(waits))

    def _print_progress(self, stats: DataStats, start: float, now: float,
                        prev) -> None:
        elapsed = now - start
        dt = now - prev[0]
        inst_rate = (stats.messages - prev[1]) / dt if dt > 0 else 0.0
        inst_bw = (stats.bytes - prev[2]) / dt if dt > 0 else 0.0
        eod = " EOD" if stats.end_of_data else ""
        cprint(f"  [{fmt_hms(elapsed)}] msgs={stats.messages:,} "
               f"({inst_rate:,.0f}/s) data={human_bytes(stats.bytes)} "
               f"({human_bytes(inst_bw)}/s) pkgs={stats.packages:,} "
               f"mdids={len(stats.by_mdid)} gaps={stats.gaps} "
               f"lost={stats.lost}{eod}", C.BLUE)

    def _consume_datagram(self, data: bytes, stats: DataStats) -> None:
        dm = decode_datamsg(data)
        if dm is None:
            if self.verbose:
                cprint(f"! runt datagram ({len(data)} bytes)", C.YELLOW)
            return
        stats.note(dm)
        self._report(dm)

    def _consume_stream(self, buf: bytes, stats: DataStats) -> bytes:
        while len(buf) >= DATAMSG_HEADER_LEN:
            h = parse_datamsg_header(buf)
            if h.length < DATAMSG_HEADER_LEN:
                # bad length; resync by dropping a byte
                buf = buf[1:]
                continue
            if len(buf) < h.length:
                break
            dm = decode_datamsg(buf[:h.length])
            stats.note(dm)
            self._report(dm)
            buf = buf[h.length:]
            if h.end_of_data:
                break
        return buf

    def _report(self, dm: DecodedMessage) -> None:
        if self.decode:
            if self.decode_limit <= 0 or self._decoded_shown < self.decode_limit:
                print_decoded(dm, hexdump=self.hexdump)
                self._decoded_shown += 1
                if self.decode_limit > 0 and self._decoded_shown == self.decode_limit:
                    cprint(f"    ... (decode limit {self.decode_limit} reached; "
                           f"still counting)", C.DIM)
        elif self.verbose:
            m = dm.header
            tag = " [END-OF-DATA]" if m.end_of_data else ""
            pb = "playback" if m.playback else "live"
            cprint(f"    data: mdid={m.mdid} seq={m.seq} len={m.length} "
                   f"flags=0x{m.flags:04x} ({pb}){tag}", C.MAGENTA)

    def close(self) -> None:
        for s in (self.conn, self.sock):
            if s:
                try:
                    s.close()
                except OSError:
                    pass
        self.conn = self.sock = None


# ----- Conformance test suite --------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""


class ConformanceTester:
    """Runs a sequence of RTSP exchanges and asserts TmNS/RFC-2326 rules."""

    def __init__(self, client: RTSPClient, uri: str, transport: str,
                 data_lower: str, client_port: int, play_seconds: float,
                 prange: Optional[str], decode: bool = False,
                 hexdump: bool = False, check_timeout: bool = False,
                 stats_interval: float = 0.0, keepalive_arg=None,
                 keepalive_method: str = "auto"):
        self.c = client
        self.uri = uri
        self.transport = transport
        self.data_lower = data_lower
        self.client_port = client_port
        self.play_seconds = play_seconds
        self.prange = prange
        self.decode = decode
        self.hexdump = hexdump
        self.check_timeout = check_timeout
        self.stats_interval = stats_interval
        self.keepalive_arg = keepalive_arg
        self.keepalive_method = keepalive_method
        self.results: List[TestResult] = []

    def _reconnect(self) -> None:
        """Get a clean control connection + session state for an isolated test."""
        self.c.close()
        self.c.session = None
        self.c.connect()

    def _mkchannel(self) -> "DataChannel":
        return DataChannel(self.data_lower, self.client_port, verbose=self.c.verbose,
                           decode=self.decode, hexdump=self.hexdump)

    def _expect(self, name, action, ok_codes, allow_drop=True) -> None:
        """Run action() (fresh connection) and assert its status is in ok_codes.

        A dropped connection is treated as an acceptable rejection when
        allow_drop is set (some servers close the socket on error paths).
        """
        try:
            self._reconnect()
            r = action()
            self._add(name, r.status_code in ok_codes, f"{r.status_code} {r.reason}")
        except (RTSPError, OSError) as e:
            self._add(name, allow_drop, f"connection dropped ({e})")

    def _negative_tests(self) -> None:
        c4xx = set(range(400, 500))

        # unknown/unsupported method -> 501 Not Implemented or 405 Method Not Allowed
        self._expect(
            "Unknown method rejected (501/405)",
            lambda: c.request("FLYAWAY", self.uri) if (c := self.c) else None,
            {501, 405})

        # SETUP with a non-TmNS transport profile -> 461 Unsupported Transport
        bad_transport = "RTP/AVP/UDP;unicast;client_port=%d" % self.client_port
        self._expect(
            "SETUP with unsupported Transport rejected (461/4xx)",
            lambda: self.c.setup(self.uri, bad_transport),
            {461} | c4xx)

        # PLAY with an invalid Range -> 457 Invalid Range (Ch.26 26.4.1.2)
        def invalid_range():
            self.c.setup(self.uri, self.transport)
            return self.c.play(self.uri, prange="ptp-clock=not-a-valid-range")
        self._expect(
            "PLAY with invalid Range rejected (457/4xx)",
            invalid_range, {457} | c4xx)

        # PLAY without an established Session -> 454 Session Not Found
        self._expect(
            "PLAY without Session rejected (454/4xx)",
            lambda: (setattr(self.c, "session", None), self.c.play(self.uri))[1],
            {454} | c4xx)

        # TEARDOWN with an unknown Session id -> 454 Session Not Found
        def bogus_teardown():
            self.c.session = "DEADBEEF"
            return self.c.request("TEARDOWN", self.uri, {"Session": "DEADBEEF"})
        self._expect(
            "TEARDOWN with unknown Session rejected (454/4xx)",
            bogus_teardown, {454} | c4xx)

        # optional: session-timeout expiry (opt-in; needs a short server timeout)
        if self.check_timeout:
            self._check_session_timeout()

    def _check_session_timeout(self) -> None:
        try:
            self._reconnect()
            r = self.c.setup(self.uri, self.transport)
            if not r.ok or self.c.session_timeout is None:
                self._add("Session expires after timeout", False,
                          "no Session/timeout returned by SETUP")
                return
            wait = self.c.session_timeout + 3
            cprint(f"* waiting {wait}s for session {self.c.session} to expire ...", C.DIM)
            time.sleep(wait)
            # probe with OPTIONS (mandatory) so this works even without GET_PARAMETER
            r = self.c.options(self.uri)
            self._add("Session expires after timeout (454)",
                      r.status_code == 454, f"{r.status_code} {r.reason}")
        except (RTSPError, OSError) as e:
            self._add("Session expires after timeout (454)", True,
                      f"connection dropped ({e})")

    def _add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.results.append(TestResult(name, passed, detail))
        status = C.wrap(" PASS ", C.GREEN) if passed else C.wrap(" FAIL ", C.RED)
        line = f"[{status}] {name}"
        if detail:
            line += C.wrap(f"  -- {detail}", C.DIM)
        print(line)
        return passed

    def run(self) -> bool:
        # 1. OPTIONS
        try:
            r = self.c.options(self.uri)
            self._add("OPTIONS returns 2xx", r.ok, f"{r.status_code} {r.reason}")
            public = r.header("public", "")
            required = {"OPTIONS", "SETUP", "TEARDOWN", "PLAY", "PAUSE"}
            advertised = {m.strip().upper() for m in public.split(",") if m.strip()}
            missing = required - advertised
            self._add("OPTIONS advertises required methods in Public header",
                      not missing and bool(advertised),
                      f"Public: {public!r}" if missing else f"{sorted(advertised)}")
            self._add("OPTIONS echoes CSeq",
                      r.header("cseq") == str(self.c.cseq), r.header("cseq") or "missing")
        except RTSPError as e:
            self._add("OPTIONS returns 2xx", False, str(e))

        # 2. SETUP
        data = self._mkchannel()
        setup_ok = False
        try:
            data.open()
            r = self.c.setup(self.uri, self.transport)
            setup_ok = self._add("SETUP returns 2xx", r.ok, f"{r.status_code} {r.reason}")
            self._add("SETUP response includes Session header",
                      self.c.session is not None, self.c.session or "missing")
            self._add("SETUP response includes Transport header",
                      r.header("transport") is not None,
                      r.header("transport") or "missing")
        except (RTSPError, OSError) as e:
            self._add("SETUP returns 2xx", False, str(e))

        # 3. PLAY  (and receive data)
        if setup_ok:
            stats = DataStats()
            try:
                r = self.c.play(self.uri, prange=self.prange)
                play_ok = self._add("PLAY returns 2xx", r.ok, f"{r.status_code} {r.reason}")
                self._add("PLAY requires an active Session",
                          self.c.session is not None, self.c.session or "missing")
                if play_ok:
                    dur = self.play_seconds if self.play_seconds > 0 else None
                    ka, ka_int = make_keepalive(self.c, self.uri, self.keepalive_arg,
                                                self.keepalive_method)
                    cprint("* Receiving data "
                           + (f"for {self.play_seconds:g}s ..." if dur
                              else "until End-of-Data ..."), C.DIM)
                    stats = data.receive(dur, stats_interval=self.stats_interval,
                                         keepalive=ka, keepalive_interval=ka_int)
                    print_play_summary(stats)
                    self._add("Data received on data channel", stats.messages > 0,
                              f"{stats.messages} msgs, {stats.bytes} bytes, "
                              f"mdids={sorted(stats.by_mdid)}")
                    self._add("No sequence gaps observed", stats.gaps == 0,
                              f"{stats.gaps} gap(s)")
            except RTSPError as e:
                self._add("PLAY returns 2xx", False, str(e))

            # 4. PAUSE
            try:
                r = self.c.pause(self.uri)
                self._add("PAUSE returns 2xx", r.ok, f"{r.status_code} {r.reason}")
            except RTSPError as e:
                self._add("PAUSE returns 2xx", False, str(e))

            # 4b. OPTIONS mid-session refreshes/keeps the session (keep-alive path)
            try:
                r = self.c.options(self.uri)
                self._add("OPTIONS mid-session (keep-alive) returns 2xx",
                          r.ok, f"{r.status_code} {r.reason}")
            except RTSPError as e:
                self._add("OPTIONS mid-session (keep-alive) returns 2xx", False, str(e))

            # 4c. Second PLAY after PAUSE (resume/replay on the same session)
            try:
                r = self.c.play(self.uri, prange=self.prange)
                if self._add("Second PLAY after PAUSE returns 2xx (resume)",
                             r.ok, f"{r.status_code} {r.reason}") and r.ok:
                    self.c.pause(self.uri)      # settle back to READY before teardown
            except RTSPError as e:
                self._add("Second PLAY after PAUSE returns 2xx (resume)", False, str(e))

            # 5. TEARDOWN
            try:
                r = self.c.teardown(self.uri)
                self._add("TEARDOWN returns 2xx", r.ok, f"{r.status_code} {r.reason}")
            except RTSPError as e:
                self._add("TEARDOWN returns 2xx", False, str(e))

        data.close()

        # ---- negative / error-path tests ----
        cprint("\n-- negative tests --", C.BOLD)
        self._negative_tests()

        passed = sum(1 for r in self.results if r.passed)
        total = len(self.results)
        color = C.GREEN if passed == total else (C.YELLOW if passed else C.RED)
        cprint(f"\n{'='*56}\nResult: {passed}/{total} checks passed", color)
        return passed == total


# ----- CLI ----------------------------------------------------------------

def add_common_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("host", help="RTSPDataSource host (IPv4 or name)")
    p.add_argument("-p", "--port", type=int, default=DEFAULT_PORT,
                   help=f"control-channel TCP port (default {DEFAULT_PORT})")
    p.add_argument("-t", "--timeout", type=float, default=10.0,
                   help="socket timeout seconds (default 10)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print raw RTSP request/response and per-message data")
    p.add_argument("--log", nargs="?", const="__AUTO__", default=None, metavar="FILE",
                   help="also write all messages to a log file "
                        "(default name rtsp_log_<date>_<time>.txt)")


def add_uri_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--uri", help="full TmNS_Request_Defined_URI (overrides builders)")
    p.add_argument("--mdid", action="append", default=[],
                   help="MessageDefinitionID (repeatable); use A-B for a range")
    p.add_argument("--pdid", action="append", default=[],
                   help="PackageDefinitionID (repeatable)")
    p.add_argument("--measid", action="append", default=[],
                   help="MeasurementID (repeatable)")
    p.add_argument("--delivery-mdid", type=int, help="delivery MDID for pdid/measid requests")
    p.add_argument("--delivery-pdid", type=int, help="delivery PDID for measid requests")
    p.add_argument("--dest-ip", help="TmNSdestIP for delivered data")
    p.add_argument("--dest-port", type=int, help="TmNSdestport for delivered data")
    p.add_argument("--playback", choices=["l", "p"], help="playback opt (l=live, p=playback)")
    p.add_argument("--timeopt", choices=["o", "c"], help="time opt (o=original, c=current)")
    p.add_argument("--dscp", type=int, help="delivery DSCP")


def add_transport_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--lower", choices=["UDP", "TCP", "udp", "tcp"], default="UDP",
                   help="data-channel lower transport (default UDP)")
    p.add_argument("--cast", choices=["unicast", "multicast"], default="unicast")
    p.add_argument("--destination", help="Transport destination= address")
    p.add_argument("--ttl", type=int, help="Transport ttl= for multicast")
    p.add_argument("--client-port", type=int, default=6970,
                   help="local data-channel port (default 6970)")
    p.add_argument("--client-port-hi", type=int, help="high end of client_port range")
    p.add_argument("--group", help="multicast group to join for UDP reception "
                   "(if the server delivers to a multicast address); also sets "
                   "the Transport destination")
    p.add_argument("--interface", help="local interface IP for the multicast "
                   "join / data socket (default: any)")


def add_decode_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--decode", action="store_true",
                   help="decode TmNSDataMessage payload (options + Packages)")
    p.add_argument("--hexdump", action="store_true",
                   help="hex-dump MeasurementData bytes (implies --decode)")
    p.add_argument("--decode-limit", type=int, default=10,
                   help="max messages to print decoded (0 = unlimited; default 10)")


def add_receive_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stats-interval", type=float, default=10.0,
                   help="seconds between running-statistics lines during PLAY "
                        "(0 = off; default 10)")
    p.add_argument("--keepalive", type=float, default=None,
                   help="send an RTSP keep-alive every N seconds during PLAY; "
                        "0 = off; omit = auto (session_timeout/2). "
                        "Recommended for long playbacks.")
    p.add_argument("--keepalive-method",
                   choices=["auto", "options", "get_parameter", "set_parameter"],
                   default="auto",
                   help="keep-alive request method (default auto: GET_PARAMETER, "
                        "falling back to OPTIONS if the server returns 501/405). "
                        "Use 'options' for servers without GET_PARAMETER.")


def _send_keepalive(client: "RTSPClient", uri: str, method: str) -> RTSPResponse:
    # quiet=True: keep-alives never print a wire dump (they'd flood the REPL)
    if method == "options":
        return client.request("OPTIONS", uri, quiet=True)   # carries Session
    if method == "set_parameter":
        return client.request("SET_PARAMETER", uri, quiet=True)
    return client.request("GET_PARAMETER", uri, quiet=True)


def make_keepalive(client: "RTSPClient", uri: str, arg, method: str = "auto",
                   status: Optional[dict] = None):
    """Build (callable, interval) for periodic session keep-alives.

    A keep-alive is any request carrying the Session header; it refreshes the
    server's session timeout.  arg: None = auto interval (session_timeout/2),
    0 = disabled, >0 = explicit seconds.  method selects the request; "auto"
    tries GET_PARAMETER and, if the server answers 501/405 (not implemented),
    switches to OPTIONS -- which every TmNS server must support.

    Keep-alives are silent; the resolved method is recorded in the optional
    `status` dict (for display in 'help'), not printed.
    """
    if arg == 0:
        return None, None
    if arg is not None and arg > 0:
        interval = float(arg)
    elif client.session_timeout:
        # half the advertised timeout keeps the session alive with margin;
        # floor at 1s so a tiny server timeout still gets a timely keep-alive.
        interval = max(1.0, client.session_timeout / 2.0)
    else:
        return None, None

    state = {"method": None if method == "auto" else method}
    if status is not None:
        status["interval"] = interval
        status["method"] = method            # e.g. "auto" until resolved

    def ping():
        m = state["method"] or "get_parameter"
        r = _send_keepalive(client, uri, m)
        if method == "auto" and state["method"] is None:
            if r is not None and r.status_code in (501, 405):
                state["method"] = "options"
                _send_keepalive(client, uri, "options")   # refresh now
            else:
                state["method"] = "get_parameter"
            if status is not None:
                status["method"] = state["method"]

    return ping, interval


def resolve_uri(args) -> str:
    if getattr(args, "uri", None):
        return args.uri
    return build_tmns_uri(
        host=args.host, port=args.port,
        mdids=args.mdid or None, pdids=args.pdid or None,
        measids=args.measid or None,
        delivery_mdid=args.delivery_mdid, delivery_pdid=args.delivery_pdid,
        dest_ip=args.dest_ip, dest_port=args.dest_port,
        playback=args.playback, timeopt=args.timeopt, delivery_dscp=args.dscp,
    )


def resolve_transport(args) -> str:
    dest = (args.destination or getattr(args, "group", None)
            or (args.dest_ip if getattr(args, "dest_ip", None) else None))
    cast = "multicast" if is_multicast(dest) else args.cast
    return build_transport(
        lower=args.lower, cast=cast, destination=dest, ttl=args.ttl,
        client_port=args.client_port, client_port_hi=args.client_port_hi,
    )


def initial_group(args) -> Optional[str]:
    """Multicast group to join from the CLI, if any (--group/--destination/--dest-ip)."""
    for attr in ("group", "destination", "dest_ip"):
        cand = getattr(args, attr, None)
        if is_multicast(cand):
            return cand
    return None


def make_data_channel(args, verbose: Optional[bool] = None) -> "DataChannel":
    return DataChannel(args.lower, args.client_port,
                       verbose=args.verbose if verbose is None else verbose,
                       decode=args.decode, hexdump=args.hexdump,
                       decode_limit=args.decode_limit,
                       group=initial_group(args),
                       interface=getattr(args, "interface", None))


def apply_server_transport(data: "DataChannel", client: "RTSPClient",
                           requested_lower: str) -> None:
    """Reconcile the data channel with the server's SETUP Transport response.

    Adopts the server's destination (multicast group) and client_port, and
    warns if the server's lower-transport differs from what we requested.
    """
    st = parse_transport(client.server_transport)
    if not st:
        return
    srv_lower = st.get("lower")
    if srv_lower and srv_lower != requested_lower.upper():
        cprint(f"! server data transport is {srv_lower} but you requested "
               f"{requested_lower.upper()}; reception will fail -- re-run with "
               f"--lower {srv_lower.lower()}", C.YELLOW)
    if data.reconfigure(group=st.get("destination"),
                        client_port=_low_port(st.get("client_port"))):
        where = f"multicast {data.group}" if data.group else "unicast"
        cprint(f"* adopted server Transport: receiving {where} on "
               f"UDP :{data.client_port}", C.GREEN)


def cmd_test(args) -> int:
    uri = resolve_uri(args)
    transport = resolve_transport(args)
    cprint(f"* Target : {args.host}:{args.port}", C.BOLD)
    cprint(f"* URI    : {uri}", C.DIM)
    cprint(f"* Transp : {transport}\n", C.DIM)
    with RTSPClient(args.host, args.port, args.timeout, args.verbose) as c:
        tester = ConformanceTester(
            c, uri, transport, args.lower, args.client_port,
            args.play_seconds, args.range,
            decode=args.decode, hexdump=args.hexdump,
            check_timeout=args.check_timeout,
            stats_interval=args.stats_interval, keepalive_arg=args.keepalive,
            keepalive_method=args.keepalive_method,
        )
        ok = tester.run()
    return 0 if ok else 1


def cmd_stream(args) -> int:
    uri = resolve_uri(args)
    transport = resolve_transport(args)
    with RTSPClient(args.host, args.port, args.timeout, args.verbose) as c:
        r = c.options(uri)
        cprint(f"OPTIONS -> {r.status_code} {r.reason}  Public: {r.header('public','')}",
               C.GREEN if r.ok else C.RED)
        data = make_data_channel(args)
        data.open()
        r = c.setup(uri, transport)
        cprint(f"SETUP   -> {r.status_code} {r.reason}  Session: {c.session}  "
               f"Transport: {r.header('transport','')}", C.GREEN if r.ok else C.RED)
        if not r.ok:
            data.close()
            return 1
        apply_server_transport(data, c, args.lower)
        r = c.play(uri, prange=args.range, speed=args.speed, bandwidth=args.bandwidth)
        cprint(f"PLAY    -> {r.status_code} {r.reason}", C.GREEN if r.ok else C.RED)
        if r.ok:
            dur = args.play_seconds if args.play_seconds > 0 else None
            ka, ka_int = make_keepalive(c, uri, args.keepalive, args.keepalive_method)
            cprint("* Receiving "
                   + (f"for {args.play_seconds:g}s" if dur else "until End-of-Data")
                   + (f", stats every {args.stats_interval:g}s"
                      if args.stats_interval > 0 else "")
                   + (f", keep-alive every {ka_int:g}s" if ka_int else "")
                   + " ...", C.DIM)
            stats = data.receive(dur, stats_interval=args.stats_interval,
                                 keepalive=ka, keepalive_interval=ka_int)
            print_play_summary(stats)
        r = c.teardown(uri)
        cprint(f"TEARDOWN-> {r.status_code} {r.reason}", C.GREEN if r.ok else C.RED)
        data.close()
    return 0


def cmd_method(args) -> int:
    """Run one or more RTSP methods in sequence on a single session.

    Supports the full RFC 2326 / TmNS set including SETUP/PLAY/PAUSE/TEARDOWN.
    State (Session, CSeq, data channel) is kept across methods; SETUP opens the
    data channel, PLAY receives data for --play-seconds, and TEARDOWN ends the
    sequence.  This is the scriptable counterpart to interactive mode, e.g.:
        method 10.0.0.5 OPTIONS SETUP PLAY PAUSE PLAY TEARDOWN
    """
    uri = resolve_uri(args)
    transport = resolve_transport(args)
    body = b""
    if args.body_file:
        with open(args.body_file, "rb") as fh:
            body = fh.read()
    elif args.body:
        body = args.body.encode()
    methods = [m.upper() for m in args.method]
    data: Optional[DataChannel] = None
    with RTSPClient(args.host, args.port, args.timeout, verbose=True) as c:
        try:
            for meth in methods:
                if meth == "OPTIONS":
                    c.options(uri)
                elif meth == "DESCRIBE":
                    r = c.describe(uri)
                    if r.ok and r.body:
                        _print_sdp(r.body)
                elif meth == "SETUP":
                    if data:
                        data.close()
                    data = make_data_channel(args)
                    data.open()
                    c.setup(uri, transport)
                    apply_server_transport(data, c, args.lower)
                elif meth == "PLAY":
                    r = c.play(uri, prange=args.range, speed=args.speed,
                               bandwidth=args.bandwidth)
                    if r.ok and data:
                        dur = args.play_seconds if args.play_seconds > 0 else None
                        ka, ka_int = make_keepalive(c, uri, args.keepalive,
                                                    args.keepalive_method)
                        cprint("* Receiving "
                               + (f"for {args.play_seconds:g}s" if dur
                                  else "until End-of-Data")
                               + " ...", C.DIM)
                        stats = data.receive(dur, stats_interval=args.stats_interval,
                                             keepalive=ka, keepalive_interval=ka_int)
                        print_play_summary(stats)
                    elif r.ok and not data:
                        cprint("! PLAY sent but no data channel (run SETUP first)",
                               C.YELLOW)
                elif meth == "PAUSE":
                    c.pause(uri)
                elif meth == "TEARDOWN":
                    c.teardown(uri)
                    if data:
                        data.close(); data = None
                    break                       # session ended; stop the sequence
                elif meth == "GET_PARAMETER":
                    c.get_parameter(uri, args.param or None)
                elif meth == "SET_PARAMETER":
                    params = dict(kv.split("=", 1) for kv in (args.param or [])
                                  if "=" in kv)
                    c.set_parameter(uri, params)
                elif meth == "ANNOUNCE":
                    c.announce(uri, body)
                elif meth == "RECORD":
                    c.record(uri, prange=args.range)
                elif meth == "REDIRECT":
                    c.redirect(uri)
                else:
                    hdrs = {"Content-Type": args.content_type} \
                        if (body and args.content_type) else {}
                    c.request(meth, uri, hdrs, body)
        finally:
            if data:
                data.close()
    return 0


# (command, argument-spec, description) for the interactive help listing
INTERACTIVE_COMMANDS = [
    ("help, ?", "", "show this command list"),
    ("options", "", "send OPTIONS (list supported methods)"),
    ("describe", "", "send DESCRIBE and show the parsed SDP"),
    ("setup", "", "open the data channel and send SETUP"),
    ("play", "[secs]", "send PLAY; no secs = stream in the background (prompt "
                       "stays free); secs = receive in the foreground for N s"),
    ("pause", "", "send PAUSE (server stops sending; 'play' to resume)"),
    ("resume", "", "resume a paused/background stream (alias for 'play')"),
    ("stats", "", "show running totals of the background stream"),
    ("status", "", "show session state (RTSP state, session, data channel, ...)"),
    ("stop", "", "stop the background receiver, keep the session"),
    ("teardown", "", "send TEARDOWN and close the data channel"),
    ("record", "[range]", "send RECORD (e.g. record ptp-clock=now-)"),
    ("redirect", "", "send REDIRECT"),
    ("announce", "<sdp...>", "send ANNOUNCE with the given text as the SDP body"),
    ("get", "[param...]", "send GET_PARAMETER (optional parameter names)"),
    ("set", "<key> <value...>", "send SET_PARAMETER as 'key: value'"),
    ("uri", "[new-uri]", "show or change the request URI used by later commands"),
    ("range", "[value]", "show or set the PLAY Range (e.g. range ptp-clock=now-)"),
    ("speed", "[value]", "show or set the PLAY Speed (clears bandwidth)"),
    ("bandwidth", "[value]", "show or set the PLAY Bandwidth bps (clears speed)"),
    ("config", "[key [value]]", "list context vars, or change one (range, speed, "
                                "bandwidth, lower, client_port, group, ...)"),
    ("log", "[file|off]", "start logging to a file (auto-named if omitted) or "
                          "stop with 'log off'"),
    ("quit, exit, q", "", "close the session and exit"),
]

# Runtime-configurable interactive context variables (argparse dest name ->
# (type, affects_transport, help)).  A value of none/-/"" clears it (None).
CONFIG_KEYS = {
    "range": (str, False, "PLAY Range header, e.g. ptp-clock=now-"),
    "speed": (float, False, "PLAY Speed header"),
    "bandwidth": (int, False, "PLAY Bandwidth header (bps)"),
    "play_seconds": (float, False, "default foreground play duration (s)"),
    "keepalive": (float, False, "keep-alive interval (s); 0 = off"),
    "keepalive_method": (str, False, "auto|options|get_parameter|set_parameter"),
    "stats_interval": (float, False, "foreground running-stats interval (s)"),
    "lower": (str, True, "data lower-transport UDP|TCP (next setup)"),
    "cast": (str, True, "unicast|multicast (next setup)"),
    "client_port": (int, True, "data-channel port (next setup)"),
    "client_port_hi": (int, True, "high end of client_port range"),
    "destination": (str, True, "Transport destination= address (next setup)"),
    "group": (str, True, "multicast group to join (next setup)"),
    "interface": (str, True, "local interface IP for multicast (next setup)"),
    "ttl": (int, True, "Transport ttl= for multicast"),
}

_CONFIG_CHOICES = {
    "keepalive_method": {"auto", "options", "get_parameter", "set_parameter"},
    "lower": {"UDP", "TCP"},
    "cast": {"unicast", "multicast"},
}


def parse_config_value(key: str, val_str: str):
    """Parse/validate a config value. Returns (ok, value_or_errmsg)."""
    typ = CONFIG_KEYS[key][0]
    if val_str.strip().lower() in ("none", "null", "-", ""):
        return True, None
    try:
        value = typ(val_str)
    except ValueError:
        return False, f"invalid {typ.__name__} value: {val_str!r}"
    if key in _CONFIG_CHOICES:
        norm = value.upper() if key == "lower" else value
        if norm not in _CONFIG_CHOICES[key]:
            return False, f"{key} must be one of {sorted(_CONFIG_CHOICES[key])}"
        value = norm
    return True, value


_CONN_DEAD_ERRNOS = {
    errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN, errno.EBADF,
    errno.ESHUTDOWN, errno.ECONNABORTED, errno.ETIMEDOUT,
}


def _connection_dead(exc: Exception) -> bool:
    """True if the control connection is unusable and needs reconnecting."""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError)):
        return True
    if isinstance(exc, OSError) and exc.errno in _CONN_DEAD_ERRNOS:
        return True
    if isinstance(exc, RTSPError):
        m = str(exc).lower()
        return ("connection closed" in m or "not connected" in m
                or "timeout" in m)
    return False


# Commands that start a fresh exchange and are safe to auto-retry after a
# reconnect (they don't depend on prior session state).
_RETRYABLE_CMDS = {"options", "describe", "descr", "setup"}


def _setup_readline():
    """Enable up/down-arrow command history and line editing for the REPL.

    Returns the readline module (or None if unavailable, e.g. on Windows
    without a readline implementation).  Loads persisted history so previous
    sessions' commands are recalled too.
    """
    try:
        import readline
    except ImportError:
        return None
    try:
        readline.read_history_file(HISTORY_FILE)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(1000)
    return readline


def _save_history(readline) -> None:
    if readline is None:
        return
    try:
        readline.write_history_file(HISTORY_FILE)
    except OSError:
        pass


def _prompt() -> str:
    """REPL prompt; wraps color escapes in readline-ignore markers so line
    editing and history recall compute the cursor width correctly."""
    if C.enabled:
        return f"\001{C.CYAN}\002tmns-rtsp> \001{C.RESET}\002"
    return "tmns-rtsp> "


def _keepalive_line(args, ka_status) -> str:
    """One-line keep-alive status for the help/context display."""
    if ka_status and ka_status.get("active"):
        m = ka_status.get("method") or "auto"
        iv = ka_status.get("interval")
        iv_s = f"{iv:g}s" if iv else "?"
        return f"{m} every {iv_s} — session alive"
    if getattr(args, "keepalive", None) == 0:
        return "off"
    when = (f"every {args.keepalive:g}s" if getattr(args, "keepalive", None)
            else "auto (session_timeout/2)")
    return f"{args.keepalive_method}, {when} (starts at 'play')"


def _print_interactive_help(uri: str, transport: str, args, session,
                            ka_status=None) -> None:
    cprint("Commands:", C.BOLD)
    for name, argspec, desc in INTERACTIVE_COMMANDS:
        left = f"{name} {argspec}".strip()
        cprint(f"  {left:<24} {desc}", C.DIM)
    cprint("\nTypical flow:", C.BOLD)
    cprint("  options -> setup -> play -> (pause -> play) -> stop/teardown", C.DIM)
    cprint("  ('play' streams in the background; use 'play 5' for a quick "
           "5s foreground look)", C.DIM)
    cprint("\nCurrent context (change with 'config'/'range'/'uri'):", C.BOLD)
    cprint(f"  uri       : {uri}", C.DIM)
    cprint(f"  transport : {transport}", C.DIM)
    cprint(f"  range     : {args.range!r}   speed={args.speed!r} "
           f"bandwidth={args.bandwidth!r}", C.DIM)
    cprint(f"  data      : {args.lower} client_port={args.client_port}"
           + (f" group={args.group}" if args.group else ""), C.DIM)
    cprint(f"  session   : {session or '<none — run setup first>'}", C.DIM)
    cprint(f"  keep-alive: {_keepalive_line(args, ka_status)}", C.DIM)
    cprint("", C.DIM)


def _print_sdp(body: bytes) -> None:
    cprint("--- parsed SDP ---", C.BOLD)
    for line in body.decode("iso-8859-1").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("m="):
            cprint(f"  media    : {line[2:]}", C.CYAN)
        elif line.startswith("a="):
            cprint(f"  attribute: {line[2:]}", C.DIM)
        elif line.startswith("s="):
            cprint(f"  session  : {line[2:]}", C.DIM)


def cmd_interactive(args) -> int:
    """Keep one control connection open and drive methods by hand."""
    uri = resolve_uri(args)
    transport = resolve_transport(args)
    c = RTSPClient(args.host, args.port, args.timeout, verbose=True)
    try:
        c.connect()
    except OSError as e:
        cprint(f"! could not connect to {c.host}:{c.port} ({e}); "
               f"commands will retry the connection.", C.YELLOW)
    data: Optional[DataChannel] = None
    readline = _setup_readline()
    cprint("Interactive TmNS RTSP session. Type 'help' (or '?') "
           "for the command list.", C.BOLD)
    if readline is not None:
        cprint("Use the up/down arrows to recall previous commands.\n", C.DIM)
    else:
        print()

    # background-receiver state (lets you pause/resume during streaming)
    bg = {"thread": None, "stop": None, "stats": None, "saved": None}
    # persistent session keep-alive (runs from SETUP until TEARDOWN)
    ka_task = {"thread": None, "stop": None}
    # keep-alive status shown in 'help' (kept off the screen otherwise)
    ka_status = {"active": False, "interval": None, "method": None}
    # RTSP state machine (RFC 2326): INIT -> READY -> PLAYING/PAUSED
    sess = {"rtsp": "INIT"}

    def start_session_keepalive():
        """Keep the RTSP session alive from SETUP until TEARDOWN, independent
        of PLAY/pause/EoD, so the range can be changed and replayed freely."""
        stop_session_keepalive()
        ping, interval = make_keepalive(c, uri, args.keepalive,
                                        args.keepalive_method, status=ka_status)
        if ping is None or interval is None:
            ka_status["active"] = False       # disabled (--keepalive 0 / no timeout)
            return
        ev = threading.Event()
        ka_task["stop"] = ev

        def loop():
            while not ev.wait(interval):       # fires every `interval` seconds
                try:
                    ping()
                except (RTSPError, OSError):
                    pass                        # surfaced via the next command
        ka_task["thread"] = threading.Thread(target=loop, daemon=True)
        ka_task["thread"].start()
        ka_status["active"] = True

    def stop_session_keepalive():
        if ka_task["stop"]:
            ka_task["stop"].set()
        if ka_task["thread"]:
            ka_task["thread"].join(timeout=2.0)
        ka_task["thread"] = ka_task["stop"] = None
        ka_status["active"] = False

    def bg_active():
        return bg["thread"] is not None and bg["thread"].is_alive()

    def start_background():
        # keep totals in bg["stats"]; silence per-message output so the prompt
        # stays usable (query totals with 'stats').  Keep-alive is handled by
        # the persistent session keep-alive thread, not the receive loop.
        bg["saved"] = (data.verbose, data.decode)
        data.verbose = False
        data.decode = False
        bg["stop"] = threading.Event()
        bg["stats"] = DataStats()

        def run():
            data.receive(duration=None, stats_interval=0.0,
                         stop_event=bg["stop"], stats=bg["stats"])
        bg["thread"] = threading.Thread(target=run, daemon=True)
        bg["thread"].start()

    def stop_background(summary=True):
        if bg["thread"] is None:
            return
        if bg["stop"]:
            bg["stop"].set()
        bg["thread"].join(timeout=3.0)
        if data is not None and bg["saved"]:
            data.verbose, data.decode = bg["saved"]
        st = bg["stats"]
        bg["thread"] = bg["stop"] = bg["saved"] = None
        ka_status["active"] = False
        if summary and st is not None:
            print_play_summary(st)
        bg["stats"] = None

    def close_data():
        nonlocal data
        stop_background(summary=False)
        if data:
            data.close()
            data = None

    def show_status():
        cprint("Session status:", C.BOLD)
        conn = (f"connected to {c.host}:{c.port}" if c.sock is not None
                else f"not connected ({c.host}:{c.port})")
        cprint(f"  connection : {conn}", C.DIM)
        cprint(f"  rtsp state : {sess['rtsp']}", C.DIM)
        to = f", timeout {c.session_timeout}s" if (c.session and c.session_timeout) else ""
        cprint(f"  session    : {c.session or '<none — run setup>'}{to}", C.DIM)
        cprint(f"  keep-alive : {_keepalive_line(args, ka_status)}", C.DIM)
        cprint(f"  request URI: {uri}", C.DIM)
        cprint(f"  transport  : {transport}", C.DIM)
        cprint(f"  range      : {args.range!r}  speed={args.speed!r}  "
               f"bandwidth={args.bandwidth!r}", C.DIM)
        if data is not None:
            dc = f"{data.lower} :{data.client_port}"
            if data.group:
                dc += f" group={data.group}"
            dc += f"  open={'yes' if data.sock is not None else 'no'}"
            recv = ("paused" if sess["rtsp"] == "PAUSED"
                    else "background" if bg_active() else "no")
            cprint(f"  data chan  : {dc}  receiving={recv}", C.DIM)
        else:
            cprint("  data chan  : <none — run setup>", C.DIM)
        s = bg["stats"]
        if s is not None:
            cprint(f"  received   : {s.messages:,} msgs, {human_bytes(s.bytes)}, "
                   f"{s.packages:,} pkgs, gaps={s.gaps}, lost={s.lost}, "
                   f"eod={s.end_of_data}", C.DIM)
        cprint(f"  CSeq       : {c.cseq}", C.DIM)

    def show_context():
        cprint("Context ('config <key> <value>' to change; 'uri'/'range' too):",
               C.BOLD)
        cprint(f"  {'uri':<16} = {uri}", C.DIM)
        cprint(f"  {'transport':<16} = {transport}", C.DIM)
        for k, (_typ, _at, hlp) in CONFIG_KEYS.items():
            cprint(f"  {k:<16} = {getattr(args, k, None)!r:<22} {hlp}", C.DIM)

    def set_context(key, val_str):
        nonlocal transport, uri
        key = key.lower().replace("-", "_")
        if key == "uri":
            uri = val_str; cprint(f"* uri = {uri}", C.DIM); return
        if key not in CONFIG_KEYS:
            cprint(f"? unknown config key: {key}  ('config' lists them)", C.YELLOW)
            return
        ok, result = parse_config_value(key, val_str)
        if not ok:
            cprint(f"! {result}", C.RED); return
        setattr(args, key, result)
        note = ""
        # Speed and Bandwidth are mutually exclusive on PLAY (Ch.26 26.4.1.3):
        # setting one clears the other so PLAY never carries both.
        if key == "speed" and result is not None and args.bandwidth is not None:
            args.bandwidth = None; note = "  (cleared bandwidth; mutually exclusive)"
        elif key == "bandwidth" and result is not None and args.speed is not None:
            args.speed = None; note = "  (cleared speed; mutually exclusive)"
        elif CONFIG_KEYS[key][1]:               # affects the Transport header
            transport = resolve_transport(args)
            note = "  (applies at next 'setup')"
        cprint(f"* {key} = {result!r}{note}", C.DIM)

    def handle(cmd, parts):
        """Execute one command. Returns 'quit' to exit, else None."""
        nonlocal data, uri
        # finalize a background stream that ended on its own (End-of-Data)
        if bg["thread"] is not None and not bg["thread"].is_alive():
            cprint("* background receive ended (End-of-Data).", C.DIM)
            stop_background(summary=True)
            sess["rtsp"] = "READY"

        if cmd in ("quit", "exit", "q"):
            return "quit"
        elif cmd == "status":
            show_status()
        elif cmd in ("help", "?", "h"):
            _print_interactive_help(uri, transport, args, c.session, ka_status)
        elif cmd == "uri":
            if len(parts) > 1:
                set_context("uri", parts[1])
            else:
                cprint(f"  uri = {uri}", C.DIM)
        elif cmd in ("config", "cfg", "context", "ctx"):
            if len(parts) == 1:
                show_context()
            elif len(parts) == 2:
                k = parts[1].lower().replace("-", "_")
                if k == "uri":
                    cprint(f"  uri = {uri}", C.DIM)
                elif k in CONFIG_KEYS:
                    cprint(f"  {k} = {getattr(args, k, None)!r}", C.DIM)
                else:
                    cprint(f"? unknown config key: {k}", C.YELLOW)
            else:
                set_context(parts[1], " ".join(parts[2:]))
        elif cmd in ("range", "speed", "bandwidth"):
            if len(parts) > 1:
                set_context(cmd, " ".join(parts[1:]))
            else:
                cprint(f"  {cmd} = {getattr(args, cmd)!r}", C.DIM)
        elif cmd == "log":
            if len(parts) > 1 and parts[1].lower() in ("off", "stop", "none"):
                p = close_logfile()
                cprint(f"* logging stopped ({p})" if p else "* not logging", C.DIM)
            else:
                try:
                    p = open_logfile(parts[1] if len(parts) > 1 else None)
                    cprint(f"* logging to {p}", C.GREEN)
                except OSError as e:
                    cprint(f"! could not open log file: {e}", C.RED)
        elif cmd == "options":
            c.options(uri)
        elif cmd == "setup":
            stop_session_keepalive()     # end any prior session's keep-alive
            close_data()                 # release any previous data channel
            data = make_data_channel(args, verbose=True)
            data.open()
            r = c.setup(uri, transport)
            apply_server_transport(data, c, args.lower)
            if r.ok:
                sess["rtsp"] = "READY"
                start_session_keepalive()  # keep alive until TEARDOWN
        elif cmd in ("play", "resume"):
            if not data:
                cprint("! run 'setup' first", C.YELLOW); return None
            r = c.play(uri, prange=args.range, speed=args.speed,
                       bandwidth=args.bandwidth)
            if r.ok:
                sess["rtsp"] = "PLAYING"
            if bg_active():
                cprint("* resumed (streaming in background; "
                       "'stats' / 'pause' / 'stop')", C.DIM)
            elif cmd == "play" and len(parts) > 1:
                # foreground bounded receive (blocking) for N seconds; the
                # persistent keep-alive thread keeps the session alive meanwhile
                secs = float(parts[1]); dur = secs if secs > 0 else None
                cprint("* Receiving "
                       + (f"for {secs:g}s" if dur else "until End-of-Data")
                       + " (Ctrl-C to stop) ...", C.DIM)
                stats = data.receive(dur, stats_interval=args.stats_interval)
                print_play_summary(stats)
                sess["rtsp"] = "READY"    # foreground receive finished
            else:
                # non-blocking background streaming; prompt returns immediately
                start_background()
                cprint("* streaming in background -- 'stats' for totals, "
                       "'pause' to pause, 'stop'/'teardown' to end", C.GREEN)
        elif cmd == "pause":
            r = c.pause(uri)
            if r.ok:
                sess["rtsp"] = "PAUSED"
            if bg_active():
                cprint("* paused; type 'play' to resume", C.DIM)
        elif cmd == "stats":
            if bg["stats"] is not None:
                s = bg["stats"]
                cprint(f"* {s.messages:,} msgs, {human_bytes(s.bytes)}, "
                       f"{s.packages:,} pkgs, mdids={compact_ranges(s.by_mdid)}, "
                       f"gaps={s.gaps}, lost={s.lost}, eod={s.end_of_data}", C.BOLD)
            else:
                cprint("* not receiving (use 'play')", C.YELLOW)
        elif cmd == "stop":
            if bg["thread"] is not None:
                stop_background(summary=True)
                sess["rtsp"] = "READY"
                cprint("* stopped receiving (session still active; 'play' to "
                       "resume, 'teardown' to end)", C.DIM)
            else:
                cprint("* not receiving", C.YELLOW)
        elif cmd == "teardown":
            stop_session_keepalive()
            stop_background(summary=True)
            c.teardown(uri)
            close_data()
            sess["rtsp"] = "INIT"
        elif cmd == "record":
            c.record(uri, prange=parts[1] if len(parts) > 1 else args.range)
        elif cmd == "redirect":
            c.redirect(uri)
        elif cmd == "announce":
            c.announce(uri, (" ".join(parts[1:])).encode())
        elif cmd in ("describe", "descr"):
            r = c.describe(uri)
            if r.ok and r.body:
                _print_sdp(r.body)
        elif cmd == "get":
            c.get_parameter(uri, parts[1:] or None)
        elif cmd == "set" and len(parts) >= 3:
            c.set_parameter(uri, {parts[1]: " ".join(parts[2:])})
        else:
            cprint(f"? unknown command: {cmd}  (type 'help')", C.YELLOW)
        return None

    try:
        while True:
            try:
                line = input(_prompt()).strip()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0].lower()
            try:
                if handle(cmd, parts) == "quit":
                    break
            except (RTSPError, OSError) as e:
                if not _connection_dead(e):
                    cprint(f"! {e}", C.RED)
                    continue
                # control connection dropped -- recover transparently
                cprint(f"! control connection lost ({e})", C.YELLOW)
                stop_session_keepalive()  # stop before touching the socket
                close_data()             # session is gone; drop the data channel
                sess["rtsp"] = "INIT"
                try:
                    c.reconnect()
                except OSError as re_err:
                    cprint(f"! reconnect to {c.host}:{c.port} failed ({re_err}); "
                           f"check the server, then retry.", C.RED)
                    continue
                cprint(f"* reconnected to {c.host}:{c.port}; session reset.",
                       C.GREEN)
                if cmd in _RETRYABLE_CMDS:
                    cprint(f"* retrying '{cmd}' ...", C.DIM)
                    try:
                        handle(cmd, parts)
                    except (RTSPError, OSError) as e2:
                        cprint(f"! {e2}", C.RED)
                else:
                    cprint("  (re-run 'setup' to start a new stream.)", C.DIM)
    finally:
        _save_history(readline)
        stop_session_keepalive()
        close_data()
        c.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tmns_rtsp_client.py",
        description="RTSP client / conformance tester for IRIG-106 TmNS RTSPDataSources.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  # Full conformance run against a source, requesting MDIDs 1-4 over UDP:
  %(prog)s test 10.0.0.5 --mdid 1-4 --lower UDP --client-port 6970

  # Stream data for 15s using an explicit request URI over a TCP data channel:
  %(prog)s stream 10.0.0.5 --uri 'rtsp://10.0.0.5:55554/TmNS/1.0/&1' \\
      --lower TCP --client-port 6970 --play-seconds 15

  # Scripted method sequence on one session (OPTIONS probe, or a full flow):
  %(prog)s method 10.0.0.5 OPTIONS
  %(prog)s method 10.0.0.5 --mdid 1 --client-port 6970 --dest-ip 10.0.0.9 \\
      OPTIONS SETUP PLAY PAUSE PLAY TEARDOWN --play-seconds 5

  # Interactive session:
  %(prog)s interactive 10.0.0.5 --mdid 1
""")
    sub = parser.add_subparsers(dest="command", required=True)

    # test
    pt = sub.add_parser("test", help="run the TmNS RTSP conformance suite")
    add_common_target_args(pt); add_uri_args(pt); add_transport_args(pt)
    add_decode_args(pt); add_receive_args(pt)
    pt.add_argument("--play-seconds", type=float, default=5.0,
                    help="seconds to receive data during PLAY "
                         "(0 = until End-of-Data; default 5)")
    pt.add_argument("--range", help="Range header value, e.g. 'ptp-clock=now-'")
    pt.add_argument("--check-timeout", action="store_true",
                    help="also test session-timeout expiry (waits for the timeout)")
    pt.set_defaults(func=cmd_test)

    # stream
    ps = sub.add_parser("stream", help="OPTIONS/SETUP/PLAY/receive/TEARDOWN once")
    add_common_target_args(ps); add_uri_args(ps); add_transport_args(ps)
    add_decode_args(ps); add_receive_args(ps)
    ps.add_argument("--play-seconds", type=float, default=10.0,
                    help="seconds to receive data (0 = until End-of-Data; "
                         "default 10)")
    ps.add_argument("--range", help="Range header, e.g. 'ptp-clock=start-end'")
    ps.add_argument("--speed", type=float, help="Speed header value")
    ps.add_argument("--bandwidth", type=int, help="Bandwidth header value (bps)")
    ps.set_defaults(func=cmd_stream)

    # method
    pm = sub.add_parser("method",
                        help="run one or more RTSP methods on a single session")
    add_common_target_args(pm); add_uri_args(pm); add_transport_args(pm)
    add_decode_args(pm); add_receive_args(pm)
    pm.add_argument("method", nargs="+",
                    help="one or more methods run in order on one session: "
                         "OPTIONS DESCRIBE SETUP PLAY PAUSE TEARDOWN "
                         "GET_PARAMETER SET_PARAMETER ANNOUNCE RECORD REDIRECT")
    pm.add_argument("--param", action="append",
                    help="param name (GET_PARAMETER) or k=v (SET_PARAMETER)")
    pm.add_argument("--range", help="Range header (PLAY/RECORD)")
    pm.add_argument("--speed", type=float, help="Speed header value (PLAY)")
    pm.add_argument("--bandwidth", type=int, help="Bandwidth header value (PLAY)")
    pm.add_argument("--play-seconds", type=float, default=5.0,
                    help="seconds to receive data on PLAY "
                         "(0 = until End-of-Data; default 5)")
    pm.add_argument("--body", help="raw request body")
    pm.add_argument("--body-file", help="file whose contents form the request body")
    pm.add_argument("--content-type", help="Content-Type for a body")
    pm.set_defaults(func=cmd_method)

    # interactive
    pi = sub.add_parser("interactive", help="drive a live control channel by hand")
    add_common_target_args(pi); add_uri_args(pi); add_transport_args(pi)
    add_decode_args(pi); add_receive_args(pi)
    pi.add_argument("--play-seconds", type=float, default=5.0)
    pi.add_argument("--range", help="Range header value")
    pi.add_argument("--speed", type=float)
    pi.add_argument("--bandwidth", type=int)
    pi.set_defaults(func=cmd_interactive)

    args = parser.parse_args(argv)
    if getattr(args, "log", None) is not None:
        try:
            p = open_logfile(None if args.log == "__AUTO__" else args.log)
            cprint(f"* logging to {p}", C.DIM)
        except OSError as e:
            cprint(f"! could not open log file: {e}", C.RED)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        cprint("\n! interrupted", C.YELLOW)
        return 130
    except (RTSPError, OSError, ValueError) as e:
        cprint(f"! error: {e}", C.RED)
        return 2
    finally:
        close_logfile()


if __name__ == "__main__":
    sys.exit(main())
