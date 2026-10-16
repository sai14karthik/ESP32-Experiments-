"""PostgreSQL writer for CSI presence collect (Mini `postgresql:///csi`)."""

from __future__ import annotations

import os
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from uuid import UUID

from csi_presence.csi_parse import CsiSample
from csi_presence.features import FEATURE_VERSION

DEFAULT_DATABASE_URL = "postgresql:///csi"
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_SQL = ROOT / "schema.sql"


def database_url(override: str | None = None) -> str:
    return (override or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL).strip()


def _git_commit() -> str | None:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=ROOT,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            or None
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _ts(host_ts: float) -> datetime:
    return datetime.fromtimestamp(host_ts, tz=timezone.utc)


class PresenceDb:
    """Batched inserts into csi_sessions / csi_samples / presence_windows."""

    def __init__(self, url: str | None = None, *, batch_size: int = 64):
        import psycopg

        self.url = database_url(url)
        self.batch_size = max(1, batch_size)
        self._psycopg = psycopg
        self.conn = psycopg.connect(self.url)
        self.session_id: UUID | None = None
        self.session_tag: str = ""
        self._sample_batch: list[tuple[Any, ...]] = []
        self.n_samples = 0
        self.n_windows = 0
        self.n_empty = 0
        self.n_occupied = 0

    def close(self) -> None:
        try:
            self.flush_samples()
        finally:
            self.conn.close()

    def __enter__(self) -> PresenceDb:
        return self
    def __exit__(self, *exc: object) -> None:
        self.close()

    def ensure_schema(self) -> None:
        sql = SCHEMA_SQL.read_text() if SCHEMA_SQL.exists() else ""
        if not sql.strip():
            raise FileNotFoundError(f"missing schema: {SCHEMA_SQL}")
        with self.conn.cursor() as cur:
            cur.execute(sql)
        self.conn.commit()

    def start_session(
        self,
        *,
        method: str,
        label: str | None,
        recv_port: str,
        baud: int | None = None,
        session_tag: str = "",
    ) -> UUID:
        self.session_tag = session_tag
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO csi_sessions
                    (method, label, recv_port, baud, host, git_commit)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    method,
                    label,
                    recv_port,
                    baud,
                    socket.gethostname(),
                    _git_commit(),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            self.session_id = row[0]
        self.conn.commit()
        return self.session_id

    def end_session(self) -> None:
        if self.session_id is None:
            return
        self.flush_samples()
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE csi_sessions SET ended_at = now() WHERE id = %s",
                (self.session_id,),
            )
        self.conn.commit()

    def enqueue_sample(self, sample: CsiSample) -> None:
        if self.session_id is None:
            raise RuntimeError("start_session() first")
        meta = _sample_meta(sample)
        self._sample_batch.append(
            (
                self.session_id,
                meta.get("seq"),
                sample.mac or None,
                int(sample.rssi) if sample.rssi is not None else None,
                meta.get("rate"),
                meta.get("noise_floor"),
                meta.get("fft_gain"),
                meta.get("agc_gain"),
                meta.get("channel"),
                meta.get("device_ts"),
                _ts(sample.host_ts),
                meta.get("sig_len"),
                meta.get("rx_format"),
                meta.get("len"),
                meta.get("first_word"),
                sample.iq,
                sample.source_id or None,
            )
        )
        if len(self._sample_batch) >= self.batch_size:
            self.flush_samples()

    def flush_samples(self) -> int:
        if not self._sample_batch:
            return 0
        rows = self._sample_batch
        self._sample_batch = []
        with self.conn.cursor() as cur:
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
        self.conn.commit()
        self.n_samples += len(rows)
        return len(rows)

    def insert_window(
        self,
        *,
        host_ts: float,
        label: int,
        label_src: str,
        method: str,
        n_csi: int,
        sources: Sequence[str],
        macs: Sequence[str],
        feat: Sequence[float],
        feature_version: int | None = None,
    ) -> None:
        if self.session_id is None:
            raise RuntimeError("start_session() first")
        ver = int(feature_version if feature_version is not None else FEATURE_VERSION)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO presence_windows (
                    session_id, host_ts, label, label_src, method, n_csi,
                    sources, macs, feat, session_tag, feature_version
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    self.session_id,
                    _ts(host_ts),
                    int(label),
                    label_src,
                    method,
                    n_csi,
                    list(sources) or None,
                    list(macs) or None,
                    list(feat),
                    self.session_tag or None,
                    ver,
                ),
            )
        self.conn.commit()
        self.n_windows += 1
        if label:
            self.n_occupied += 1
        else:
            self.n_empty += 1


def _sample_meta(sample: CsiSample) -> dict[str, int | None]:
    """Best-effort ints from CSI_DATA meta (C5: seq,mac,rssi,rate,...)."""
    out: dict[str, int | None] = {
        "seq": None,
        "rate": None,
        "noise_floor": None,
        "fft_gain": None,
        "agc_gain": None,
        "channel": None,
        "device_ts": None,
        "sig_len": None,
        "rx_format": None,
        "len": None,
        "first_word": None,
    }
    line = sample.raw_line.strip()
    if not line.startswith("CSI_DATA,"):
        return out
    start = line.rfind('"[')
    meta = line[len("CSI_DATA,") : start].rstrip(",") if start > 0 else ""
    parts = meta.split(",")
    if len(parts) < 3:
        return out

    def _i(idx: int) -> int | None:
        if idx >= len(parts):
            return None
        try:
            return int(float(parts[idx].strip()))
        except ValueError:
            return None

    # C5 between/router: seq, mac, rssi, rate, noise, fft, agc, ch, ts, sig_len, rx, len, first
    if len(parts) >= 2 and ":" in parts[1]:
        out["seq"] = _i(0)
        out["rate"] = _i(3)
        out["noise_floor"] = _i(4)
        out["fft_gain"] = _i(5)
        out["agc_gain"] = _i(6)
        out["channel"] = _i(7)
        out["device_ts"] = _i(8)
        out["sig_len"] = _i(9)
        out["rx_format"] = _i(10)
        out["len"] = _i(11)
        out["first_word"] = _i(12)
    return out


def distribution_report(url: str | None = None, *, tag: str | None = None) -> str:
    """Return empty/occupied counts from presence_windows."""
    import psycopg

    u = database_url(url)
    where = "WHERE session_tag = %s" if tag else ""
    args: tuple[Any, ...] = (tag,) if tag else ()
    with psycopg.connect(u) as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
              count(*) FILTER (WHERE label = 0) AS empty,
              count(*) FILTER (WHERE label = 1) AS occupied,
              count(*) AS total
            FROM presence_windows
            {where}
            """,
            args,
        )
        e, o, t = cur.fetchone() or (0, 0, 0)
        cur.execute(
            f"""
            SELECT session_tag, count(*) FILTER (WHERE label = 0),
                   count(*) FILTER (WHERE label = 1), count(*)
            FROM presence_windows
            {where}
            GROUP BY session_tag
            ORDER BY max(host_ts) DESC
            LIMIT 30
            """,
            args,
        )
        rows = cur.fetchall()
    lines = [
        f"DB {u}",
        f"TOTAL  empty={e}  occupied={o}  total={t}",
    ]
    if t:
        lines.append(
            f"share empty={100 * e / t:.1f}%  occupied={100 * o / t:.1f}%"
        )
    lines.append("")
    lines.append(f"{'session_tag':40s}  empty  occupied  total")
    for tag_v, ee, oo, tt in rows:
        lines.append(f"{(tag_v or '—'):40s}  {ee:5d}  {oo:8d}  {tt:5d}")
    return "\n".join(lines)
