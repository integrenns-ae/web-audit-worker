"""Tests fuer kontakt.py - Absenderdaten fuer den Briefkanal.

Diese Extraktion liest handgeschriebene Prosa. Die Tests halten deshalb nicht
nur fest, was erkannt wird, sondern auch wo bewusst NICHTS geliefert wird -
eine falsche Anschrift auf einem Brief ist schlimmer als keine.
"""
from __future__ import annotations

import json

import kontakt

PRAXIS = """Impressum
Praxis Dr. med. Anna Beispiel
Musterweg 3
35390 Gießen
Telefon: 0641 123456
Fax: 0641 123457
E-Mail: praxis@beispiel.de
Angaben gemäß § 5 DDG"""

EINZEILIG = """Impressum
Beispiel GmbH
Hauptstraße 12a, 35410 Hungen
Tel. +49 6402 98765
info@beispiel-gmbh.de"""

MIT_NAVIGATION = """Startseite
Leistungen
Kontakt
Impressum
Angaben gemäß § 5 DDG
Schreinerei Holzwurm e.K.
Am Markt 7
35428 Langgöns
Fon: 06403 / 1234-56
mail@holzwurm-langgoens.de
Umsatzsteuer-ID: DE123456789"""


def test_praxis_wird_vollstaendig_erkannt():
    d = kontakt.aus_impressum(PRAXIS, "/impressum")
    assert d["firma"] == "Praxis Dr. med. Anna Beispiel"
    assert d["anschrift"] == "Musterweg 3, 35390 Gießen"
    assert d["email"] == "praxis@beispiel.de"
    assert d["quelle"] == "/impressum"


def test_faxnummer_wird_nicht_als_telefon_genommen():
    """Ein Brief mit der Faxnummer als Rueckrufnummer waere peinlich."""
    assert kontakt.aus_impressum(PRAXIS)["telefon"] == "0641 123456"


def test_anschrift_in_einer_zeile():
    d = kontakt.aus_impressum(EINZEILIG)
    assert d["anschrift"] == "Hauptstraße 12a, 35410 Hungen"
    assert d["firma"] == "Beispiel GmbH"
    assert d["telefon"] == "+49 6402 98765"


def test_navigationsreste_werden_nicht_zum_firmennamen():
    """innerText enthaelt das ganze Menue. Ohne Filter stuende "Impressum"
    oder "Angaben gemäß § 5 DDG" als Absender auf dem Brief."""
    d = kontakt.aus_impressum(MIT_NAVIGATION)
    assert d["firma"] == "Schreinerei Holzwurm e.K."
    assert d["anschrift"] == "Am Markt 7, 35428 Langgöns"
    assert d["telefon"] == "06403 / 1234-56"
    assert d["email"] == "mail@holzwurm-langgoens.de"


def test_ohne_anschrift_und_ohne_mail_kommt_nichts():
    """Fuer beide Kanaele nutzlos - dann lieber gar kein Block."""
    assert kontakt.aus_impressum("""Impressum
Vertreten durch: Max Mustermann, Geschäftsführer.
Angaben gemäß § 5 DDG.""") is None
    assert kontakt.aus_impressum("") is None
    assert kontakt.aus_impressum("   \n  \n") is None


def test_nur_mail_ergibt_einen_teilblock():
    d = kontakt.aus_impressum("Impressum\nKontakt: mail@nurmail.de")
    assert d["email"] == "mail@nurmail.de"
    assert d["anschrift"] is None
    assert d["firma"] is None


def test_noreply_adressen_werden_uebersprungen():
    d = kontakt.aus_impressum("""Impressum
Beispiel GmbH
Hauptstraße 1
35390 Gießen
noreply@beispiel.de
kontakt@beispiel.de""")
    assert d["email"] == "kontakt@beispiel.de"


def test_telefon_braucht_genug_ziffern():
    d = kontakt.aus_impressum("""Impressum
Beispiel GmbH
Hauptstraße 1
35390 Gießen
Tel: 123
info@beispiel.de""")
    assert d["telefon"] is None


# --- Aus den Crawl-Daten ----------------------------------------------------

def _crawl_schreiben(verzeichnis, domain, impressum: dict | None):
    datei = verzeichnis / f"{domain}_crawl.json"
    datei.write_text(json.dumps({
        "domain": domain,
        "legal_subpages": {"impressum": impressum} if impressum else {},
    }), encoding="utf-8")
    return datei


def test_liest_aus_den_crawl_daten(tmp_path):
    _crawl_schreiben(tmp_path, "beispiel.de", {
        "reachable": True, "path": "/impressum", "text_content": PRAXIS})
    d = kontakt.aus_lauf(tmp_path, "beispiel.de")
    assert d["anschrift"] == "Musterweg 3, 35390 Gießen"
    assert d["quelle"] == "/impressum"


def test_unerreichbares_impressum_ergibt_nichts(tmp_path):
    _crawl_schreiben(tmp_path, "beispiel.de", {
        "reachable": False, "path": None, "text_content": ""})
    assert kontakt.aus_lauf(tmp_path, "beispiel.de") is None


def test_fehlende_crawl_datei_ergibt_nichts(tmp_path):
    assert kontakt.aus_lauf(tmp_path, "beispiel.de") is None


def test_kaputte_crawl_datei_ergibt_nichts(tmp_path):
    (tmp_path / "beispiel.de_crawl.json").write_text("{kein json", encoding="utf-8")
    assert kontakt.aus_lauf(tmp_path, "beispiel.de") is None


def test_echte_fixture_des_regelwerks(tmp_path):
    """Gegen die Crawl-Daten, die das Regelwerk selbst in seinen Tests nutzt."""
    from conftest import REPO
    import shutil

    quelle = REPO / "ruleset/tests/fixtures/clean_site/clean.example_crawl.json"
    shutil.copy(quelle, tmp_path)
    # Diese Fixture hat ein erreichbares Impressum ohne Anschrift -> None,
    # und genau das ist richtig.
    assert kontakt.aus_lauf(tmp_path, "clean.example") is None
