"""Tests fuer vilikon.py - Checks und Anfragen des Trichters als Lead im Cockpit.

Die Zusage an den Besucher steht ueber allem: Ob das Cockpit da ist, langsam
ist oder ablehnt, der Check laeuft genauso wie vorher. Die Zusage an Vilikon:
das Format aus ki-cockpit/docs/LEADS-API.md, ohne Besucherdaten, und der
Schluessel steht nur im Authorization-Header.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
import pytest

import vilikon
from conftest import AGENT_AUTH, AGENT_EINTRAG, AUTH, warte_auf_ende

URL = "https://cockpit.test"
KEY = "vlk_pytest-nur-ein-testschluessel-0815"
BESUCHER_IP = "203.0.113.7"

# Ein Ergebnis, wie das Regelwerk es liefert - mit allem, was NICHT nach
# draussen darf (interne Fassung, Regel-IDs, manuelle Pruefpunkte).
ERGEBNIS = {
    "domain": "praxis-sommer.de",
    "ruleset_version": "2026-07-27.16",
    "overall": 48,
    "category_scores": {
        "technik_performance": 11, "mobil_zugaenglichkeit": 15, "recht": 0,
        "auffindbarkeit_seo": 8, "lokale_sichtbarkeit": 7, "inhalt_aktualitaet": 7,
    },
    "business_locality": "local",
    "is_non_profit": False,
    "is_heilberuf": True,
    "page_count": {"estimate": 7, "source": "sitemap"},
    "findings": [
        {"id": "recht.no_impressum", "category": "recht", "score_impact": -12,
         "message_customer": "Ein Impressum ist nicht auffindbar.",
         "message_internal": "INTERN: tmg_5 fehlt, Footer-Link 404"},
        {"id": "technik.slow", "category": "technik_performance", "score_impact": -5,
         "message_customer": "Die Seite lädt merklich langsam.",
         "message_internal": "INTERN: LCP 6.1s"},
        {"id": "seo.meta", "category": "auffindbarkeit_seo", "score_impact": -4,
         "message_customer": "Die Kurzbeschreibung für Google fehlt.",
         "message_internal": "INTERN: no meta description"},
        {"id": "inhalt.alt", "category": "inhalt_aktualitaet", "score_impact": -2,
         "message_customer": "Die letzte Neuigkeit ist zwei Jahre alt.",
         "message_internal": "INTERN: newest date 2024"},
    ],
    "manual_review_needed": [{"id": "x", "message_internal": "INTERN: manuell"}],
}

BEWERTUNG = {
    "score": 48,
    "skala": 100,
    "hebel": "Recht 0/20",
    "zusammenfassung": "Technik & Performance 11/23 · Mobil & Zugänglichkeit 15/15 · "
                       "Recht 0/20 · Auffindbarkeit (SEO) 8/15 · Lokale Sichtbarkeit 7/12 · "
                       "Inhalt & Aktualität 7/15",
    "befunde": [
        {"bereich": "Recht", "text": "Ein Impressum ist nicht auffindbar."},
        {"bereich": "Technik & Performance", "text": "Die Seite lädt merklich langsam."},
        {"bereich": "Auffindbarkeit (SEO)", "text": "Die Kurzbeschreibung für Google fehlt."},
    ],
}

FORMULAR = {
    "typ": "umbau",
    "name": "Sabine Sommer",
    "email": "praxis@example.com",
    "telefon": "0641 7654321",
    "nachricht": "Unsere Seite ist alt – können Sie helfen?",
    "paket_wunsch": "schaufenster",
    "sprache": "de",
}


def _job(**anders) -> dict:
    job = {"id": "9f3c" * 8, "url": "praxis-sommer.de", "status": "done",
           "result": ERGEBNIS, "finished_at": 1792656000.0}   # 2026-10-22T08:00:00Z
    job.update(anders)
    return job


# --- Abbildung: Ergebnis -> Lead -------------------------------------------

def test_check_ohne_formular_wird_eine_spur():
    assert vilikon.info_lead(_job()) == {
        "standard": "vilikon.lead/1",
        "externe_id": "audit-" + "9f3c" * 8,
        "art": "info",
        "zeitpunkt": "2026-10-22T08:00:00Z",
        "kontakt": {"website": "https://praxis-sommer.de"},
        "bewertung": BEWERTUNG,
        "merkmale": {"heilberuf": True, "befunde_gesamt": 4, "geschaeftstyp": "local",
                     "regelwerk": "2026-07-27.16"},
    }


def test_anfrage_zum_check_wird_ein_todo_mit_derselben_bewertung():
    assert vilikon.anfrage_lead(_job(), FORMULAR) == {
        "standard": "vilikon.lead/1",
        "externe_id": "audit-" + "9f3c" * 8 + "-anfrage",
        "art": "anfrage",
        "person": "Sabine Sommer",
        "kontakt": {"email": "praxis@example.com", "telefon": ["0641 7654321"],
                    "website": "https://praxis-sommer.de"},
        "anliegen": {"betreff": "Website-Check: Umbau-Anfrage (mit Audit)",
                     "nachricht": "Unsere Seite ist alt – können Sie helfen?",
                     "kategorie": "umbau"},
        "bewertung": BEWERTUNG,
        "merkmale": {"heilberuf": True, "befunde_gesamt": 4, "geschaeftstyp": "local",
                     "regelwerk": "2026-07-27.16", "paket_wunsch": "schaufenster",
                     "sprache": "de"},
    }


def test_spur_und_anfrage_haben_verschiedene_externe_ids():
    """Das Cockpit verwirft eine zweite Lieferung mit derselben externe_id als
    „bekannt" - auch eine Anfrage. Mit derselben id wuerde der Besucher, der
    nach dem Check fragt, still verschluckt."""
    assert vilikon.info_lead(_job())["externe_id"] != \
        vilikon.anfrage_lead(_job(), FORMULAR)["externe_id"]


def test_interne_fassung_steht_in_keinem_lead():
    for lead in (vilikon.info_lead(_job()), vilikon.anfrage_lead(_job(), FORMULAR)):
        text = json.dumps(lead, ensure_ascii=False)
        assert "INTERN" not in text
        assert "recht.no_impressum" not in text


def test_hoechstens_drei_befunde_nach_hebel():
    befunde = vilikon.info_lead(_job())["bewertung"]["befunde"]
    assert len(befunde) == 3
    assert "zwei Jahre" not in json.dumps(befunde, ensure_ascii=False)


def test_makellose_seite_hat_keinen_hebel():
    voll = {"overall": 100, "category_scores": {"recht": 20, "technik_performance": 23},
            "findings": []}
    b = vilikon.info_lead(_job(result=voll))["bewertung"]
    assert b["hebel"] == ""
    assert b["befunde"] == []
    assert b["score"] == 100


def test_anfrage_ohne_fertigen_check_kommt_ohne_bewertung():
    """Ohne Ergebnis keine erfundene Bewertung - das Cockpit prueft dann selbst."""
    for job in (_job(status="pending", result=None),
                _job(status="nicht_auditierbar", result=None)):
        lead = vilikon.anfrage_lead(job, FORMULAR)
        assert "bewertung" not in lead
        assert lead["merkmale"] == {"paket_wunsch": "schaufenster", "sprache": "de"}
        assert lead["kontakt"]["website"] == "https://praxis-sommer.de"


def test_anfrage_nur_mit_telefon():
    lead = vilikon.anfrage_lead(_job(), {"typ": "ki-beratung", "telefon": " 0641 1 ",
                                         "name": "", "email": ""})
    assert lead["kontakt"]["email"] == ""
    assert lead["kontakt"]["telefon"] == ["0641 1"]
    assert lead["anliegen"]["betreff"] == "Website-Check: Anfrage KI-Beratung (Seite ohne Befunde)"
    assert lead["anliegen"]["kategorie"] == "ki-beratung"


def test_unbekannter_formulartyp_bekommt_einen_neutralen_betreff():
    lead = vilikon.anfrage_lead(_job(), {**FORMULAR, "typ": "rueckruf"})
    assert lead["anliegen"]["betreff"] == "Website-Check: Anfrage (mit Audit)"
    assert lead["anliegen"]["kategorie"] == "rueckruf"


def test_lead_bleibt_in_den_grenzen_des_cockpits():
    """Die Grenzen aus LEADS-API.md - ein zu langer Text gibt dort 422."""
    lang = {**ERGEBNIS, "findings": [
        {"category": "recht", "score_impact": -9, "message_customer": "Wort " * 400}]}
    lead = vilikon.info_lead(_job(result=lang))
    b = lead["bewertung"]
    assert len(b["hebel"]) <= 300
    assert len(b["zusammenfassung"]) <= 1000
    assert all(len(f["text"]) <= 600 and len(f["bereich"]) <= 120 for f in b["befunde"])
    assert len(lead["kontakt"]["website"]) <= 500
    assert len(lead["merkmale"]) <= 50


# --- Zustellung -------------------------------------------------------------

class Netz:
    """MockTransport, der jede Anfrage mitschreibt. `antworten` wird der Reihe
    nach abgearbeitet: ein Statuscode oder eine Ausnahme."""

    def __init__(self, *antworten):
        self.antworten = list(antworten) or [201]
        self.anfragen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.anfragen.append(request)
        naechste = self.antworten.pop(0) if len(self.antworten) > 1 else self.antworten[0]
        if isinstance(naechste, Exception):
            raise naechste
        if isinstance(naechste, httpx.Response):
            return naechste
        return httpx.Response(naechste, json={"ergebnisse": []})

    @property
    def leads(self) -> list[dict]:
        return [json.loads(a.content) for a in self.anfragen]

    def warte(self, anzahl: int = 1, timeout: float = 5.0) -> list[dict]:
        ende = time.time() + timeout
        while len(self.anfragen) < anzahl and time.time() < ende:
            time.sleep(0.02)
        assert len(self.anfragen) >= anzahl, f"{len(self.anfragen)} statt {anzahl} Lieferungen"
        return self.leads


@pytest.fixture
def cockpit(monkeypatch):
    """Lead-Push eingeschaltet, gegen ein gefaelschtes Cockpit, ohne Pausen."""
    monkeypatch.setenv("VILIKON_URL", URL)
    monkeypatch.setenv("VILIKON_LEADS_KEY", KEY)
    monkeypatch.setattr(vilikon, "PAUSEN_S", (0.0, 0.0))

    def einrichten(*antworten) -> Netz:
        netz = Netz(*antworten)
        monkeypatch.setattr(vilikon, "TRANSPORT", httpx.MockTransport(netz))
        return netz

    return einrichten


def test_zustellung_mit_bearer_an_api_leads(cockpit):
    netz = cockpit(201)
    assert asyncio.run(vilikon.senden(vilikon.info_lead(_job()))) is True
    (anfrage,) = netz.anfragen
    assert anfrage.method == "POST"
    assert str(anfrage.url) == "https://cockpit.test/api/leads"
    assert anfrage.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(anfrage.content)["art"] == "info"


def test_schraegstrich_am_ende_der_adresse_stoert_nicht(cockpit, monkeypatch):
    netz = cockpit(200)
    monkeypatch.setenv("VILIKON_URL", URL + "/")
    assert asyncio.run(vilikon.senden({"externe_id": "x"})) is True
    assert str(netz.anfragen[0].url) == "https://cockpit.test/api/leads"


@pytest.mark.parametrize("url,key", [("", ""), (URL, ""), ("", KEY), ("  ", "  ")])
def test_ohne_beide_variablen_ist_alles_aus(cockpit, monkeypatch, url, key):
    netz = cockpit(201)
    monkeypatch.setenv("VILIKON_URL", url)
    monkeypatch.setenv("VILIKON_LEADS_KEY", key)
    assert vilikon.aktiv() is False
    assert asyncio.run(vilikon.senden({"externe_id": "x"})) is False
    assert netz.anfragen == []


def test_netzfehler_drei_versuche_und_kein_schluessel_im_log(cockpit, caplog):
    """Die Ausnahme traegt hier absichtlich den Schluessel im Text - genau so
    etwas darf nie im Log landen."""
    netz = cockpit(httpx.ConnectError(f"kaputt mit {KEY}"))
    caplog.set_level("INFO")
    assert asyncio.run(vilikon.senden(vilikon.info_lead(_job()))) is False
    assert len(netz.anfragen) == 3
    assert "ConnectError" in caplog.text
    assert KEY not in caplog.text
    assert "vlk_" not in caplog.text


def test_zeitueberschreitung_ist_ein_gewoehnlicher_fehlschlag(cockpit, caplog):
    netz = cockpit(httpx.ReadTimeout("zu langsam"))
    assert asyncio.run(vilikon.senden({"externe_id": "audit-1"})) is False
    assert len(netz.anfragen) == 3
    assert "audit-1" in caplog.text


def test_timeout_ist_kurz():
    assert vilikon.TIMEOUT.connect <= 5
    assert vilikon.TIMEOUT.read <= 10
    assert len(vilikon.PAUSEN_S) <= 3


@pytest.mark.parametrize("voruebergehend", [500, 502, 503, 429])
def test_voruebergehende_fehler_werden_wiederholt(cockpit, voruebergehend):
    netz = cockpit(voruebergehend, voruebergehend, 201)
    assert asyncio.run(vilikon.senden({"externe_id": "x"})) is True
    assert len(netz.anfragen) == 3


@pytest.mark.parametrize("endgueltig", [400, 401, 403, 413, 415, 422])
def test_ablehnung_wird_nicht_wiederholt(cockpit, endgueltig, caplog):
    netz = cockpit(endgueltig)
    assert asyncio.run(vilikon.senden({"externe_id": "audit-2"})) is False
    assert len(netz.anfragen) == 1
    assert f"HTTP {endgueltig}" in caplog.text
    assert KEY not in caplog.text


def test_422_nennt_felder_aber_keine_besucherdaten(cockpit, caplog):
    antwort = httpx.Response(422, json={"detail": [
        {"loc": ["body", "kontakt", "email"], "msg": "too long",
         "input": "geheim@besucher.example"}]})
    cockpit(antwort)
    assert asyncio.run(vilikon.senden({"externe_id": "audit-3"})) is False
    assert "body.kontakt.email" in caplog.text
    assert "geheim@besucher.example" not in caplog.text


def test_melden_ohne_laufende_schleife_wirft_nicht(cockpit):
    """Ausserhalb von FastAPI gibt es keinen Event-Loop. Auch das darf nur
    protokolliert werden."""
    cockpit(201)
    vilikon.melden(vilikon.info_lead, _job())


# --- Im Worker: Check des Trichters -> Spur ---------------------------------

def test_fertiger_check_wird_ohne_besucher_ip_gemeldet(cockpit, client_mit_audit):
    netz = cockpit(201)
    r = client_mit_audit.post("/audit", json={"url": "www.example.de"},
                              headers={**AUTH, "X-Client-IP": BESUCHER_IP})
    job_id = r.json()["job_id"]
    assert warte_auf_ende(client_mit_audit, job_id)["status"] == "done"
    (lead,) = netz.warte(1)
    assert lead["art"] == "info"
    assert lead["externe_id"] == f"audit-{job_id}"
    assert lead["kontakt"] == {"website": "https://www.example.de"}
    assert lead["bewertung"]["score"] == 73
    assert lead["bewertung"]["hebel"] == "Recht 0/20"
    assert BESUCHER_IP not in netz.anfragen[0].content.decode()
    assert "client-ip" not in {k.lower() for k in netz.anfragen[0].headers}


def test_auftraege_des_agenten_werden_nicht_zurueckgemeldet(cockpit, client_mit_audit,
                                                          mandanten_datei):
    """Die Auftraege hat Vilikon selbst erteilt - sie zurueckzumelden waere ein Kreis."""
    netz = cockpit(201)
    mandanten_datei([AGENT_EINTRAG])
    job_id = client_mit_audit.post("/audit", json={"url": "agent.de"},
                                   headers=AGENT_AUTH).json()["job_id"]
    assert warte_auf_ende(client_mit_audit, job_id, AGENT_AUTH)["status"] == "done"
    time.sleep(0.2)
    assert netz.anfragen == []


def test_ohne_variablen_laeuft_der_check_wie_bisher(client_mit_audit, monkeypatch):
    aufgerufen = []
    monkeypatch.setattr(vilikon, "TRANSPORT",
                        httpx.MockTransport(lambda r: aufgerufen.append(r) or httpx.Response(201)))
    job_id = client_mit_audit.post("/audit", json={"url": "example.de"},
                                   headers=AUTH).json()["job_id"]
    assert warte_auf_ende(client_mit_audit, job_id)["status"] == "done"
    time.sleep(0.2)
    assert aufgerufen == []


def test_cockpit_weg_der_check_bleibt_heil(cockpit, client_mit_audit):
    netz = cockpit(httpx.ConnectError("weg"))
    job_id = client_mit_audit.post("/audit", json={"url": "example.de"},
                                   headers=AUTH).json()["job_id"]
    body = warte_auf_ende(client_mit_audit, job_id)
    assert body["status"] == "done"
    assert body["result"]["overall"] == 73
    netz.warte(3)
    assert client_mit_audit.get(f"/audit/{job_id}", headers=AUTH).json()["status"] == "done"


def test_unbaubarer_lead_macht_den_check_nicht_kaputt(cockpit, client_mit_audit, monkeypatch):
    """Der Lead wird NACH finish_job gebaut. Wuerfe das Bauen, landete die
    Ausnahme sonst im except von _run_job - und der fertige Check stuende
    beim Besucher als "failed"."""
    cockpit(201)

    def kaputt(_job):
        raise KeyError("unerwartet")

    monkeypatch.setattr(vilikon, "info_lead", kaputt)
    job_id = client_mit_audit.post("/audit", json={"url": "example.de"},
                                   headers=AUTH).json()["job_id"]
    assert warte_auf_ende(client_mit_audit, job_id)["status"] == "done"


def test_geerbtes_ergebnis_wird_als_eigener_check_gemeldet(cockpit, client, db):
    """Hat der Agent die Seite gerade geprueft, erbt der Besucher das Ergebnis
    unter einer neuen job_id. Fuer Vilikon ist das ein Check wie jeder andere."""
    netz = cockpit(201)
    db.create_job("a" * 32, "geerbt.de", "geerbt.de", "vilikon")
    db.finish_job("a" * 32, "done", result=ERGEBNIS)
    r = client.post("/audit", json={"url": "geerbt.de"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["cached"] is True
    (lead,) = netz.warte(1)
    assert lead["externe_id"] == f"audit-{r.json()['job_id']}"
    assert lead["bewertung"]["score"] == 48


def test_eigenes_wiederverwendetes_ergebnis_wird_nicht_doppelt_gemeldet(cockpit, client, db):
    netz = cockpit(201)
    db.create_job("b" * 32, "zweimal.de", "zweimal.de", "trichter")
    db.finish_job("b" * 32, "done", result=ERGEBNIS)
    r = client.post("/audit", json={"url": "zweimal.de"}, headers=AUTH)
    assert r.json()["job_id"] == "b" * 32
    time.sleep(0.2)
    assert netz.anfragen == []


# --- Im Worker: Anfrage zum Check -> To-do ----------------------------------

def _fertiger_job(db, client_id: str = "trichter") -> str:
    job_id = secrets.token_hex(16)
    db.create_job(job_id, "praxis-sommer.de", "praxis-sommer.de", client_id)
    db.finish_job(job_id, "done", result=ERGEBNIS)
    return job_id


def test_anfrage_wird_als_todo_gemeldet(cockpit, client, db):
    netz = cockpit(201)
    job_id = _fertiger_job(db)
    r = client.post(f"/audit/{job_id}", json=FORMULAR,
                    headers={**AUTH, "X-Client-IP": BESUCHER_IP})
    assert r.status_code == 202
    assert r.json() == {"status": "angenommen"}
    (lead,) = netz.warte(1)
    assert lead["art"] == "anfrage"
    assert lead["externe_id"] == f"audit-{job_id}-anfrage"
    assert lead["person"] == "Sabine Sommer"
    assert lead["bewertung"] == BEWERTUNG
    assert BESUCHER_IP not in netz.anfragen[0].content.decode()


def test_anfrage_antwortet_auch_ohne_cockpit_sofort(cockpit, client, db):
    """Der Besucher wartet nie auf Vilikon - auch nicht auf drei Fehlversuche."""
    cockpit(httpx.ConnectError("weg"))
    job_id = _fertiger_job(db)
    start = time.monotonic()
    r = client.post(f"/audit/{job_id}", json=FORMULAR, headers=AUTH)
    assert r.status_code == 202
    assert time.monotonic() - start < 2


def test_anfrage_bei_ausgeschaltetem_push_ist_trotzdem_202(client, db):
    job_id = _fertiger_job(db)
    r = client.post(f"/audit/{job_id}", json=FORMULAR, headers=AUTH)
    assert r.status_code == 202


def test_anfrage_braucht_das_geheimnis(client, db):
    job_id = _fertiger_job(db)
    assert client.post(f"/audit/{job_id}", json=FORMULAR).status_code == 401


def test_anfrage_zu_unbekanntem_oder_fremdem_job_ist_404(cockpit, client, db, mandanten_datei):
    netz = cockpit(201)
    mandanten_datei([AGENT_EINTRAG])
    fremd = _fertiger_job(db, client_id="vilikon")
    assert client.post("/audit/" + "0" * 32, json=FORMULAR, headers=AUTH).status_code == 404
    assert client.post(f"/audit/{fremd}", json=FORMULAR, headers=AUTH).status_code == 404
    # Auch der Agent selbst stellt keine Anfragen - nur der Trichter.
    assert client.post(f"/audit/{fremd}", json=FORMULAR, headers=AGENT_AUTH).status_code == 404
    time.sleep(0.2)
    assert netz.anfragen == []


def test_anfrage_ohne_mail_und_telefon_ist_422(client, db):
    job_id = _fertiger_job(db)
    r = client.post(f"/audit/{job_id}", json={**FORMULAR, "email": " ", "telefon": ""},
                    headers=AUTH)
    assert r.status_code == 422


def test_anfrage_mit_zu_langer_nachricht_ist_422(client, db):
    job_id = _fertiger_job(db)
    r = client.post(f"/audit/{job_id}", json={**FORMULAR, "nachricht": "x" * 5001},
                    headers=AUTH)
    assert r.status_code == 422


def test_anfragen_sind_pro_besucher_begrenzt(client, db):
    """Jede Anfrage ist ein Aufruf beim Cockpit. Ohne Grenze koennte ein
    einziger Besucher mit einer job_id dessen Limit aufbrauchen."""
    job_id = _fertiger_job(db)
    kopf = {**AUTH, "X-Client-IP": "10.0.0.1"}
    for _ in range(5):
        assert client.post(f"/audit/{job_id}", json=FORMULAR, headers=kopf).status_code == 202
    assert client.post(f"/audit/{job_id}", json=FORMULAR, headers=kopf).status_code == 429
    # Ein anderer Besucher ist davon nicht betroffen ...
    assert client.post(f"/audit/{job_id}", json=FORMULAR,
                       headers={**AUTH, "X-Client-IP": "10.0.0.2"}).status_code == 202
    # ... und Pruefungen zaehlen getrennt.
    assert client.post("/audit", json={"url": "neu.de"}, headers=kopf).status_code == 202
