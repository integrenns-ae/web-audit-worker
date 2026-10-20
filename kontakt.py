"""kontakt — Absenderdaten aus dem Impressum lesen (für den Briefkanal).

Für einen Brief braucht der Agent Firmenname und Postanschrift. Beides steht
im Impressum, das der Crawl ohnehin abruft (`legal_subpages.impressum.
text_content` ist der vollständige `innerText` der Seite). Es wird also keine
zusätzliche Seite geladen — die fremde Website wird durch diese Funktion nicht
ein einziges Mal mehr angefasst.

**Das ist eine Textextraktion, keine Datenbankabfrage.** Impressen sind
handgeschriebene Prosa in beliebiger Reihenfolge. Felder können fehlen,
verrutschen oder falsch sein; vor dem Druck muss jemand hinsehen. Deshalb
liefert jedes Feld entweder einen Treffer oder `None` — niemals eine Schätzung,
die wie ein Treffer aussieht.

Bei Praxen, Kanzleien und Einzelunternehmern sind das personenbezogene Daten.
Sie werden nur für Mandanten mit `kontaktdaten: true` erhoben, unterliegen
deren `retention_days` und verlangen den Art.-14-Hinweis im Erstkontakt.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("audit-worker.kontakt")

# Zeilen, die nie ein Firmenname sind.
_KEIN_NAME = re.compile(
    r"^(impressum|kontakt|angaben\s+gem|informationspflicht|verantwortlich|"
    r"anbieterkennzeichnung|rechtliche|home|startseite|menü|menu|navigation|"
    r"inhalt|cookie|datenschutz)", re.I)

# "Musterweg 3", "Am Markt 12a", "Hauptstr. 5-7"
_STRASSE = re.compile(
    r"^(.{2,60}?(?:stra(?:ß|ss)e|str\.|weg|platz|allee|gasse|ring|damm|ufer|"
    r"chaussee|markt|hof|berg|feld)\s*\d+\s*[a-zA-Z]?(?:\s*[-/]\s*\d+\s*[a-zA-Z]?)?)$",
    re.I)
# Fallback: irgendein Text, der auf eine Hausnummer endet.
_STRASSE_LOSE = re.compile(r"^([A-ZÄÖÜ][^,;]{2,60}?\s\d+\s*[a-zA-Z]?)$")

_PLZ_ORT = re.compile(r"^(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-.' ]{1,40})$")
# "Musterweg 3, 35390 Gießen" in einer Zeile
_EINZEILIG = re.compile(
    r"^(.{3,60}?\s\d+\s*[a-zA-Z]?)\s*,\s*(\d{5})\s+([A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-.' ]{1,40})$")

_TELEFON_BESCHRIFTET = re.compile(
    r"(?:tel(?:efon)?|fon|ruf|phone)\b[^0-9+]{0,12}((?:\+\d{1,3}|0)[\d\s()/.\-]{5,25})", re.I)
_TELEFON_LOSE = re.compile(r"^((?:\+49|\+\d{1,3}|0)[\d\s()/.\-]{7,25})$")
_FAX = re.compile(r"\bfax\b", re.I)

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Adressen, die nie die des Unternehmens sind.
_EMAIL_UNBRAUCHBAR = re.compile(
    r"^(no-?reply|postmaster|webmaster@|abuse@|example|.*@(example|sentry|"
    r"googlemail\.com\.invalid))", re.I)


def _zeilen(text: str) -> list[str]:
    roh = (z.strip(" \t ·|•") for z in text.splitlines())
    return [z for z in roh if z]


def _telefon(text: str, zeilen: list[str]) -> str | None:
    for treffer in _TELEFON_BESCHRIFTET.finditer(text):
        anfang = max(0, treffer.start() - 30)
        if _FAX.search(text[anfang:treffer.start()]):
            continue            # Faxnummer ist keine Telefonnummer
        nummer = " ".join(treffer.group(1).split())
        if sum(c.isdigit() for c in nummer) >= 6:
            return nummer
    for zeile in zeilen:
        m = _TELEFON_LOSE.match(zeile)
        if m and sum(c.isdigit() for c in m.group(1)) >= 7:
            return " ".join(m.group(1).split())
    return None


def _email(text: str) -> str | None:
    for treffer in _EMAIL.finditer(text):
        adresse = treffer.group(0).rstrip(".,;")
        if not _EMAIL_UNBRAUCHBAR.match(adresse):
            return adresse.lower()
    return None


def _anschrift_und_firma(zeilen: list[str]) -> tuple[str | None, str | None]:
    """Sucht die Postanschrift und nimmt die Zeile darüber als Firmennamen."""
    for i, zeile in enumerate(zeilen):
        einzeilig = _EINZEILIG.match(zeile)
        if einzeilig:
            strasse, plz, ort = einzeilig.groups()
            return f"{strasse.strip()}, {plz} {ort.strip()}", _firma_vor(zeilen, i)

        plz_ort = _PLZ_ORT.match(zeile)
        if not plz_ort or i == 0:
            continue
        vorige = zeilen[i - 1]
        strasse = _STRASSE.match(vorige) or _STRASSE_LOSE.match(vorige)
        if not strasse:
            continue
        anschrift = f"{strasse.group(1).strip()}, {plz_ort.group(1)} {plz_ort.group(2).strip()}"
        return anschrift, _firma_vor(zeilen, i - 1)
    return None, None


def _firma_vor(zeilen: list[str], index: int) -> str | None:
    """Erste brauchbare Zeile oberhalb der Anschrift."""
    for kandidat in reversed(zeilen[max(0, index - 3):index]):
        if _KEIN_NAME.match(kandidat):
            continue
        if _EMAIL.search(kandidat) or _PLZ_ORT.match(kandidat):
            continue
        if len(kandidat) < 3 or len(kandidat) > 80:
            continue
        if sum(c.isdigit() for c in kandidat) > len(kandidat) / 3:
            continue
        return kandidat.rstrip(",;")
    return None


def aus_impressum(text: str, quelle: str | None = None) -> dict | None:
    """Impressumstext -> Absenderdaten. None, wenn nichts Brauchbares drinsteht."""
    if not text or not text.strip():
        return None
    zeilen = _zeilen(text)
    anschrift, firma = _anschrift_und_firma(zeilen)
    daten = {
        "firma": firma,
        "anschrift": anschrift,
        "telefon": _telefon(text, zeilen),
        "email": _email(text),
        "quelle": quelle,
    }
    # Ohne Anschrift und ohne E-Mail ist der Block fuer beide Kanaele nutzlos.
    if not daten["anschrift"] and not daten["email"]:
        return None
    return daten


def aus_lauf(lauf_verzeichnis: Path, domain: str) -> dict | None:
    """Liest den Impressumstext aus den Crawl-Daten eines Laufs."""
    datei = Path(lauf_verzeichnis) / f"{domain}_crawl.json"
    try:
        daten = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Kontaktdaten: %s nicht lesbar (%s)", datei, e)
        return None
    impressum = (daten.get("legal_subpages") or {}).get("impressum") or {}
    if not impressum.get("reachable"):
        return None
    return aus_impressum(impressum.get("text_content") or "", impressum.get("path"))
