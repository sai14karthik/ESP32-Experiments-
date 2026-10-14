# Firmware — wall-powered C5 → Mac Mini TCP

**Production path (Mini):** `csi_recv_router` + LabPSK + `CSI_TCP_HOST=<Mini IP>`.  
USB only to flash; then wall power. Collect listens on Mini `:9055`.

```bash
# On flash machine (boards plugged in)
cp ../configs/wifi.env.example ../configs/wifi.env   # edit SSID/PASS/CSI_TCP_HOST
./scripts/flash_wall_tcp.sh /dev/cu.usbmodem101 /dev/cu.usbmodem1101
# unplug → wall power
```

**Laptop USB debug only:** ESP-NOW pair `flash_tx.sh` / `flash_rx.sh` + collect `--rx /dev/cu…`.

Mini LabPSK IP default in example: `10.128.93.13`.
