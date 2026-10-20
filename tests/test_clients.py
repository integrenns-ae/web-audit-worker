"""Tests fuer clients.py.

Der wichtigste Test dieser Datei ist
`test_trichter_funktioniert_trotz_kaputter_clients_json`. Er sichert die
Zusage ab, die den ganzen Umbau erst vertretbar macht: Der Weg, der Geld
verdient, darf nicht an einer Datei haengen, die nur ein Nebenverbraucher
braucht.
"""
from __future__ import annotations

import clients
from conftest import AGENT_EINTRAG, AGENT_SECRET, TEST_SECRET, sha256


# --- Der Trichter ----------------------------------------------------------

def test_legacy_geheimnis_ergibt_den_trichter():
    c = clients.resolve(TEST_SECRET)
    assert c is not None
    assert c.id == clients.LEGACY_CLIENT_ID
    assert c.limit_scope == "ip"
    assert c.trust_client_ip is True
    assert c.keep_raw is True


def test_trichter_laeuft_ohne_clients_json():
    """Stufe 1 wird genau so ausgeliefert: Die Datei existiert noch nicht."""
    assert clients.resolve(TEST_SECRET).id == clients.LEGACY_CLIENT_ID
    assert clients.alle_mandanten() == []


def test_trichter_funktioniert_trotz_kaputter_clients_json(mandanten_datei):
    """Der entscheidende Test. Kaputtes JSON darf den Trichter nicht toeten."""
    for muell in ('{ das ist kein json', '', '[', 'null', '"text"', '{"id":"x"}'):
        mandanten_datei(muell)
        assert clients.resolve(TEST_SECRET).id == clients.LEGACY_CLIENT_ID, \
            f"Trichter gesperrt durch Inhalt {muell!r}"
        assert clients.resolve("falsch") is None


def test_trichter_funktioniert_bei_unlesbarer_datei(mandanten_datei, monkeypatch):
    pfad = mandanten_datei([AGENT_EINTRAG])
    pfad.chmod(0o000)
    try:
        clients.reload()
        assert clients.resolve(TEST_SECRET).id == clients.LEGACY_CLIENT_ID
    finally:
        pfad.chmod(0o600)


def test_ohne_geheimnis_ist_nichts_konfiguriert(monkeypatch):
    for leer in ("", "changeme", "CHANGE_ME"):
        monkeypatch.setenv("AUDIT_SHARED_SECRET", leer)
        assert clients.legacy_configured() is False
        assert clients.resolve(leer) is None


def test_limits_des_trichters_kommen_aus_der_umgebung(monkeypatch):
    monkeypatch.setenv("AUDIT_RATE_HOUR", "7")
    monkeypatch.setenv("AUDIT_DOMAIN_COOLDOWN", "999")
    c = clients.resolve(TEST_SECRET)
    assert c.rate_hour == 7
    assert c.cooldown_s == 999


def test_unsinnige_zahl_in_der_umgebung_faellt_auf_standard(monkeypatch):
    monkeypatch.setenv("AUDIT_RATE_HOUR", "viele")
    assert clients.resolve(TEST_SECRET).rate_hour == 5


# --- Mandanten aus clients.json --------------------------------------------

def test_mandant_wird_ueber_den_hash_erkannt(mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    c = clients.resolve(AGENT_SECRET)
    assert c is not None
    assert c.id == "vilikon"
    assert c.limit_scope == "client"
    assert (c.rate_hour, c.rate_day, c.cooldown_s) == (20, 60, 43200)
    assert c.keep_raw is False
    assert c.kontaktdaten is True
    assert c.retention_days == 30


def test_in_der_datei_steht_kein_klartext(mandanten_datei):
    pfad = mandanten_datei([AGENT_EINTRAG])
    assert AGENT_SECRET not in pfad.read_text(encoding="utf-8")


def test_falsches_geheimnis_ergibt_keinen_mandanten(mandanten_datei):
    mandanten_datei([AGENT_EINTRAG])
    assert clients.resolve("irgendwas") is None
    assert clients.resolve(None) is None
    assert clients.resolve("") is None


def test_mehrere_hashes_erlauben_schluesselwechsel(mandanten_datei):
    """Ohne zweiten Hash muesste man den Schluessel mit einem Ausfall tauschen."""
    eintrag = dict(AGENT_EINTRAG)
    eintrag["secrets_sha256"] = [sha256("alt"), sha256("neu")]
    mandanten_datei([eintrag])
    assert clients.resolve("alt").id == "vilikon"
    assert clients.resolve("neu").id == "vilikon"


def test_standardwerte_fuer_einen_knappen_eintrag(mandanten_datei):
    mandanten_datei([{"id": "knapp", "secrets_sha256": [sha256("k")]}])
    c = clients.resolve("k")
    assert c.limit_scope == "client"
    assert c.trust_client_ip is False
    assert c.keep_raw is False          # Fremdmandanten horten standardmaessig nicht
    assert c.kontaktdaten is False
    assert c.max_queued == 2


# --- Abweisung schlechter Eintraege ----------------------------------------

def test_reservierte_id_wird_abgewiesen(mandanten_datei):
    """Sonst koennte ein Eintrag die Auftraege des Trichters uebernehmen."""
    mandanten_datei([{"id": clients.LEGACY_CLIENT_ID, "secrets_sha256": [sha256("x")]}])
    assert clients.resolve("x") is None
    assert clients.resolve(TEST_SECRET).id == clients.LEGACY_CLIENT_ID


def test_eintrag_ohne_hashes_wird_abgewiesen(mandanten_datei):
    for kaputt in (
        {"id": "a", "secrets_sha256": []},
        {"id": "a", "secrets_sha256": "nichtliste"},
        {"id": "a"},
        {"secrets_sha256": [sha256("x")]},
        {"id": "   ", "secrets_sha256": [sha256("x")]},
        "garkeinobjekt",
    ):
        mandanten_datei([kaputt])
        assert clients.alle_mandanten() == [], f"{kaputt!r} wurde akzeptiert"


def test_kein_hash_bedeutet_kein_klartextvergleich(mandanten_datei):
    """Ein Eintrag mit Klartext statt Hash darf NICHT funktionieren - sonst
    waere das Geheimnis im Klartext auf dem Server gelandet."""
    mandanten_datei([{"id": "a", "secrets_sha256": [AGENT_SECRET]}])
    assert clients.resolve(AGENT_SECRET) is None
    assert clients.alle_mandanten() == []


def test_unbekannter_limit_scope_wird_abgewiesen(mandanten_datei):
    mandanten_datei([{"id": "a", "secrets_sha256": [sha256("x")], "limit_scope": "mond"}])
    assert clients.resolve("x") is None


def test_ip_vertrauen_nur_mit_ip_zaehlung(mandanten_datei):
    """trust_client_ip bei Mandantenzaehlung waere sinnlos und gefaehrlich:
    Nur der Strato-Proxy kennt eine echte Besucher-IP."""
    mandanten_datei([{"id": "a", "secrets_sha256": [sha256("x")],
                      "limit_scope": "client", "trust_client_ip": True}])
    assert clients.resolve("x") is None


def test_doppelter_hash_gewinnt_nur_einmal(mandanten_datei):
    h = sha256("x")
    mandanten_datei([{"id": "erst", "secrets_sha256": [h]},
                     {"id": "zweit", "secrets_sha256": [h]}])
    assert clients.resolve("x").id == "erst"
    assert [c.id for c in clients.alle_mandanten()] == ["erst"]


def test_ein_kaputter_eintrag_nimmt_die_guten_nicht_mit(mandanten_datei):
    mandanten_datei([{"id": "kaputt"}, AGENT_EINTRAG])
    assert clients.resolve(AGENT_SECRET).id == "vilikon"


def test_negative_limits_fallen_auf_standard(mandanten_datei):
    mandanten_datei([{"id": "a", "secrets_sha256": [sha256("x")], "rate_hour": -5,
                      "rate_day": True, "retention_days": 0}])
    c = clients.resolve("x")
    assert c.rate_hour == 20
    assert c.rate_day == 60          # True ist keine brauchbare Zahl
    assert c.retention_days is None  # 0 Tage waere "sofort loeschen" - nicht gemeint
