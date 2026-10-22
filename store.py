"""store — SQLite-Persistenz für Jobs + Rate-Limit-Log.

Synchron (sqlite3) und über asyncio.to_thread aufgerufen. Für Trichter-Volumen
mehr als ausreichend; überlebt Neustarts.

Zwei Dinge sind hier nicht offensichtlich:

1. **Eine globale Verbindung, geteilt über Threads.** `check_same_thread=False`
   erlaubt das, serialisiert aber nichts. Deshalb liegt ein Lock um jede
   oeffentliche Funktion. Bei diesem Volumen ist das billig und nimmt die
   Frage, ob WAL allein genuegt, aus dem Betrieb heraus.
2. **`req_log.ip` ist nicht immer eine IP.** Fuer Mandanten mit
   `limit_scope="client"` steht dort `client:<id>` - die IP waere immer die
   des Vilikon-VPS und damit nutzlos. Es wird also bewusst *keine* IP
   gespeichert, wo keine gebraucht wird. Anfragen zum Check zaehlen getrennt
   als `anfrage:<IP>` (gleiche 24-Stunden-Frist).
"""
from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
import time
from typing import Optional

from clients import LEGACY_CLIENT_ID

DB_PATH = os.environ.get("AUDIT_DB_PATH", "/data/audit.db")

_conn: Optional[sqlite3.Connection] = None
_LOCK = threading.RLock()


def _synchronisiert(fn):
    """Serialisiert den Zugriff auf die geteilte SQLite-Verbindung."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _migrate(conn: sqlite3.Connection) -> None:
    """Schema nachziehen, ohne Daten zu verlieren.

    `client_id` kam mit der Mandantenfaehigkeit dazu. Bestehende Zeilen
    gehoeren dem Trichter - ohne diese Nachfuellung wuerde die
    Eigentumspruefung laufende Auftraege beim Deploy unlesbar machen.
    """
    spalten = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "client_id" not in spalten:
        conn.execute("ALTER TABLE jobs ADD COLUMN client_id TEXT")
        conn.execute("UPDATE jobs SET client_id=? WHERE client_id IS NULL", (LEGACY_CLIENT_ID,))
        conn.commit()


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        verzeichnis = os.path.dirname(DB_PATH)
        if verzeichnis:
            os.makedirs(verzeichnis, exist_ok=True)
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
                finished_at REAL,
                client_id   TEXT                    -- welcher Mandant hat beauftragt
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_domain ON jobs(domain, finished_at);
            CREATE TABLE IF NOT EXISTS req_log (
                ip   TEXT NOT NULL,                 -- IP, "client:<id>" oder "anfrage:<IP>", s. Modulkopf
                ts   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_req_ip ON req_log(ip, ts);
            """
        )
        _conn.commit()
        _migrate(_conn)
    return _conn


@_synchronisiert
def create_job(job_id: str, url: str, domain: str, client_id: str = LEGACY_CLIENT_ID) -> None:
    _c().execute(
        "INSERT INTO jobs (id, url, domain, status, created_at, client_id) VALUES (?,?,?,?,?,?)",
        (job_id, url, domain, "pending", time.time(), client_id),
    )
    _c().commit()


@_synchronisiert
def finish_job(job_id: str, status: str, result: dict | None = None, grund: str | None = None) -> None:
    _c().execute(
        "UPDATE jobs SET status=?, result=?, grund=?, finished_at=? WHERE id=?",
        (status, json.dumps(result) if result is not None else None, grund, time.time(), job_id),
    )
    _c().commit()


def _zeile_zu_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    return d


@_synchronisiert
def get_job(job_id: str) -> dict | None:
    return _zeile_zu_dict(_c().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


@_synchronisiert
def recent_done_for_domain(domain: str, within_s: int) -> dict | None:
    """Für Domain-Cooldown: letzten fertigen Job innerhalb des Fensters liefern.

    Absichtlich **ueber alle Mandanten hinweg**: Hat der Trichter die Domain
    gerade geprueft, soll der Agent dieses Ergebnis benutzen statt die fremde
    Seite ein zweites Mal zu crawlen. Dass der Auftrag dann einem anderen
    Mandanten gehoert, loest `clone_job_for_client` auf.
    """
    cutoff = time.time() - within_s
    return _zeile_zu_dict(_c().execute(
        "SELECT * FROM jobs WHERE domain=? AND status='done' AND finished_at>=? "
        "ORDER BY finished_at DESC LIMIT 1",
        (domain, cutoff),
    ).fetchone())


@_synchronisiert
def clone_job_for_client(quelle: dict, neue_job_id: str, client_id: str) -> None:
    """Fertiges Ergebnis einem anderen Mandanten als eigenen Auftrag gutschreiben.

    `finished_at` wird uebernommen, nicht neu gesetzt: Sonst wuerde ein
    wiederverwendetes Ergebnis als frisch gemeldet - und `geprueft_am` im
    kompakten Format waere eine Luege.
    """
    _c().execute(
        "INSERT INTO jobs (id, url, domain, status, result, grund, created_at, finished_at, client_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            neue_job_id, quelle["url"], quelle["domain"], quelle["status"],
            json.dumps(quelle["result"]) if quelle.get("result") is not None else None,
            quelle.get("grund"), time.time(), quelle.get("finished_at"), client_id,
        ),
    )
    _c().commit()


@_synchronisiert
def mark_stale_pending_failed(older_than_s: int) -> None:
    """Beim Start: hängengebliebene pending-Jobs (Crash) als failed markieren."""
    cutoff = time.time() - older_than_s
    _c().execute(
        "UPDATE jobs SET status='failed', grund='abgebrochen (Neustart)', finished_at=? "
        "WHERE status='pending' AND created_at<?",
        (time.time(), cutoff),
    )
    _c().commit()


@_synchronisiert
def log_request(subject: str) -> None:
    """Zaehlt einen Aufruf. `subject` ist eine IP oder `client:<id>`."""
    _c().execute("INSERT INTO req_log (ip, ts) VALUES (?,?)", (subject, time.time()))
    _c().commit()


@_synchronisiert
def count_requests(subject: str, within_s: int) -> int:
    cutoff = time.time() - within_s
    row = _c().execute(
        "SELECT COUNT(*) AS n FROM req_log WHERE ip=? AND ts>=?", (subject, cutoff)
    ).fetchone()
    return int(row["n"]) if row else 0


@_synchronisiert
def count_pending_for_client(client_id: str) -> int:
    """Offene Auftraege eines Mandanten (fuer max_queued)."""
    row = _c().execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE client_id=? AND status='pending'",
        (client_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


@_synchronisiert
def cleanup_jobs(older_than_s: int) -> None:
    """Alte Auftraege samt URL und Ergebnis loeschen.

    Die Datenschutzerklaerung nennt zwoelf Monate; ohne diese Funktion blieben
    gepruefte Adressen unbegrenzt gespeichert.
    """
    cutoff = time.time() - older_than_s
    _c().execute(
        "DELETE FROM jobs WHERE COALESCE(finished_at, created_at) < ?", (cutoff,)
    )
    # Ohne commit() blieb das DELETE in einer offenen Transaktion haengen, bis
    # zufaellig eine andere Funktion committete - die zugesagte Loeschfrist war
    # damit faktisch nicht eingehalten.
    _c().commit()


@_synchronisiert
def cleanup_jobs_for_client(client_id: str, older_than_s: int) -> int:
    """Kuerzere Aufbewahrung fuer einen einzelnen Mandanten (retention_days).

    Der Agent prueft fremde Firmenseiten. Was nicht angeschrieben wird, soll
    nach Tagen verschwinden und nicht nach Monaten - wer nicht hortet, hat das
    Datenschutzproblem strukturell nicht.
    """
    cutoff = time.time() - older_than_s
    cur = _c().execute(
        "DELETE FROM jobs WHERE client_id=? AND COALESCE(finished_at, created_at) < ?",
        (client_id, cutoff),
    )
    _c().commit()
    return cur.rowcount or 0


@_synchronisiert
def cleanup_req_log(older_than_s: int) -> None:
    _c().execute("DELETE FROM req_log WHERE ts<?", (time.time() - older_than_s,))
    _c().commit()
