"""stream — a single unmarked UDP or TCP stream from an explicit source socket.

This module owns the whole `stream` pipeline: its socket setup, its send
worker, and its `stream` CLI command. qosgen.py just imports the command and
hangs it off the top-level group.

Three things make this pipeline different from qos.py:

  1. It binds. The qos pipeline lets the kernel choose the source address and
     an ephemeral source port. Here the source IP and source port are part of
     what we're testing — a router's QoS classifier may match on them — so we
     bind() them explicitly before sending anything.

  2. It speaks TCP as well as UDP. TCP changes the shape of the work: we must
     connect() to a listener first, and "packets per second" becomes "sends
     per second" (see the TCP_NODELAY note in open_stream_socket).

  3. It is deliberately unmarked — DSCP 0, TOS byte 0. This is the traffic you
     point at a policy to confirm it lands in the default class while the qos
     pipeline's marked streams land in their priority queues.
"""

import errno
import random
import signal
import socket
import threading
from time import perf_counter

import click

# TOS = DSCP << 2 (lower 2 bits are ECN). DSCP 0 means unmarked / best effort.
TOS_UNMARKED = 0

DEFAULT_PPS   = 10    # gentle by default — pass --pps to offer real load
DEFAULT_BYTES = 512

# IANA dynamic/private range. When --dst-port is omitted we pick from here, because
# this pipeline usually models a server talking *to* a user: the interesting port is
# the source (the application), and the destination is just whatever ephemeral port
# the client happened to open. Windows clients allocate from exactly this range.
EPHEMERAL_LOW  = 49152
EPHEMERAL_HIGH = 65535


def open_stream_socket(src_ip: str, src_port: int, dst_ip: str,
                       dst_port: int, protocol: str) -> socket.socket:
    """Build and prepare the socket. Raises OSError if the OS refuses any step."""

    sock_type = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
    sock = socket.socket(socket.AF_INET, sock_type)

    # Why SO_REUSEADDR: we bind a *fixed* source port rather than letting the
    # kernel pick a free one. After a TCP run that port sits in TIME_WAIT for
    # a minute or two, and a re-bind would fail with "address already in use".
    # This lets back-to-back runs reuse --src-port immediately.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Why set TOS at all when 0 is already the default: the spec requires this
    # pipeline to be unmarked, so we state it in code rather than relying on a
    # default. It also documents where you'd change it to mark this stream.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, TOS_UNMARKED)

    # Why bind: without it the kernel picks the source IP (whichever address
    # owns the route to dst) and a random high source port. bind() pins both,
    # so the 5-tuple on the wire is exactly the one you asked for.
    sock.bind((src_ip, src_port))

    if protocol == "tcp":
        # Why TCP_NODELAY: Nagle's algorithm holds small writes back and merges
        # them into fewer, larger segments. That would make --pps a lie — 100
        # send() calls could leave the host as a handful of packets. Disabling
        # Nagle pushes each send out on its own, which is what a generator wants.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # TCP needs a peer that answers the handshake; UDP does not. This is
        # the step that fails with "connection refused" when nothing listens.
        sock.connect((dst_ip, dst_port))

    return sock


def explain_setup_error(exc: OSError, src_ip: str, src_port: int,
                        dst_ip: str, dst_port: int) -> str:
    """Turn a bare OSError from socket setup into something actionable."""

    if isinstance(exc, ConnectionRefusedError):
        return (f"nothing is listening on {dst_ip}:{dst_port} — TCP needs a peer to "
                f"complete the handshake. Start one there (e.g. `nc -l {dst_port}`), "
                f"or use --protocol udp, which sends without a listener.")
    if exc.errno == errno.EADDRNOTAVAIL:
        return (f"--src-ip {src_ip} is not an address on this host. The kernel only "
                f"lets you bind addresses it owns; sending from a foreign source IP "
                f"means forging the header yourself with a raw socket as root, which "
                f"this tool deliberately does not do. Check `ip addr`.")
    if exc.errno == errno.EADDRINUSE:
        return (f"source port {src_port} is already in use on {src_ip}. Pick another "
                f"--src-port, or wait for the previous run's socket to clear.")
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (f"not allowed to bind source port {src_port}. Ports below 1024 are "
                f"privileged — re-run under sudo, or pick a --src-port above 1023.")
    if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH):
        return f"no route from {src_ip} to {dst_ip}. Check the interface and routing table."
    return str(exc)


def stream_worker(src_ip, src_port, dst_ip, dst_port, protocol, pps,
                  payload_size, stop_event, result) -> None:
    """Send to (dst_ip, dst_port) at `pps` until stop_event is set.

    Reports back through `result`: 'sent' packets and, on failure, 'error'.
    """

    try:
        sock = open_stream_socket(src_ip, src_port, dst_ip, dst_port, protocol)
    except OSError as exc:
        result["error"] = explain_setup_error(exc, src_ip, src_port, dst_ip, dst_port)
        # Unblock the main thread immediately — there is nothing to wait for.
        stop_event.set()
        return

    payload = b"\x00" * payload_size

    # Why sendto() for UDP even though we could connect() it too: an unconnected
    # UDP socket ignores the ICMP "port unreachable" a bare destination sends
    # back. A connected one would surface it as an error and kill the run — but
    # a QoS test usually cares about what the *router* does, not whether anything
    # is listening at the far end. TCP has no such choice; it must be connected.
    if protocol == "udp":
        send = lambda: sock.sendto(payload, (dst_ip, dst_port))
    else:
        send = lambda: sock.sendall(payload)

    interval = 1.0 / pps
    sent = 0

    # Same drift-free pacing as qos.py: anchor next_send to a fixed cadence and
    # sleep only the remainder, so the time spent in send() doesn't slow the rate.
    next_send = perf_counter()
    try:
        while not stop_event.is_set():
            send()
            sent += 1
            next_send += interval
            sleep_for = next_send - perf_counter()
            if sleep_for > 0:
                # wait() rather than sleep(): it returns the moment the event is
                # set, so Ctrl+C stops the stream instead of waiting out a sleep.
                stop_event.wait(sleep_for)
    except OSError as exc:
        # Mid-run failure: typically the TCP peer went away (broken pipe / reset).
        result["error"] = f"send failed after {sent} packet(s) — {exc}"
        stop_event.set()
    finally:
        sock.close()
        result["sent"] = sent


@click.command("stream")
@click.option("--src-ip", required=True,
              help="Source IPv4 address. Must be an address on this host.")
@click.option("--dst-ip", required=True, help="Destination IPv4 address.")
@click.option("--protocol", required=True, type=click.Choice(["udp", "tcp"]),
              help="Transport protocol.")
@click.option("--src-port", required=True, type=click.IntRange(1, 65535),
              help="Source port.")
@click.option("--dst-port", default=None, type=click.IntRange(1, 65535),
              help="Destination port. Omit for a random port in "
                   f"{EPHEMERAL_LOW}-{EPHEMERAL_HIGH}.")
@click.option("--pps", default=DEFAULT_PPS, show_default=True,
              type=click.IntRange(min=1),
              help="Packets/sec (UDP) or sends/sec (TCP).")
@click.option("--size", default=DEFAULT_BYTES, show_default=True,
              type=click.IntRange(1, 65507),   # 65507 = largest possible UDP payload
              help="Payload bytes per packet.")
@click.option("--duration", type=click.IntRange(min=1), default=None,
              help="Runtime in seconds. Omit to run until Ctrl+C.")
def stream_pipeline(src_ip, dst_ip, protocol, src_port, dst_port, pps, size, duration):
    """Generate one unmarked (DSCP 0) UDP or TCP stream."""

    # An omitted --dst-port stands in for a client's ephemeral port, so any high
    # port will do. Chosen here rather than in the worker so the value is known
    # up front: it goes in the banner, and TCP needs a listener on it.
    random_dst_port = dst_port is None
    if random_dst_port:
        dst_port = random.randint(EPHEMERAL_LOW, EPHEMERAL_HIGH)

    stop_event = threading.Event()
    result: dict = {"sent": 0, "error": None}

    # Why a thread for a single stream: it keeps the shutdown story identical to
    # the qos pipeline — the main thread only waits and handles SIGINT, and the
    # worker exits at its next loop check.
    worker = threading.Thread(
        target=stream_worker,
        args=(src_ip, src_port, dst_ip, dst_port, protocol, pps, size,
              stop_event, result),
        daemon=True,
    )

    signal.signal(signal.SIGINT, lambda signum, frame: stop_event.set())

    offered_bps = pps * size * 8
    click.echo(f"Starting {protocol} stream {src_ip}:{src_port} → {dst_ip}:{dst_port}"
               + (" (random)" if random_dst_port else "")
               + f", {pps} pps × {size} B (~{offered_bps / 1000:.1f} kbps payload)"
               + (f" for {duration}s" if duration else " (Ctrl+C to stop)"))

    worker.start()

    # Wait for the duration to expire, for SIGINT, or for the worker to give up
    # on a setup error (it sets stop_event itself in that case).
    if duration is not None:
        stop_event.wait(duration)
        stop_event.set()
    else:
        while not stop_event.is_set():
            stop_event.wait(1.0)

    worker.join(timeout=2.0)

    if result["error"]:
        raise click.ClickException(result["error"])

    sent = result["sent"]
    click.echo(f"\nSent {sent} packet(s), {sent * size} bytes of payload.")
