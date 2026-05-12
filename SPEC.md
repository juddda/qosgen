# qosgen — Specification

A Python CLI tool for generating UDP traffic with DSCP markings to test QoS queue behavior on Cisco and Juniper routers.

## Purpose

Generate marked UDP traffic flows that simulate voice (EF), call signaling (AF31 or CS3), and optional best-effort noise so router QoS policies can be observed under congestion. Designed to run inside an EVE-NG GUI Linux Docker container connected to virtual routers.

## Target environment

- Python 3.10
- Deployed inside a prebuilt EVE-NG GUI Linux Docker container (two interfaces: docker backend + client traffic)
- EVE-NG handles container networking — no `--network host` or special docker flags required
- No elevated privileges needed (uses `setsockopt(IP_TOS, ...)` on standard UDP sockets)
- Pulled into the container via `git clone https://github.com/juddda/qosgen.git`

## CLI

Single Click command:

```
python qosgen.py --dst <ip> [options]
```

### Required
- `--dst IP` — destination IP address for all streams

### Options
- `--calls N` — number of voice streams (default: 1)
- `--signaling` — add one signaling stream (flag, default: off)
- `--signaling-dscp [af31|cs3]` — DSCP value for signaling (default: `af31`)
- `--noise` — add one best-effort noise stream per voice call (flag, default: off)
- `--duration SECONDS` — runtime; omit to run until Ctrl+C

## Stream definitions

### Voice streams
- DSCP: EF (46), TOS byte = 184
- Rate: 50 packets/second per stream
- Payload: 160 bytes (zero-filled is fine)
- Destination port: 16384, 16386, 16388, … (incrementing by 2 per call, RTP-style even ports)
- One thread per call

### Signaling stream (when `--signaling` is set)
- DSCP: AF31 (26, TOS=104) by default; CS3 (24, TOS=96) if `--signaling-dscp cs3`
- Rate: ~5 packets/second
- Payload: 200 bytes
- Destination port: 5060 (SIP)
- One thread total

### Noise streams (one per voice call when `--noise` is set)
- DSCP: 0 (best-effort, TOS=0)
- Rate: 20 packets/second per stream
- Payload: 1000 bytes
- Destination port: 30000, 30001, 30002, … (incrementing by 1)
- Bandwidth: ~2× the paired voice stream

## Mechanism

- Standard UDP sockets: `socket.socket(AF_INET, SOCK_DGRAM)`
- DSCP set per socket: `sock.setsockopt(IPPROTO_IP, IP_TOS, value)` — kernel writes the TOS byte into the IP header of every outbound packet
- One socket per stream (TOS is per-socket, so voice and noise can't share a socket)
- One thread per stream, all spawned from `main()`
- Pacing loop uses `time.perf_counter()` with absolute scheduled times to avoid drift:

  ```python
  next_send = perf_counter()
  while not stop_event.is_set():
      sock.sendto(payload, (dst, port))
      next_send += interval
      sleep_for = next_send - perf_counter()
      if sleep_for > 0:
          stop_event.wait(sleep_for)
  ```

- Shutdown via `threading.Event` set by SIGINT handler — Ctrl+C signals all threads to exit cleanly, main thread joins them with a short timeout
- Final summary printed on exit: total packets sent across all streams

## Behavior example

```
python qosgen.py --dst 10.0.0.1 --calls 10 --signaling --noise --duration 60
```

Spawns:
- 10 voice threads (EF, ports 16384–16402, 50 pps × 160 bytes)
- 1 signaling thread (AF31, port 5060, 5 pps × 200 bytes)
- 10 noise threads (BE, ports 30000–30009, 20 pps × 1000 bytes)

= 21 threads, ~2.4 Mbps offered load, runs for 60 seconds, prints summary on exit.

## Repository structure

```
qosgen/
├── qosgen.py        # the program (single file, ~200 lines)
├── requirements.txt # just: click
├── README.md        # usage examples + DSCP reference table
├── SPEC.md          # this file
└── .gitignore       # standard Python
```

Run with:
```
pip install -r requirements.txt
python qosgen.py --dst <ip> ...
```

No `setup.py`, no packaging, no entry points.

## DSCP reference

| Marking | DSCP (decimal) | DSCP (binary) | TOS byte | Use |
|---------|----------------|---------------|----------|-----|
| Default / BE | 0  | 000000 | 0   | Best effort, noise traffic |
| CS3          | 24 | 011000 | 96  | Legacy signaling marking |
| AF31         | 26 | 011010 | 104 | Modern call signaling |
| EF           | 46 | 101110 | 184 | Voice bearer (RTP) |

(TOS = DSCP shifted left by 2 bits to leave room for ECN bits in the lower 2 positions.)

## Out of scope for v1

Deliberately deferred — easy to add later:

- Configurable noise multiplier (`--noise-multiplier N`)
- Multiple destination IPs
- RTP header simulation
- TCP signaling
- Custom arbitrary DSCP streams (`--custom dscp:pps:size:port`)
- Per-interface socket binding (`--interface eth1`)
- Live stats output (current pps, bandwidth) during run
- Config file support
- Video stream preset (AF41)

## Design constraints

- Single source file, target ~200 lines
- Rough and ready over polished — working first, polish later
- Code clarity prioritized for learning purposes (developer is new to network programming, wants to genuinely understand the code)
- Inline comments should explain *why*, especially around `setsockopt`/TOS, threading model, and the pacing loop
- Use type hints where they add clarity, skip them where they don't

## Verification approach

To confirm the tool is working correctly:

1. Run a generator stream against a destination on the lab network
2. Capture on the destination side with `tcpdump -v -n -i <iface> udp` — check the TOS field in the output matches the expected value (e.g. `tos 0xb8` for EF)
3. On the router under test, observe queue counters incrementing in the expected class:
   - Cisco: `show policy-map interface <int>`
   - Juniper: `show class-of-service interface <int>`
