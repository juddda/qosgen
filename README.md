# qosgen

A Python 3.10 single-file CLI that generates marked UDP traffic — voice (EF), call signaling (AF31 / CS3), and optional best-effort noise — so router QoS policies can be observed under congestion.

Designed to run inside an EVE-NG GUI Linux Docker container connected to virtual Cisco / Juniper routers. No root needed: DSCP is set per-socket via `setsockopt(IP_TOS, ...)` and the kernel stamps the TOS byte on every outbound packet.

> **Status:** spec-only. See [SPEC.md](SPEC.md) for the full specification. `qosgen.py` not yet implemented.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
python qosgen.py --dst <ip> [--calls N] [--signaling] [--signaling-dscp af31|cs3] [--noise] [--duration SECONDS]
```

Example — 10 voice calls + signaling + noise for 60 seconds:

```bash
python qosgen.py --dst 10.0.0.1 --calls 10 --signaling --noise --duration 60
```

That's 21 concurrent UDP streams and ~2.4 Mbps offered load.

## Streams

| Stream    | DSCP       | TOS      | Port(s)                | Rate    | Payload |
|-----------|------------|----------|------------------------|---------|---------|
| Voice     | EF (46)    | 184      | 16384, 16386, 16388 …  | 50 pps  | 160 B   |
| Signaling | AF31 / CS3 | 104 / 96 | 5060 (SIP)             | ~5 pps  | 200 B   |
| Noise     | BE (0)     | 0        | 30000, 30001, 30002 …  | 20 pps  | 1000 B  |

## Verifying

1. Capture on the receiver: `tcpdump -v -n -i <iface> udp` — confirm the TOS byte arrives intact (e.g. `tos 0xb8` for EF).
2. On the router under test, watch the queue counters:
   - Cisco: `show policy-map interface <int>`
   - Juniper: `show class-of-service interface <int>`

## Design notes

- **One UDP socket per stream.** `IP_TOS` is a per-socket option, so streams with different DSCPs can't share a socket.
- **One thread per stream**, all spawned from `main()`. Work is sleep-bound, not CPU-bound.
- **Drift-free pacing** via absolute scheduled times with `time.perf_counter()`; `threading.Event.wait(...)` instead of `time.sleep(...)` keeps Ctrl+C responsive.
- TOS = DSCP shifted left by 2 bits (lower 2 bits are ECN).

## DSCP reference

| Marking      | DSCP | TOS | Use                        |
|--------------|------|-----|----------------------------|
| Default / BE | 0    | 0   | Best effort, noise traffic |
| CS3          | 24   | 96  | Legacy signaling marking   |
| AF31         | 26   | 104 | Modern call signaling      |
| EF           | 46   | 184 | Voice bearer (RTP)         |
