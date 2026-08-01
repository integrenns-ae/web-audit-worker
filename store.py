"""store — SQLite-Persistenz für Jobs + Rate-Limit-Log.

Synchron (sqlite3) und über asyncio.to_thread aufgerufen. Für Trichter-Volumen
mehr als ausreichend; überlebt Neustarts.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Optional

DB_PATH = os.environ.get("AUDIT_DB_PATH", "/data/audit.db")

_conn: Optional[sqlite3.Connection] = None


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                url         TEXT NOT NULL,
                domain      TEXT NOT NULL,
                status      TEXT NOT NULL,          -- pending|done|nicht_auditierbar|failed
                result      TEXT,                   -- JSON (bei done)
                grund       TEXT,                   -- bei nicht_auditierbar/failed
                created_at  REAL NOT NULL,
                finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_domain ON jobs(domain, finished_at);
            CREATE TABLE IF NOT EXISTS req_log (
                ip   TEXT NOT NULL,
                ts   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_req_ip ON req_log(ip, ts);
            """
        )
        _conn.commit()
    return _conn


def create_job(job_id: str, url: str, domain: str) -> None:
    _c().execute(
        "INSERT INTO jobs (id, url, domain, status, created_at) VALUES (?,?,?,?,?)",
        (job_id, url, domain, "pending", time.time()),
    )
    _c().commit()


def finish_job(job_id: str, status: str, result: dict | None = None, grund: str | None = None) -> None:
    _c().execute(
        "UPDATE jobs SET status=?, result=?, grund=?, finished_at=? WHERE id=?",
        (status, json.dumps(result) if result is not None else None, grund, time.time(), job_id),
    )
    _c().commit()


def get_job(job_id: str) -> dict | None:
    row = _c().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    return d


def recent_done_for_domain(domain: str, within_s: int) -> dict | None:
    """Für Domain-Cooldown: letzten fertigen Job innerhalb des Fensters liefern."""
    cutoff = time.time() - within_s
    row = _c().execute(
        "SELECT * FROM jobs WHERE domain=? AND status='done' AND finished_at>=? "
        "ORDER BY finished_at DESC LIMIT 1",
        (domain, cutoff),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    return d


def mark_stale_pending_failed(older_than_s: int) -> None:
    """Beim Start: hängengebliebene pending-Jobs (Crash) als failed markieren."""
    cutoff = time.time() - older_than_s
    _c().execute(
        "UPDATE jobs SET status='failed', grund='abgebrochen (Neustart)', finished_at=? "
        "WHERE status='pending' AND created_at<?",
        (time.time(), cutoff),
    )
    _c().commit()


def log_request(ip: str) -> None:
    _c().execute("INSERT INTO req_log (ip, ts) VALUES (?,?)", (ip, time.time()))
    _c().commit()


def count_requests(ip: str, within_s: int) -> int:
    cutoff = time.time() - within_s
    row = _c().execute(
        "SELECT COUNT(*) AS n FROM req_log WHERE ip=? AND ts>=?", (ip, cutoff)
    ).fetchone()
    return int(row["n"]) if row else 0


def cleanup_req_log(older_than_s: int) -> None:
    _c().execute("DELETE FROM req_log WHERE ts<?", (time.time() - older_than_s,))
    _c().commit()
