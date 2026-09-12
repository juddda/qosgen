# lab/ — setting up and running the streaming test

Everything here supports one test: traffic leaves the generator unmarked, crosses the
perimeter router, and should come out the other side marked **AF31** (DSCP 26,
`tos 0x68`). Direction is **application → user**, which is why the application ports
(TCP 80, 443, 3389, 8443 and UDP 3389) are *source* ports — that is what the WAN QoS
ACL matches on.

| File | Runs on | Purpose |
|---|---|---|
| `dummy-interfaces.sh` | generator | Creates the customer source addresses |
| `customer-streams.sh` | generator | Starts every subnet × every app port, stops them together |
| `custLinux_setup.sh` | user host | Lab address, listeners (TCP + UDP with DSCP), status, capture |
| `listener.py` | user host | Receives the streams and reports the DSCP that arrived |
| `af31-marking-swan2.cfg` | router | The AF31 ingress marking policy this test exercises |
| `qos-policy-1mb.cfg` | router | Example Cisco 1 Mbps shaper + queueing policy |

Two Linux nodes:

- **generator — WestLinux**, `10.248.76.10/25` on ens4. Stands in for the customer's
  application servers and sources the traffic for the west subnets. A second generator,
  **EastLinux**, will do the same for the 10.248.8x subnets; it isn't built yet.
- **user — custLinux**, `10.10.10.10/24` on ens4. Stands in for the person using those
  applications, and receives the traffic.

## The stream matrix

The customer /25s in the ACL are split across two generators. Both scripts take
`SITE=west` (the default) or `SITE=east`:

**west — WestLinux**

| Source address | Subnet | Note |
|---|---|---|
| `10.248.76.10` | 10.248.76.0/25 | WestLinux's own lab NIC — no dummy needed |
| `10.248.76.138` | 10.248.76.128/25 | dummy0 |
| `10.248.77.138` | 10.248.77.128/25 | dummy1 |

**east — EastLinux, not built yet**

| Source address | Subnet | Note |
|---|---|---|
| `10.248.82.10` | 10.248.82.0/25 | assumed to be EastLinux's own lab NIC — confirm when built |
| `10.248.82.138` | 10.248.82.128/25 | dummy0 |
| `10.248.83.138` | 10.248.83.128/25 | dummy1 |

Three subnets × five application ports = **15 concurrent streams per site** — 12 TCP and 3 UDP.

| Application | Protocol | Source port | Destination port |
|---|---|---|---|
| RDP | tcp | 3389 | 59001 |
| HTTPS | tcp | 443 | 59002 |
| HTTPS-alt | tcp | 8443 | 59003 |
| RDP over UDP | udp | 3389 | 59004 |
| HTTP | tcp | 80 | 59005 |

## Order of operations

### 1. User host — custLinux

```bash
cd ~/qosgen && git pull
./lab/custLinux_setup.sh status          # confirm 10.10.10.10 on ens4
./lab/custLinux_setup.sh listeners       # accept TCP on 59001-59003 and 59005
```

The listener is [`listener.py`](listener.py) rather than `nc`, because every source hits
each destination port at once and netcat serves one connection at a time with a backlog
of 1. Nothing needs installing — python3 is in every Ubuntu image. Output goes to
`/tmp/qosgen-listeners.log`.

**For a demo, run it in the foreground** — the live table is the point:

```bash
python3 lab/listener.py --tcp 59001 59002 59003 59005 --udp 59004
```

```
22:31:04  NEW  tcp  10.248.76.10:3389    -> :59001   dscp ?
22:31:05  NEW  udp  10.248.76.10:3389    -> :59004   dscp 26 AF31
--- 22:31:14 ------------------------------------------------------
  proto source                   dport dscp        packets        bytes
  udp   10.248.76.10:3389         59004 26 AF31           140      71,680
```

`dscp 26 AF31` on the UDP flows is the result the lab exists to produce: the generator
sent DSCP 0, so the router applied that marking. **TCP shows `?`** — the kernel doesn't
expose the TOS byte on stream sockets (measured on Ubuntu 24.04), so TCP marking has to
come from the router's per-line ACL counters or a tcpdump capture.

If the address ever needs (re)configuring:

```bash
sudo ./lab/custLinux_setup.sh netplan    # defaults to 10.10.10.10/24 via 10.10.10.1
```

### 2. Generator — WestLinux

```bash
cd ~/qosgen && git pull
sudo ./lab/dummy-interfaces.sh up
./lab/dummy-interfaces.sh status
```

The dummy addresses exist because `bind()` only accepts addresses the kernel owns, and
those customer subnets aren't on this node. WestLinux needs two; its third subnet is the
one its own lab NIC sits in. `/32` each — the mask never appears in
the packet, so a `/32` matches a `/25` ACL entry identically, while a `/25` would
blackhole the whole block locally.

### 3. Routers

A host route per source address, pointing back at the generator, more specific than any
null route covering those prefixes:

```
ip route 10.248.76.138 255.255.255.255 10.248.76.10
ip route 10.248.77.138 255.255.255.255 10.248.76.10
```

`10.248.76.10` is WestLinux's real address and should already be routable. When
EastLinux exists, it needs the same for `10.248.82.138` and `10.248.83.138`.

Without these, the user's TCP ACKs have nowhere to go and the 12 TCP streams never get
past the handshake. The 3 UDP streams flow regardless — a useful way to tell a routing
problem from a marking problem.

### 4. Run it

On the generator:

```bash
sudo ./lab/customer-streams.sh                    # west, 15 streams, 10 pps each
sudo PPS=50 ./lab/customer-streams.sh             # heavier
sudo SITE=east ./lab/customer-streams.sh          # once EastLinux exists
```

`sudo` because source ports 80 and 443 are privileged, and because a mixed root/non-root
process group can't be stopped by one Ctrl+C — an unprivileged shell isn't allowed to
signal a root process. Ctrl+C stops all 15.

Each stream prints its own packet count on exit, so a combination that isn't matching
stands out as an outlier.

### 5. Watch it

On the user host:

```bash
sudo ./lab/custLinux_setup.sh capture      # tcpdump from 10.248.0.0/16, expect tos 0x68
tail -f /tmp/qosgen-listeners.log          # which sources actually connected
```

Or capture either side of the perimeter router in Wireshark:

| Capture point | Filter | Expected |
|---|---|---|
| Generator side (LAN) | `ip.dsfield.dscp == 0` | unmarked, as sent |
| WAN side | `ip.dsfield.dscp == 26` | AF31, applied by the router |

Useful display filters:

```
ip.src == 10.248.77.138 && tcp.srcport == 3389    # one specific stream
ip.dsfield.dscp != 0                              # everything the router marked
```

On the router: `show policy-map interface <interface>`.

## Teardown

```bash
# generator
sudo ./lab/dummy-interfaces.sh down

# user host
./lab/custLinux_setup.sh stop
```

## After an EVE-NG node wipe

A wipe resets the node to its base image, so the repo, the netplan file, and any
installed packages are gone. Dummy interfaces and `sysctl` settings don't survive a
plain reboot either — neither is persisted. Re-run the steps above; that is most of the
reason these are scripts rather than notes.

Also worth knowing: add both NICs **before** first boot. Inserting an interface into a
running, configured node can reshuffle which topology link lands on which NIC, and the
symptom — carrier up, config valid, no DHCP — takes a while to pin down.

## More detail

- [../stream.md](../stream.md) — the `stream` pipeline: every flag, sourcing from
  addresses the host doesn't own, running several streams at once
- [../qos.md](../qos.md) — the `qos` pipeline: marked voice, signaling and noise
- [../README.md](../README.md) — install, troubleshooting, DSCP reference
