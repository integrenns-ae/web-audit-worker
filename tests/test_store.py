"""Tests fuer store.py - Persistenz, Rate-Limit-Zaehler, Loeschfristen."""
from __future__ import annotations

import sqlite3
import time


def test_job_runde_dreht_sich(db):
    db.create_job("abc", "https://example.de/", "example.de")
    job = db.get_job("abc")
    assert job["status"] == "pending"
    assert job["url"] == "https://example.de/"
    assert job["domain"] == "example.de"
    assert job["finished_at"] is None


def test_ergebnis_ueberlebt_als_json(db):
    db.create_job("abc", "https://example.de/", "example.de")
    db.finish_job("abc", "done", result={"overall": 42, "liste": [1, 2, 3]})
    job = db.get_job("abc")
    assert job["status"] == "done"
    # Muss wieder ein Dict sein, nicht ein JSON-String.
    assert job["result"]["overall"] == 42
    assert job["result"]["liste"] == [1, 2, 3]
    assert job["finished_at"] is not None


def test_grund_bei_nicht_auditierbar(db):
    db.create_job("abc", "https://example.de/", "example.de")
    db.finish_job("abc", "nicht_auditierbar", grund="robots_disallow")
    job = db.get_job("abc")
    assert job["status"] == "nicht_auditierbar"
    assert job["grund"] == "robots_disallow"
    assert job["result"] is None


def test_unbekannter_job_ist_none(db):
    assert db.get_job("gibtsnicht") is None


# --- Domain-Cooldown ---------------------------------------------------------

def test_cooldown_findet_frisches_ergebnis(db):
    db.create_job("neu", "https://example.de/", "example.de")
    db.finish_job("neu", "done", result={"overall": 50})
    treffer = db.recent_done_for_domain("example.de", 600)
    assert treffer is not None
    assert treffer["id"] == "neu"
    assert treffer["result"]["overall"] == 50


def test_cooldown_ignoriert_altes_ergebnis(db):
    db.create_job("alt", "https://example.de/", "example.de")
    db.finish_job("alt", "done", result={"overall": 50})
    # Kuenstlich altern lassen - zwei Stunden zurueck.
    db._c().execute("UPDATE jobs SET finished_at=? WHERE id='alt'", (time.time() - 7200,))
    db._c().commit()
    assert db.recent_done_for_domain("example.de", 600) is None


def test_cooldown_ignoriert_fehlgeschlagene_laeufe(db):
    """Ein Fehlversuch darf keinen Cooldown ausloesen - sonst bekaeme der
    Besucher 20 Minuten lang dieselbe Fehlermeldung statt eines neuen Versuchs."""
    db.create_job("kaputt", "https://example.de/", "example.de")
    db.finish_job("kaputt", "failed", grund="interner Fehler")
    assert db.recent_done_for_domain("example.de", 600) is None


def test_cooldown_trennt_domains(db):
    db.create_job("a", "https://example.de/", "example.de")
    db.finish_job("a", "done", result={"overall": 50})
    assert db.recent_done_for_domain("andere.de", 600) is None


# --- Crash-Recovery ---------------------------------------------------------

def test_haengende_jobs_werden_als_gescheitert_markiert(db):
    db.create_job("haengt", "https://example.de/", "example.de")
    db._c().execute("UPDATE jobs SET created_at=? WHERE id='haengt'", (time.time() - 3600,))
    db._c().commit()
    db.mark_stale_pending_failed(older_than_s=900)
    assert db.get_job("haengt")["status"] == "failed"


def test_frischer_pending_job_bleibt_bei_900s_unberuehrt(db):
    """Dokumentiert den Altfehler: Mit older_than_s=900 ueberlebt ein gerade
    erst erzeugter pending-Job die Crash-Recovery - und bleibt danach fuer
    immer pending, weil die zugehoerige Task mit dem Prozess gestorben ist.
    Deshalb ruft app.py die Funktion mit 0 auf."""
    db.create_job("frisch", "https://example.de/", "example.de")
    db.mark_stale_pending_failed(older_than_s=900)
    assert db.get_job("frisch")["status"] == "pending"

    db.mark_stale_pending_failed(older_than_s=0)
    assert db.get_job("frisch")["status"] == "failed"


def test_recovery_laesst_fertige_jobs_in_ruhe(db):
    db.create_job("fertig", "https://example.de/", "example.de")
    db.finish_job("fertig", "done", result={"overall": 90})
    db.mark_stale_pending_failed(older_than_s=0)
    job = db.get_job("fertig")
    assert job["status"] == "done"
    assert job["result"]["overall"] == 90


# --- Rate-Limit-Zaehler -----------------------------------------------------

def test_zaehler_zaehlt_nur_das_fenster(db):
    for _ in range(3):
        db.log_request("1.2.3.4")
    db._c().execute("INSERT INTO req_log (ip, ts) VALUES (?,?)", ("1.2.3.4", time.time() - 7200))
    db._c().commit()
    assert db.count_requests("1.2.3.4", 3600) == 3
    assert db.count_requests("1.2.3.4", 86400) == 4


def test_zaehler_trennt_adressen(db):
    db.log_request("1.2.3.4")
    db.log_request("1.2.3.4")
    db.log_request("5.6.7.8")
    assert db.count_requests("1.2.3.4", 3600) == 2
    assert db.count_requests("5.6.7.8", 3600) == 1
    assert db.count_requests("9.9.9.9", 3600) == 0


# --- Loeschfristen (muessen zur Datenschutzerklaerung passen) ---------------

def test_alte_adressen_werden_geloescht(db):
    db._c().execute("INSERT INTO req_log (ip, ts) VALUES (?,?)", ("1.2.3.4", time.time() - 90000))
    db._c().commit()
    db.log_request("5.6.7.8")
    db.cleanup_req_log(older_than_s=86400)
    assert db.count_requests("1.2.3.4", 86400 * 30) == 0
    assert db.count_requests("5.6.7.8", 3600) == 1


def test_alte_jobs_werden_geloescht(db):
    db.create_job("alt", "https://alt.de/", "alt.de")
    db.finish_job("alt", "done", result={"overall": 10})
    db._c().execute("UPDATE jobs SET finished_at=? WHERE id='alt'", (time.time() - 400 * 86400,))
    db._c().commit()
    db.create_job("neu", "https://neu.de/", "neu.de")

    db.cleanup_jobs(older_than_s=365 * 86400)
    assert db.get_job("alt") is None
    assert db.get_job("neu") is not None


def test_jobloeschung_wird_wirklich_geschrieben(db):
    """Altfehler: cleanup_jobs() hatte kein commit(). Das DELETE blieb in einer
    offenen Transaktion haengen, bis zufaellig eine andere Funktion committete -
    im Extremfall also nie. Eine zweite Verbindung auf dieselbe Datei beweist,
    ob die Loeschung dauerhaft ist."""
    db.create_job("alt", "https://alt.de/", "alt.de")
    db.finish_job("alt", "done", result={"overall": 10})
    db._c().execute("UPDATE jobs SET finished_at=? WHERE id='alt'", (time.time() - 400 * 86400,))
    db._c().commit()

    db.cleanup_jobs(older_than_s=365 * 86400)

    zweite = sqlite3.connect(db.DB_PATH)
    try:
        anzahl = zweite.execute("SELECT COUNT(*) FROM jobs WHERE id='alt'").fetchone()[0]
    finally:
        zweite.close()
    assert anzahl == 0, "cleanup_jobs() hat nicht committet"


def test_loeschung_vertraegt_leere_datenbank(db):
    db.cleanup_jobs(older_than_s=1)
    db.cleanup_req_log(older_than_s=1)
    db.mark_stale_pending_failed(older_than_s=0)


# ===========================================================================
# Migration auf die Mandantenfaehigkeit (Stufe 1)
# ===========================================================================

# Das Schema GENAU so, wie es vor der Mandantenfaehigkeit auf dem VPS lag.
ALTES_SCHEMA = """
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    domain      TEXT NOT NULL,
    status      TEXT NOT NULL,
    result      TEXT,
    grund       TEXT,
    created_at  REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX idx_jobs_domain ON jobs(domain, finished_at);
CREATE TABLE req_log (
    ip   TEXT NOT NULL,
    ts   REAL NOT NULL
);
CREATE INDEX idx_req_ip ON req_log(ip, ts);
"""


def _alte_datenbank_anlegen(pfad: str) -> None:
    """Datenbank im Vor-Mandanten-Schema mit echten Zeilen."""
    conn = sqlite3.connect(pfad)
    conn.executescript(ALTES_SCHEMA)
    jetzt = time.time()
    conn.execute(
        "INSERT INTO jobs (id, url, domain, status, result, created_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("fertig1", "https://alt.de/", "alt.de", "done",
         '{"overall": 88}', jetzt - 60, jetzt - 30),
    )
    conn.execute(
        "INSERT INTO jobs (id, url, domain, status, created_at) VALUES (?,?,?,?,?)",
        ("laeuft1", "https://neu.de/", "neu.de", "pending", jetzt),
    )
    conn.execute("INSERT INTO req_log (ip, ts) VALUES (?,?)", ("9.9.9.9", jetzt))
    conn.commit()
    conn.close()


def test_migration_erhaelt_bestehende_auftraege(db):
    """Beim Deploy darf nichts verloren gehen - auch nicht ein Auftrag, den
    ein Besucher in diesem Moment pollt."""
    _alte_datenbank_anlegen(db.DB_PATH)

    job = db.get_job("fertig1")
    assert job is not None
    assert job["status"] == "done"
    assert job["result"]["overall"] == 88
    assert job["url"] == "https://alt.de/"
    assert db.get_job("laeuft1")["status"] == "pending"
    assert db.count_requests("9.9.9.9", 3600) == 1


def test_migration_schreibt_alte_zeilen_dem_trichter_zu(db):
    """Ohne diese Nachfuellung wuerde die Eigentumspruefung laufende
    Auftraege nach dem Deploy mit 404 abweisen."""
    from clients import LEGACY_CLIENT_ID

    _alte_datenbank_anlegen(db.DB_PATH)
    assert db.get_job("fertig1")["client_id"] == LEGACY_CLIENT_ID
    assert db.get_job("laeuft1")["client_id"] == LEGACY_CLIENT_ID


def test_migration_laeuft_zweimal_ohne_schaden(db):
    """Jeder Containerstart oeffnet die Datenbank erneut."""
    _alte_datenbank_anlegen(db.DB_PATH)
    assert db.get_job("fertig1")["client_id"] is not None

    db._conn.close()
    db._conn = None
    assert db.get_job("fertig1")["result"]["overall"] == 88
    db.create_job("danach", "https://x.de/", "x.de", client_id="vilikon")
    assert db.get_job("danach")["client_id"] == "vilikon"


def test_cooldown_findet_auch_fremde_ergebnisse(db):
    """Der Cooldown ist absichtlich mandantenuebergreifend: Eine fremde Seite
    soll nicht zweimal gecrawlt werden."""
    db.create_job("vomtrichter", "https://example.de/", "example.de", client_id="trichter")
    db.finish_job("vomtrichter", "done", result={"overall": 50})
    treffer = db.recent_done_for_domain("example.de", 600)
    assert treffer["client_id"] == "trichter"


def test_klon_uebernimmt_ergebnis_und_zeitstempel(db):
    db.create_job("quelle", "https://example.de/", "example.de", client_id="trichter")
    db.finish_job("quelle", "done", result={"overall": 50})
    quelle = db.get_job("quelle")

    db.clone_job_for_client(quelle, "klon", "vilikon")
    klon = db.get_job("klon")
    assert klon["client_id"] == "vilikon"
    assert klon["status"] == "done"
    assert klon["result"]["overall"] == 50
    assert klon["finished_at"] == quelle["finished_at"]
    assert klon["domain"] == "example.de"
    # Die Quelle bleibt unberuehrt.
    assert db.get_job("quelle")["client_id"] == "trichter"


def test_kuerzere_aufbewahrung_trifft_nur_den_mandanten(db):
    """retention_days: Der Agent prueft fremde Firmenseiten. Was nicht
    angeschrieben wird, soll nach Tagen verschwinden, nicht nach Monaten -
    waehrend der Trichter seine zwoelf Monate behaelt."""
    alt = time.time() - 40 * 86400
    for jid, cid in (("agent_alt", "vilikon"), ("trichter_alt", "trichter")):
        db.create_job(jid, f"https://{jid}.de/", f"{jid}.de", client_id=cid)
        db.finish_job(jid, "done", result={"overall": 1})
        db._c().execute("UPDATE jobs SET finished_at=? WHERE id=?", (alt, jid))
    db.create_job("agent_neu", "https://neu.de/", "neu.de", client_id="vilikon")
    db._c().commit()

    weg = db.cleanup_jobs_for_client("vilikon", 30 * 86400)
    assert weg == 1
    assert db.get_job("agent_alt") is None
    assert db.get_job("agent_neu") is not None
    assert db.get_job("trichter_alt") is not None
