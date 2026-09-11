#!/usr/bin/env bash
#
# customer-streams.sh — every customer subnet × every application port, at once.
#
# Direction is customer/DC -> user. This host stands in for the application
# servers, so the applications' ports are the SOURCE ports, and the destination
# is a user host consuming those applications across the WAN:
#
#   src-ip    one address per customer /25 in the WAN QoS ACL
#   src-port  the application port — TCP 80, 443, 3389, 8443 and UDP 3389
#   dst-ip    the user host (DST_IP below)
#   dst-port  one per application port, so captures are easy to tell apart
#
# That is why the ACL matches source ports: it classifies this direction, and
# from the network's point of view the application server is the talker.
#
# The customer /25s in the ACL are split across two generators:
#
#   west (10.248.76.x, 10.248.77.x)   WestLinux  — 3 subnets
#   east (10.248.8x.x)                EastLinux  — 3 subnets, node not built yet
#
# Pick one with SITE:  sudo SITE=east ./lab/customer-streams.sh
#
# Each site's 3 subnets × 5 application ports = 15 concurrent streams, covering
# that site's ACL lines in a single run. Each stream reports its own packet
# count on exit, so a combination that isn't matching shows up as an outlier.
#
# Path under test: traffic leaves this host unmarked (DSCP 0), reaches the
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
#   - source ports 80 and 443 are privileged, so binding them needs root
#   - a mixed root/non-root process group can't be stopped by one Ctrl+C: an
#     unprivileged shell isn't allowed to signal a root process, so those
#     streams would survive and keep sending
#
#   sudo ./lab/customer-streams.sh
#   sudo DST_IP=10.10.10.10 PPS=50 ./lab/customer-streams.sh
#
# On a host where qosgen runs in a conda env or venv, pass the interpreter too —
# sudo resets PATH to root's, so a bare "python3" may not have click:
#
#   sudo PYTHON="$(command -v python)" ./lab/customer-streams.sh
#
# Ctrl+C stops all 15.
#
# Prerequisites — see ../stream.md and streaming-lab-setup_README.md:
#   1. A dummy interface per source address, so bind() accepts them:
#        sudo ./lab/dummy-interfaces.sh up
#      (the generator's own lab address needs no dummy — on WestLinux that is
#       10.248.76.10)
#   2. A /32 route on the routers pointing each source address back at this
#      host, more specific than any null route — the user's TCP ACKs are
#      addressed to those source addresses, and without a path back the
#      handshake never completes:
#        ip route 10.248.76.138 255.255.255.255 <this-host-lab-ip>
#   3. Listeners on the user host for the four TCP destination ports:
#        ./lab/custLinux_setup.sh listeners
#      UDP needs no listener.

set -uo pipefail

DST_IP="${DST_IP:-10.10.10.10}"     # the USER host — traffic is application -> user
PPS="${PPS:-10}"                    # packets/sends per second, per stream
SIZE="${SIZE:-512}"                 # payload bytes per packet
PYTHON="${PYTHON:-python3}"

SITE="${SITE:-west}"

# One address per customer /25 in the ACL, grouped by generator. The first entry
# in each is that host's own lab NIC; the rest live on dummy interfaces created
# by dummy-interfaces.sh.
WEST_SOURCES=(
  "10.248.76.10"     # 10.248.76.0/25   — WestLinux's own lab NIC
  "10.248.76.138"    # 10.248.76.128/25
  "10.248.77.10"     # 10.248.77.0/25
)

# EastLinux is not built yet; the native address is an assumption to confirm.
EAST_SOURCES=(
  "10.248.82.10"     # 10.248.82.0/25   — EastLinux's own lab NIC (assumed)
  "10.248.82.138"    # 10.248.82.128/25
  "10.248.83.138"    # 10.248.83.128/25
)

case "$SITE" in
  west) SOURCES=("${WEST_SOURCES[@]}") ;;
  east) SOURCES=("${EAST_SOURCES[@]}") ;;
  *) echo "error: SITE must be 'west' or 'east', not '$SITE'" >&2; exit 1 ;;
esac

# One line per application: protocol, source port, destination port.
# Distinct destination ports keep the five applications separable in a capture
# even though every stream shares a source subnet with four others.
APPS=(
  "tcp  3389  6001"    # RDP
  "tcp   443  6002"    # HTTPS
  "tcp  8443  6003"    # HTTPS-alt
  "udp  3389  6004"    # RDP over UDP
  "tcp    80  6005"    # HTTP
)

cd "$(dirname "$0")/.." || exit 1   # repo root, so qosgen.py resolves

# Fail now with a fix, rather than 24 identical tracebacks a second from now.
if ! "$PYTHON" -c 'import click' > /dev/null 2>&1; then
  echo "error: '$PYTHON' cannot import click." >&2
  echo "       Point PYTHON at the interpreter that has it, e.g." >&2
  echo "         sudo PYTHON=\"\$(command -v python)\" $0" >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "warning: not running as root — the port 80 and 443 streams will fail," >&2
  echo "         and Ctrl+C will not stop any stream started by sudo." >&2
fi

total=$(( ${#SOURCES[@]} * ${#APPS[@]} ))
echo "Site '$SITE' → $DST_IP — $total streams, ${PPS} pps × ${SIZE} B each. Ctrl+C to stop."
echo

# kill 0 signals the whole process group, so one Ctrl+C takes down every stream.
trap 'kill 0' INT TERM

for src_ip in "${SOURCES[@]}"; do
  # Heads-up if the address isn't configured — but not a hard stop: with
  # net.ipv4.ip_nonlocal_bind=1 the bind succeeds with no interface at all.
  # If it really is wrong, qosgen's own error says so precisely.
  if command -v ip > /dev/null 2>&1 && ! ip -o addr show | grep -q "inet ${src_ip}/"; then
    echo "warning: ${src_ip} is not configured on this host. Run" >&2
    echo "         'sudo ./lab/dummy-interfaces.sh up', or set ip_nonlocal_bind=1." >&2
  fi

  for app in "${APPS[@]}"; do
    read -r protocol src_port dst_port <<< "$app"

    "$PYTHON" qosgen.py stream \
      --src-ip "$src_ip" \
      --dst-ip "$DST_IP" \
      --protocol "$protocol" \
      --src-port "$src_port" \
      --dst-port "$dst_port" \
      --pps "$PPS" \
      --size "$SIZE" &
  done
done

wait
