#!/usr/bin/env python3
"""Read CSI_DATA lines from ESP32-C5 serial and batch-insert into PostgreSQL."""

from __future__ import annotations

import argparse
import glob
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from uuid import UUID

import serial
from psycopg import Connection

from csi_parse import DEFAULT_BAUD, parse_csi_line

DEFAULT_BATCH_SIZE = 100
DEFAULT_FLUSH_S = 0.1
DEFAULT_DATABASE_URL = "postgresql://localhost/csi"


def find_port() -> str:
    ports = sorted(
        glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*")
    )
    if not ports:
        sys.exit("No /dev/cu.usbmodem* or /dev/cu.usbserial* — plug in the C5 recv.")
    return ports[0]


def git_commit() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def create_session(
    conn: Connection,
    *,
    method: str,
    label: str | None,
    recv_port: str,
    baud: int | None,
    channel: int | None,
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO csi_sessions
                (method, label, recv_port, baud, channel, host, git_commit)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                method,
                label,
                recv_port,
                baud,
                channel,
                socket.gethostname(),
                git_commit(),
            ),
        )
        row = cur.fetchone()
        assert row is not None
        session_id = row[0]
    conn.commit()
    return session_id


def end_session(conn: Connection, session_id: UUID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE csi_sessions SET ended_at = now() WHERE id = %s",
            (session_id,),
        )
    conn.commit()


def ensure_source_id_column(conn: Connection) -> None:
    """Idempotent migration for multi-C5 fan-in."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE csi_samples ADD COLUMN IF NOT EXISTS source_id TEXT"
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS csi_samples_session_source_idx
            ON csi_samples (session_id, source_id)
            """
        )
    conn.commit()


def flush_batch(conn: Connection, session_id: UUID, batch: list[dict[str, Any]]) -> int:
    if not batch:
        return 0
    rows = [
        (
            session_id,
            s["seq"],
            s["mac"],
            s["rssi"],
            s["rate"],
            s["noise_floor"],
            s["fft_gain"],
            s["agc_gain"],
            s["channel"],
            s["device_ts"],
            s["host_ts"],
            s["sig_len"],
            s["rx_format"],
            s["len"],
            s["first_word"],
            s["iq"],
            s.get("source_id"),
        )
        for s in batch
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO csi_samples (
                session_id, seq, mac, rssi, rate, noise_floor, fft_gain, agc_gain,
                channel, device_ts, host_ts, sig_len, rx_format, len, first_word, iq,
                source_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s
            )
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def open_serial(port: str, baud: int) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 1
    ser.dtr = False
    ser.rts = False
    try:
        ser.open()
    except serial.SerialException as exc:
        sys.exit(
            f"{exc}\n"
            "Port is busy. Quit idf.py monitor / screen / ./scripts/plot_csi.sh, then retry."
        )
    time.sleep(2.0)
    ser.reset_input_buffer()
    return ser


def iter_lines_serial(ser: serial.Serial):
    while True:
        raw = ser.readline()
        if not raw:
            yield None
            continue
        yield raw.decode("utf-8", errors="replace").strip()


def iter_lines_file(path: str):
    with open(path, encoding="utf-8") as f:
        for line in f:
            yield line.strip()


def iter_lines_tcp(
    port: int,
    bind: str = "0.0.0.0",
    backlog: int = 8,
    *,
    status: dict[str, Any] | None = None,
):
    """Fan-in: accept multiple ESP32-C5 TCP clients; yield CSI lines concurrently.

    One client disconnect/error must not stop others — each board has its own
    reader thread; the acceptor keeps listening for reconnects.

    Yields None (idle) or (source_id, line) where source_id is the client IP.

    If ``status`` is provided, it is updated under an internal lock with:
    ``active`` (unique client IPs), ``connections`` (open TCP sockets),
    and ``ips`` (sorted unique client IPs).
    """
    q: queue.Queue[Any] = queue.Queue(maxsize=20000)
    stop = threading.Event()
    clients_lock = threading.Lock()
    n_clients = 0
    # IP → open connection count (one board usually = 1; briefly 2 on reconnect).
    ip_counts: dict[str, int] = {}
    sentinel = object()

    def _publish_status() -> None:
        if status is None:
            return
        status["connections"] = n_clients
        status["active"] = len(ip_counts)
        status["ips"] = sorted(ip_counts.keys())

    if status is not None:
        _publish_status()

    def _client_reader(conn: socket.socket, addr: tuple[str, int]) -> None:
        nonlocal n_clients
        source_id = addr[0]
        conn.settimeout(1.0)
        # Fail one socket fast; do not block sibling clients on TCP wait.
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        buf = b""
        try:
            while not stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 2_000_000:
                    # Corrupt/flooded client — drop this board only.
                    print(
                        f"client {source_id}: buffer overrun, dropping connection",
                        flush=True,
                    )
                    break
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw = buf[:nl]
                    buf = buf[nl + 1 :]
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        q.put((source_id, line), timeout=0.05)
                    except queue.Full:
                        # Prefer keeping newest from any client over blocking.
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            q.put_nowait((source_id, line))
                        except queue.Full:
                            pass
        except Exception as exc:  # noqa: BLE001 — isolate one bad client
            print(f"client {source_id} error (others continue): {exc}", flush=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with clients_lock:
                n_clients = max(0, n_clients - 1)
                left = ip_counts.get(source_id, 0) - 1
                if left <= 0:
                    ip_counts.pop(source_id, None)
                else:
                    ip_counts[source_id] = left
                left_n = n_clients
                n_ips = len(ip_counts)
                _publish_status()
            print(
                f"client disconnected {addr[0]}:{addr[1]} "
                f"(sockets={left_n}, boards={n_ips}; ingest continues)",
                flush=True,
            )

    def _acceptor() -> None:
        nonlocal n_clients
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind, port))
        srv.listen(backlog)
        srv.settimeout(1.0)
        print(
            f"listening tcp://{bind}:{port} (multi-C5 fan-in, backlog={backlog})",
            flush=True,
        )
        try:
            while not stop.is_set():
                try:
                    conn, addr = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with clients_lock:
                    n_clients += 1
                    ip_counts[addr[0]] = ip_counts.get(addr[0], 0) + 1
                    active = n_clients
                    n_ips = len(ip_counts)
                    _publish_status()
                print(
                    f"client connected {addr[0]}:{addr[1]} "
                    f"(sockets={active}, boards={n_ips})",
                    flush=True,
                )
                threading.Thread(
                    target=_client_reader,
                    args=(conn, addr),
                    name=f"csi-tcp-{addr[0]}-{addr[1]}",
                    daemon=True,
                ).start()
        finally:
            try:
                srv.close()
            except OSError:
                pass
            q.put(sentinel)

    threading.Thread(target=_acceptor, name="csi-tcp-accept", daemon=True).start()
    try:
        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                yield None
                continue
            if item is sentinel:
                break
            yield item
    finally:
        stop.set()


def process_line(
    line: str | None,
    batch: list[dict[str, Any]],
    *,
    source_id: str | None = None,
) -> dict[str, Any] | None:
    if not line or line.startswith("#") or line.startswith("type,"):
        return None
    sample = parse_csi_line(line)
    if sample is None:
        return None
    iq = sample.get("iq") or []
    if len(iq) < 2 or len(iq) % 2 != 0:
        return None
    sample["source_id"] = source_id
    batch.append(sample)
    return sample


def _ingest_stream(
    conn: Connection,
    session_id: UUID,
    lines,
    *,
    batch_size: int,
    flush_s: float,
    idle_warn_s: float = 15.0,
) -> int:
    """Flush loop for serial / TCP. TCP multi yields (source_id, line)."""
    batch: list[dict[str, Any]] = []
    last_flush = time.monotonic()
    last_sample = time.monotonic()
    last_idle_warn = 0.0
    last_by_source: dict[str, float] = {}
    last_status = time.monotonic()
    status_s = 30.0
    total = 0
    seen_sources: set[str] = set()
    try:
        for item in lines:
            if item is None:
                now = time.monotonic()
                if batch and (now - last_flush) >= flush_s:
                    total += flush_batch(conn, session_id, batch)
                    batch.clear()
                    last_flush = now
                    print(f"flushed total={total}", flush=True)
                if (
                    last_by_source
                    and status_s > 0
                    and (now - last_status) >= status_s
                ):
                    parts = []
                    for sid in sorted(last_by_source):
                        age = now - last_by_source[sid]
                        parts.append(f"{sid}({age:.0f}s ago)")
                    print(
                        f"status: {len(last_by_source)} source(s) "
                        f"rows={total}  {' '.join(parts)}",
                        flush=True,
                    )
                    last_status = now
                if idle_warn_s > 0 and (now - last_idle_warn) >= idle_warn_s:
                    quiet = [
                        sid
                        for sid, t in last_by_source.items()
                        if (now - t) >= idle_warn_s
                    ]
                    live = [
                        sid
                        for sid, t in last_by_source.items()
                        if (now - t) < idle_warn_s
                    ]
                    if quiet and live:
                        print(
                            f"warning: quiet sources {quiet} "
                            f"(>{idle_warn_s:.0f}s); still ingesting from {live}",
                            flush=True,
                        )
                        last_idle_warn = now
                    elif (now - last_sample) >= idle_warn_s:
                        print(
                            f"warning: no CSI lines for {now - last_sample:.0f}s "
                            f"(all C5s idle; watchdog should recover)",
                            flush=True,
                        )
                        last_idle_warn = now
                continue

            source_id: str | None = None
            if isinstance(item, tuple) and len(item) == 2:
                source_id, line = item
            else:
                line = item  # type: ignore[assignment]

            sample = process_line(line, batch, source_id=source_id)
            if sample is None:
                continue
            if source_id and source_id not in seen_sources:
                seen_sources.add(source_id)
                n_iq = len(sample.get("iq") or [])
                print(
                    f"first CSI from {source_id}: format={sample.get('format')}  "
                    f"mac={sample.get('mac')}  rssi={sample.get('rssi')}  "
                    f"iq_len={n_iq}  sources={sorted(seen_sources)}",
                    flush=True,
                )
            elif not source_id and not seen_sources:
                seen_sources.add("usb")
                n_iq = len(sample.get("iq") or [])
                print(
                    f"first CSI: format={sample.get('format')}  "
                    f"mac={sample.get('mac')}  rssi={sample.get('rssi')}  "
                    f"iq_len={n_iq}  subcarriers={n_iq // 2}",
                    flush=True,
                )
            last_sample = time.monotonic()
            if source_id:
                last_by_source[source_id] = last_sample
            now = last_sample
            if len(batch) >= batch_size or (now - last_flush) >= flush_s:
                total += flush_batch(conn, session_id, batch)
                batch.clear()
                last_flush = now
                print(
                    f"inserted total={total} last_seq={sample['seq']} "
                    f"source={source_id or '?'}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print()
    finally:
        try:
            total += flush_batch(conn, session_id, batch)
        except Exception as exc:  # noqa: BLE001
            print(f"final flush failed: {exc}", file=sys.stderr)
    return total


def run(args: argparse.Namespace) -> None:
    database_url = args.database_url or os.environ.get(
        "DATABASE_URL", DEFAULT_DATABASE_URL
    )
    baud = args.baud
    from_file = args.from_file
    listen_tcp = args.listen_tcp
    port = args.port

    if from_file and listen_tcp is not None:
        sys.exit("Use only one of --from-file / --listen-tcp / --port")
    if listen_tcp is not None and port:
        sys.exit("Use only one of --listen-tcp / --port")

    if from_file:
        recv_port = f"file:{from_file}"
        session_baud: int | None = None
    elif listen_tcp is not None:
        recv_port = f"tcp:{listen_tcp}:multi"
        session_baud = None
    else:
        port = port or find_port()
        recv_port = port
        session_baud = baud

    print(f"database {database_url}")
    if from_file:
        print(f"source file {from_file}")
    elif listen_tcp is not None:
        print(f"listen tcp 0.0.0.0:{listen_tcp} (multi-C5)")
    else:
        print(f"port {port} @ {baud}")
    print(f"method {args.method} label={args.label!r}")

    with Connection.connect(database_url) as conn:
        ensure_source_id_column(conn)
        session_id = create_session(
            conn,
            method=args.method,
            label=args.label,
            recv_port=recv_port,
            baud=session_baud,
            channel=args.channel,
        )
        print(f"session_id {session_id}")

        total = 0
        ser: serial.Serial | None = None

        try:
            if from_file:
                print("replaying file…")
                batch: list[dict[str, Any]] = []
                saw_format = False
                try:
                    for line in iter_lines_file(from_file):
                        sample = process_line(line, batch)
                        if sample is None:
                            continue
                        if not saw_format:
                            saw_format = True
                            n_iq = len(sample.get("iq") or [])
                            print(
                                f"first CSI: format={sample.get('format')}  "
                                f"mac={sample.get('mac')}  rssi={sample.get('rssi')}  "
                                f"iq_len={n_iq}  subcarriers={n_iq // 2}",
                                flush=True,
                            )
                        if len(batch) >= args.batch_size:
                            total += flush_batch(conn, session_id, batch)
                            batch.clear()
                            print(
                                f"inserted total={total} last_seq={sample['seq']}",
                                flush=True,
                            )
                finally:
                    try:
                        total += flush_batch(conn, session_id, batch)
                    except Exception as exc:  # noqa: BLE001
                        print(f"final flush failed: {exc}", file=sys.stderr)
            elif listen_tcp is not None:
                print(
                    "Ctrl+C to stop — ingesting from ALL connected C5s "
                    "(boards reconnect on their own if Mini listens)"
                )
                total = _ingest_stream(
                    conn,
                    session_id,
                    iter_lines_tcp(listen_tcp),
                    batch_size=args.batch_size,
                    flush_s=args.flush_s,
                )
            else:
                print("Ctrl+C to stop")
                ser = open_serial(port, baud)
                total = _ingest_stream(
                    conn,
                    session_id,
                    iter_lines_serial(ser),
                    batch_size=args.batch_size,
                    flush_s=args.flush_s,
                )
        except KeyboardInterrupt:
            print()
        finally:
            end_session(conn, session_id)
            if ser is not None:
                ser.close()
            print(f"stopped session_id={session_id} rows={total}")
            print(
                "verify:\n"
                f"  SELECT source_id, count(*), min(host_ts), max(host_ts)\n"
                f"  FROM csi_samples WHERE session_id = '{session_id}'\n"
                f"  GROUP BY 1 ORDER BY 1;"
            )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Ingest ESP32 CSI_DATA (USB or TCP) into PostgreSQL. "
            "TCP accepts multiple C5 clients (fan-in) on one port."
        )
    )
    p.add_argument(
        "--port",
        help="Serial port (default: first /dev/cu.usbmodem* or usbserial*)",
    )
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument(
        "--listen-tcp",
        nargs="?",
        const=9055,
        type=int,
        default=None,
        metavar="PORT",
        help="Listen for CSI_DATA over TCP (default 9055). Multi-C5 fan-in.",
    )
    p.add_argument(
        "--method",
        default="4.3",
        help="CSI method tag stored on the session (default: 4.3)",
    )
    p.add_argument(
        "--label",
        default=None,
        help="Session label / notes (subject, distance, activity, …)",
    )
    p.add_argument(
        "--channel",
        type=int,
        default=None,
        help="Optional Wi-Fi channel stored on the session (4.3 often 11)",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help=f"Postgres URL (default: $DATABASE_URL or {DEFAULT_DATABASE_URL})",
    )
    p.add_argument(
        "--from-file",
        default=None,
        help="Replay CSI_DATA lines from a text/CSV file (no serial)",
    )
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument(
        "--flush-s",
        type=float,
        default=DEFAULT_FLUSH_S,
        help="Max seconds between batch flushes (default: 0.1)",
    )
    return p


def main() -> None:
    run(build_parser().parse_args())


if __name__ == "__main__":
    main()
