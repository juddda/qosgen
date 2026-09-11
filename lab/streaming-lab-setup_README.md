# lab/ — setting up and running the streaming test

Everything here supports one test: traffic leaves the generator unmarked, crosses the
perimeter router, and should come out the other side marked **AF31** (DSCP 26,
`tos 0x68`). Direction is **application → user**, which is why the application ports
(TCP 3389, 443, 8443 and UDP 3389) are *source* ports — that is what the WAN QoS ACL
matches on.

| File | Runs on | Purpose |
|---|---|---|
| `dummy-interfaces.sh` | generator | Creates the customer source addresses |
| `customer-streams.sh` | generator | Starts every subnet × every app port, stops them together |
| `custLinux_setup.sh` | user host | Lab address, TCP listeners, status, capture |
| `qos-policy-1mb.cfg` | router | Example Cisco 1 Mbps shaper + queueing policy |

Two Linux nodes:

- **generator — WestLinux**, `10.248.76.10/25` on ens4. Stands in for the customer's
  application servers and sources all the traffic.
- **user — NorthLinux**, `10.10.10.10/24` on ens4. Stands in for the person using those
  applications, and receives it.

## The stream matrix

Six customer subnets from the ACL, each sending on all four application ports —
**24 concurrent streams**, covering every ACL line in one run:

| Source address | Subnet | Note |
|---|---|---|
| `10.248.76.10` | 10.248.76.0/25 | the generator's own lab NIC — no dummy needed |
| `10.248.76.138` | 10.248.76.128/25 | dummy0 |
| `10.248.77.10` | 10.248.77.0/25 | dummy1 |
| `10.248.82.10` | 10.248.82.0/25 | dummy2 |
| `10.248.82.138` | 10.248.82.128/25 | dummy3 |
| `10.248.83.138` | 10.248.83.128/25 | dummy4 |

| Application | Protocol | Source port | Destination port |
|---|---|---|---|
| RDP | tcp | 3389 | 6001 |
| HTTPS | tcp | 443 | 6002 |
| HTTPS-alt | tcp | 8443 | 6003 |
| RDP over UDP | udp | 3389 | 6004 |

## Order of operations

### 1. User host — NorthLinux

```bash
cd ~/qosgen && git pull
./lab/custLinux_setup.sh status          # confirm 10.10.10.10 on ens4
./lab/custLinux_setup.sh listeners       # accept TCP on 6001-6003
```

The listener is a small python3 program rather than `nc`, because six sources hit each
destination port at once and netcat serves one connection at a time even with `-k`.
Nothing needs installing — python3 is in every Ubuntu image. Connections are logged to
`/tmp/qosgen-listeners.log`, so you can see exactly which sources arrived.

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

The five dummy addresses exist because `bind()` only accepts addresses the kernel owns,
and those customer subnets aren't on this node. `/32` each — the mask never appears in
the packet, so a `/32` matches a `/25` ACL entry identically, while a `/25` would
blackhole the whole block locally.

### 3. Routers

A host route per source address, pointing back at the generator, more specific than any
null route covering those prefixes:

```
ip route 10.248.76.138 255.255.255.255 <westlinux-lab-ip>
ip route 10.248.77.10  255.255.255.255 <westlinux-lab-ip>
ip route 10.248.82.10  255.255.255.255 <westlinux-lab-ip>
ip route 10.248.82.138 255.255.255.255 <westlinux-lab-ip>
ip route 10.248.83.138 255.255.255.255 <westlinux-lab-ip>
```

`10.248.76.10` is the generator's real address and should already be routable.

Without these, the user's TCP ACKs have nowhere to go and the 18 TCP streams never get
past the handshake. The 6 UDP streams flow regardless — a useful way to tell a routing
problem from a marking problem.

### 4. Run it

On the generator:

```bash
sudo ./lab/customer-streams.sh                    # 24 streams, 10 pps each
sudo PPS=50 ./lab/customer-streams.sh             # heavier
sudo DST_IP=10.10.10.10 ./lab/customer-streams.sh # explicit destination
```

`sudo` because source port 443 is privileged, and because a mixed root/non-root process
group can't be stopped by one Ctrl+C — an unprivileged shell isn't allowed to signal a
root process. Ctrl+C stops all 24.

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
ip.src == 10.248.77.10 && tcp.srcport == 3389     # one specific stream
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
