# `qos` — marked voice, signaling and noise streams

The `qos` pipeline (`qos.py`) simulates a branch office's voice traffic: some number
of RTP-style voice streams marked **EF**, an optional SIP-style signaling stream
marked **AF31** or **CS3**, and optional best-effort **noise** to create the
congestion that makes a QoS policy show its work.

All streams are UDP. Nothing needs root.

See [README.md](README.md) for the tool as a whole, and [stream.md](stream.md) for the
other pipeline.

## CLI syntax

```bash
python qosgen.py qos \
  --dst <destination-ip>          # required
  [--calls <n>]                   # default 1
  [--signaling]                   # flag, default off
  [--signaling-dscp <af31|cs3>]   # default af31
  [--noise]                       # flag, default off
  [--noise-multiplier <n>]        # default 2
  [--duration <seconds>]          # omit = run until Ctrl+C
```

| Flag | What it does |
|---|---|
| `--dst` | Destination IP for every stream. There is no `--src` — the kernel picks the source address and ephemeral source ports. If you need control over those, use the [`stream`](stream.md) pipeline. |
| `--calls` | How many voice streams to run. Each is a separate socket, thread, and destination port. |
| `--signaling` | Adds one signaling stream on port 5060. |
| `--signaling-dscp` | `af31` (DSCP 26) or `cs3` (DSCP 24). Which one your network uses is a policy decision — CS3 is the older convention. |
| `--noise` | Adds best-effort streams to compete with the marked traffic. Without congestion, a QoS policy has nothing to do and every queue looks healthy. |
| `--noise-multiplier` | Noise streams per voice call. Default 2, so `--calls 5 --noise` gives 10 noise streams. |
| `--duration` | Seconds to run. Omitted, it runs until Ctrl+C. |

## What each stream looks like

| Stream | DSCP | TOS byte | Destination port | Rate | Payload | Per-stream L3 rate |
|---|---|---|---|---|---|---|
| Voice | EF (46) | 184 | 16384, 16386, 16388 … | 50 pps | 160 B | ~75 kbps |
| Signaling | AF31 (26) or CS3 (24) | 104 / 96 | 5060 | 5 pps | 200 B | ~9 kbps |
| Noise | BE (0) | 0 | 30000, 30001, 30002 … | 20 pps | 1000 B | ~165 kbps |

Voice ports increment by 2 because RTP conventionally uses even ports (the odd one
above each is RTCP). L3 rates include the 28 bytes of IP + UDP header per packet,
which is what a router's shaper actually meters — the payload figures alone are
64 kbps, 8 kbps and 160 kbps.

## Examples

```bash
# One call, nothing else — the simplest check that EF is being marked.
python qosgen.py qos --dst 10.20.20.10 --calls 1 --duration 30

# Ten calls with signaling, no congestion.
python qosgen.py qos --dst 10.20.20.10 --calls 10 --signaling --duration 60

# Ten calls, signaling, and 20 noise streams — ~2.4 Mbps of offered load.
python qosgen.py qos --dst 10.20.20.10 --calls 10 --signaling --noise --duration 60

# Legacy signaling marking, run until Ctrl+C.
python qosgen.py qos --dst 10.20.20.10 --calls 2 --signaling --signaling-dscp cs3
```

On exit it prints what each stream actually sent:

```
Total packets sent: 3377
  noise-0: 401
  signaling: 101
  voice-0: 1001
  …
```

Compare that against the router's class counters — a gap between "sent" and
"classified" is usually the interesting part.

## How it works

**One socket per stream, because `IP_TOS` is a per-socket option.** The kernel
stamps the TOS byte of every packet leaving a socket from that one value, so a
voice stream (EF) and a noise stream (BE) can never share a socket. That is the
whole reason the code opens a fresh socket per stream rather than reusing one.

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 184)   # EF
```

TOS = DSCP << 2. The low two bits are ECN and stay zero, so DSCP 46 becomes 184.
No root is required — marking your own outbound packets is unprivileged; only
*receiving* or forging arbitrary headers needs elevated rights.

**One thread per stream.** The work is sleep-bound rather than CPU-bound, so the
GIL is irrelevant and dozens of threads cost nothing. `--calls 10 --signaling
--noise` is 31 threads.

**Drift-free pacing.** Each thread anchors its next send to a fixed cadence
instead of sleeping a fixed interval, so time spent in `sendto()` is absorbed by a
shorter sleep rather than pushing the rate down over a long run:

```python
next_send = perf_counter()
while not stop_event.is_set():
    sock.sendto(payload, (dst, port))
    next_send += interval
    sleep_for = next_send - perf_counter()
    if sleep_for > 0:
        stop_event.wait(sleep_for)
```

`stop_event.wait()` rather than `time.sleep()` means Ctrl+C stops every thread at
once, instead of each waiting out its remaining sleep.

## Confirming it on the wire

```bash
sudo tcpdump -v -n -i <iface> 'udp and (port 16384 or port 5060 or port 30000)'
```

Look for the TOS byte in the verbose output: `tos 0xb8` is EF, `0x68` is AF31,
`0x60` is CS3, `0x0` is best-effort. In Wireshark, filter on
`ip.dsfield.dscp == 46` and read the Differentiated Services field in the IP header.

On the router:

```
show policy-map interface <interface>        # Cisco
show class-of-service interface <interface>  # Juniper
```

Voice should land in the priority/EF class, signaling in the AF3 class, and noise
in class-default. Under enough load, drops should appear in class-default long
before they appear in EF — that separation is the policy doing its job.
