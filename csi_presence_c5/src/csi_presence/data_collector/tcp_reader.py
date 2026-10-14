"""TCP fan-in for CSI_DATA (boards → Mac Mini :9055).

Matches lab csi_recv_router forwarder: each wall-powered C5 is a TCP client;
this host listens and accepts many clients.
"""

from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Iterator
from typing import Optional

from csi_presence.csi_parse import CsiSample, parse_csi_line


class CsiTcpListener:
    def __init__(self, port: int = 9055, host: str = "0.0.0.0", backlog: int = 8):
        self.port = port
        self.host = host
        self.backlog = backlog
        self._sock: Optional[socket.socket] = None
        self._clients: dict[socket.socket, tuple[str, str]] = {}  # sock -> (ip, buf)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def open(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(self.backlog)
        s.setblocking(False)
        self._sock = s
        print(f"[csi] TCP listen {self.host}:{self.port} (wall-powered C5 → Mini)")

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for c in list(self._clients):
                try:
                    c.close()
                except OSError:
                    pass
            self._clients.clear()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> CsiTcpListener:
        self.open()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @property
    def active_clients(self) -> int:
        with self._lock:
            return len(self._clients)

    def iter_samples(self) -> Iterator[CsiSample]:
        if not self._sock:
            raise RuntimeError("TCP listener not open")
        last_status = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_status >= 5.0:
                n = self.active_clients
                if n == 0:
                    print(
                        f"[csi] waiting for C5 TCP clients on :{self.port}… "
                        "(boards on LabPSK + wall power)"
                    )
                else:
                    print(f"[csi] active TCP clients={n}")
                last_status = now

            try:
                readable, _, _ = select.select(
                    [self._sock, *self._clients.keys()], [], [], 0.2
                )
            except (ValueError, OSError):
                time.sleep(0.05)
                continue

            for sock in readable:
                if sock is self._sock:
                    try:
                        conn, addr = self._sock.accept()
                    except OSError:
                        continue
                    ip = addr[0]
                    # Health checks (check_esp) briefly connect to :9055 — ignore localhost
                    if ip in ("127.0.0.1", "::1"):
                        try:
                            conn.close()
                        except OSError:
                            pass
                        continue
                    conn.setblocking(False)
                    with self._lock:
                        self._clients[conn] = (ip, "")
                    print(f"[csi] client connected {ip}:{addr[1]}  active={self.active_clients}")
                    continue

                with self._lock:
                    meta = self._clients.get(sock)
                if meta is None:
                    continue
                ip, buf = meta
                try:
                    chunk = sock.recv(8192)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    try:
                        sock.close()
                    except OSError:
                        pass
                    with self._lock:
                        self._clients.pop(sock, None)
                    print(f"[csi] client disconnected {ip}  active={self.active_clients}")
                    continue

                buf += chunk.decode("utf-8", errors="ignore")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    sample = parse_csi_line(line, time.time(), source_id=ip)
                    if sample is not None:
                        yield sample
                with self._lock:
                    if sock in self._clients:
                        self._clients[sock] = (ip, buf)
