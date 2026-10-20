"""Tests fuer audit.py - Rohdaten-Loeschung und Domain-Sperre.

Der eigentliche Lauf (Playwright-Subprozess) wird hier nicht ausgefuehrt; er
braucht Chromium und Netzwerk. Geprueft wird die Logik um ihn herum, und die
ist die datenschutzrelevante: Was bleibt nach einem Lauf auf der Platte liegen?
"""
from __future__ import annotations

import asyncio
import json

import audit
import pytest


def _lauf_anlegen(tmp_path, domain: str, stempel: str, mit_html: bool = True):
    d = tmp_path / "runs" / domain / stempel
    d.mkdir(parents=True)
    if mit_html:
        (d / f"{domain}_crawl.json").write_text(
            json.dumps({"html": "<html>…vollstaendige Seite…</html>"}), encoding="utf-8")
    (d / f"{domain}_score_result.json").write_text('{"overall": 50}', encoding="utf-8")
    return d


@pytest.fixture
def ruleset_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "RULESET_DIR", str(tmp_path))
    return tmp_path


def test_eigene_rohdaten_werden_geloescht(ruleset_dir):
    neu = _lauf_anlegen(ruleset_dir, "fremd.de", "20261020-120000")
    audit._rohdaten_loeschen("fremd.de", vorher=set())
    assert not neu.exists()


def test_fremde_rohdaten_bleiben_liegen(ruleset_dir):
    """Die Laeufe des Trichters sind die Vorher/Nachher-Grundlage fuer
    compare.py. Ein Agentenlauf darf sie nicht mitnehmen."""
    alt = _lauf_anlegen(ruleset_dir, "kunde.de", "20261001-090000")
    vorher = audit._laufverzeichnisse("kunde.de")
    neu = _lauf_anlegen(ruleset_dir, "kunde.de", "20261020-120000")

    audit._rohdaten_loeschen("kunde.de", vorher)
    assert alt.exists(), "fremder Lauf wurde geloescht"
    assert not neu.exists()


def test_leerer_domain_ordner_verschwindet_mit(ruleset_dir):
    """Sonst waere runs/ nach einem Jahr ein Verzeichnis fremder Firmennamen,
    auch ohne ein Byte Inhalt."""
    _lauf_anlegen(ruleset_dir, "fremd.de", "20261020-120000")
    audit._rohdaten_loeschen("fremd.de", vorher=set())
    assert not (ruleset_dir / "runs" / "fremd.de").exists()


def test_domain_ordner_bleibt_wenn_noch_was_drin_ist(ruleset_dir):
    _lauf_anlegen(ruleset_dir, "kunde.de", "20261001-090000")
    vorher = audit._laufverzeichnisse("kunde.de")
    _lauf_anlegen(ruleset_dir, "kunde.de", "20261020-120000")
    audit._rohdaten_loeschen("kunde.de", vorher)
    assert (ruleset_dir / "runs" / "kunde.de").is_dir()


def test_loeschen_vertraegt_fehlendes_verzeichnis(ruleset_dir):
    audit._rohdaten_loeschen("gibtsnicht.de", vorher=set())


# --- Das finally ------------------------------------------------------------

def test_rohdaten_verschwinden_auch_wenn_der_lauf_scheitert(ruleset_dir, monkeypatch):
    """Der wichtigste Test hier. Bei "NICHT AUDITIERBAR" kehrt das Regelwerk
    zurueck, NACHDEM der Crawl HTML und Screenshot geschrieben hat. Ohne das
    finally blieben genau diese Faelle fuer Monate liegen."""
    async def _lauf_der_schreibt_und_scheitert(url, domain, kontaktdaten):
        _lauf_anlegen(ruleset_dir, domain, "20261020-120000")
        raise audit.NotAuditableError("https+http lieferten Fehler")

    monkeypatch.setattr(audit, "_lauf", _lauf_der_schreibt_und_scheitert)

    with pytest.raises(audit.NotAuditableError):
        asyncio.run(audit.run_audit("https://fremd.de", keep_raw=False))
    assert not (ruleset_dir / "runs" / "fremd.de").exists()


def test_unerwarteter_fehler_laesst_auch_nichts_liegen(ruleset_dir, monkeypatch):
    async def _explodiert(url, domain, kontaktdaten):
        _lauf_anlegen(ruleset_dir, domain, "20261020-120000")
        raise RuntimeError("irgendwas")

    monkeypatch.setattr(audit, "_lauf", _explodiert)
    with pytest.raises(RuntimeError):
        asyncio.run(audit.run_audit("https://fremd.de", keep_raw=False))
    assert not (ruleset_dir / "runs" / "fremd.de").exists()


def test_keep_raw_behaelt_die_daten(ruleset_dir, monkeypatch):
    """Der Trichter braucht sie - fuer compare.py und fuer Regressionsbeweise."""
    async def _lauf(url, domain, kontaktdaten):
        _lauf_anlegen(ruleset_dir, domain, "20261020-120000")
        return {"overall": 50}

    monkeypatch.setattr(audit, "_lauf", _lauf)
    asyncio.run(audit.run_audit("https://kunde.de", keep_raw=True))
    assert (ruleset_dir / "runs" / "kunde.de" / "20261020-120000").is_dir()


def test_kontaktdaten_werden_vor_dem_loeschen_gelesen(ruleset_dir, monkeypatch):
    """Reihenfolge-Beweis: Die Extraktion muss VOR dem finally laufen, sonst
    liest sie ein Verzeichnis, das es nicht mehr gibt."""
    gelesen = {}

    async def _lauf(url, domain, kontaktdaten):
        d = _lauf_anlegen(ruleset_dir, domain, "20261020-120000")
        if kontaktdaten:
            gelesen["vorhanden"] = (d / f"{domain}_crawl.json").exists()
        return {"overall": 50, "absender_kandidat": {"firma": "X"}}

    monkeypatch.setattr(audit, "_lauf", _lauf)
    ergebnis = asyncio.run(audit.run_audit("https://fremd.de", kontaktdaten=True,
                                           keep_raw=False))
    assert gelesen["vorhanden"] is True
    assert ergebnis["absender_kandidat"]["firma"] == "X"
    assert not (ruleset_dir / "runs" / "fremd.de").exists()


# --- Domain-Sperre ----------------------------------------------------------

def test_dieselbe_domain_laeuft_nicht_zweimal_gleichzeitig(ruleset_dir, monkeypatch):
    """Hoeflichkeit gegenueber der fremden Seite - und Voraussetzung dafuer,
    dass beim Loeschen eindeutig ist, welche Daten zu welchem Lauf gehoeren."""
    gleichzeitig = 0
    hoechststand = 0

    async def _lauf(url, domain, kontaktdaten):
        nonlocal gleichzeitig, hoechststand
        gleichzeitig += 1
        hoechststand = max(hoechststand, gleichzeitig)
        await asyncio.sleep(0.03)
        gleichzeitig -= 1
        return {"overall": 1}

    monkeypatch.setattr(audit, "_lauf", _lauf)

    async def viele():
        await asyncio.gather(*[audit.run_audit("https://fremd.de") for _ in range(4)])

    asyncio.run(viele())
    assert hoechststand == 1


def test_verschiedene_domains_laufen_parallel(ruleset_dir, monkeypatch):
    gleichzeitig = 0
    hoechststand = 0

    async def _lauf(url, domain, kontaktdaten):
        nonlocal gleichzeitig, hoechststand
        gleichzeitig += 1
        hoechststand = max(hoechststand, gleichzeitig)
        await asyncio.sleep(0.03)
        gleichzeitig -= 1
        return {"overall": 1}

    monkeypatch.setattr(audit, "_lauf", _lauf)

    async def viele():
        await asyncio.gather(*[audit.run_audit(f"https://seite{i}.de") for i in range(4)])

    asyncio.run(viele())
    assert hoechststand == 4


def test_sperren_wachsen_nicht_unbegrenzt(ruleset_dir, monkeypatch):
    """Ein Container laeuft Wochen. Ohne Aufraeumen waere die Sperrenliste
    nach einem Jahr eine Liste aller je gepruefen Domains."""
    async def _lauf(url, domain, kontaktdaten):
        return {"overall": 1}

    monkeypatch.setattr(audit, "_lauf", _lauf)

    async def viele():
        for i in range(20):
            await audit.run_audit(f"https://seite{i}.de")

    asyncio.run(viele())
    assert audit._domain_sperren == {}
