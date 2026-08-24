
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL_HEADER = ROOT / "firmware" / "CameraRemoteUpload" / "upload_url.h"
NGROK_DOMAIN = "silica-cosigner-quail.ngrok-free.dev"


def wait_ngrok_url(timeout: float = 25.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=1) as resp:
                data = json.load(resp)
            for t in data.get("tunnels", []):
                url = t.get("public_url") or ""
                if url.startswith("https://"):
                    return url.rstrip("/") + "/upload"
        except Exception:
            time.sleep(0.4)
    raise SystemExit("ngrok did not publish an HTTPS URL. Is ngrok authtoken set?")


def write_header(url: str) -> None:
    URL_HEADER.write_text(
        "#pragma once\n"
        f'#define UPLOAD_URL "{url}"\n'
    )
    print(f"Wrote {URL_HEADER}")
    print(f"UPLOAD_URL={url}")


def stop_leftovers() -> None:
    """Stop a previous ngrok/receiver so this domain is free."""
    patterns = (
        "remote_server.py",
        f"ngrok http --url {NGROK_DOMAIN}",
        "start_remote_stream.py",
    )
    me = str(os.getpid())
    for pat in patterns:
        try:
            out = subprocess.check_output(["pgrep", "-f", pat], text=True)
        except subprocess.CalledProcessError:
            continue
        for pid in out.split():
            if pid == me:
                continue
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
    time.sleep(0.6)


def main() -> None:
    os.chdir(ROOT)
    stop_leftovers()
    server = subprocess.Popen([sys.executable, str(ROOT / "remote_server.py")])
    time.sleep(0.6)
    ngrok_log = open(ROOT / "ngrok.log", "w")
    ngrok = subprocess.Popen(
        ["ngrok", "http", "--url", NGROK_DOMAIN, "8000", "--log=stdout"],
        stdout=ngrok_log,
        stderr=subprocess.STDOUT,
    )
    try:
        url = wait_ngrok_url()
        write_header(url)
        print()
        print("Viewer on this laptop:  http://127.0.0.1:8000")
        print(f"Other devices:          https://{NGROK_DOMAIN}")
        print("Keep this script running.")
        print("If the ESP was flashed with this same UPLOAD_URL before, do not flash again.")
        print("Otherwise plug the XIAO in ONCE, flash firmware/CameraRemoteUpload, then unplug.")
        print("Ctrl+C to stop.")
        while True:
            time.sleep(1)
            if server.poll() is not None:
                raise SystemExit(f"remote_server.py exited ({server.returncode})")
            if ngrok.poll() is not None:
                raise SystemExit(
                    f"ngrok exited ({ngrok.returncode}). "
                    "Usually another ngrok still has this domain. "
                    "Run: pkill -f ngrok   then start again."
                )
    except KeyboardInterrupt:
        pass
    finally:
        ngrok.terminate()
        server.terminate()
        ngrok_log.close()


if __name__ == "__main__":
    main()
