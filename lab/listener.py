#!/usr/bin/env python3
"""listener.py — receive the qosgen test streams and report what arrived.

The destination end of the lab. Accepts every TCP connection concurrently and
binds the UDP ports with IP_RECVTOS, so the DSCP the router applied is visible
without root and without tcpdump.

    python3 lab/listener.py --tcp 6001 6002 6003 6005 --udp 6004

Output is a line per new flow, then a summary table every few seconds and once
more on exit:

    22:31:04  NEW  tcp  10.248.76.10:3389    -> :6001   dscp 26 AF31
    22:31:14  udp  10.248.76.10:3389    ->:6004  dscp 26 AF31   140 pkts

Why not netcat: netcat accepts one connection at a time per port and listens
with a backlog of 1. Several customer subnets send to the same destination
port here, so all but one would stall or be refused. This accepts all of them.

DSCP is read reliably on UDP only. IP_RECVTOS on a stream socket returns no
ancillary data on Ubuntu 24.04 (measured, not assumed), so TCP flows show "?".
That is a property of the kernel, not a fault here: for TCP marking, read the
router's per-line ACL counters, or capture with tcpdump as root. The UDP stream
exists partly for this reason — it is the unprivileged proof that the router
marked the traffic.
"""

import argparse
import socket
import sys
import threading
import time

# The markings this lab cares about, plus the neighbours worth recognising.
DSCP_NAMES = {
    0: "BE", 8: "CS1", 10: "AF11", 16: "CS2", 18: "AF21", 24: "CS3",
    26: "AF31", 32: "CS4", 34: "AF41", 40: "CS5", 46: "EF", 48: "CS6",
    56: "CS7",
}


def dscp_label(tos):
    if tos is None:
        return "?"
    dscp = tos >> 2
    return f"{dscp} {DSCP_NAMES.get(dscp, '')}".strip()


class Flows:
    """Per-flow counters, keyed by the 4-tuple that identifies a test stream."""

    def __init__(self, quiet=False):
        self.lock = threading.Lock()
        self.data = {}          # (proto, src, sport, dport) -> dict
        self.quiet = quiet

    def record(self, proto, src, sport, dport, tos, nbytes):
        key = (proto, src, sport, dport)
        now = time.time()
        with self.lock:
            flow = self.data.get(key)
            if flow is None:
                flow = {"packets": 0, "bytes": 0, "tos": tos,
                        "first": now, "last": now}
                self.data[key] = flow
                new = True
            else:
                new = False
                # A router changing its marking mid-run is worth seeing, so keep
                # the latest rather than the first.
                if tos is not None:
                    flow["tos"] = tos
            flow["packets"] += 1
            flow["bytes"] += nbytes
            flow["last"] = now
        if new and not self.quiet:
            print(f"{time.strftime('%H:%M:%S')}  NEW  {proto}  "
                  f"{src}:{sport:<5} -> :{dport}   dscp {dscp_label(tos)}",
                  flush=True)

    def table(self, title):
        with self.lock:
            rows = sorted(self.data.items())
        out = [f"--- {title} " + "-" * max(0, 62 - len(title))]
        if not rows:
            out.append("  (nothing received yet)")
        else:
            out.append(f"  {'proto':5} {'source':24} {'dport':>5} "
                       f"{'dscp':9} {'packets':>9} {'bytes':>12}")
            for (proto, src, sport, dport), f in rows:
                out.append(f"  {proto:5} {src + ':' + str(sport):24} "
                           f"{dport:>5} {dscp_label(f['tos']):9} "
                           f"{f['packets']:>9,} {f['bytes']:>12,}")
            marks = {dscp_label(f["tos"]) for _k, f in rows}
            out.append(f"  {len(rows)} flow(s), markings seen: "
                       f"{', '.join(sorted(marks))}")
        return "\n".join(out)


def serve_tcp(port, flows):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", port))
    srv.listen(128)          # every source connects at once; don't queue them
    print(f"listening tcp/{port}", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=drain_tcp, args=(conn, addr, port, flows),
                         daemon=True).start()


def drain_tcp(conn, addr, port, flows):
    src, sport = addr[0], addr[1]
    tos = None
    try:
        conn.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
        use_recvmsg = True
    except (AttributeError, OSError):
        use_recvmsg = False
    try:
        while True:
            if use_recvmsg:
                data, anc, _flags, _a = conn.recvmsg(65536,
                                                     socket.CMSG_SPACE(64))
                for lvl, _t, val in anc:
                    if lvl == socket.IPPROTO_IP and val:
                        tos = val[0]
            else:
                data = conn.recv(65536)
            if not data:
                break
            flows.record("tcp", src, sport, port, tos, len(data))
    except OSError:
        pass
    finally:
        conn.close()


def serve_udp(port, flows):
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
        have_tos = True
    except (AttributeError, OSError):
        have_tos = False
    srv.bind(("", port))
    print(f"listening udp/{port}" + ("" if have_tos else "  (no TOS support)"),
          flush=True)
    while True:
        if have_tos:
            data, anc, _flags, addr = srv.recvmsg(65535, socket.CMSG_SPACE(64))
            tos = next((v[0] for lvl, _t, v in anc
                        if lvl == socket.IPPROTO_IP and v), None)
        else:
            data, addr = srv.recvfrom(65535)
            tos = None
        flows.record("udp", addr[0], addr[1], port, tos, len(data))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tcp", nargs="*", type=int, default=[6001, 6002, 6003, 6005],
                    help="TCP ports to accept on")
    ap.add_argument("--udp", nargs="*", type=int, default=[6004],
                    help="UDP ports to bind (these report DSCP reliably)")
    ap.add_argument("--summary-every", type=int, default=10, metavar="SECONDS",
                    help="seconds between summary tables, 0 to disable")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress the per-flow NEW lines")
    args = ap.parse_args()

    flows = Flows(quiet=args.quiet)
    print(f"qosgen listener — tcp {args.tcp}  udp {args.udp}")
    print("Ctrl+C to stop.\n", flush=True)

    for p in args.tcp:
        threading.Thread(target=serve_tcp, args=(p, flows), daemon=True).start()
    for p in args.udp:
        threading.Thread(target=serve_udp, args=(p, flows), daemon=True).start()

    try:
        while True:
            if args.summary_every:
                time.sleep(args.summary_every)
                print(flows.table(time.strftime("%H:%M:%S")), flush=True)
            else:
                time.sleep(3600)
    except KeyboardInterrupt:
        print()
        print(flows.table("final"), flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
