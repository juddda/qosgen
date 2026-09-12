# Quick start

**custLinux** (do this first)

```bash
cd ~/qosgen && git pull
sudo python3 lab/listener.py --sniff --iface ens4 --summary-every 5
```

**WestLinux**

```bash
cd ~/qosgen && git pull
sudo ./lab/dummy-interfaces.sh up
sudo ./lab/customer-streams.sh
```

**Stop**

```bash
# Ctrl+C both terminals, then on WestLinux:
sudo ./lab/dummy-interfaces.sh down
```

Detail: [streaming-lab-setup_README.md](streaming-lab-setup_README.md)
