#!/usr/bin/env bash
#
# dummy-interfaces.sh — create the source addresses customer-streams.sh sends from.
#
# A QoS ACL usually matches source subnets the generator host isn't in, but bind()
# only accepts addresses the kernel owns. A dummy interface solves that: the address
# is local as far as bind() is concerned, but sits on no physical NIC. Packets still
# leave via whichever interface routes to the destination — the bind only decides
# what goes in the source field.
#
#   sudo ./lab/dummy-interfaces.sh up      # create them
#   sudo ./lab/dummy-interfaces.sh down    # remove them
#   ./lab/dummy-interfaces.sh status       # show what exists (no root needed)
#
# Addresses match the SOURCES arrays in customer-streams.sh — edit both together.
#
# The customer /25s in the WAN QoS ACL are split across two generators:
#
#   west (10.248.76.x, 10.248.77.x)   WestLinux
#   east (10.248.8x.x)                EastLinux — not built yet
#
# Pick one with SITE:  sudo SITE=east ./lab/dummy-interfaces.sh up
#
# Each site's generator already holds one of its subnets on its own lab NIC, so
# that subnet gets no dummy — the streams bind the real address directly.
#
# Why /32 and not the ACL's real /25: the mask never appears in the packet. The IP
# header carries a bare 32-bit source address, and the router tests it against its
# own wildcard, so a /32 matches a /25 ACL entry identically. A /25 here would also
# create a connected route for all 128 addresses pointing at the dummy, quietly
# blackholing anything this host later sends toward that block.
#
# Linux only — dummy interfaces are a Linux construct.

set -uo pipefail

# One line per source: interface  address
SITE="${SITE:-west}"

# WestLinux holds 10.248.76.10 on ens4, so 10.248.76.0/25 needs no dummy.
WEST_SOURCES=(
  "dummy0  10.248.76.138"    # 10.248.76.128/25
  "dummy1  10.248.77.10"     # 10.248.77.0/25
)

# EastLinux does not exist yet. The native address below is an assumption —
# confirm it when the node is built, and drop whichever subnet it lands in.
EAST_SOURCES=(
  "dummy0  10.248.82.138"    # 10.248.82.128/25
  "dummy1  10.248.83.138"    # 10.248.83.128/25
)                            # 10.248.82.0/25 assumed native on EastLinux

case "$SITE" in
  west) SOURCES=("${WEST_SOURCES[@]}") ;;
  east) SOURCES=("${EAST_SOURCES[@]}") ;;
  *) echo "error: SITE must be 'west' or 'east', not '$SITE'" >&2; exit 1 ;;
esac

PREFIX=32          # host route only — see the note above
ACTION="${1:-up}"

if ! command -v ip > /dev/null 2>&1; then
  echo "error: no 'ip' command — this script is Linux-only." >&2
  exit 1
fi

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "error: '$ACTION' needs root. Re-run with sudo." >&2
    exit 1
  fi
}

case "$ACTION" in
  up)
    need_root
    for spec in "${SOURCES[@]}"; do
      read -r dev addr <<< "$spec"

      # Idempotent: adding an existing link or address is an error, so skip it.
      if ip link show "$dev" > /dev/null 2>&1; then
        echo "$dev already exists"
      else
        ip link add "$dev" type dummy || exit 1
        echo "created $dev"
      fi

      ip link set "$dev" up

      if ip -o addr show dev "$dev" | grep -q "inet ${addr}/"; then
        echo "  $addr/$PREFIX already on $dev"
      else
        ip addr add "${addr}/${PREFIX}" dev "$dev" || exit 1
        echo "  added $addr/$PREFIX to $dev"
      fi
    done

    echo
    echo "Done. These addresses can now be used with --src-ip."
    echo
    echo "The routers still need a way back, or TCP handshakes will not complete."
    echo "Add a host route per address, more specific than any null route:"
    for spec in "${SOURCES[@]}"; do
      read -r _dev addr <<< "$spec"
      echo "  ip route $addr 255.255.255.255 <this-host-lab-ip>"
    done
    ;;

  down)
    need_root
    for spec in "${SOURCES[@]}"; do
      read -r dev _addr <<< "$spec"
      if ip link show "$dev" > /dev/null 2>&1; then
        ip link del "$dev"          # deleting the link takes its addresses with it
        echo "removed $dev"
      else
        echo "$dev not present"
      fi
    done
    ;;

  status)
    for spec in "${SOURCES[@]}"; do
      read -r dev addr <<< "$spec"
      if ip link show "$dev" > /dev/null 2>&1; then
        state=$(ip -br link show "$dev" | awk '{print $2}')
        have=$(ip -o addr show dev "$dev" | grep -c "inet ${addr}/")
        [ "$have" -gt 0 ] && ok="$addr present" || ok="$addr MISSING"
        echo "$dev  $state  $ok"
      else
        echo "$dev  absent"
      fi
    done
    ;;

  *)
    echo "usage: $0 [up|down|status]" >&2
    exit 1
    ;;
esac
