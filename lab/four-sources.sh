#!/usr/bin/env bash
#
# four-sources.sh — fire the four ACL-matching streams at once.
#
# Direction is datacentre -> user. This host stands in for the DC application
# servers, so the applications' ports are the SOURCE ports, and the destination
# is a user consuming those applications across the WAN:
#
#   src-ip    an application address, one per /25 in the WAN QoS ACL
#   src-port  the application port — TCP 3389, TCP 443, TCP 8443, UDP 3389
#   dst-ip    the user host (DST_IP below)
#   dst-port  arbitrary — it stands in for the user's ephemeral client port
#
# That is why the ACL matches source ports: it classifies the DC-to-user
# direction, and from the network's point of view the app server is the talker.
#
# Path under test: traffic leaves this host unmarked (DSCP 0), reaches the DC
# perimeter router on its LAN-facing interface, and is marked AF31 there before
# heading out to the WAN. So the test is a capture either side of that router:
#
#   before (LAN side)  ip.dsfield.dscp == 0     tcpdump: tos 0x0
#   after  (WAN side)  ip.dsfield.dscp == 26    tcpdump: tos 0x68
#
# The generator stays unmarked on purpose — every DSCP value you see downstream
# was put there by the router, not by this host.
#
# Run it under sudo:
#   - source port 443 is privileged, so binding it needs root
#   - a mixed root/non-root process group can't be stopped by one Ctrl+C: an
#     unprivileged shell isn't allowed to signal a root process, so that stream
#     would survive and keep sending
#
#   sudo PYTHON="$(command -v python)" ./lab/four-sources.sh
#   sudo PYTHON="$(command -v python)" DST_IP=10.248.248.1 PPS=50 ./lab/four-sources.sh
#
# Pass PYTHON explicitly when the tool runs in a conda env: sudo resets PATH to
# root's, so a bare "python3" would be the system interpreter, which has no click.
#
# Ctrl+C stops all four.
#
# Prerequisites on the generator host — see stream.md:
#   1. A dummy interface per source address, so bind() accepts them:
#        sudo ./lab/dummy-interfaces.sh up
#      (/32 per address — a /25 would blackhole the whole block locally)
#   2. A /32 route on the routers pointing each application address back at this
#      host, more specific than the null routes — the user's TCP ACKs are
#      addressed to those app addresses, and without a path back the handshake
#      never completes:
#        ip route 10.1.1.10 255.255.255.255 <this-host-ens4-ip>
#   3. A listener on each TCP destination port, on the user host:
#        nc -l 6001 > /dev/null      # and 6002, 6003
#      UDP needs no listener.

set -uo pipefail

DST_IP="${DST_IP:-10.248.248.1}"    # the USER host — traffic is DC -> user
PPS="${PPS:-10}"                    # packets/sends per second, per stream
SIZE="${SIZE:-512}"                 # payload bytes per packet
PYTHON="${PYTHON:-python3}"

# One line per stream: src-ip  src-port  protocol  dst-port
# Edit the source addresses to real ones from each /25.
STREAMS=(
  "10.1.1.10  3389  tcp  6001"
  "10.1.2.10   443  tcp  6002"
  "10.1.3.10  8443  tcp  6003"
  "10.1.4.10  3389  udp  6004"
)

cd "$(dirname "$0")/.." || exit 1   # repo root, so qosgen.py resolves

# Fail now with a fix, rather than four identical tracebacks a second from now.
if ! "$PYTHON" -c 'import click' > /dev/null 2>&1; then
  echo "error: '$PYTHON' cannot import click." >&2
  echo "       Point PYTHON at the interpreter that has it, e.g." >&2
  echo "         sudo PYTHON=\"\$(command -v python)\" $0" >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "warning: not running as root — the source port 443 stream will fail," >&2
  echo "         and Ctrl+C will not stop any stream started by sudo." >&2
fi

echo "Destination $DST_IP — ${PPS} pps × ${SIZE} B per stream. Ctrl+C to stop all."
echo

# kill 0 signals the whole process group, so one Ctrl+C takes down every stream.
trap 'kill 0' INT TERM

for spec in "${STREAMS[@]}"; do
  read -r src_ip src_port protocol dst_port <<< "$spec"

  # Heads-up if the address isn't configured — but not a hard stop: with
  # net.ipv4.ip_nonlocal_bind=1 the bind succeeds with no interface at all.
  # If it really is wrong, qosgen's own error says so precisely.
  if command -v ip > /dev/null 2>&1 && ! ip -o addr show | grep -q "inet ${src_ip}/"; then
    echo "warning: ${src_ip} is not configured on this host. Create its dummy" >&2
    echo "         interface (see the header), or set ip_nonlocal_bind=1." >&2
  fi

  "$PYTHON" qosgen.py stream \
    --src-ip "$src_ip" \
    --dst-ip "$DST_IP" \
    --protocol "$protocol" \
    --src-port "$src_port" \
    --dst-port "$dst_port" \
    --pps "$PPS" \
    --size "$SIZE" &
done

wait
