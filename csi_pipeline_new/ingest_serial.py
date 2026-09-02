#!/usr/bin/env python3
"""Read CSI_DATA lines from ESP32-C5 serial and batch-insert into PostgreSQL."""

from __future__ import annotations

import argparse
import glob
import os
import socket
import subprocess
import sys
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
        )
        for s in batch
    ]
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO csi_samples (
                session_id, seq, mac, rssi, rate, noise_floor, fft_gain, agc_gain,
                channel, device_ts, host_ts, sig_len, rx_format, len, first_word, iq
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
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
            "Port is busy. Quit idf.py monitor / screen / plot_csi.sh, then retry."
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


def process_line(line: str | None, batch: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not line or line.startswith("#") or line.startswith("type,"):
        return None
    sample = parse_csi_line(line)
    if sample is None:
        return None
    batch.append(sample)
    return sample


def run(args: argparse.Namespace) -> None:
    database_url = args.database_url or os.environ.get(
        "DATABASE_URL", DEFAULT_DATABASE_URL
    )
    baud = args.baud
    from_file = args.from_file
    port = args.port
    if from_file:
        recv_port = f"file:{from_file}"
    else:
        port = port or find_port()
        recv_port = port

    print(f"database {database_url}")
    if from_file:
        print(f"source file {from_file}")
    else:
        print(f"port {port} @ {baud}")
    print(f"method {args.method} label={args.label!r}")

    with Connection.connect(database_url) as conn:
        session_id = create_session(
            conn,
            method=args.method,
            label=args.label,
            recv_port=recv_port,
            baud=baud if not from_file else None,
            channel=args.channel,
        )
        print(f"session_id {session_id}")

        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        total = 0
        ser: serial.Serial | None = None

        try:
            if from_file:
                print("replaying file…")
                for line in iter_lines_file(from_file):
                    sample = process_line(line, batch)
                    if sample is None:
                        continue
                    if len(batch) >= args.batch_size:
                        total += flush_batch(conn, session_id, batch)
                        batch.clear()
                        print(
                            f"inserted total={total} last_seq={sample['seq']}",
                            flush=True,
                        )
            else:
                print("Ctrl+C to stop")
                ser = open_serial(port, baud)
                for line in iter_lines_serial(ser):
                    if line is None:
                        if batch and (time.monotonic() - last_flush) >= args.flush_s:
                            total += flush_batch(conn, session_id, batch)
                            batch.clear()
                            last_flush = time.monotonic()
                            print(f"flushed total={total}", flush=True)
                        continue
                    sample = process_line(line, batch)
                    if sample is None:
                        continue
                    now = time.monotonic()
                    if len(batch) >= args.batch_size or (now - last_flush) >= args.flush_s:
                        total += flush_batch(conn, session_id, batch)
                        batch.clear()
                        last_flush = now
                        print(
                            f"inserted total={total} last_seq={sample['seq']}",
                            flush=True,
                        )
        except KeyboardInterrupt:
            print()
        finally:
            try:
                total += flush_batch(conn, session_id, batch)
            except Exception as exc:  # noqa: BLE001 — best-effort final flush
                print(f"final flush failed: {exc}", file=sys.stderr)
            end_session(conn, session_id)
            if ser is not None:
                ser.close()
            print(f"stopped session_id={session_id} rows={total}")
            print(
                "verify:\n"
                f"  SELECT count(*), min(host_ts), max(host_ts)\n"
                f"  FROM csi_samples WHERE session_id = '{session_id}';"
            )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest ESP32 CSI_DATA serial lines into PostgreSQL."
    )
    p.add_argument(
        "--port",
        help="Serial port (default: first /dev/cu.usbmodem* or usbserial*)",
    )
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
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
        help="Replay CSI_DATA lines from a text/CSV file (no serial; for local dry-run)",
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
