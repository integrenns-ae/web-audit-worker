#!/usr/bin/env python3
"""
audit.py - manuell getriggerter Audit-Durchlauf für eine einzelne URL.

Nutzung:
    python audit.py https://integrenns.de

Läuft NICHT in einer netzwerk-eingeschränkten Sandbox - braucht echten
Zugriff auf die Zielseite (Playwright startet einen echten Chromium-Browser).
Gedacht für Ausführung auf dem Hetzner-VPS oder lokal.

Speichert alles unter runs/<domain>/<timestamp>/ - jeder Lauf bleibt erhalten,
damit compare.py spätere Läufe gegen frühere vergleichen kann (Vorher/Nachher).

Setup einmalig:
    pip install -r requirements.txt
    playwright install chromium --with-deps
"""

import asyncio
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from pipeline.crawl import crawl
from pipeline.robots import is_crawl_allowed
from pipeline.probe_infra import probe_infra
from pipeline.probe_perf import probe_perf
from pipeline.probe_network import probe_network
from pipeline.score import run_score
from pipeline.report import run_report


async def run_audit(url: str) -> Path:
    if not url.startswith("http"):
        url = "https://" + url

    # Hoeflichkeits-Gate: sperrt die robots.txt unseren Bot komplett aus, wird
    # KEINE Seite abgerufen. Steht bewusst ganz vorn - vor dem Crawl UND vor dem
    # Anlegen des Lauf-Ordners, damit eine abgelehnte Domain keinerlei Spur
    # hinterlaesst (wir versprechen "kein einziger Request").
    allowed, robots_reason = is_crawl_allowed(url)
    if not allowed:
        print()
        print(f"ROBOTS DISALLOW: {robots_reason} - es wird keine Seite abgerufen "
              f"und bewusst KEIN Score erzeugt.")
        return None

    domain = urlparse(url).netloc.lower().removeprefix("www.")
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = Path("runs") / domain / timestamp
    run_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] Crawle {url} ...")
    crawl_result = await crawl(url, run_dir)

    # Schema-Fallback + Fehlerseiten-Schutz: Antwortet die Startseite nicht sauber
    # (Timeout/None oder 4xx/5xx), das andere Schema versuchen. So wird z.B. eine
    # kaputte HTTPS-Seite (503), die per http funktioniert, korrekt über http
    # auditiert - statt die Fehlerseite zu bewerten.
    status = crawl_result.get("main_status")
    if status is None or status >= 400:
        scheme = urlparse(url).scheme
        alt = (url.replace("https://", "http://", 1) if scheme == "https"
               else url.replace("http://", "https://", 1))
        print(f"      Startseite antwortet mit Status {status} über {scheme} "
              f"- versuche {urlparse(alt).scheme} ...")
        alt_result = await crawl(alt, run_dir)
        alt_status = alt_result.get("main_status")
        if alt_status is not None and alt_status < 400:
            url, crawl_result, status = alt, alt_result, alt_status
            print(f"      OK über {urlparse(alt).scheme} (Status {alt_status}).")

    # Beide Schemata unbrauchbar -> NICHT scoren (sonst Müll-Score aus Fehlerseite).
    if status is None or status >= 400:
        print()
        print(f"NICHT AUDITIERBAR: Startseite liefert Status {status} (auch über das "
              f"alternative Schema). Server blockiert automatisierte Zugriffe oder ist "
              f"offline - es wird bewusst KEIN Score erzeugt.")
        return None

    print(f"      {len(crawl_result['requests'])} Requests, "
          f"FCP={crawl_result['timing'].get('first_contentful_paint_ms')}ms")

    print("[2/5] Prüfe Infrastruktur (TLS, DNS, CMS, Rechtsseiten, robots/sitemap) ...")
    probe_infra(crawl_result, run_dir)

    print("[3/5] Prüfe Performance-Kennzahlen ...")
    probe_perf(crawl_result, run_dir)

    print("[4/5] Prüfe Netzwerk/Inhalt (Tracker, Viewport, Platzhalter, Copyright) ...")
    probe_network(crawl_result, run_dir)

    print("[5/5] Berechne Score und erzeuge Bericht ...")
    score_result = run_score(run_dir)
    report_path = run_report(run_dir)

    print()
    print(f"Gesamtscore: {score_result['overall']} / 100")
    print(f"Bericht: {report_path}")
    print(f"Alle Rohdaten: {run_dir}/")
    return run_dir


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Nutzung: python audit.py <url>")
        sys.exit(1)
    result = asyncio.run(run_audit(sys.argv[1]))
    sys.exit(0 if result else 2)
