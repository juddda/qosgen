# qosgen

A small Python 3.10 CLI that generates traffic for exercising router QoS policies — marked voice/signaling/noise streams, or a single arbitrary stream with an explicit source socket.

Designed to run inside an EVE-NG GUI Linux Docker container connected to virtual Cisco / Juniper routers. No root needed: DSCP is set per-socket via `setsockopt(IP_TOS, ...)` and the kernel stamps the TOS byte on every outbound packet.

## Install

```bash
pip install -r requirements.txt
```

## Pipelines

Each pipeline is one subcommand, and lives in one self-contained module:

| Command  | Module      | Traffic                                                        |
|----------|-------------|----------------------------------------------------------------|
| `qos`    | `qos.py`    | Marked UDP: voice (EF), signaling (AF31/CS3), noise (BE)        |
| `stream` | `stream.py` | One unmarked (DSCP 0) UDP **or** TCP stream from a bound source |

```bash
python qosgen.py --help
```

### `qos` — marked voice scenario

```bash
python qosgen.py qos --dst <ip> [--calls N] [--signaling] [--signaling-dscp af31|cs3] \
                     [--noise] [--noise-multiplier N] [--duration SECONDS]
```

Example — 10 voice calls + signaling + noise for 60 seconds:

```bash
python qosgen.py qos --dst 10.0.0.1 --calls 10 --signaling --noise --duration 60
```

That's 31 concurrent UDP streams at the default noise multiplier of 2.

| Stream    | DSCP       | TOS      | Port(s)                | Rate    | Payload |
|-----------|------------|----------|------------------------|---------|---------|
| Voice     | EF (46)    | 184      | 16384, 16386, 16388 …  | 50 pps  | 160 B   |
| Signaling | AF31 / CS3 | 104 / 96 | 5060 (SIP)             | ~5 pps  | 200 B   |
| Noise     | BE (0)     | 0        | 30000, 30001, 30002 …  | 20 pps  | 1000 B  |

### `stream` — one arbitrary stream

Full reference: **[stream.md](stream.md)**.

```bash
python qosgen.py stream --src-ip <ip> --dst-ip <ip> --protocol <udp|tcp> \
                        --src-port <port> --dst-port <port> \
                        [--pps <rate>] [--size <bytes>] [--duration <seconds>]
```

Example:

```bash
python qosgen.py stream --src-ip 10.10.10.10 --dst-ip 10.20.20.20 --protocol udp \
                        --src-port 5000 --dst-port 6000 --pps 100 --size 512 --duration 60
```

- `--src-ip`, `--dst-ip`, `--protocol`, `--src-port`, `--dst-port` are required; `--pps` (default 10), `--size` (default 512), and `--duration` are optional. Omit `--duration` to run until Ctrl+C.
- Traffic is always **unmarked — DSCP 0**, so it lands in the default class. That makes it the counterpart to `qos`: the stream a policy should *not* prioritize.
- `--src-ip` must be an address this host actually owns. The kernel refuses to bind anything else, and forging a foreign source address would require a raw socket and root — deliberately out of scope.
- `--protocol tcp` needs something listening on `--dst-port` (e.g. `nc -l 6000`) or the connection is refused. UDP sends regardless.
- For TCP, `--pps` means *sends* per second. Nagle is disabled (`TCP_NODELAY`) so each send goes out on its own rather than being coalesced.

## Verifying

1. Capture on the receiver: `tcpdump -v -n -i <iface> udp` — confirm the TOS byte arrives intact (e.g. `tos 0xb8` for EF, `tos 0x0` for `stream`).
2. On the router under test, watch the queue counters:
   - Cisco: `show policy-map interface <int>`
   - Juniper: `show class-of-service interface <int>`

## Layout

```
qosgen.py   # entry point: the click group, nothing else
qos.py      # the qos pipeline — constants, worker, CLI options
stream.py   # the stream pipeline — socket setup, worker, CLI options
lab/        # router configs used to test against
```

Each pipeline module owns everything it needs, so you can read one end-to-end without jumping between files. Adding a pipeline = one new module + one `add_command()` line in `qosgen.py`.

## Design notes

- **One socket per stream.** `IP_TOS` is a per-socket option, so streams with different DSCPs can't share a socket.
- **One thread per stream**, all spawned from the pipeline's command. Work is sleep-bound, not CPU-bound.
- **Drift-free pacing** via absolute scheduled times with `time.perf_counter()`; `threading.Event.wait(...)` instead of `time.sleep(...)` keeps Ctrl+C responsive.
- **`qos` doesn't bind, `stream` does.** Left alone, the kernel picks the source IP and a random ephemeral source port. `stream` calls `bind((src_ip, src_port))` to pin both, so the 5-tuple on the wire is exactly the one a router's classifier will match on.
- TOS = DSCP shifted left by 2 bits (lower 2 bits are ECN).

## DSCP reference

| Marking      | DSCP | TOS | Use                             |
|--------------|------|-----|---------------------------------|
| Default / BE | 0    | 0   | Best effort, noise, `stream`    |
| CS3          | 24   | 96  | Legacy signaling marking        |
| AF31         | 26   | 104 | Modern call signaling           |
| EF           | 46   | 184 | Voice bearer (RTP)              |
