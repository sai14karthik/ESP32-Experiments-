# SoftAP + internet (no constant Wi‑Fi flipping)

## The conflict

| Mac Wi‑Fi joined to | Video to ESP SoftAP | Internet |
|---------------------|---------------------|----------|
| SaiPhone / Instant Hotspot | No | Yes |
| **XIAO-CAM** | Yes | No (unless USB tether) |

You cannot put one Wi‑Fi radio on two SSIDs. SoftAP needs XIAO-CAM; SaiPhone needs SaiPhone.

## Recommended: SoftAP video + USB internet

1. Flash SoftAP (already on board if serial shows `XIAO-CAM`).
2. Mac **Wi‑Fi** → **XIAO-CAM** / `12345678` → IP `192.168.4.2`.
3. Plug **iPhone USB** into Mac → Personal Hotspot on → internet via USB.
4. Leave Wi‑Fi on XIAO-CAM (don’t switch back to SaiPhone).
5. `./scripts/run_softap_with_internet.sh`  
   or `./scripts/run_softap_cam.sh`

Watch:
- RTSP: `rtsp://127.0.0.1:8554/cam_xiao`
- WebRTC: http://127.0.0.1:8889/cam_xiao/

## Alternate: everything on SaiPhone Wi‑Fi (one network)

Only works if Mac joins SaiPhone as **normal Wi‑Fi** (`172.20.10.x`), **not** Instant Hotspot (`192.0.0.x`).

1. iPhone: Personal Hotspot → Maximize Compatibility + Allow Others to Join.
2. Mac: Wi‑Fi → SaiPhone (password), confirm `172.20.10.x`.
3. Flash cam STA to SaiPhone (not SoftAP).
4. `XIAO_RTSP_URL=rtsp://172.20.10.3:554/mjpeg/1 ./scripts/mediamtx_run.sh`

If Mac stays on Instant Hotspot (`192.0.0.x`), cam on `172.20.10.x` will never connect — that was the earlier failure.
