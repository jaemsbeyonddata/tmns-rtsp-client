# TmNS RTSP Client / Conformance Tester

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A Linux command-line tool for testing **IRIG-106 TmNS RTSP server**
implementations (`RTSPDataSource`s). It exercises the full RTSP method set —
`OPTIONS`, `SETUP`, `PLAY`, `PAUSE`, `TEARDOWN`, `DESCRIBE`, `GET_PARAMETER`,
`SET_PARAMETER`, `ANNOUNCE`, `RECORD`, `REDIRECT` — receives and **decodes** the
resulting `TmNSDataMessage` data stream (ApplicationDefinedFields options +
standard-PackageHeader Packages + MeasurementData), and validates the exchange
against the standard with both positive and negative conformance checks.

- **No dependencies** — Python 3.7+ standard library only.
- Includes a **mock TmNS source** so you can try the client without hardware.

## Protocol background

The tool targets the *RTSPControlChannel* / *RTSPDataChannel* defined in
**RCC / IRIG 106 Chapter 26** ("TmNS Data Message Transport"), with data
framed by the `TmNSMessageHeader` from **Chapter 24**. Key points the tool
relies on:

| Aspect | TmNS rule (Ch. 26) |
| --- | --- |
| Control transport | RTSP 1.0 (RFC 2326) over **TCP** |
| Roles | Sink = RTSP **client**, Source = RTSP **server** |
| Default port | **55554** (MDL `ListeningPort`) |
| Required methods | OPTIONS, SETUP, TEARDOWN, PLAY, PAUSE |
| Transport header | `TMNS/TMNSP/{UDP\|TCP};unicast\|multicast;destination=;ttl=;client_port=` |
| TCP data channel | **Separate** TCP channel (no RFC-2326 interleaving); **sink listens, source connects** |
| Range header | PTP form `ptp-clock=<start>-<end>` (`start`/`now`/ts and `end`/`now`) |
| PLAY pacing | `Speed` **or** `Bandwidth` (mutually exclusive) |
| Request URI | `TmNS_Request_Defined_URI` (MDID/PDID/MeasID lists, dest, opts, DSCP) |
| Data | `TmNSDataMessage`s; end signalled by **EndOfDataFlag** (MessageFlags bit 0) or an empty header with `MessageFlags=0x0001`, `MessageLength=24` |

The 24-byte big-endian `TmNSMessageHeader` parsed on the data channel:

```
word0: MessageVersion(4) OptionWordCount(4) Reserved(4) MessageType(4) MessageFlags(16)
word1: MessageDefinitionID(32)
word2: MessageDefinitionSequenceNumber(32)
word3: MessageLength(32)          # whole message length in bytes, incl. header
word4: MessageTimestamp(64)
```
`MessageFlags` bit 0 = `EndOfDataFlag`, bit 6 = `PlaybackDataFlag`.

> Reference: RTSP = RFC 2326 (obsoleted by RFC 7826). TmNS = IRIG 106
> Chapters 21–28.
>
> **Validated against RCC 106-24 (October 2024)** Chapters 24 and 26. The
> RTSP control/data channel (Ch.26) — methods, Transport/Range headers, URI
> syntax, port 55554, 457, End-of-Data — and the `TmNSMessageHeader` /
> MessageFlags / standard `PackageHeader` (Ch.24) are byte-identical to the
> 106-19/106-20 editions. The only 106-24 addition relevant to the tool is
> ApplicationDefinedFields option-kind `0x89` (Egress Timestamp), which the
> decoder recognizes alongside `0x88` (Ingress Timestamp).

## Files

- `tmns_rtsp_client.py` — the client / conformance tester.
- `tmns_mock_server.py` — a minimal mock `RTSPDataSource` test fixture.
- `chapter11.py` — IRIG-106 Chapter 11 packet reader + Appendix 24-A → TmNS mapping.
- `make_sample_ch10.py` — generates a small valid `.ch10` file for testing.

## Usage

```
tmns_rtsp_client.py <command> <host> [options]
```

Commands: `test`, `stream`, `method`, `interactive`.

### `test` — run the conformance suite

Runs a **positive phase** (OPTIONS → SETUP → PLAY with data reception → PAUSE →
TEARDOWN) followed by a **negative phase**, asserting the required status codes,
headers, and data behaviour, and reporting a pass/fail summary. Exit code is
`0` only if every check passes (good for CI).

```bash
python3 tmns_rtsp_client.py test 10.0.0.5 \
    --mdid 1-4 --lower UDP --client-port 6970 \
    --dest-ip 10.0.0.9 --range 'ptp-clock=now-' --play-seconds 5 --decode
```

**Positive checks:** OPTIONS 2xx; `Public` advertises the five required
methods; CSeq echo; SETUP 2xx with `Session` + `Transport`; PLAY 2xx with an
active session; data received on the data channel; no per-MDID sequence gaps;
PAUSE 2xx; TEARDOWN 2xx.

**Negative checks:**

| Check | Expected |
| --- | --- |
| Unknown method (`FLYAWAY`) | 501 Not Implemented / 405 |
| SETUP with a non-TmNS Transport profile | 461 Unsupported Transport / 4xx |
| PLAY with a malformed Range | 457 Invalid Range / 4xx |
| PLAY without an established Session | 454 Session Not Found / 4xx |
| TEARDOWN with an unknown Session id | 454 Session Not Found / 4xx |
| Session-timeout expiry (`--check-timeout`, opt-in) | 454 after the timeout |

Each negative test uses a fresh control connection so it is isolated, and a
dropped connection is accepted as a valid rejection.

Add `--decode` (or `--hexdump`) to see the received payload broken down into
options, Packages, and MeasurementData. Add `--check-timeout` to also verify
session expiry (this waits for the server's advertised timeout).

### `stream` — one full session and receive data

```bash
python3 tmns_rtsp_client.py stream 10.0.0.5 \
    --uri 'rtsp://10.0.0.5:55554/TmNS/1.0/&1' \
    --lower TCP --client-port 6970 --play-seconds 15
```

At the end of the PLAY it prints a **summary** of the data received:

```
==== PLAY summary ====
  duration      : 1:00:00  (3600.0 s)
  messages      : 1,080,000  (avg 300.0 msg/s)
  data          : 4.21 GiB  (avg 1.20 MiB/s)
  packages      : 2,160,000
  MDIDs         : 7  (1 distinct)
  PDIDs         : 200-201  (2 distinct)
  sequence gaps : 3  (est. 12 message(s) lost)
  gaps by MDID  : 7=3 gap(s)/12 lost
  duplicates/reorders : 0
  end-of-data   : yes
```

MDIDs/PDIDs are shown as compact ranges (e.g. `1-4,7`). Sequence gaps are
detected per-MDID (`MessageDefinitionSequenceNumber`, Ch.26 §26.5.1); the
**estimated lost-message count** is the sum of the sequence-number deltas
across gaps, with a per-MDID breakdown. Duplicate or out-of-order sequence
numbers are counted separately as `duplicates/reorders` (not as loss).

### Long / continuous playback

For large datasets that take a long time to transfer (e.g. a bounded
`ptp-clock=start-end` range that streams for an hour), three options make this
practical:

| Option | Effect |
| --- | --- |
| `--play-seconds 0` | receive **until End-of-Data** instead of a fixed time (Ctrl-C also stops cleanly and still prints the summary) |
| `--stats-interval N` | print a **running statistics** line every `N` seconds (default 10; `0` disables) |
| `--keepalive N` | send an RTSP keep-alive every `N` seconds so the server doesn't expire the session during the long PLAY. Omit it for **auto** (half the server's advertised session timeout); `0` disables |
| `--keepalive-method` | keep-alive request: `auto` (default — `GET_PARAMETER`, falling back to `OPTIONS` if the server returns 501/405), `options`, `get_parameter`, or `set_parameter`. Use `options` for servers that don't implement `GET_PARAMETER` |

```bash
# Play an entire recorded range to completion, with live stats and keep-alive:
python3 tmns_rtsp_client.py stream 10.0.0.5 --mdid 1 --lower TCP \
    --client-port 6970 --dest-ip 10.0.0.9 \
    --range 'ptp-clock=start-end' --play-seconds 0 --stats-interval 5
```

Running output looks like:

```
* Receiving until End-of-Data, stats every 5s, keep-alive every 30s ...
  [0:00:05] msgs=1,500 (300/s) data=6.05 MiB (1.21 MiB/s) pkgs=3,000 mdids=1 gaps=0
  [0:00:10] msgs=3,000 (300/s) data=12.1 MiB (1.21 MiB/s) pkgs=6,000 mdids=1 gaps=0
  ...
```

These options apply to `stream`, `test`, and `interactive`.

### `method` — send a single method (stateless, full wire dump)

Supports the full RFC 2326 / TmNS method set. `DESCRIBE` responses are parsed
and the SDP `m=`/`a=`/`s=` lines are printed.

```bash
python3 tmns_rtsp_client.py method 10.0.0.5 OPTIONS
python3 tmns_rtsp_client.py method 10.0.0.5 DESCRIBE --mdid 1
python3 tmns_rtsp_client.py method 10.0.0.5 GET_PARAMETER --param packets_received
python3 tmns_rtsp_client.py method 10.0.0.5 SET_PARAMETER --param rate=100
python3 tmns_rtsp_client.py method 10.0.0.5 ANNOUNCE --body-file stream.sdp
python3 tmns_rtsp_client.py method 10.0.0.5 RECORD --range 'ptp-clock=now-'
```

### `interactive` — drive the control channel by hand

Keeps one connection (and session) open. Type `help` (or `?`) at the prompt
for the full command list with parameters plus the current URI/transport/
session context. Commands: `help`, `status`, `options`, `describe`, `setup`,
`play [secs]`, `pause`, `resume`, `stats`, `stop`, `teardown`,
`record [range]`, `redirect`, `announce <sdp>`, `get [param...]`,
`set <k> <v>`, `uri <new>`, `range <v>`, `speed <v>`, `bandwidth <v>`,
`config [k [v]]`, `log [file|off]`, `quit`.

The PLAY headers — **Range**, **Speed**, and **Bandwidth** (Ch.26 Table 26-1)
— each have a shortcut command (and are also settable via `config`); the next
`play` uses them. Speed and Bandwidth are mutually exclusive, so setting one
clears the other.

`status` shows the session state at a glance — connection, RTSP state
(`INIT`/`READY`/`PLAYING`/`PAUSED`), session id and timeout, keep-alive,
request URI/transport/range, data-channel (transport, port, multicast group,
open, receiving) and received totals, and the current CSeq. (`stats` is just
the received-data totals; `status` is the whole session.)

**Streaming, pause and resume.** Plain `play` (no seconds) streams in the
**background** and returns you to the prompt immediately, so you can
`pause` and then `play` (or `resume`) to control the stream live:

```
tmns-rtsp> setup
tmns-rtsp> play            # streams in the background
tmns-rtsp> stats           # running totals: messages, bytes, packages, gaps
tmns-rtsp> pause           # server stops sending
tmns-rtsp> play            # resume
tmns-rtsp> stop            # stop receiving (keep the session) + print summary
tmns-rtsp> teardown
```

`play N` instead does a **foreground** receive for `N` seconds (blocking, with
per-message output) — handy for a quick look. Background streaming is quiet by
design; use `stats` for totals on demand.

**The session is kept alive from `setup` until `teardown`.** A background
keep-alive thread starts at SETUP and runs continuously — through PLAY, PAUSE,
End-of-Data, and while idle at the prompt — so the server never times the
session out. This lets you change the Range (`range …`/`config`) and `play`
again, repeatedly, on the same session without worrying about the timeout.
Keep-alives run **silently** (no per-interval RTSP dump); their status shows in
`help`/`?` and `status` as a `keep-alive:` line, e.g.
`options every 20s — session alive`. It stops on `teardown`, disconnect, or
quit. (Set `--keepalive 0` to disable it, or `--keepalive N` to force an
interval when the server doesn't advertise a timeout.)

**Changing the session context at runtime.** `config` (no args) lists every
context variable; `config <key> <value>` changes one; `uri`/`range` are
shortcuts. Header/behaviour values (`range`, `speed`, `bandwidth`,
`keepalive`, `stats_interval`, …) apply to the **next `play`**; transport
values (`lower`, `cast`, `client_port`, `group`, `interface`, `destination`,
`ttl`) apply to the **next `setup`**. Use `none` to clear an optional value.

```
tmns-rtsp> range ptp-clock=start-end     # set the PLAY Range
tmns-rtsp> config speed 2.0              # 2x
tmns-rtsp> setup
tmns-rtsp> play                          # uses the new Range/Speed
tmns-rtsp> config lower TCP              # switch data transport (next setup)
tmns-rtsp> config group 239.1.2.3        # multicast group (next setup)
tmns-rtsp> config                        # list the full context
```

**Logging.** Add `--log` on the command line to record everything (RTSP
requests/responses, warnings, summaries) to a timestamped file
`rtsp_log_<date>_<time>.txt` (or `--log <file>` for a specific path). In
interactive mode you can also toggle it at runtime: `log` starts a log,
`log <file>` logs to a named file, and `log off` stops. Log lines are
timestamped and stripped of color, so they capture exactly what the server
sent — handy for diagnosing things like a `CSeq`/header quirk.

The prompt supports line editing and **command history** — use the up/down
arrows to recall previous commands. History is saved to
`~/.tmns_rtsp_history`, so it persists across sessions (requires the `readline`
module, standard on Linux/macOS).

If the control connection drops (server restart, idle timeout, network blip),
the session **auto-recovers**: the next command reports the loss, reconnects
the control channel, resets the RTSP session, and (for `options`/`describe`/
`setup`) retries automatically — so you don't have to quit and relaunch. After
a reconnect the server-side session is gone, so re-run `setup` before
`play`/`pause`/`teardown`.

```bash
python3 tmns_rtsp_client.py interactive 10.0.0.5 --mdid 1 --lower UDP --client-port 6970
```

### Building the request URI

Either pass a complete `--uri`, or let the tool build a
`TmNS_Request_Defined_URI` from parts:

| Option | Meaning |
| --- | --- |
| `--mdid N` (repeatable, `A-B` for a range) | MessageDefinitionID request |
| `--pdid N` + `--delivery-mdid M` | PackageDefinitionID request |
| `--measid N` + `--delivery-mdid` + `--delivery-pdid` | MeasurementID request |
| `--dest-ip` / `--dest-port` | where the source should send data |
| `--playback l\|p` | live / playback marking |
| `--timeopt o\|c` | original / current timestamps |
| `--dscp N` | delivery DSCP |

### Transport / data-channel options

`--lower UDP|TCP`, `--cast unicast|multicast`, `--destination`, `--ttl`,
`--client-port`, `--client-port-hi`, `--group <mcast>`, `--interface <ip>`.

For **UDP** the client binds `client_port` locally and receives datagrams.
For **TCP** the client **listens** on `client_port` and the source connects to
it (per Ch. 26 §26.4.2.1).

**Multicast:** TmNS DataChannels are frequently UDP multicast. If the server
delivers to a multicast group, the client must **join** that group or the
kernel silently drops the datagrams (you'll see them in `tcpdump` but the app
receives nothing). Pass the group with `--group 239.x.y.z` (or a multicast
`--dest-ip`/`--destination`) so the client joins it; use `--interface <local-ip>`
to pick the receiving NIC. The client also **adopts the server's SETUP
`Transport` response** automatically — if the server's reply specifies a
multicast `destination` or a different `client_port`, the data channel is
re-opened to match before PLAY (and it warns if the server's lower-transport
differs from `--lower`).

## Try it locally with the mock server

```bash
# terminal 1 — start a mock source on the default port
python3 tmns_mock_server.py --port 55554 --mdid 7 --rate 50

# terminal 2 — run the conformance suite against it
python3 tmns_rtsp_client.py test 127.0.0.1 --mdid 7 \
    --lower UDP --client-port 6970 --dest-ip 127.0.0.1 --play-seconds 2
```

Expected: all checks `PASS`. Use `--range 'ptp-clock=start-end'` to exercise
the bounded-range End-of-Data path (`end-of-data: yes`).

## Chapter 11 playback (Appendix 24-A)

The mock can play back a real **IRIG-106 Chapter 11** (`.ch10`/`.c10`)
recording instead of synthetic data, encapsulating each Chapter 11 packet body
as a TmNS Package using the **Chapter 24 Appendix 24-A** field mapping:

| Chapter 11 field | → TmNS field |
| --- | --- |
| Channel ID (16b) | lower 16 bits of **MDID** (upper 16 via `--mdid-upper`) |
| Data Type (8b) | bits 15..8 of **PDID** |
| Data Type Version (8b) | bits 7..0 of **PDID** |
| Packet Body | TmNS Package payload (MeasurementData) |

```bash
# generate a small valid sample recording (or use your own .ch10)
python3 make_sample_ch10.py -o sample.ch10 -n 12

# terminal 1 — play it back as a TmNS RTSPDataSource
python3 tmns_mock_server.py --port 55554 --ch10 sample.ch10 --rate 30

# terminal 2 — receive and decode the mapped Packages until End-of-Data
python3 tmns_rtsp_client.py stream 127.0.0.1 --lower TCP --client-port 6970 \
    --dest-ip 127.0.0.1 --play-seconds 0 --decode --hexdump
```

Fidelity of the mapping:

- **Large data types are split across multiple Packages** per Appendix 24-A
  A.1.d(2): a body larger than the 16-bit `PackageLength` (or than
  `--max-msg-bytes`) is divided into standard Packages, packed into as many
  messages as needed — nothing is dropped. `--max-msg-bytes` (default 60000)
  caps each message so it fits the transport (e.g. lower it toward the MTU for
  UDP).
- **Package Flags → PackageStatusFlags** and **secondary-header absolute
  (PTP) time → MessageTimestamp** (IEEE-1588 lower 64 bits; the RTC is carried
  in `PackageTimeDelta`, and is used as a relative timestamp when no secondary
  header is present).
- **The request URI's MDID list is honored** — only packets whose mapped MDID
  is requested are streamed (no `&`-list = all MDIDs).
- Delivered messages carry the `PlaybackDataFlag`; `MessageDefinitionSequence
  Number` is a monotonic per-MDID counter (Ch.26 §26.5.1). `--loop` replays the
  file; `--rate 0` sends as fast as possible; End-of-Data is sent when the file
  is exhausted (unless looping).

Remaining limitation: wire-level TmNSDataMessage **fragmentation** (Ch.26
§26.5.3) is not used — instead, message size is bounded via the multi-Package
split above, which is the Appendix 24-A mechanism for oversized data. The
IEEE-1588 secondary-time byte order is assumed (u32 LE seconds ‖ u32 LE
nanoseconds); files using other Chapter 11 time formats fall back to the RTC.

## Notes & limits

- The mock server is a **test fixture**, not a conformance reference — it
  implements just enough behaviour to drive the client.
- Payload decoding covers the message-level structure: ApplicationDefinedFields
  options (Table 24-1), and **standard** PackageHeaders + MeasurementData
  bytes. Individual measurements inside a Package cannot be split without the
  MDL instance document; those bytes are shown raw (use `--hexdump`). Packages
  using MDL-described (custom) PackageHeaders are reported but not parsed.
- `Speed`/`Bandwidth` are sent as-is; the tool enforces that they are not both
  set on one PLAY.
- Add `-v/--verbose` to any command for the raw RTSP request/response and
  per-message data logging.

## Payload decoding

With `--decode`, each received `TmNSDataMessage` is broken down:

```
  msg mdid=7 seq=0 len=68 v=1 flags=0x0080 (live) ts=1785812738452373504
    options: PackageCount=1
    package[0] pdid=42 len=36 status=0x00 time_delta=0ns measdata=24B
```

`flags=0x0080` is the `StandardPackageHeaderFlag`; bit 6 = `PlaybackDataFlag`,
bit 0 = `EndOfDataFlag`. Add `--hexdump` to also dump the MeasurementData
bytes, and `--decode-limit N` to cap how many messages are printed (`0` =
unlimited).

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) and
[NOTICE](NOTICE).

Copyright 2026 James Y.

This licenses the tool's source code only; it does not cover the RCC/IRIG 106
standard documents, which are published separately by the Range Commanders
Council (DISTRIBUTION A: approved for public release) and are only referenced
here, not redistributed.
