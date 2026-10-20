"""audit — Integrationsnaht zum Regelwerk (ruleset/, v2026-07-27.x).

run_audit(url) ruft das Regelwerk pro Audit als SUBPROZESS auf
(`python audit.py <url>` im Ruleset-Verzeichnis). Der Subprozess isoliert
Playwright/Chromium sauber je Lauf (kein Event-Loop-/Browser-Konflikt mit
dem FastAPI-Prozess) und lässt sich hart per Timeout beenden.

Danach wird das erzeugte `<domain>_score_result.json` gelesen und als Dict
zurückgegeben (Vertrag). Wirft NotAuditableError, wenn die Seite über https
UND http nicht sauber antwortet (Regelwerk gibt dann None zurück und schreibt
kein Ergebnis).

Hinweis (Notiz aus dem Handoff): Der Datei-Umweg über score_result.json ist
bewusst der erste, robuste Wurf. Später kann man run_score(run_dir) direkt
importieren und das Dict ohne Datei zurückgeben.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import kontakt

log = logging.getLogger("audit")

RULESET_DIR = os.environ.get("RULESET_DIR", "/opt/ruleset")
AUDIT_TIMEOUT_S = int(os.environ.get("AUDIT_TIMEOUT_S", "120"))

# Crawl-/Netzwerk-Marker: nicht aufloesbare Domain, Timeout, TLS, Verbindung.
# Das ist KEIN interner Fehler -> nicht_auditierbar (Tippfehler, Seite offline).
_NET_MARKERS = (
    "ERR_NAME_NOT_RESOLVED", "ERR_CONNECTION", "ERR_ABORTED", "ERR_TIMED_OUT",
    "ERR_ADDRESS_UNREACHABLE", "ERR_SSL", "ERR_CERT", "ERR_HTTP2",
    "net::", "TimeoutError", "Page.goto",
)


class NotAuditableError(Exception):
    """Seite über https UND http nicht sauber abrufbar."""


# Je Domain hoechstens ein Lauf gleichzeitig. [Sperre, Zaehler] je Domain.
_domain_sperren: dict[str, list] = {}
_sperren_guard = asyncio.Lock()


@asynccontextmanager
async def _nur_einmal_pro_domain(domain: str):
    """Serialisiert Laeufe derselben Domain.

    Zwei Gruende, einer davon ist nicht offensichtlich:

    1. Die fremde Seite wird nie gleichzeitig doppelt gecrawlt.
    2. Beim Loeschen der Rohdaten (keep_raw=False) ist dadurch eindeutig,
       welche Lauf-Verzeichnisse zu DIESEM Lauf gehoeren. Ohne die Sperre
       koennte ein Agentenlauf die Rohdaten eines parallel laufenden
       Trichter-Laufs mitloeschen - und genau die sind die Vorher/Nachher-
       Grundlage fuer compare.py.
    """
    async with _sperren_guard:
        eintrag = _domain_sperren.get(domain)
        if eintrag is None:
            eintrag = [asyncio.Lock(), 0]
            _domain_sperren[domain] = eintrag
        eintrag[1] += 1
    sperre = eintrag[0]
    await sperre.acquire()
    try:
        yield
    finally:
        sperre.release()
        async with _sperren_guard:
            eintrag[1] -= 1
            if eintrag[1] <= 0:
                _domain_sperren.pop(domain, None)


def _laufverzeichnisse(domain: str) -> set:
    basis = Path(RULESET_DIR) / "runs" / domain
    if not basis.is_dir():
        return set()
    try:
        return {p for p in basis.iterdir() if p.is_dir()}
    except OSError:
        return set()


def _rohdaten_loeschen(domain: str, vorher: set) -> None:
    """Loescht die Lauf-Verzeichnisse, die in DIESEM Lauf entstanden sind.

    Das KOMPLETTE Verzeichnis, nicht einzelne Dateien: Die Messwerte stehen
    schon in jobs.result, alles uebrige ist redundant - und
    <domain>_crawl.json enthaelt das vollstaendige HTML samt Impressumstext.

    Der leere Domain-Ordner geht mit. Sonst waere runs/ nach einem Jahr ein
    Verzeichnis fremder Firmennamen, auch wenn kein Byte Inhalt mehr darin
    liegt.
    """
    for verzeichnis in _laufverzeichnisse(domain) - vorher:
        shutil.rmtree(verzeichnis, ignore_errors=True)
        log.info("Rohdaten verworfen: %s", verzeichnis)
    basis = Path(RULESET_DIR) / "runs" / domain
    try:
        if basis.is_dir() and not any(basis.iterdir()):
            basis.rmdir()
    except OSError:
        pass


def _normalize(url: str) -> str:
    u = url.strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _host(url: str) -> str:
    h = (urlparse(_normalize(url)).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _ruleset_domain(url: str) -> str:
    """Domain-Ordnername genau wie das Regelwerk ihn bildet (netloc ohne www)."""
    u = url if url.startswith("http") else "https://" + url
    return urlparse(u).netloc.lower().removeprefix("www.")


async def run_audit(url: str, *, kontaktdaten: bool = False,
                    keep_raw: bool = True) -> dict:
    domain = _ruleset_domain(url)
    if not domain:
        raise NotAuditableError("Ungültige URL")
    async with _nur_einmal_pro_domain(domain):
        # Bestand VOR dem Lauf merken, damit am Ende nur die eigenen
        # Verzeichnisse geloescht werden.
        vorher = _laufverzeichnisse(domain) if not keep_raw else set()
        try:
            return await _lauf(url, domain, kontaktdaten)
        finally:
            # Im finally, nicht im Erfolgsfall: Bei "NICHT AUDITIERBAR" kehrt
            # das Regelwerk zurueck, NACHDEM der Crawl HTML und Screenshot
            # geschrieben hat. Ohne das blieben genau die Faelle liegen, die
            # nie ein Ergebnis erzeugt haben.
            if not keep_raw:
                await asyncio.to_thread(_rohdaten_loeschen, domain, vorher)


async def _lauf(url: str, domain: str, kontaktdaten: bool) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "python", "audit.py", url,
        cwd=RULESET_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=AUDIT_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        raise NotAuditableError("Zeitüberschreitung beim Prüfen der Seite")

    text = out.decode("utf-8", "replace")

    # Zielseite sperrt Prüf-Bots per robots.txt aus. Kein Fehler, sondern ein
    # respektierter Wunsch -> eigener Grund, den das Frontend erkennt.
    if "ROBOTS DISALLOW" in text:
        raise NotAuditableError("robots_disallow")

    # Regelwerk signalisiert Unerreichbarkeit explizit und schreibt kein Ergebnis.
    if "NICHT AUDITIERBAR" in text:
        raise NotAuditableError("https+http lieferten Fehler")

    # Jüngstes score_result.json für diese Domain einlesen.
    pattern = os.path.join(RULESET_DIR, "runs", domain, "*", f"{domain}_score_result.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if proc.returncode == 0 and files:
        ergebnis_datei = Path(files[-1])
        with open(ergebnis_datei, encoding="utf-8") as fh:
            ergebnis = json.load(fh)
        if kontaktdaten:
            # MUSS hier passieren - gleich danach koennen die Rohdaten
            # geloescht werden. Es wird keine zusaetzliche Seite abgerufen.
            ergebnis["absender_kandidat"] = kontakt.aus_lauf(
                ergebnis_datei.parent, domain)
        return ergebnis

    # Kein Ergebnis: Crawl-/Netzwerkfehler (nicht auflösbar, Timeout, TLS …)
    # -> nicht_auditierbar, NICHT als interner Fehler.
    if any(m in text for m in _NET_MARKERS):
        raise NotAuditableError("Seite nicht erreichbar")

    # Echter, unerwarteter Fehler -> loggen (für Diagnose) und failed.
    log.warning("Audit rc=%s ohne Ergebnis für %s. Ausgabe (Ende):\n%s",
                proc.returncode, domain, text[-1200:])
    raise RuntimeError(f"Audit fehlgeschlagen (rc={proc.returncode})")
