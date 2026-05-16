#!/usr/bin/env python3
"""qosgen — generate marked UDP traffic for QoS testing.

See SPEC.md for the full specification. The architecture in three points:

  1. DSCP is set per-socket via setsockopt(IP_TOS, ...). The kernel stamps
     every outbound packet's TOS byte from that value, so each stream
     needs its own socket — voice (EF) and noise (BE) cannot share one.

  2. One thread per stream. The work is sleep-bound, not CPU-bound, so the
     GIL is irrelevant and 21 threads is fine.

  3. Pacing uses absolute scheduled times via perf_counter() so rates don't
     drift. Sleeps go through stop_event.wait() so Ctrl+C unblocks every
     worker within milliseconds.
"""

import signal
import socket
import threading
from time import perf_counter

import click

# TOS = DSCP << 2 (the lower 2 bits are ECN, left at 0).
TOS_EF   = 184   # DSCP 46 — voice bearer (RTP)
TOS_AF31 = 104   # DSCP 26 — modern call signaling
TOS_CS3  = 96    # DSCP 24 — legacy call signaling
TOS_BE   = 0     # DSCP 0  — best effort

VOICE_PORT_BASE = 16384   # even ports, RTP-style: 16384, 16386, 16388, …
VOICE_PPS       = 50
VOICE_BYTES     = 160

SIG_PORT  = 5060          # SIP
SIG_PPS   = 5
SIG_BYTES = 200

NOISE_PORT_BASE = 30000   # 30000, 30001, 30002, …
NOISE_PPS       = 20
NOISE_BYTES     = 1000


def stream_worker(
    name: str,
    dst: str,
    port: int,
    tos: int,
    pps: int,
    payload_size: int,
    stop_event: threading.Event,
    counter: dict,
) -> None:
    """Send UDP packets to (dst, port) at `pps` rate, marked with `tos`,
    until stop_event is set. Records final send count in counter[name]."""

    # Why a fresh socket per stream: IP_TOS is a *per-socket* option. The
    # kernel writes whatever value we set here into the TOS byte of every
    # packet sent on this socket — so two streams with different DSCPs
    # (e.g. voice EF vs. noise BE) can never share a socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)

    payload = b"\x00" * payload_size
    interval = 1.0 / pps
    sent = 0

    # Why absolute scheduling: a naive `sleep(interval)` loop drifts because
    # each iteration also pays the cost of sendto() and the loop body. By
    # anchoring next_send to a fixed cadence and sleeping only the remainder,
    # the rate stays honest over long runs.
    next_send = perf_counter()
    try:
        while not stop_event.is_set():
            sock.sendto(payload, (dst, port))
            sent += 1
            next_send += interval
            sleep_for = next_send - perf_counter()
            if sleep_for > 0:
                # Why event.wait() and not time.sleep(): wait() returns
                # immediately when the event is set, so Ctrl+C unblocks
                # all threads at once instead of having each one wait out
                # the rest of its sleep.
                stop_event.wait(sleep_for)
    finally:
        sock.close()
        # Single-key dict assignment is atomic under CPython's GIL, and each
        # thread writes its own unique key — so no lock needed.
        counter[name] = sent


@click.command()
@click.option("--dst", required=True, help="Destination IP for all streams.")
@click.option("--calls", default=1, show_default=True, type=int,
              help="Number of voice streams.")
@click.option("--signaling", is_flag=True, default=False,
              help="Add one signaling stream.")
@click.option("--signaling-dscp", type=click.Choice(["af31", "cs3"]),
              default="af31", show_default=True,
              help="DSCP marking for the signaling stream.")
@click.option("--noise", is_flag=True, default=False,
              help="Add one best-effort noise stream per voice call.")
@click.option("--duration", type=int, default=None,
              help="Runtime in seconds. Omit to run until Ctrl+C.")
def main(dst, calls, signaling, signaling_dscp, noise, duration):
    """Generate marked UDP traffic to exercise router QoS queues."""
    stop_event = threading.Event()
    counter: dict = {}
    threads = []

    # Voice streams — EF, even ports starting at 16384.
    for i in range(calls):
        threads.append(threading.Thread(
            target=stream_worker,
            args=(f"voice-{i}", dst, VOICE_PORT_BASE + 2 * i, TOS_EF,
                  VOICE_PPS, VOICE_BYTES, stop_event, counter),
            daemon=True,
        ))

    # Signaling stream — AF31 (default) or CS3, port 5060.
    if signaling:
        sig_tos = TOS_AF31 if signaling_dscp == "af31" else TOS_CS3
        threads.append(threading.Thread(
            target=stream_worker,
            args=("signaling", dst, SIG_PORT, sig_tos,
                  SIG_PPS, SIG_BYTES, stop_event, counter),
            daemon=True,
        ))

    # Noise streams — two per voice call, BE, ports starting at 30000.
    if noise:
        for i in range(calls * 2):
            threads.append(threading.Thread(
                target=stream_worker,
                args=(f"noise-{i}", dst, NOISE_PORT_BASE + i, TOS_BE,
                      NOISE_PPS, NOISE_BYTES, stop_event, counter),
                daemon=True,
            ))

    # SIGINT handler. Setting the event causes every worker to exit at its
    # next loop check, and unblocks any worker currently in wait().
    signal.signal(signal.SIGINT, lambda signum, frame: stop_event.set())

    click.echo(f"Starting {len(threads)} stream(s) → {dst}"
               + (f" for {duration}s" if duration else " (Ctrl+C to stop)"))

    for t in threads:
        t.start()

    # Main thread waits for either the duration to expire or SIGINT to fire.
    # The 1-second poll in the no-duration branch gives Python's signal
    # machinery a checkpoint to deliver the SIGINT and run the handler.
    if duration is not None:
        stop_event.wait(duration)
        stop_event.set()
    else:
        while not stop_event.is_set():
            stop_event.wait(1.0)

    # Short join timeout — workers should exit promptly once the event is set.
    for t in threads:
        t.join(timeout=2.0)

    total = sum(counter.values())
    click.echo(f"\nTotal packets sent: {total}")
    for name in sorted(counter):
        click.echo(f"  {name}: {counter[name]}")


if __name__ == "__main__":
    main()
