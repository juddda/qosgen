# qosgen

A traffic generator for testing router QoS policies. It sends UDP or TCP streams with
whatever DSCP marking, rate, size, and 5-tuple you specify, so you can watch a router
classify, queue, police, or re-mark them.

Built for lab work — EVE-NG, GNS3, or any pair of hosts either side of a router under
test. It is a single-purpose tool: it generates traffic and counts what it sent. It does
not measure latency, jitter, or loss (use iperf3 or a hardware tester for that), and it
does not receive traffic. What it gives you is **precise control over what leaves the
host**, which is exactly what you need when the question is "does my QoS policy do what
I think it does?"

Two pipelines, one subcommand each:

| Command | Reference | What it generates |
|---|---|---|
| **`qos`** | [qos.md](qos.md) | Marked UDP: voice (EF), call signaling (AF31/CS3), best-effort noise. Simulates a branch office under congestion. |
| **`stream`** | [stream.md](stream.md) | One unmarked (DSCP 0) UDP **or** TCP stream from an explicitly bound source IP and port. For testing what a router *does* to traffic — classification, marking, policing. |

## Requirements

- **Python 3.10 or newer**
- **[click](https://click.palletsprojects.com/)** — the only dependency
- **Linux** for the full feature set. It runs on macOS and BSD, but the dummy-interface
  and non-local-bind techniques in [stream.md](stream.md) are Linux-specific.
- **No root**, with two exceptions: binding a source port below 1024, and creating
  dummy interfaces. Setting DSCP on your own outbound packets is unprivileged.

## Install

```bash
git clone https://github.com/juddda/qosgen.git
cd qosgen
pip install -r requirements.txt
```

A virtual environment keeps it isolated:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

To update an existing clone:

```bash
cd qosgen
git pull
```

There is no packaging step, no `setup.py`, and no entry point to install — clone it and
run `python qosgen.py`. That is deliberate: on a lab host `git pull` is the entire
upgrade process.

## Quick start

Send 100 unmarked UDP packets per second, from a specific source socket, for 30 seconds:

```bash
python qosgen.py stream \
  --src-ip 10.10.10.10 --dst-ip 10.20.20.10 \
  --protocol udp --src-port 5000 --dst-port 6000 \
  --pps 100 --size 512 --duration 30
```

Simulate ten voice calls with signaling and congestion:

```bash
python qosgen.py qos --dst 10.20.20.10 --calls 10 --signaling --noise --duration 60
```

Every command self-documents:

```bash
python qosgen.py --help
python qosgen.py stream --help
python qosgen.py qos --help
```

Omit `--duration` in either pipeline to run until Ctrl+C. Both print a per-stream packet
count on exit.

## The two pipelines

### `qos` — marked voice scenario

```bash
python qosgen.py qos --dst <ip> [--calls N] [--signaling] [--signaling-dscp af31|cs3] \
                     [--noise] [--noise-multiplier N] [--duration SECONDS]
```

| Stream | DSCP | TOS | Port(s) | Rate | Payload |
|---|---|---|---|---|---|
| Voice | EF (46) | 184 | 16384, 16386, 16388 … | 50 pps | 160 B |
| Signaling | AF31 / CS3 | 104 / 96 | 5060 (SIP) | 5 pps | 200 B |
| Noise | BE (0) | 0 | 30000, 30001, 30002 … | 20 pps | 1000 B |

Only `--dst` is required. Every stream goes to that one destination; the kernel picks
the source address and ephemeral source ports.

| Flag | Default | What it does |
|---|---|---|
| `--dst IP` | *required* | Destination for every stream. |
| `--calls N` | 1 | Number of voice streams. Each gets its own socket, thread, and even destination port. |
| `--signaling` | off | Adds one signaling stream on port 5060. |
| `--signaling-dscp af31\|cs3` | `af31` | Marking for that stream. CS3 is the older convention. |
| `--noise` | off | Adds best-effort streams. Without congestion a QoS policy has nothing to do. |
| `--noise-multiplier N` | 2 | Noise streams per voice call, so `--calls 5 --noise` gives 10. |
| `--duration SECONDS` | until Ctrl+C | Runtime. |

```bash
# One call, nothing else — the simplest check that EF marking is applied.
python qosgen.py qos --dst 10.20.20.10 --calls 1 --duration 30

# Ten calls plus signaling, no congestion. 11 streams, ~760 kbps.
python qosgen.py qos --dst 10.20.20.10 --calls 10 --signaling --duration 60

# Ten calls, signaling, and 20 noise streams — 31 streams, ~4.1 Mbps offered.
python qosgen.py qos --dst 10.20.20.10 --calls 10 --signaling --noise --duration 60

# Heavier congestion without more calls: 5 noise streams per call.
python qosgen.py qos --dst 10.20.20.10 --calls 4 --noise --noise-multiplier 5 --duration 60

# Legacy CS3 signaling, running until you stop it.
python qosgen.py qos --dst 10.20.20.10 --calls 2 --signaling --signaling-dscp cs3
```

On exit it prints what each stream actually sent, which is the number to compare against
the router's class counters:

```
$ python qosgen.py qos --dst 10.20.20.10 --calls 1 --signaling --noise --duration 20
Starting 4 stream(s) → 10.20.20.10 for 20s

Total packets sent: 1904
  noise-0: 401
  noise-1: 401
  signaling: 101
  voice-0: 1001
```

Full reference: **[qos.md](qos.md)**.

### `stream` — one arbitrary stream

```bash
python qosgen.py stream --src-ip <ip> --dst-ip <ip> --protocol <udp|tcp> \
                        --src-port <port> --dst-port <port> \
                        [--pps <rate>] [--size <bytes>] [--duration <seconds>]
```

The first five are required; `--pps` defaults to 10 and `--size` to 512. Traffic is
always unmarked (DSCP 0), the source IP and port are bound explicitly, and TCP needs a
listener on the far end.

Full reference: **[stream.md](stream.md)** — including how to source traffic from
addresses this host doesn't own, which is what you need when a QoS ACL matches subnets
your generator isn't in.

## Verifying the marking

The tool reports what it *sent*. Proving what *arrived*, and what the router did to it,
takes a capture:

```bash
# on the receiving host
sudo tcpdump -v -n -i <iface> 'host <generator-ip>'
```

The verbose output shows the TOS byte: `tos 0xb8` (EF), `0x68` (AF31), `0x60` (CS3),
`0x0` (unmarked). In Wireshark, look at **Differentiated Services Field** in the IP
header, or filter with `ip.dsfield.dscp == 46`.

Capturing on both sides of the router is the useful trick: send unmarked with `stream`,
capture before and after, and the DSCP change proves the router's policy is marking.

On the router itself:

```
show policy-map interface <interface>          # Cisco IOS / IOS-XE
show class-of-service interface <interface>    # Juniper
```

## Troubleshooting

| Message or symptom | Cause and fix |
|---|---|
| `--src-ip X is not an address on this host` | `bind()` only accepts addresses the kernel owns. Add a dummy interface, or set `net.ipv4.ip_nonlocal_bind=1` — see [stream.md](stream.md#sourcing-from-an-address-this-host-doesnt-own). |
| `not allowed to bind source port N` | Ports below 1024 are privileged. Run under `sudo`, or choose a source port above 1023. |
| `nothing is listening on IP:PORT` | TCP needs a peer to complete the handshake. Start `nc -l <port>` at the far end, or use `--protocol udp`, which needs no listener. |
| `source port N is already in use` | Another process — often a previous run still in `TIME_WAIT`. Pick another `--src-port` or wait. |
| `no route from X to Y` | The host has no path to the destination. Check `ip route` and the interface state. |
| `ModuleNotFoundError: No module named 'click'` | Wrong interpreter. Common under `sudo`, which resets `PATH` to root's and misses your venv or conda env: `sudo PYTHON="$(command -v python)" …`. |
| Packets sent, nothing arrives | Check a firewall on either host, the routing in both directions, and **uRPF** on the router's ingress interface — strict reverse-path forwarding silently drops traffic whose source it can't route back toward. |
| Router shows no packets in the expected class | The ACL may not match. Confirm it keys on the protocol you're sending (a `permit tcp … eq 443` entry ignores UDP on port 443) and on the right direction. |
| Packet count at the receiver doesn't match | `--size` above 1472 fragments on a 1500-byte MTU, so one send becomes several packets. Keep UDP payloads at or below 1472. |
| TCP sends fewer, larger packets than expected | Shouldn't happen — `TCP_NODELAY` is set. If you see it, something downstream is coalescing (GSO/GRO offload on the NIC). Check `ethtool -k <iface>`. |

## Repository layout

```
qosgen.py            entry point — the Click group, nothing else
qos.py               the qos pipeline: constants, worker, CLI options
stream.py            the stream pipeline: socket setup, worker, CLI options
qos.md               qos reference
stream.md            stream reference
SPEC.md              the specification both pipelines are built to
lab/
  qos-policy-1mb.cfg  example Cisco hierarchical shaper + queueing policy
  four-sources.sh     launcher: four concurrent streams from four source subnets
requirements.txt     click
LICENSE              MIT
```

**One module per pipeline.** Each owns everything it needs — constants, socket setup,
worker threads, CLI options — so you can read one end to end without jumping between
files. Adding a pipeline means writing one module and adding one `add_command()` line to
`qosgen.py`. Small duplication between pipelines is the accepted price of that.

## How it works

- **DSCP is set per socket** with `setsockopt(IPPROTO_IP, IP_TOS, value)`. The kernel
  writes that byte into the IP header of every packet leaving the socket. Because it is a
  *per-socket* option, streams with different markings cannot share a socket — hence one
  socket per stream.
- **TOS = DSCP << 2.** The low two bits are ECN and stay zero, so DSCP 46 (EF) is TOS 184.
- **One thread per stream.** The work is sleep-bound, not CPU-bound, so the GIL doesn't
  matter and dozens of threads are cheap.
- **Pacing uses absolute scheduled times** from `time.perf_counter()`, so the rate doesn't
  drift as send costs accumulate.
- **Shutdown is a `threading.Event`.** Sleeps go through `event.wait()`, so Ctrl+C stops
  every stream within milliseconds instead of each waiting out its sleep.

## DSCP reference

| Marking | DSCP | Binary | TOS byte | Typical use |
|---|---|---|---|---|
| Default / BE | 0 | 000000 | 0 | Best effort, noise, `stream` traffic |
| CS1 | 8 | 001000 | 32 | Scavenger / bulk |
| AF21 | 18 | 010010 | 72 | Transactional data |
| CS3 | 24 | 011000 | 96 | Legacy call signaling |
| AF31 | 26 | 011010 | 104 | Modern call signaling |
| CS4 | 32 | 100000 | 128 | Realtime interactive / video |
| AF41 | 34 | 100010 | 136 | Interactive video |
| EF | 46 | 101110 | 184 | Voice bearer (RTP) |
| CS6 | 48 | 110000 | 192 | Network control (routing protocols) |

## Notes for EVE-NG

The tool was written to run on Linux nodes inside EVE-NG, wired to virtual Cisco or
Juniper routers.

- No special Docker flags or `--network host` are needed; EVE-NG handles node networking.
- A second NIC on a Linux node stays `NO-CARRIER` until you draw the link on the canvas.
  Netplan will not configure an interface with no carrier, so the address silently never
  appears. Adding or rewiring an interface usually requires stopping the node.
- Give the lab-facing interface a static address and **no default route** — leave the
  default on the management interface so the node keeps its internet access. Point
  specific prefixes at the lab router instead.
- `lab/qos-policy-1mb.cfg` is a working example: a 1 Mbps hierarchical shaper with EF
  priority, per-class bandwidth ratios, and WRED, scaled down from a production MPLS
  WAN policy.

## License

[MIT](LICENSE) — use it, fork it, ship it, no warranty.
