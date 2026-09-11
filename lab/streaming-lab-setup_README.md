# lab/ — files for running qosgen against a lab

Everything here supports one test: traffic leaves a Linux node unmarked, crosses the
DC perimeter router, and should come out the other side marked **AF31** (DSCP 26,
`tos 0x68`). Direction is **datacentre → user**, which is why the application ports
(TCP 3389, 443, 8443 and UDP 3389) are *source* ports.

| File | Runs on | Purpose |
|---|---|---|
| `dummy-interfaces.sh` | generator node | Creates the four DC application source addresses |
| `four-sources.sh` | generator node | Starts the four streams together, stops them together |
| `custLinux_setup.sh` | user node | Lab address, TCP listeners, status, capture |
| `qos-policy-1mb.cfg` | router | Example Cisco 1 Mbps shaper + queueing policy |

Two Linux nodes are involved:

- **generator** — stands in for the DC application servers. Sources the traffic.
- **user** — stands in for the person using those applications. Receives it.

## Order of operations

### 1. User node

```bash
git clone https://github.com/juddda/qosgen.git && cd qosgen
sudo ADDR=<user-ip> PREFIX=<len> GATEWAY=<lab-router-ip> ./lab/custLinux_setup.sh netplan
./lab/custLinux_setup.sh listeners
./lab/custLinux_setup.sh status
```

`netplan` gives the lab NIC a static address with the lab prefixes (10/8 and 172.16/12)
via the lab router and **no default route** — the default stays on the DHCP interface so
the node keeps its internet path. 192.168/16 is deliberately not routed to the lab: the
lab doesn't use it and the management network does. It applies with `netplan try`, which rolls back in 120 seconds if the
change cuts your session.

`listeners` starts `nc` on the TCP destination ports. UDP needs nothing listening.
Requires `netcat-openbsd`; install it with `sudo apt install netcat-openbsd` if the node
image doesn't have it.

### 2. Generator node

```bash
git clone https://github.com/juddda/qosgen.git && cd qosgen
pip install -r requirements.txt
sudo ./lab/dummy-interfaces.sh up
./lab/dummy-interfaces.sh status
```

The source addresses live on dummy interfaces because `bind()` only accepts addresses
the kernel owns, and the DC application subnets aren't on this node. `/32` each — the
mask never appears in the packet, and a `/25` would blackhole the whole block locally.

### 3. Routers

Add a host route per source address, pointing back at the generator. More specific than
any null route covering those prefixes:

```
ip route 10.1.1.10 255.255.255.255 <generator-lab-ip>
```

Without these the user's TCP ACKs have nowhere to go and the handshakes never complete.
UDP flows regardless, which makes it a useful way to isolate a routing problem.

### 4. Run it

Edit `DST_IP` and the `STREAMS` array in `four-sources.sh` to your real addresses, then
on the generator:

```bash
sudo PYTHON="$(command -v python)" DST_IP=<user-ip> ./lab/four-sources.sh
```

`sudo` because source port 443 is privileged, and because a mixed root/non-root process
group can't be stopped by one Ctrl+C. `PYTHON` explicitly because sudo resets `PATH` to
root's, where a conda or venv interpreter — and click — isn't found.

Ctrl+C stops all four streams.

### 5. Watch it

On the user node:

```bash
sudo ./lab/custLinux_setup.sh capture      # expect tos 0x68
```

Or capture either side of the perimeter router in Wireshark:

| Capture point | Filter | Expected |
|---|---|---|
| Generator side (LAN) | `ip.dsfield.dscp == 0` | unmarked, as sent |
| WAN side | `ip.dsfield.dscp == 26` | AF31, applied by the router |

On the router: `show policy-map interface <interface>`.

## Teardown

```bash
# generator
sudo ./lab/dummy-interfaces.sh down

# user
./lab/custLinux_setup.sh stop
```

## After an EVE-NG node wipe

A wipe resets the node to its base image, so the repo, the netplan file, and any
installed packages are gone. Dummy interfaces and `sysctl` settings don't survive a
plain reboot either — neither is persisted. Re-run the steps above; that is most of the
reason these are scripts rather than notes.

## More detail

- [../stream.md](../stream.md) — the `stream` pipeline: every flag, sourcing from
  addresses the host doesn't own, running several streams at once
- [../qos.md](../qos.md) — the `qos` pipeline: marked voice, signaling and noise
- [../README.md](../README.md) — install, troubleshooting, DSCP reference
