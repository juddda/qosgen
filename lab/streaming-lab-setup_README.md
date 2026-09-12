# Running the lab

Just the commands, nothing else: [quickstart_README.md](quickstart_README.md).

Copy-paste, top to bottom. Two terminals: one on **custLinux** (10.10.10.10, the user),
one on **WestLinux** (10.248.76.10, the generator). Order matters — the listener must be
up before any traffic.

## 1. custLinux — start the listener FIRST

```bash
cd ~/qosgen && git pull
sudo python3 lab/listener.py --sniff --iface ens4 --summary-every 5
```

Expect five `listening …` lines and `sniffing IP headers on ens4`. Leave it running.

## 2. WestLinux — source addresses, then traffic

```bash
cd ~/qosgen && git pull
sudo ./lab/dummy-interfaces.sh up
sudo ./lab/customer-streams.sh
```

## 3. Watch the listener

Within five seconds:

```
--- 23:19:10 ------------------------------------------------------
  proto source                   dport dscp        packets        bytes
  tcp   10.248.76.10:3389        59001 26 AF31          46       23,552
  ... 15 rows ...
  15 flow(s), markings seen: 26 AF31
```

**`15 flow(s), markings seen: 26 AF31`** is the result. qosgen sends DSCP 0, so every
marking was applied by the router.

## 4. Stop

Ctrl+C the streams, then Ctrl+C the listener (it prints a final table).

```bash
sudo ./lab/dummy-interfaces.sh down      # WestLinux, when finished with the node
```

---

# If it doesn't work

| Symptom | Cause |
|---|---|
| TCP streams exit instantly, "nothing is listening" | Listener not running, or started after the streams |
| Only UDP flows in the table | Same — the TCP streams died at launch, only UDP survived |
| Flood of ICMP port-unreachable in a capture | UDP arriving with no listener bound. Harmless; start the listener first |
| UDP fine, every TCP stream hangs | No `/32` return route — the SYN-ACK cannot get back |
| `bind()` fails, "is not an address on this host" | Dummy interfaces missing; a reboot removes them |
| "not allowed to bind source port" | Ports 80 and 443 are privileged — run the launcher under `sudo` |
| Everything arrives but DSCP is 0 | Policy on the wrong interface or direction, or the ACL matches destination ports |
| TCP rows show `dscp ?` | Listener started without `--sniff`, or without root |
| Nothing at all, no errors | Wrong `--iface`, or the node's link isn't drawn on the EVE-NG canvas |

## Checks, if you want to confirm before running

```bash
# custLinux
ip -br addr show ens4                    # 10.10.10.10/24
ss -ltun | grep -cE ':(5900[1-5]) '      # 5, once the listener is up

# WestLinux
ip -br addr show ens4                    # 10.248.76.10/25
ip neigh show dev ens4                   # gateway MAC starts 00:00:0c:07:ac (HSRP VIP)
ping -c2 10.10.10.10                     # end to end
./lab/dummy-interfaces.sh status         # both dummies UP and present
pgrep -fc 'qosgen.py stream'             # 0, no leftovers from an earlier run

# router
show ip route 10.248.76.138              # a /32, not Null0
show ip access-lists AF31_SWAN2_MARKING  # entries read "eq 80 any", NOT "any eq 80"
show policy-map interface GigabitEthernet0/5 input
```

Note the ACL hit counts before you start, so new matches are distinguishable from old.

---

# Reference

## The files

| File | Runs on | Purpose |
|---|---|---|
| `listener.py` | custLinux | Receives the streams, reports the DSCP that arrived |
| `custLinux_setup.sh` | custLinux | Lab address, detached listener, status, pcap capture |
| `dummy-interfaces.sh` | WestLinux | Creates the customer source addresses |
| `customer-streams.sh` | WestLinux | Every subnet × every application port |
| `af31-marking-swan2.cfg` | router | The AF31 ingress marking policy under test |
| `qos-policy-1mb.cfg` | router | Example 1 Mbps shaper + queueing policy |

## The stream matrix

Direction is **application → user**, so the application ports are *source* ports — which
is what the WAN QoS ACL matches on.

**west — WestLinux** (`SITE=west`, the default)

| Source address | Subnet | |
|---|---|---|
| `10.248.76.10` | 10.248.76.0/25 | WestLinux's own lab NIC — no dummy needed |
| `10.248.76.138` | 10.248.76.128/25 | dummy0 |
| `10.248.77.138` | 10.248.77.128/25 | dummy1 |

**east — EastLinux** (`SITE=east`, node not built yet)

| Source address | Subnet | |
|---|---|---|
| `10.248.82.10` | 10.248.82.0/25 | assumed to be its own lab NIC — confirm when built |
| `10.248.82.138` | 10.248.82.128/25 | dummy0 |
| `10.248.83.138` | 10.248.83.128/25 | dummy1 |

| Application | Protocol | Source port | Destination port |
|---|---|---|---|
| RDP | tcp | 3389 | 59001 |
| HTTPS | tcp | 443 | 59002 |
| HTTPS-alt | tcp | 8443 | 59003 |
| RDP over UDP | udp | 3389 | 59004 |
| HTTP | tcp | 80 | 59005 |

Three subnets × five applications = **15 concurrent streams per site**, covering that
site's ACL lines in one run.

## Router prerequisites

A host route per dummy address, more specific than any null route covering the block —
without these, UDP flows and every TCP stream hangs at the handshake:

```
ip route 10.248.76.138 255.255.255.255 10.248.76.10
ip route 10.248.77.138 255.255.255.255 10.248.76.10
```

`10.248.76.10` is WestLinux's real address and should already be routable. EastLinux
will need the same for `10.248.82.138` and `10.248.83.138`.

## Evidence for a customer

Three independent views. Three agreeing is proof; one is an assertion.

1. **The listener table** — 15 flows, `markings seen: 26 AF31`
2. **A pcap** — `sudo ./lab/custLinux_setup.sh capture`, then `ip.dsfield.dscp == 26`
   in Wireshark. The forward direction is marked; the ACKs coming back are DSCP 0,
   which is correct — the policy only classifies the application → user direction
3. **Router counters** — `show ip access-lists AF31_SWAN2_MARKING`, per-line hits.
   The east lines stay at zero until EastLinux exists, which is a useful control

## Other options

```bash
sudo PPS=50 ./lab/customer-streams.sh          # heavier, 50 pps per stream
sudo SITE=east ./lab/customer-streams.sh       # once EastLinux exists
./lab/custLinux_setup.sh listeners             # detached listener, no live table
python3 lab/listener.py --self-test            # prove the header parsing, no privilege
```

## After an EVE-NG node wipe

A wipe resets the node to its base image: repo, netplan file and installed packages all
gone. Dummy interfaces don't survive a plain reboot either. Re-clone, re-run
`custLinux_setup.sh netplan`, and work down this page again.

Add both NICs **before** a node's first boot. Inserting an interface into a running,
configured node can reshuffle which topology link lands on which NIC, and the symptom —
carrier up, config valid, no DHCP — takes a long time to pin down.

## More detail

- [../stream.md](../stream.md) — the `stream` pipeline: every flag, sourcing from
  addresses the host doesn't own, why the order of operations is what it is
- [../qos.md](../qos.md) — the `qos` pipeline: marked voice, signaling and noise
- [../README.md](../README.md) — install, troubleshooting, DSCP reference
