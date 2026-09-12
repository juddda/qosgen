#!/usr/bin/env bash
#
# custLinux_setup.sh — prepare a Linux server to be the USER end of a qosgen test.
#
# Traffic in this lab runs application -> user: the generator stands in for the
# customer's application servers (see customer-streams.sh), and this host stands
# in for the user consuming those applications across the WAN. It is the
# destination, so it needs very little — a routable lab address and something
# listening for the TCP streams. qosgen itself is not required here.
#
#   sudo ./lab/custLinux_setup.sh netplan     # static address on the lab NIC
#        ./lab/custLinux_setup.sh listeners   # accept the TCP streams
#        ./lab/custLinux_setup.sh stop        # stop the listener
#        ./lab/custLinux_setup.sh status      # address, routes, listeners, firewall
#        ./lab/custLinux_setup.sh capture     # tcpdump the arriving streams (needs sudo)
#
# Defaults match this lab: 10.10.10.10/24 via 10.10.10.1, listening on tcp
# 6001-6003 and 6005, capturing traffic from 10.248.0.0/16. Override any of them:
#
#   sudo ADDR=10.10.10.10 PREFIX=24 GATEWAY=10.10.10.1 IFACE=ens4 \
#        ./lab/custLinux_setup.sh netplan
#
# The 'netplan' action is Ubuntu-only. The listener needs python3, which every
# Ubuntu image has — netcat is not required.

set -uo pipefail

IFACE="${IFACE:-ens4}"                  # the lab-facing NIC, not the DHCP one
ADDR="${ADDR:-10.10.10.10}"             # this host's address in the user subnet
PREFIX="${PREFIX:-24}"                  # the user subnet's real prefix length
GATEWAY="${GATEWAY:-10.10.10.1}"        # lab router on this segment
TCP_PORTS="${TCP_PORTS:-6001 6002 6003 6005}"   # destination ports of the TCP streams
UDP_PORTS="${UDP_PORTS:-6004}"             # destination port of the UDP stream
SRC_FILTER="${SRC_FILTER:-10.248.0.0/16}"  # customer source subnets, for the capture
NETPLAN_FILE="${NETPLAN_FILE:-/etc/netplan/60-ens4-lab.yaml}"
PIDFILE="${PIDFILE:-/tmp/qosgen-listeners.pid}"
LOGFILE="${LOGFILE:-/tmp/qosgen-listeners.log}"
HERE="$(cd "$(dirname "$0")" && pwd)"     # so listener.py is found from anywhere

ACTION="${1:-status}"

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "error: '$ACTION' needs root. Re-run with sudo." >&2
    exit 1
  fi
}

case "$ACTION" in
  netplan)
    need_root
    command -v netplan > /dev/null 2>&1 || { echo "error: netplan not found — this action is Ubuntu-only." >&2; exit 1; }

    # Keep a copy of whatever is there: this file is ours, but a second run with
    # different settings shouldn't silently discard the previous ones.
    if [ -f "$NETPLAN_FILE" ]; then
      backup="${NETPLAN_FILE}.$(date +%Y%m%d-%H%M%S).bak"
      cp "$NETPLAN_FILE" "$backup"
      echo "backed up existing config to $backup"
    fi

    # Only this interface is declared. The DHCP interface keeps its own file
    # (usually 50-cloud-init.yaml, owned by cloud-init) — netplan merges them,
    # and editing that one invites cloud-init to overwrite you on reboot.
    #
    # No default route here on purpose: the default must stay on the DHCP
    # interface or the host loses its internet path, and two default routes give
    # you asymmetric routing that is miserable to debug. Specific prefixes via
    # the lab router instead — that is what carries return traffic back toward
    # the DC application addresses.
    #
    # 10/8 and 172.16/12 only. 192.168/16 is deliberately absent: the lab never
    # uses it, and the management network does, so routing it at the lab router
    # would be all risk and no benefit.
    cat > "$NETPLAN_FILE" <<EOF
network:
  version: 2
  ethernets:
    ${IFACE}:
      dhcp4: false
      dhcp6: false
      optional: true
      addresses:
        - ${ADDR}/${PREFIX}
      routes:
        - to: 10.0.0.0/8
          via: ${GATEWAY}
        - to: 172.16.0.0/12
          via: ${GATEWAY}
EOF
    chmod 600 "$NETPLAN_FILE"      # netplan warns if the file is world-readable
    echo "wrote $NETPLAN_FILE"
    echo

    # 'try' rather than 'apply': a route change on the wrong interface can cut the
    # session you are typing into, and this rolls back in 120 seconds unless you
    # confirm. Cheap insurance on a host you reach only over SSH.
    echo "Applying with 'netplan try' — press ENTER to keep it, or wait 120s to roll back."
    netplan try

    echo
    echo "Check: exactly one default route, and it should be on the DHCP interface."
    ip route
    ;;

  listeners)
    # The listener is a program in its own right: lab/listener.py. It accepts
    # every TCP connection concurrently — netcat manages one per port — and
    # binds the UDP ports with IP_RECVTOS so the DSCP the router applied is
    # readable without root.
    #
    # Started detached here so it survives the SSH session that launched it. For
    # a demo, run it in the foreground instead, where the live table is the
    # point:
    #
    #   python3 lab/listener.py --tcp 6001 6002 6003 6005 --udp 6004
    #
    : > "$PIDFILE"
    SETSID=""
    command -v setsid > /dev/null 2>&1 && SETSID="setsid"   # not present on macOS
    $SETSID nohup "${PYTHON:-python3}" "$HERE/listener.py" \
        --tcp $TCP_PORTS --udp $UDP_PORTS > "$LOGFILE" 2>&1 &
    echo "$!" > "$PIDFILE"
    sleep 0.5
    echo "listener started (pid $(cat "$PIDFILE"))"
    echo "  tcp: $TCP_PORTS"
    echo "  udp: $UDP_PORTS  (reports the DSCP of arriving packets)"
    echo "  log: $LOGFILE"
    echo "  stop it with: $0 stop"
    ;;

  stop)
    if [ ! -s "$PIDFILE" ]; then
      echo "no listeners recorded in $PIDFILE"
      exit 0
    fi
    while read -r pid; do
      if kill "$pid" 2> /dev/null; then
        echo "stopped pid $pid"
      else
        echo "pid $pid already gone"
      fi
    done < "$PIDFILE"
    # Belt and braces: setsid means the listener may outlive a stale pidfile.
    pkill -f "$HERE/listener.py" 2> /dev/null && echo "cleaned up a stray listener"
    rm -f "$PIDFILE"
    ;;

  status)
    echo "== address on $IFACE =="
    ip -br addr show "$IFACE" 2>/dev/null || echo "  $IFACE not present"
    echo
    echo "== routes =="
    ip route 2>/dev/null || echo "  no ip command"
    echo
    echo "== listeners =="
    if command -v ss > /dev/null 2>&1; then
      ss -ltn 2>/dev/null | awk 'NR==1 || /:(6001|6002|6003|6005)\>/'
    else
      echo "  ss not available"
    fi
    echo
    echo "== firewall =="
    if command -v ufw > /dev/null 2>&1; then
      ufw status 2>/dev/null || echo "  (needs root to read)"
    else
      echo "  ufw not installed"
    fi
    ;;

  capture)
    need_root
    # This host sits beyond the perimeter router, so traffic arriving here should
    # already carry the marking the router applied — AF31 shows as tos 0x68.
    #
    # Written to a pcap as well as printed: a capture file is evidence the
    # customer can open in Wireshark themselves, which a screenful of scrollback
    # is not. --print makes tcpdump do both (4.99+); older builds only write.
    PCAP="${PCAP:-/tmp/qosgen-capture.pcap}"
    echo "Capturing on $IFACE from $SRC_FILTER — expect tos 0x68 (AF31)."
    echo "  saving to $PCAP"
    echo "  Wireshark filter afterwards:  ip.dsfield.dscp == 26"
    echo "  Ctrl+C to stop."
    echo
    if tcpdump --help 2>&1 | grep -q -- '--print'; then
      tcpdump -v -n -i "$IFACE" --print -w "$PCAP" "net ${SRC_FILTER}"
    else
      echo "  (this tcpdump cannot print and write at once — writing only)"
      tcpdump -n -i "$IFACE" -w "$PCAP" "net ${SRC_FILTER}"
    fi
    ;;

  *)
    echo "usage: $0 [netplan|listeners|stop|status|capture]" >&2
    exit 1
    ;;
esac
