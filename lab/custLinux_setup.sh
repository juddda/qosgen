#!/usr/bin/env bash
#
# custLinux_setup.sh — prepare a Linux server to be the USER end of a qosgen test.
#
# Traffic in this lab runs datacentre -> user: the generator stands in for the DC
# application servers (see four-sources.sh), and this host stands in for the user
# consuming those applications across the WAN. It is the destination, so it needs
# very little — a routable lab address and something listening for the TCP streams.
# qosgen itself is not required here.
#
#   sudo ./lab/custLinux_setup.sh netplan     # static address on the lab NIC
#        ./lab/custLinux_setup.sh listeners   # start nc listeners for the TCP streams
#        ./lab/custLinux_setup.sh stop        # stop those listeners
#        ./lab/custLinux_setup.sh status      # address, routes, listeners, firewall
#        ./lab/custLinux_setup.sh capture     # tcpdump the arriving streams (needs sudo)
#
# Override any of these on the command line:
#
#   sudo ADDR=10.20.20.10 PREFIX=24 GATEWAY=10.20.20.1 IFACE=ens4 \
#        ./lab/custLinux_setup.sh netplan
#
# Ubuntu/netplan only for the 'netplan' action; the rest work anywhere with nc.

set -uo pipefail

IFACE="${IFACE:-ens4}"                  # the lab-facing NIC, not the DHCP one
ADDR="${ADDR:-10.20.20.10}"             # this host's address in the user subnet
PREFIX="${PREFIX:-24}"                  # the user subnet's real prefix length
GATEWAY="${GATEWAY:-10.20.20.1}"        # lab router on this segment
TCP_PORTS="${TCP_PORTS:-6001 6002 6003}"   # destination ports of the TCP streams
SRC_FILTER="${SRC_FILTER:-10.1.0.0/16}"    # DC app addresses, for the capture filter
NETPLAN_FILE="${NETPLAN_FILE:-/etc/netplan/60-ens4-lab.yaml}"
PIDFILE="${PIDFILE:-/tmp/qosgen-listeners.pid}"

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
    # TCP needs a peer to complete the handshake, or the generator's connect()
    # fails outright. UDP needs nothing listening — the packets are one-way.
    # -k keeps the listener alive across reconnects, so a restarted stream works.
    : > "$PIDFILE"
    for port in $TCP_PORTS; do
      # Port as a positional argument: '-l -p PORT' is accepted by some netcat
      # builds and rejected by others, while '-l PORT' works everywhere.
      if nc -h 2>&1 | grep -q -- '-k'; then
        nc -l -k "$port" > /dev/null 2>&1 &
      else
        nc -l "$port" > /dev/null 2>&1 &
      fi
      echo "$!" >> "$PIDFILE"
      echo "listening on tcp/$port (pid $!)"
    done
    echo
    echo "Stop them with: $0 stop"
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
      ss -ltn 2>/dev/null | awk 'NR==1 || /:(6001|6002|6003)\>/'
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
    echo "Capturing on $IFACE from $SRC_FILTER — expect tos 0x68 (AF31). Ctrl+C to stop."
    tcpdump -v -n -i "$IFACE" "net ${SRC_FILTER}"
    ;;

  *)
    echo "usage: $0 [netplan|listeners|stop|status|capture]" >&2
    exit 1
    ;;
esac
