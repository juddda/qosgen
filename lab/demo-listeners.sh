#!/usr/bin/env bash
#
# demo-listeners.sh — netcat listeners for the customer demo.
#
# Runs one verbose netcat per destination port, so a customer can watch
# connections arrive in real time:
#
#     Connection received on 10.248.76.10 3389
#
# That readability is the point. For a measured run use the python listener
# instead — 'custLinux_setup.sh listeners' — which also reports the DSCP of
# arriving UDP packets, the actual result the lab exists to produce.
#
#   ./lab/demo-listeners.sh start     # one nc per port
#   ./lab/demo-listeners.sh tail      # follow the connection log
#   ./lab/demo-listeners.sh status    # what's bound
#   ./lab/demo-listeners.sh stop
#
# LIMITATION, and it matters for a live demo: netcat serves ONE connection at a
# time per port, even with -k, and it listens with a backlog of 1 — 'ss' shows
# Send-Q 1 on the listening socket. Three customer subnets send to each port
# here, so of the three: one is accepted and read, one waits in the backlog, and
# the third may be refused outright or sit retrying.
#
# The accepted one behaves perfectly. The queued one completes its handshake in
# the kernel, so it looks connected, then stalls once its socket buffer fills —
# roughly 15-25 seconds at 10 pps x 512 B.
#
# So a netcat demo shows real connections arriving and is fine for a minute of
# narration, but the stream counts at the end will not add up. For anything
# measured, use the python listener: it accepts every connection concurrently
# and reports the DSCP of arriving UDP packets.
#
#     ./lab/custLinux_setup.sh listeners
#
# Needs netcat-openbsd: sudo apt install netcat-openbsd

set -uo pipefail

TCP_PORTS="${TCP_PORTS:-6001 6002 6003 6005}"
UDP_PORTS="${UDP_PORTS:-6004}"
LOGDIR="${LOGDIR:-/tmp/qosgen-demo}"
PIDFILE="${PIDFILE:-$LOGDIR/pids}"

ACTION="${1:-start}"

case "$ACTION" in
  start)
    command -v nc > /dev/null 2>&1 || {
      echo "error: nc not found — sudo apt install netcat-openbsd" >&2
      exit 1
    }
    mkdir -p "$LOGDIR"
    : > "$PIDFILE"

    KEEP=""
    nc -h 2>&1 | grep -q -- '-k' && KEEP="-k"   # accept again after a close

    for port in $TCP_PORTS; do
      # -v prints "Connection received ..." per connection, which is the demo.
      # setsid/nohup so the listeners survive the SSH session that started them.
      SETSID=""; command -v setsid > /dev/null 2>&1 && SETSID="setsid"
      $SETSID nohup nc -l $KEEP -v "$port" > /dev/null 2> "$LOGDIR/tcp-$port.log" &
      echo "$!" >> "$PIDFILE"
      echo "  tcp/$port  (pid $!)"
    done

    for port in $UDP_PORTS; do
      SETSID=""; command -v setsid > /dev/null 2>&1 && SETSID="setsid"
      $SETSID nohup nc -u -l -v "$port" > /dev/null 2> "$LOGDIR/udp-$port.log" &
      echo "$!" >> "$PIDFILE"
      echo "  udp/$port  (pid $!)"
    done

    sleep 0.5
    echo
    echo "Watch connections arrive:  $0 tail"
    echo "Stop them:                 $0 stop"
    echo
    echo "Reminder: netcat reads one connection per port at a time. For a run"
    echo "longer than ~20s, use: ./lab/custLinux_setup.sh listeners"
    ;;

  tail)
    echo "following $LOGDIR/*.log — Ctrl+C to stop"
    tail -f "$LOGDIR"/*.log
    ;;

  status)
    echo "== bound =="
    if command -v ss > /dev/null 2>&1; then
      ss -ltun 2>/dev/null | grep -E ":($(echo "$TCP_PORTS $UDP_PORTS" | tr ' ' '|')) " \
        | sed 's/^/  /' || echo "  nothing bound"
    else
      echo "  ss not available"
    fi
    echo
    echo "== connections seen =="
    grep -h -i 'connect' "$LOGDIR"/*.log 2>/dev/null | sed 's/^/  /' || echo "  none yet"
    ;;

  stop)
    if [ ! -s "$PIDFILE" ]; then
      echo "no listeners recorded in $PIDFILE"
      exit 0
    fi
    while read -r pid; do
      kill "$pid" 2> /dev/null && echo "stopped pid $pid" || echo "pid $pid already gone"
    done < "$PIDFILE"
    rm -f "$PIDFILE"
    ;;

  *)
    echo "usage: $0 [start|tail|status|stop]" >&2
    exit 1
    ;;
esac
