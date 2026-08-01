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
from urllib.parse import urlparse

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


async def run_audit(url: str) -> dict:
    domain = _ruleset_domain(url)
    if not domain:
        raise NotAuditableError("Ungültige URL")

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

    # Regelwerk signalisiert Unerreichbarkeit explizit und schreibt kein Ergebnis.
    if "NICHT AUDITIERBAR" in text:
        raise NotAuditableError("https+http lieferten Fehler")

    # Jüngstes score_result.json für diese Domain einlesen.
    pattern = os.path.join(RULESET_DIR, "runs", domain, "*", f"{domain}_score_result.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if proc.returncode == 0 and files:
        with open(files[-1], encoding="utf-8") as fh:
            return json.load(fh)

    # Kein Ergebnis: Crawl-/Netzwerkfehler (nicht auflösbar, Timeout, TLS …)
    # -> nicht_auditierbar, NICHT als interner Fehler.
    if any(m in text for m in _NET_MARKERS):
        raise NotAuditableError("Seite nicht erreichbar")

    # Echter, unerwarteter Fehler -> loggen (für Diagnose) und failed.
    log.warning("Audit rc=%s ohne Ergebnis für %s. Ausgabe (Ende):\n%s",
                proc.returncode, domain, text[-1200:])
    raise RuntimeError(f"Audit fehlgeschlagen (rc={proc.returncode})")
