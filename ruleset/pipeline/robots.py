"""
pipeline/robots.py

Hoeflichkeits-Gate VOR dem Crawl: respektiert eine robots.txt, die unseren
Pruef-Bot fuer die GESAMTE Site aussperrt.

Bewusst eng gefasst: nur ein vollstaendiges "Disallow: /" fuer unseren Bot
(oder fuer "*") stoppt den Lauf. Teilpfad-Sperren (z.B. "Disallow: /admin")
werden hier NICHT ausgewertet - wir rufen ohnehin nur die Startseite und wenige
von dort verlinkte Seiten ab, und ein Betreiber, der eine Pruefung seiner
eigenen Seite beauftragt, soll nicht an einer /admin-Regel scheitern.

Fehlende, leere oder nicht erreichbare robots.txt => erlaubt. Wir sperren uns
nur dort aus, wo der Betreiber es ausdruecklich sagt.
"""

from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

# Muss zum User-Agent in pipeline/crawl.py passen (Produktname ohne Version).
BOT_NAME = "IntegrennsAuditBot"

# Der volle UA, mit dem wir auch die robots.txt selbst abholen - ein Betreiber
# soll uns in seinen Logs schon beim robots-Abruf erkennen koennen.
ROBOTS_FETCH_UA = f"{BOT_NAME}/1.0 (+https://integrenns.de/bot)"

FETCH_TIMEOUT_S = 8


def _robots_url(url: str) -> str:
    parts = urlparse(url if url.startswith("http") else "https://" + url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


def fetch_robots_txt(url: str) -> str | None:
    """robots.txt der Zieldomain holen. None, wenn es keine verwertbare gibt."""
    try:
        resp = requests.get(
            _robots_url(url),
            timeout=FETCH_TIMEOUT_S,
            headers={"User-Agent": ROBOTS_FETCH_UA},
        )
    except Exception:
        return None
    if resp.status_code != 200 or not resp.text.strip():
        return None
    return resp.text


def is_crawl_allowed(url: str) -> tuple[bool, str]:
    """(erlaubt, Begruendung) - False nur bei explizitem Voll-Disallow fuer uns.

    Die Begruendung ist fuer Log/Diagnose gedacht, nicht fuer Endkunden-Text.
    """
    body = fetch_robots_txt(url)
    if body is None:
        return True, "keine verwertbare robots.txt"

    parser = RobotFileParser()
    try:
        parser.parse(body.splitlines())
    except Exception:
        # Kaputte robots.txt ist kein Verbot.
        return True, "robots.txt nicht parsebar"

    # can_fetch("/") ist genau die Frage "ist die ganze Site gesperrt?" -
    # ein "Disallow: /admin" laesst "/" weiterhin zu.
    if parser.can_fetch(BOT_NAME, "/"):
        return True, "robots.txt erlaubt den Zugriff"

    return False, "robots.txt sperrt die gesamte Site fuer unseren Bot"
