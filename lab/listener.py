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

Reading the DSCP differs by protocol:

  UDP   IP_RECVTOS delivers the TOS byte as ancillary data. No privilege needed.
  TCP   The kernel delivers nothing, even with IP_RECVTOS set on the listening
        socket before accept() — verified on Ubuntu 24.04, kernel 6.8. A stream
        socket simply does not expose per-packet headers.

So for TCP the header has to be read off the wire, which is what --sniff does:
it opens an AF_PACKET socket, reads the real IP header of every arriving packet,
and reports the DSCP for TCP and UDP alike. That needs CAP_NET_RAW — run it
under sudo, exactly as tcpdump or Wireshark would be.

    sudo python3 lab/listener.py --sniff

Without --sniff the program still works and still proves the UDP marking; TCP
flows just show "?" for DSCP.
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

    def observe_tos(self, proto, src, sport, dport, tos, nbytes):
        """Record a marking seen on the wire, for flows the sockets can't read.

        Creates the flow if the sniffer sees it before the socket layer does —
        a TCP handshake is on the wire before accept() returns.
        """
        key = (proto, src, sport, dport)
        now = time.time()
        with self.lock:
            flow = self.data.get(key)
            if flow is None:
                flow = {"packets": 0, "bytes": 0, "tos": tos,
                        "first": now, "last": now, "sniffed": 0}
                self.data[key] = flow
                new = True
            else:
                new = flow.get("tos") is None and tos is not None
                flow["tos"] = tos
            flow["sniffed"] = flow.get("sniffed", 0) + 1
            flow["last"] = now
        if new and not self.quiet:
            print(f"{time.strftime('%H:%M:%S')}  WIRE {proto}  "
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


def bind_tcp(port):
    """Bind and listen, or raise OSError. Called before any thread starts, so a
    port clash is one clear message rather than a traceback per thread."""
    srv = socket.socket()
    # SO_REUSEADDR lets us rebind promptly after a restart while old connections
    # linger in TIME_WAIT. It does NOT allow a second live listener — two
    # processes cannot both own a TCP port, which is what we want.
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("", port))
    srv.listen(128)          # every source connects at once; don't queue them
    return srv


def bind_udp(port):
    """Bind, or raise OSError.

    Deliberately no SO_REUSEADDR: on Linux it would let a second process bind
    the same UDP port, and the kernel would then hand each datagram to one of
    them. Counts would silently disagree with what was sent — far worse for a
    measurement tool than refusing to start. UDP has no TIME_WAIT, so nothing
    is lost by omitting it.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        srv.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
        have_tos = True
    except (AttributeError, OSError):
        have_tos = False
    srv.bind(("", port))
    return srv, have_tos


def serve_tcp(srv, port, flows):
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


def serve_udp(srv, port, flows, have_tos=False):
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


def parse_ipv4(pkt):
    """(proto, src, sport, dport, tos) from a raw IPv4 packet, or None.

    Split out from sniff() so it can be tested without a raw socket — see
    --self-test. IHL is read rather than assumed: 20 bytes is the common case,
    but a packet carrying IP options is longer, and assuming 20 would read the
    ports from the wrong offset.
    """
    if len(pkt) < 20 or pkt[0] >> 4 != 4:
        return None                                    # not IPv4
    ihl = (pkt[0] & 0x0F) * 4
    tos, proto = pkt[1], pkt[9]
    if proto not in (6, 17) or len(pkt) < ihl + 4:
        return None                                    # not TCP/UDP, or cut off
    sport = int.from_bytes(pkt[ihl:ihl + 2], "big")
    dport = int.from_bytes(pkt[ihl + 2:ihl + 4], "big")
    return ("tcp" if proto == 6 else "udp",
            socket.inet_ntoa(pkt[12:16]), sport, dport, tos)


def sniff(flows, ports, iface=None):
    """Read arriving IP headers directly, so TCP markings are visible too.

    AF_PACKET with SOCK_DGRAM hands us the packet starting at the IP header —
    no link-layer parsing needed, and it works the same on any interface type.
    Requires CAP_NET_RAW: run under sudo, as tcpdump does.
    """
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM,
                             socket.htons(0x0800))          # ETH_P_IP
    except AttributeError:
        print("--sniff needs AF_PACKET, which is Linux-only", file=sys.stderr)
        return
    except PermissionError:
        print("--sniff needs CAP_NET_RAW — re-run under sudo "
              "(the same privilege tcpdump needs)", file=sys.stderr)
        return
    if iface:
        sock.bind((iface, 0))
    print(f"sniffing IP headers{' on ' + iface if iface else ''} "
          f"for dst ports {sorted(ports)}", flush=True)

    while True:
        pkt, addr = sock.recvfrom(65535)
        if addr[2] == socket.PACKET_OUTGOING:
            continue                                   # our own replies
        parsed = parse_ipv4(pkt)
        if parsed is None:
            continue
        proto, src, sport, dport, tos = parsed
        if dport not in ports:
            continue
        flows.observe_tos(proto, src, sport, dport, tos, len(pkt))


def self_test():
    """Verify parse_ipv4 against hand-built packets. No privilege required."""
    def ipv4(tos, proto, src, dst, sport, dport, ihl_words=5):
        hdr = bytes([0x40 | ihl_words, tos]) + b"\x00" * 7 + bytes([proto])
        hdr += b"\x00\x00" + socket.inet_aton(src) + socket.inet_aton(dst)
        hdr += b"\x00" * ((ihl_words - 5) * 4)          # IP options, if any
        return hdr + sport.to_bytes(2, "big") + dport.to_bytes(2, "big")

    cases = [
        ("AF31 TCP", ipv4(0x68, 6, "10.248.76.10", "10.10.10.10", 3389, 6001),
         ("tcp", "10.248.76.10", 3389, 6001, 0x68)),
        ("unmarked UDP", ipv4(0x00, 17, "10.248.77.138", "10.10.10.10", 3389, 6004),
         ("udp", "10.248.77.138", 3389, 6004, 0x00)),
        ("EF TCP", ipv4(0xB8, 6, "10.248.82.10", "10.10.10.10", 443, 6002),
         ("tcp", "10.248.82.10", 443, 6002, 0xB8)),
        ("TCP with IP options", ipv4(0x68, 6, "10.248.76.138", "10.10.10.10",
                                     8443, 6003, ihl_words=7),
         ("tcp", "10.248.76.138", 8443, 6003, 0x68)),
        ("ICMP is ignored", ipv4(0x00, 1, "10.0.0.1", "10.0.0.2", 0, 0), None),
        ("truncated is ignored", b"\x45\x68", None),
    ]
    failures = 0
    for name, pkt, expected in cases:
        got = parse_ipv4(pkt)
        ok = got == expected
        failures += 0 if ok else 1
        marking = f"  dscp {dscp_label(got[4])}" if got else ""
        print(f"  {'PASS' if ok else 'FAIL'}  {name}{marking}")
        if not ok:
            print(f"        expected {expected}\n        got      {got}")
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


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
    ap.add_argument("--sniff", action="store_true",
                    help="read IP headers off the wire so TCP DSCP is visible "
                         "too; needs CAP_NET_RAW, so run under sudo")
    ap.add_argument("--iface", default=None, metavar="IFACE",
                    help="with --sniff, restrict to one interface (e.g. ens4)")
    ap.add_argument("--self-test", action="store_true",
                    help="check the IP header parsing against known packets "
                         "and exit; works without privilege or AF_PACKET")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    flows = Flows(quiet=args.quiet)
    print(f"qosgen listener — tcp {args.tcp}  udp {args.udp}")
    print("Ctrl+C to stop.\n", flush=True)

    # Bind before serving, so a port already in use is one clear message.
    bound = []          # (proto, port, socket, have_tos)
    for proto, port in ([("tcp", p) for p in args.tcp] +
                        [("udp", p) for p in args.udp]):
        try:
            if proto == "tcp":
                bound.append((proto, port, bind_tcp(port), False))
            else:
                sock, have_tos = bind_udp(port)
                bound.append((proto, port, sock, have_tos))
        except OSError as exc:
            for _pr, _po, sock, _t in bound:
                sock.close()
            print(f"\nerror: cannot bind {proto}/{port} — {exc}", file=sys.stderr)
            print("       another listener is probably already running. Stop it "
                  "with:\n         ./lab/custLinux_setup.sh stop\n"
                  "       then start this one again.", file=sys.stderr)
            return 1

    for proto, port, sock, have_tos in bound:
        if proto == "tcp":
            threading.Thread(target=serve_tcp, args=(sock, port, flows),
                             daemon=True).start()
        else:
            threading.Thread(target=serve_udp,
                             args=(sock, port, flows, have_tos),
                             daemon=True).start()
    if args.sniff:
        ports = set(args.tcp) | set(args.udp)
        threading.Thread(target=sniff, args=(flows, ports, args.iface),
                         daemon=True).start()
    else:
        print("(no --sniff: TCP flows will show dscp '?' — see --help)\n",
              flush=True)

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
