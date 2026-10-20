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
    for leer in ("", "changeme", "CHANGE_ME"):
        monkeypatch.setenv("AUDIT_SHARED_SECRET", leer)
        r = client.post("/audit", json={"url": "example.de"}, headers=AUTH)
        assert r.status_code == 503, f"AUDIT_SHARED_SECRET={leer!r} war nicht fail-closed"


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

    async def _kaputt(url: str, **_egal) -> dict:
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

    async def _explodiert(url: str, **_egal) -> dict:
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


# ===========================================================================
# Mandanten (Stufe 1)
# ===========================================================================

from conftest import AGENT_AUTH, AGENT_EINTRAG  # noqa: E402


def test_mandant_bekommt_eigene_limits(client, db, mandanten_datei):
    """Der Agent darf 20 pro Stunde, der Trichter 5. Beide zaehlen getrennt.

    Die Auftraege werden unterwegs abgeschlossen - sonst greift vorher die
    Warteschlangen-Grenze (max_queued), und der Test wuerde das falsche
    Limit messen.
    """
    mandanten_datei([AGENT_EINTRAG])
    for i in range(20):
        r = client.post("/audit", json={"url": f"agent{i}.de"}, headers=AGENT_AUTH)
        assert r.status_code == 202, f"Agent-Aufruf {i + 1} abgewiesen"
        db.finish_job(r.json()["job_id"], "done", result={"overall": 50})
    assert client.post("/audit", json={"url": "zuviel.de"},
                       headers=AGENT_AUTH).status_code == 429

    # Der Trichter ist davon voellig unberuehrt.
    assert client.post("/audit", json={"url": "besucher.de"},
                       headers=AUTH).status_code == 202


def test_mandant_kann_sein_limit_nicht_per_header_zuruecksetzen(client, db, mandanten_datei):
    """Die eigentliche Haertung: Vorher setzte jeder Geheimnis-Inhaber per
    X-Client-IP eine frei erfundene IP - und damit sein Limit beliebig oft
    zurueck. Das Limit zaehlt jetzt gegen den Mandanten."""
    mandanten_datei([AGENT_EINTRAG])
    for i in range(20):
        r = client.post("/audit", json={"url": f"a{i}.de"},
                        headers={**AGENT_AUTH, "X-Client-IP": f"10.1.1.{i}"})
        assert r.status_code == 202
        db.finish_job(r.json()["job_id"], "done", result={"overall": 50})
    r = client.post("/audit", json={"url": "nochmal.de"},
                    headers={**AGENT_AUTH, "X-Client-IP": "10.9.9.9"})
    assert r.status_code == 429, "Mandant hat sein Limit per Header umgangen"


def test_mandant_wird_nicht_unter_seiner_ip_gespeichert(client, db, mandanten_datei):
    """Datensparsamkeit: Fuer den Agenten wird gar keine IP gespeichert -
    sie waere immer die des Vilikon-VPS und damit nutzlos."""
    mandanten_datei([AGENT_EINTRAG])
    client.post("/audit", json={"url": "example.de"},
                headers={**AGENT_AUTH, "X-Client-IP": "203.0.113.7"})
    zeilen = [r["ip"] for r in db._c().execute("SELECT ip FROM req_log")]
    assert zeilen == ["client:vilikon"]
    assert "203.0.113.7" not in zeilen


def test_trichter_darf_die_besucher_ip_weiter_melden(client, db, mandanten_datei):
    """Gegenstueck zur Haertung: Der Strato-Proxy MUSS das koennen, sonst
    sperrt ein einziger Besucher den Check fuer alle."""
    mandanten_datei([AGENT_EINTRAG])
    client.post("/audit", json={"url": "example.de"},
                headers={**AUTH, "X-Client-IP": "203.0.113.7"})
    assert db.count_requests("203.0.113.7", 3600) == 1


def test_auftrag_wird_dem_mandanten_zugeordnet(client, db, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    job_id = client.post("/audit", json={"url": "example.de"},
                         headers=AGENT_AUTH).json()["job_id"]
    assert db.get_job(job_id)["client_id"] == "vilikon"


# --- Eigentumspruefung ------------------------------------------------------

def test_mandant_sieht_fremde_auftraege_nicht(client, mandanten_datei):
    """Vorher konnte jeder Geheimnis-Inhaber jeden Job lesen."""
    mandanten_datei([AGENT_EINTRAG])
    fremd = client.post("/audit", json={"url": "besucher.de"},
                        headers=AUTH).json()["job_id"]
    r = client.get(f"/audit/{fremd}", headers=AGENT_AUTH)
    assert r.status_code == 404


def test_trichter_sieht_auftraege_des_mandanten_nicht(client, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    fremd = client.post("/audit", json={"url": "agent.de"},
                        headers=AGENT_AUTH).json()["job_id"]
    assert client.get(f"/audit/{fremd}", headers=AUTH).status_code == 404


def test_fremder_auftrag_gibt_404_und_nicht_403(client, mandanten_datei):
    """404 ist hier kein Schlamperei-Fehlercode, sondern Absicht: Ein 403
    wuerde bestaetigen, dass es diese job_id gibt."""
    mandanten_datei([AGENT_EINTRAG])
    fremd = client.post("/audit", json={"url": "besucher.de"},
                        headers=AUTH).json()["job_id"]
    echt = client.get(f"/audit/{fremd}", headers=AGENT_AUTH)
    erfunden = client.get("/audit/" + "0" * 32, headers=AGENT_AUTH)
    assert echt.status_code == erfunden.status_code == 404
    assert echt.json() == erfunden.json()


def test_eigener_auftrag_bleibt_lesbar(client, db, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    job_id = client.post("/audit", json={"url": "agent.de"},
                         headers=AGENT_AUTH).json()["job_id"]
    db.finish_job(job_id, "done", result={"overall": 55})
    body = client.get(f"/audit/{job_id}", headers=AGENT_AUTH).json()
    assert body["result"]["overall"] == 55


def test_altauftrag_ohne_mandant_gehoert_dem_trichter(client, db):
    """Beim Deploy der Mandantenfaehigkeit laufende Auftraege duerfen nicht
    unlesbar werden. Die Migration fuellt client_id nach - dieser Test deckt
    den Fall ab, dass eine Zeile trotzdem NULL hat."""
    db.create_job("alt", "https://example.de/", "example.de")
    db._c().execute("UPDATE jobs SET client_id=NULL WHERE id='alt'")
    db._c().commit()
    db.finish_job("alt", "done", result={"overall": 42})
    body = client.get("/audit/alt", headers=AUTH).json()
    assert body["result"]["overall"] == 42


# --- Geteilter Domain-Cooldown ---------------------------------------------

def test_mandant_erbt_frisches_ergebnis_des_trichters(client, db, mandanten_datei):
    """Hoeflichkeit gegenueber der fremden Seite: Hat der Trichter sie gerade
    geprueft, wird nicht ein zweites Mal gecrawlt."""
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("vomtrichter", "https://example.de/", "example.de",
                  client_id="trichter")
    db.finish_job("vomtrichter", "done", result={"overall": 61})

    r = client.post("/audit", json={"url": "example.de"}, headers=AGENT_AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is True
    # Eigene job_id, nicht die fremde - sonst griffe die Eigentumspruefung.
    assert body["job_id"] != "vomtrichter"

    gelesen = client.get(f"/audit/{body['job_id']}", headers=AGENT_AUTH)
    assert gelesen.status_code == 200
    assert gelesen.json()["result"]["overall"] == 61


def test_geerbtes_ergebnis_behaelt_seinen_zeitstempel(client, db, mandanten_datei):
    """Sonst waere ein wiederverwendetes Ergebnis als frisch gemeldet - und
    geprueft_am im kompakten Format eine Luege."""
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("vomtrichter", "https://example.de/", "example.de",
                  client_id="trichter")
    db.finish_job("vomtrichter", "done", result={"overall": 61})
    original = db.get_job("vomtrichter")["finished_at"]

    neue = client.post("/audit", json={"url": "example.de"},
                       headers=AGENT_AUTH).json()["job_id"]
    assert db.get_job(neue)["finished_at"] == original


def test_eigenes_frisches_ergebnis_wird_nicht_kopiert(client, db, mandanten_datei):
    """Beim eigenen Treffer bleibt die alte job_id - sonst wuechse die
    Tabelle bei jedem Cache-Treffer um eine Zeile."""
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("eigen", "https://example.de/", "example.de", client_id="vilikon")
    db.finish_job("eigen", "done", result={"overall": 61})

    for _ in range(3):
        body = client.post("/audit", json={"url": "example.de"},
                           headers=AGENT_AUTH).json()
        assert body["job_id"] == "eigen"
    assert db._c().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


def test_laengerer_cooldown_des_mandanten_wird_benutzt(client, db, mandanten_datei):
    """Der Agent hat 12 Stunden Cooldown, der Trichter 10 Minuten. Ein zwei
    Stunden altes Ergebnis ist fuer den Agenten frisch, fuer den Besucher nicht."""
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("alt", "https://example.de/", "example.de", client_id="trichter")
    db.finish_job("alt", "done", result={"overall": 61})
    db._c().execute("UPDATE jobs SET finished_at=? WHERE id='alt'", (time.time() - 7200,))
    db._c().commit()

    assert client.post("/audit", json={"url": "example.de"},
                       headers=AGENT_AUTH).json().get("cached") is True
    assert client.post("/audit", json={"url": "example.de"},
                       headers=AUTH).status_code == 202


def test_geerbtes_ergebnis_verbraucht_kein_limit(client, db, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("vomtrichter", "https://example.de/", "example.de",
                  client_id="trichter")
    db.finish_job("vomtrichter", "done", result={"overall": 61})
    for _ in range(5):
        assert client.post("/audit", json={"url": "example.de"},
                           headers=AGENT_AUTH).status_code == 200
    assert db.count_requests("client:vilikon", 3600) == 0


# ===========================================================================
# Antwortformat, Long-Poll, Fairness (Stufe 3)
# ===========================================================================

import asyncio  # noqa: E402

import kompakt  # noqa: E402


def test_kompaktes_format_statt_vollem_ergebnis(client, db, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("fertig", "https://example.de/", "example.de", client_id="vilikon")
    db.finish_job("fertig", "done", result={
        "domain": "example.de", "overall": 42,
        "category_scores": {"recht": 0, "technik_performance": 20},
        "findings": [{"category": "recht", "score_impact": -20,
                      "message_customer": "Kein Impressum.",
                      "message_internal": "GEHEIMER-INTERNER-TEXT"}],
    })

    body = client.get("/audit/fertig?format=kompakt", headers=AGENT_AUTH).json()
    assert body["overall"] == 42
    assert body["kategorien"]["recht"] == [0, 20]
    assert body["schwaechste_kategorie"] == "recht"
    assert body["top_findings"][0]["text"] == "Kein Impressum."
    assert body["geprueft_am"].endswith("Z")
    assert "result" not in body


def test_kompaktes_format_verraet_keine_interna(client, db, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("fertig", "https://example.de/", "example.de", client_id="vilikon")
    db.finish_job("fertig", "done", result={
        "findings": [{"category": "recht", "score_impact": -20,
                      "message_customer": "Kundentext.",
                      "message_internal": "GEHEIMER-INTERNER-TEXT"}],
    })
    roh = client.get("/audit/fertig?format=kompakt", headers=AGENT_AUTH).text
    assert "GEHEIMER-INTERNER-TEXT" not in roh


def test_volles_format_bleibt_der_standard(client, db):
    """Der Trichter darf sich nicht veraendern - er liest result[]."""
    db.create_job("fertig", "https://example.de/", "example.de")
    db.finish_job("fertig", "done", result={"overall": 42, "findings": []})
    body = client.get("/audit/fertig", headers=AUTH).json()
    assert body == {"status": "done", "result": {"overall": 42, "findings": []}}


def test_unbekanntes_format_wird_abgewiesen(client, db):
    db.create_job("fertig", "https://example.de/", "example.de")
    db.finish_job("fertig", "done", result={"overall": 1})
    assert client.get("/audit/fertig?format=xml", headers=AUTH).status_code == 422


def test_kompaktes_format_meldet_wiederverwendung(client, db, mandanten_datei):
    """cached wird nicht gespeichert, sondern abgeleitet: Ein geerbtes
    Ergebnis wurde spaeter angelegt als es fertig wurde."""
    mandanten_datei([AGENT_EINTRAG])
    db.create_job("vomtrichter", "https://example.de/", "example.de",
                  client_id="trichter")
    db.finish_job("vomtrichter", "done", result={"overall": 61})

    geerbt = client.post("/audit", json={"url": "example.de"},
                         headers=AGENT_AUTH).json()["job_id"]
    body = client.get(f"/audit/{geerbt}?format=kompakt", headers=AGENT_AUTH).json()
    assert body["cached"] is True

    eigen = client.post("/audit", json={"url": "frisch.de"},
                        headers=AGENT_AUTH).json()["job_id"]
    db.finish_job(eigen, "done", result={"overall": 70})
    assert client.get(f"/audit/{eigen}?format=kompakt",
                      headers=AGENT_AUTH).json()["cached"] is False


# --- Long-Poll --------------------------------------------------------------

def test_warten_liefert_das_fertige_ergebnis(client_mit_audit, monkeypatch):
    """Ohne Long-Poll muesste der Agent pollen - jeder Poll ist bei ihm ein
    Werkzeug-Aufruf und kostet Tokens."""
    import app as app_mod

    async def _langsam(url: str, **_egal) -> dict:
        await asyncio.sleep(0.4)
        return {"overall": 64, "category_scores": {}, "findings": []}

    monkeypatch.setattr(app_mod, "run_audit", _langsam)
    job_id = client_mit_audit.post("/audit", json={"url": "example.de"},
                                   headers=AUTH).json()["job_id"]

    r = client_mit_audit.get(f"/audit/{job_id}?warten=5", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert r.json()["result"]["overall"] == 64


def test_warten_gibt_nach_ablauf_pending_zurueck(client):
    job_id = client.post("/audit", json={"url": "example.de"},
                         headers=AUTH).json()["job_id"]
    begonnen = time.monotonic()
    r = client.get(f"/audit/{job_id}?warten=1", headers=AUTH)
    gedauert = time.monotonic() - begonnen
    assert r.json() == {"status": "pending"}
    assert 0.8 < gedauert < 3.0, f"Wartezeit war {gedauert:.2f}s"


def test_warten_haelt_fremde_auftraege_nicht_hin(client, mandanten_datei):
    """Die Eigentumspruefung laeuft VOR dem Warten - sonst koennte ein
    Mandant mit erfundenen IDs Verbindungen blockieren."""
    mandanten_datei([AGENT_EINTRAG])
    fremd = client.post("/audit", json={"url": "besucher.de"},
                        headers=AUTH).json()["job_id"]
    begonnen = time.monotonic()
    r = client.get(f"/audit/{fremd}?warten=60", headers=AGENT_AUTH)
    gedauert = time.monotonic() - begonnen
    assert r.status_code == 404
    assert gedauert < 2.0, f"404 kam erst nach {gedauert:.2f}s"


def test_warten_ausserhalb_des_bereichs_wird_abgewiesen(client):
    job_id = client.post("/audit", json={"url": "example.de"},
                         headers=AUTH).json()["job_id"]
    for ungueltig in ("61", "-1", "viel"):
        r = client.get(f"/audit/{job_id}?warten={ungueltig}", headers=AUTH)
        assert r.status_code == 422, f"warten={ungueltig} wurde akzeptiert"


def test_ohne_warten_kommt_pending_sofort(client):
    job_id = client.post("/audit", json={"url": "example.de"},
                         headers=AUTH).json()["job_id"]
    begonnen = time.monotonic()
    assert client.get(f"/audit/{job_id}", headers=AUTH).json() == {"status": "pending"}
    assert time.monotonic() - begonnen < 1.0


# --- Warteschlangen-Grenze --------------------------------------------------

def test_mandant_darf_nicht_unbegrenzt_auftuermen(client, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])          # max_queued = 2
    for i in range(2):
        assert client.post("/audit", json={"url": f"a{i}.de"},
                           headers=AGENT_AUTH).status_code == 202
    r = client.post("/audit", json={"url": "a3.de"}, headers=AGENT_AUTH)
    assert r.status_code == 429
    assert "Auftraege" in r.json()["detail"]


def test_volle_warteschlange_verbraucht_kein_limit(client, db, mandanten_datei):
    """Sonst wuerde ein Agent, der seine Ergebnisse nicht abholt, zusaetzlich
    sein Tageslimit verlieren - zwei Strafen fuer einen Fehler."""
    mandanten_datei([AGENT_EINTRAG])
    for i in range(2):
        client.post("/audit", json={"url": f"a{i}.de"}, headers=AGENT_AUTH)
    vorher = db.count_requests("client:vilikon", 3600)
    for _ in range(5):
        assert client.post("/audit", json={"url": "a9.de"},
                           headers=AGENT_AUTH).status_code == 429
    assert db.count_requests("client:vilikon", 3600) == vorher


def test_abgeholte_ergebnisse_machen_die_warteschlange_wieder_frei(client, db, mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    erste = client.post("/audit", json={"url": "a0.de"}, headers=AGENT_AUTH).json()["job_id"]
    client.post("/audit", json={"url": "a1.de"}, headers=AGENT_AUTH)
    assert client.post("/audit", json={"url": "a2.de"},
                       headers=AGENT_AUTH).status_code == 429
    db.finish_job(erste, "done", result={"overall": 1})
    assert client.post("/audit", json={"url": "a2.de"},
                       headers=AGENT_AUTH).status_code == 202


def test_trichter_hat_keine_warteschlangen_grenze(client):
    """Viele echte Besucher gleichzeitig sind normal, nicht verdaechtig."""
    for i in range(5):
        assert client.post("/audit", json={"url": f"b{i}.de"},
                           headers=AUTH).status_code == 202


# --- Reservierter Nebenlaeufigkeits-Slot -----------------------------------

def test_hintergrund_hat_einen_slot_weniger():
    import app as app_mod

    assert app_mod.SEM_HINTERGRUND._value == app_mod.MAX_CONCURRENT - 1
    assert app_mod.SEM_HINTERGRUND._value >= 1


def test_hintergrundlaeufe_lassen_einen_slot_frei(db, monkeypatch):
    """Ein echter Besucher soll nie darauf warten, dass der Agent gerade
    zwanzig Seiten durchprueft."""
    import app as app_mod

    gleichzeitig = 0
    hoechststand = 0

    async def _zaehlend(url: str, **_egal) -> dict:
        nonlocal gleichzeitig, hoechststand
        gleichzeitig += 1
        hoechststand = max(hoechststand, gleichzeitig)
        await asyncio.sleep(0.05)
        gleichzeitig -= 1
        return {"overall": 1}

    monkeypatch.setattr(app_mod, "run_audit", _zaehlend)

    async def lauf(hintergrund: bool) -> int:
        nonlocal gleichzeitig, hoechststand
        gleichzeitig = hoechststand = 0
        # Frische Semaphoren fuer diese Ereignisschleife.
        monkeypatch.setattr(app_mod, "SEM", asyncio.Semaphore(3))
        monkeypatch.setattr(app_mod, "SEM_HINTERGRUND", asyncio.Semaphore(2))
        await asyncio.gather(*[
            app_mod._run_job(f"j{i}", "example.de", hintergrund=hintergrund)
            for i in range(6)
        ])
        return hoechststand

    assert asyncio.run(lauf(hintergrund=True)) == 2, "Agent belegte alle Slots"
    assert asyncio.run(lauf(hintergrund=False)) == 3, "Trichter bekam nicht alle Slots"
