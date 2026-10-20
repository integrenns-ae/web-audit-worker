"""Tests fuer app.py - der Vertrag, den der Website-Trichter braucht.

Jeder Test hier beschreibt eine Zusage an den Strato-Proxy bzw. an
/website-check. Wird einer rot, merkt es sonst zuerst ein echter Besucher.
"""
from __future__ import annotations

import time

from conftest import AUTH, TEST_SECRET


# --- /health ----------------------------------------------------------------

def test_health_braucht_kein_token(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# --- Authentifizierung ------------------------------------------------------

def test_enqueue_ohne_token_ist_401(client):
    r = client.post("/audit", json={"url": "example.de"})
    assert r.status_code == 401


def test_enqueue_mit_falschem_token_ist_401(client):
    r = client.post("/audit", json={"url": "example.de"},
                    headers={"X-Shared-Secret": "falsch"})
    assert r.status_code == 401


def test_status_ohne_token_ist_401(client):
    r = client.get("/audit/irgendwas")
    assert r.status_code == 401


def test_ohne_konfiguriertes_secret_ist_503(client, monkeypatch):
    """Fail-closed: Ein Worker ohne gesetztes Secret darf nicht offen sein."""
    import app as app_mod

    for leer in ("", "changeme", "CHANGE_ME"):
        monkeypatch.setattr(app_mod, "SHARED_SECRET", leer)
        r = client.post("/audit", json={"url": "example.de"}, headers=AUTH)
        assert r.status_code == 503, f"SHARED_SECRET={leer!r} war nicht fail-closed"


# --- Eingabepruefung --------------------------------------------------------

def test_url_ohne_punkt_ist_400(client):
    r = client.post("/audit", json={"url": "localhost"}, headers=AUTH)
    assert r.status_code == 400


def test_zu_lange_url_wird_abgewiesen(client):
    r = client.post("/audit", json={"url": "https://x.de/" + "a" * 400}, headers=AUTH)
    assert r.status_code == 422


def test_fehlendes_feld_wird_abgewiesen(client):
    r = client.post("/audit", json={}, headers=AUTH)
    assert r.status_code == 422


# --- Annahme ----------------------------------------------------------------

def test_enqueue_antwortet_202_mit_job_id(client):
    r = client.post("/audit", json={"url": "example.de"}, headers=AUTH)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    assert body["job_id"]


def test_job_id_hat_genug_entropie(client):
    """Job-IDs sind der einzige Schutz davor, fremde Ergebnisse zu erraten.
    Vier Byte waren zu wenig - hier wird die Untergrenze festgenagelt."""
    r = client.post("/audit", json={"url": "example.de"}, headers=AUTH)
    assert len(r.json()["job_id"]) >= 32


def test_job_ids_wiederholen_sich_nicht(client):
    ids = {client.post("/audit", json={"url": f"seite{i}.de"},
                       headers=AUTH).json()["job_id"] for i in range(5)}
    assert len(ids) == 5


def test_angenommener_job_steht_in_der_datenbank(client, db):
    job_id = client.post("/audit", json={"url": "https://example.de/impressum"},
                         headers=AUTH).json()["job_id"]
    job = db.get_job(job_id)
    assert job["status"] == "pending"
    assert job["domain"] == "example.de"


def test_www_wird_fuer_die_domain_abgeschnitten(client, db):
    job_id = client.post("/audit", json={"url": "https://www.example.de"},
                         headers=AUTH).json()["job_id"]
    assert db.get_job(job_id)["domain"] == "example.de"


# --- Rate-Limit -------------------------------------------------------------

def test_stundenlimit_greift_beim_sechsten_aufruf(client):
    for i in range(5):
        r = client.post("/audit", json={"url": f"seite{i}.de"}, headers=AUTH)
        assert r.status_code == 202, f"Aufruf {i + 1} wurde unerwartet abgewiesen"
    r = client.post("/audit", json={"url": "seite5.de"}, headers=AUTH)
    assert r.status_code == 429


def test_tageslimit_greift_auch_ohne_stundenlimit(client, db):
    # 15 Anfragen vor zwei Stunden: ausserhalb des Stundenfensters,
    # innerhalb des Tagesfensters.
    vorher = time.time() - 7200
    for _ in range(15):
        db._c().execute("INSERT INTO req_log (ip, ts) VALUES (?,?)", ("testclient", vorher))
    db._c().commit()
    r = client.post("/audit", json={"url": "example.de"}, headers=AUTH)
    assert r.status_code == 429


def test_limit_zaehlt_pro_absender(client):
    """Der Proxy meldet die echte Besucher-IP per X-Client-IP. Ohne diese
    Trennung wuerde ein einziger Besucher den Trichter fuer alle sperren."""
    for i in range(5):
        r = client.post("/audit", json={"url": f"seite{i}.de"},
                        headers={**AUTH, "X-Client-IP": "10.0.0.1"})
        assert r.status_code == 202
    r = client.post("/audit", json={"url": "noch.de"},
                    headers={**AUTH, "X-Client-IP": "10.0.0.1"})
    assert r.status_code == 429

    r = client.post("/audit", json={"url": "noch.de"},
                    headers={**AUTH, "X-Client-IP": "10.0.0.2"})
    assert r.status_code == 202


# --- Domain-Cooldown --------------------------------------------------------

def test_frisches_ergebnis_wird_wiederverwendet(client, db):
    db.create_job("fertig", "https://example.de/", "example.de")
    db.finish_job("fertig", "done", result={"overall": 61})

    r = client.post("/audit", json={"url": "example.de"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is True
    assert body["job_id"] == "fertig"
    assert body["status"] == "done"


def test_cooldown_verbraucht_kein_limit(client, db):
    """Sonst koennte ein Besucher sein Tageslimit mit Treffern aus dem Cache
    aufbrauchen, ohne dass ein einziges Audit gelaufen ist."""
    db.create_job("fertig", "https://example.de/", "example.de")
    db.finish_job("fertig", "done", result={"overall": 61})

    for _ in range(8):
        r = client.post("/audit", json={"url": "example.de"}, headers=AUTH)
        assert r.status_code == 200
    assert db.count_requests("testclient", 3600) == 0


# --- Statusabfrage ----------------------------------------------------------

def test_unbekannter_job_ist_404(client):
    r = client.get("/audit/gibtsnicht", headers=AUTH)
    assert r.status_code == 404


def test_pending_wird_ohne_ergebnis_gemeldet(client):
    job_id = client.post("/audit", json={"url": "example.de"},
                         headers=AUTH).json()["job_id"]
    r = client.get(f"/audit/{job_id}", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}


def test_gescheiterter_job_liefert_grund_ohne_interna(client, db):
    db.create_job("kaputt", "https://example.de/", "example.de")
    db.finish_job("kaputt", "failed", grund="interner Fehler")
    body = client.get("/audit/kaputt", headers=AUTH).json()
    assert body == {"status": "failed", "grund": "interner Fehler"}


def test_robots_disallow_kommt_als_grund_durch(client, db):
    """Das Frontend erkennt genau diesen Wert und zeigt die hoefliche
    Erklaerung statt einer Fehlermeldung."""
    db.create_job("robots", "https://example.de/", "example.de")
    db.finish_job("robots", "nicht_auditierbar", grund="robots_disallow")
    body = client.get("/audit/robots", headers=AUTH).json()
    assert body == {"status": "nicht_auditierbar", "grund": "robots_disallow"}


# --- Vollstaendiger Durchlauf ----------------------------------------------

def test_durchlauf_von_pending_bis_done(client_mit_audit):
    from conftest import FAKE_RESULT, warte_auf_ende

    job_id = client_mit_audit.post("/audit", json={"url": "example.de"},
                                   headers=AUTH).json()["job_id"]
    body = warte_auf_ende(client_mit_audit, job_id)
    assert body["status"] == "done"
    assert body["result"]["overall"] == FAKE_RESULT["overall"]


def test_nicht_auditierbar_wird_durchgereicht(client_mit_audit, monkeypatch):
    from conftest import warte_auf_ende

    import app as app_mod
    from audit import NotAuditableError

    async def _kaputt(url: str) -> dict:
        raise NotAuditableError("robots_disallow")

    monkeypatch.setattr(app_mod, "run_audit", _kaputt)
    job_id = client_mit_audit.post("/audit", json={"url": "example.de"},
                                   headers=AUTH).json()["job_id"]
    body = warte_auf_ende(client_mit_audit, job_id)
    assert body == {"status": "nicht_auditierbar", "grund": "robots_disallow"}


def test_unerwarteter_fehler_verraet_keine_interna(client_mit_audit, monkeypatch):
    """Ein Stacktrace darf nie beim Besucher landen."""
    from conftest import warte_auf_ende

    import app as app_mod

    async def _explodiert(url: str) -> dict:
        raise RuntimeError("Passwort=geheim in /opt/ruleset/secret.yaml")

    monkeypatch.setattr(app_mod, "run_audit", _explodiert)
    job_id = client_mit_audit.post("/audit", json={"url": "example.de"},
                                   headers=AUTH).json()["job_id"]
    body = warte_auf_ende(client_mit_audit, job_id)
    assert body == {"status": "failed", "grund": "interner Fehler"}


# --- Crash-Recovery beim Start ---------------------------------------------

def test_start_raeumt_haengende_jobs_auf(db, monkeypatch):
    """Nach einem Neustart ist JEDER pending-Job verwaist - die Task ist mit
    dem Prozess gestorben. Bleibt er pending, pollt der Trichter endlos."""
    from fastapi.testclient import TestClient

    import app as app_mod

    db.create_job("verwaist", "https://example.de/", "example.de")
    assert db.get_job("verwaist")["status"] == "pending"

    with TestClient(app_mod.app) as c:
        body = c.get("/audit/verwaist", headers=AUTH).json()
    assert body["status"] == "failed"
