"""Tests fuer kompakt.py - die Antwort, die im Modellkontext des Agenten landet."""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import kompakt
import pytest
from conftest import REPO


# --- Gewichtstabelle --------------------------------------------------------

def test_gewichte_kommen_aus_dem_regelwerk():
    g = kompakt.gewichte()
    assert sum(g.values()) == 100
    assert g["recht"] == 20


def test_fallback_stimmt_mit_dem_regelwerk_ueberein():
    """Drift-Test. Aendert jemand die Gewichte in ruleset.yaml, veraltet der
    Fallback in kompakt.py unbemerkt - und die Antwort an den Agenten waere
    stillschweigend falsch."""
    yaml = pytest.importorskip("yaml")
    daten = yaml.safe_load((REPO / "ruleset/ruleset.yaml").read_text(encoding="utf-8"))
    assert kompakt.GEWICHTE_FALLBACK == daten["category_weights"]


def test_unlesbares_regelwerk_faellt_auf_den_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("RULESET_DIR", str(tmp_path))
    kompakt.reload()
    assert kompakt.gewichte() == kompakt.GEWICHTE_FALLBACK


# --- Schwaechste Kategorie --------------------------------------------------

def test_schwaechste_kategorie_rechnet_relativ():
    """Absolut waere `lokale_sichtbarkeit` (5) schlechter als `recht` (6).
    Relativ zum Gewicht ist `recht` mit 6 von 20 der groessere Hebel."""
    k = kompakt.verdichten({"category_scores": {"recht": 6, "lokale_sichtbarkeit": 5}})
    assert k["kategorien"]["recht"] == [6, 20]
    assert k["kategorien"]["lokale_sichtbarkeit"] == [5, 12]
    assert k["schwaechste_kategorie"] == "recht"


def test_gleichstand_geht_an_das_groessere_gewicht():
    k = kompakt.verdichten({"category_scores": {"recht": 0, "lokale_sichtbarkeit": 0,
                                                "technik_performance": 0}})
    assert k["schwaechste_kategorie"] == "technik_performance"   # Gewicht 23


def test_ohne_kategorien_kein_absturz():
    k = kompakt.verdichten({})
    assert k["schwaechste_kategorie"] is None
    assert k["kategorien"] == {}
    assert k["top_findings"] == []


# --- Befunde ----------------------------------------------------------------

BEFUNDE = {
    "findings": [
        {"category": "recht", "score_impact": -5,
         "message_customer": "Kleiner Mangel.", "message_internal": "INTERN-A"},
        {"category": "technik_performance", "score_impact": -20,
         "message_customer": "Grosser Mangel.", "message_internal": "INTERN-B"},
        {"category": "recht", "score_impact": -12,
         "message_customer": "Mittlerer Mangel.", "message_internal": "INTERN-C"},
        {"category": "recht", "score_impact": -1,
         "message_customer": "Winziger Mangel.", "message_internal": "INTERN-D"},
    ]
}


def test_befunde_nach_hebel_sortiert_und_auf_drei_begrenzt():
    k = kompakt.verdichten(BEFUNDE)
    assert [b["gewicht"] for b in k["top_findings"]] == [-20, -12, -5]
    assert k["befunde_gesamt"] == 4


def test_interne_fassung_kommt_nicht_nach_draussen():
    """Die wichtigste Zusage dieses Moduls: Die interne Fassung darf nie in
    den Modellkontext, sonst zitiert der Agent sie im Anschreiben."""
    text = json.dumps(kompakt.verdichten(BEFUNDE), ensure_ascii=False)
    for intern in ("INTERN-A", "INTERN-B", "INTERN-C", "INTERN-D", "message_internal"):
        assert intern not in text


def test_befund_ohne_kundenfassung_wird_weggelassen():
    k = kompakt.verdichten({"findings": [
        {"category": "recht", "score_impact": -9, "message_internal": "nur intern"},
        {"category": "recht", "score_impact": -9, "message_customer": "   "},
        {"category": "recht", "score_impact": -9, "message_customer": None},
    ]})
    assert k["top_findings"] == []
    assert k["befunde_gesamt"] == 0


def test_langer_text_wird_an_der_wortgrenze_gekuerzt():
    lang = "Wort " * 200
    k = kompakt.verdichten({"findings": [
        {"category": "recht", "score_impact": -1, "message_customer": lang}]})
    text = k["top_findings"][0]["text"]
    assert len(text) <= kompakt.MAX_TEXT + 1       # +1 fuer das Auslassungszeichen
    assert text.endswith("…")
    assert not text.endswith("Wo…")                # kein zerfetztes Wort


def test_zeilenumbrueche_werden_geglaettet():
    k = kompakt.verdichten({"findings": [
        {"category": "recht", "score_impact": -1,
         "message_customer": "Erste Zeile.\n\n   Zweite   Zeile."}]})
    assert k["top_findings"][0]["text"] == "Erste Zeile. Zweite Zeile."


def test_kaputte_befunde_werden_uebersprungen():
    k = kompakt.verdichten({"findings": ["text", None, 42,
                                         {"message_customer": "ok"}]})
    assert len(k["top_findings"]) == 1
    assert k["top_findings"][0]["gewicht"] == 0


# --- Zeitstempel ------------------------------------------------------------

def test_zeitstempel_in_iso_mit_z():
    k = kompakt.verdichten({}, geprueft_am=1760000000.0)
    assert k["geprueft_am"].endswith("Z")
    assert len(k["geprueft_am"]) == 20


def test_ohne_zeitstempel_bleibt_none():
    assert kompakt.verdichten({})["geprueft_am"] is None


# --- Groesse gegen ein echtes Ergebnis -------------------------------------

def test_echtes_ergebnis_wird_deutlich_kleiner(tmp_path):
    """Gegen das echte Regelwerk gerechnet, nicht gegen eine Attrappe."""
    sys.path.insert(0, str(REPO / "ruleset"))
    from pipeline.score import run_score

    for f in (REPO / "ruleset/tests/fixtures/bad_site").glob("*.json"):
        shutil.copy(f, tmp_path)
    voll = run_score(Path(tmp_path))

    k = kompakt.verdichten(voll, geprueft_am=time.time())
    gross = len(json.dumps(voll, ensure_ascii=False))
    klein = len(json.dumps(k, ensure_ascii=False))
    assert gross > 5000, f"Vollformat unerwartet klein ({gross} Byte)"
    assert klein < 1200, f"Kompaktformat unerwartet gross ({klein} Byte)"

    # Alles, was der Agent zum Auswaehlen und Schreiben braucht, ist da.
    assert k["overall"] == voll["overall"]
    assert k["regelwerk"] == voll["ruleset_version"]
    assert k["geschaeftstyp"] == voll["business_locality"]
    assert len(k["kategorien"]) == len(voll["category_scores"])


def test_makellose_seite_hat_keine_schwaechste_kategorie():
    """Live aufgefallen: cbgg.de mit 100/100 wies "technik_performance" als
    Schwachstelle aus, weil der Gleichstand bei 23/23 ans groesste Gewicht
    ging. Der Agent haette daraus einen Hebel gelesen, den es nicht gibt."""
    k = kompakt.verdichten({"category_scores": {
        "technik_performance": 23, "mobil_zugaenglichkeit": 15, "recht": 20,
        "auffindbarkeit_seo": 15, "lokale_sichtbarkeit": 12,
        "inhalt_aktualitaet": 15}})
    assert k["schwaechste_kategorie"] is None


def test_eine_einzige_schwaeche_wird_trotzdem_gefunden():
    k = kompakt.verdichten({"category_scores": {
        "technik_performance": 23, "mobil_zugaenglichkeit": 15, "recht": 18,
        "auffindbarkeit_seo": 15, "lokale_sichtbarkeit": 12,
        "inhalt_aktualitaet": 15}})
    assert k["schwaechste_kategorie"] == "recht"


# --- Seitenzahl -------------------------------------------------------------

def test_seitenzahl_wird_aus_dem_objekt_geholt():
    """Live aufgefallen: Das Regelwerk liefert page_count als Objekt. Ohne
    Verflachung standen vier Felder im Modellkontext, wo eines genuegt - und
    API.md versprach eine Zahl."""
    k = kompakt.verdichten({"page_count": {
        "sitemap_urls": 7, "internal_links_home": 8,
        "estimate": 7, "source": "sitemap"}})
    assert k["seiten_geschaetzt"] == 7


def test_seitenzahl_vertraegt_alle_formen():
    for eingabe, erwartet in (
        (11, 11),
        (None, None),
        ({}, None),
        ({"source": "links"}, None),
        ({"estimate": None}, None),
        ("sieben", None),
        (True, None),
    ):
        k = kompakt.verdichten({"page_count": eingabe})
        assert k["seiten_geschaetzt"] == erwartet, f"{eingabe!r} -> {k['seiten_geschaetzt']!r}"
