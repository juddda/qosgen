# `stream` — one unmarked UDP or TCP stream

The `stream` pipeline (`stream.py`) generates a single TCP or UDP traffic stream from an
explicitly bound source socket, unmarked at **DSCP 0**. It's the counterpart to the `qos`
pipeline: the traffic a policy should *not* prioritize.

See [README.md](README.md) for the tool as a whole.

## CLI syntax

```bash
python qosgen.py stream \
  --src-ip   <source-ip>        # required
  --dst-ip   <destination-ip>   # required
  --protocol <udp|tcp>          # required
  --src-port <1-65535>          # required
  --dst-port <1-65535>          # required
  [--pps      <rate>]           # optional, default 10
  [--size     <bytes>]          # optional, default 512, max 65507
  [--duration <seconds>]        # optional, omit = run until Ctrl+C
```

| Flag | What it does |
|---|---|
| `--src-ip` | Source address written into every packet. Must be an address **this host owns** (`ip addr` / `ifconfig`) — the kernel refuses to bind anything else. |
| `--dst-ip` | Where the traffic goes. Routing decides which interface it leaves by. |
| `--protocol` | `udp` = fire-and-forget datagrams. `tcp` = handshake first, so something must be listening. |
| `--src-port` | Pinned, not random. Routers often classify on source port, so it has to be yours to choose. |
| `--dst-port` | Destination port in the header. |
| `--pps` | Packets per second (UDP) or `send()` calls per second (TCP). |
| `--size` | Payload bytes per packet — zero-filled. Offered rate = `pps × size × 8` bits/s. |
| `--duration` | Seconds to run. Omitted, it runs until Ctrl+C. |

Working examples:

```bash
# UDP, ~410 kbps for a minute
python qosgen.py stream --src-ip 10.10.10.10 --dst-ip 10.20.20.20 \
  --protocol udp --src-port 5000 --dst-port 6000 --pps 100 --size 512 --duration 60

# TCP — needs `nc -l 6000` running on 10.20.20.20 first
python qosgen.py stream --src-ip 10.10.10.10 --dst-ip 10.20.20.20 \
  --protocol tcp --src-port 5000 --dst-port 6000 --pps 100 --size 512 --duration 60

# UDP until you stop it
python qosgen.py stream --src-ip 10.10.10.10 --dst-ip 10.20.20.20 \
  --protocol udp --src-port 5000 --dst-port 6000
```

## What it does, step by step

**1. Open the socket** — `SOCK_DGRAM` for UDP, `SOCK_STREAM` for TCP. That single choice is
the whole protocol difference at the API level.

**2. `SO_REUSEADDR`** — because `--src-port` is fixed, not ephemeral. After a TCP run that
port lingers in `TIME_WAIT` for a minute or two; without this flag the next run fails with
"address already in use".

**3. `setsockopt(IPPROTO_IP, IP_TOS, 0)`** — sets the TOS byte to 0, i.e. **DSCP 0,
unmarked**. This is the same mechanism `qos.py` uses to stamp EF (184) or AF31 (104);
`stream` just sets it to zero on purpose. The kernel writes that byte into every outbound
packet — no root needed, which is the reason the whole tool works inside the container.

**4. `bind((src_ip, src_port))`** — the step that makes the source *explicit*. Skip it and
the kernel picks the source IP (whichever address owns the route to the destination) and a
random port around 50000. Binding pins both halves, so the 5-tuple on the wire is exactly
what you typed — which matters because that's what the router's classifier matches on.

**5. TCP only:**

- `TCP_NODELAY` disables Nagle's algorithm, which would otherwise hold small writes back and
  merge them. Without it, 100 sends could leave the host as a handful of large packets and
  `--pps` would be meaningless.
- `connect()` performs the three-way handshake. This is the call that fails with "connection
  refused" when nothing is listening.

**6. The send loop**, in its own thread:

```python
next_send = perf_counter()
while not stop_event.is_set():
    send()                                  # sendto() for UDP, sendall() for TCP
    next_send += interval                   # interval = 1.0 / pps
    sleep_for = next_send - perf_counter()
    if sleep_for > 0:
        stop_event.wait(sleep_for)
```

Two ideas worth keeping:

- `next_send` accumulates against a **fixed cadence**, so the time spent inside `send()` is
  absorbed by a shorter sleep instead of pushing every packet later. A naive
  `sleep(interval)` loop drifts and quietly under-delivers.
- `stop_event.wait()` instead of `time.sleep()` returns the instant the event is set, so
  Ctrl+C stops the stream immediately rather than after the current sleep expires.

**7. Shutdown** — SIGINT or the duration expiring sets `stop_event`; the worker exits at its
next check, closes the socket, and reports its count:
`Sent 6000 packet(s), 3072000 bytes of payload.`

Setup failures (source IP not local, port in use, connection refused, no route) exit
non-zero with a plain-English explanation instead of a traceback.

## Confirming it on the wire

```bash
# on the receiver
tcpdump -v -n -i <iface> 'host 10.10.10.10 and port 6000'
```

Look for `tos 0x0` and the source shown as `10.10.10.10.5000` — that proves both the marking
and the bind. Then on the router, `show policy-map interface <int>` (Cisco) should show these
packets landing in `class-default` while the `qos` pipeline's EF traffic goes to the priority
queue.
