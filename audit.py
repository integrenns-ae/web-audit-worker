"""audit — Basis-Website-Audit (Integrations-Naht `run_audit`).

WICHTIG: `run_audit(url) -> result` ist die EINE Stelle, die die Regelwerk-Session
später durch die echte Score-Funktion (Ruleset) ersetzt. Rückgabeform ist der
verbindliche `result`-Vertrag (siehe Integrationsdoku 2026-07-28).

Dieser Basis-Audit ist ein ehrlicher erster Wurf (echte HTTP-Prüfungen), kein
vollwertiges Regelwerk – er liefert bereits gültige, kundentaugliche Ergebnisse.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

# Feste Maximalwerte je Kategorie (Nenner) – aus dem Vertrag. Summe = 100.
CATEGORY_MAX = {
    "technik_performance": 23,
    "mobil_zugaenglichkeit": 15,
    "recht": 20,
    "auffindbarkeit_seo": 15,
    "lokale_sichtbarkeit": 12,
    "inhalt_aktualitaet": 15,
}

RULESET_VERSION = "basis-2026-07-30"
UA = "integrenns-audit/1.0 (+https://integrenns.de/website-check)"
TIMEOUT = httpx.Timeout(15.0, connect=8.0)


class NotAuditableError(Exception):
    """Seite über https UND http nicht abrufbar."""


def _normalize(url: str) -> str:
    u = url.strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def _host(url: str) -> str:
    h = (urlparse(_normalize(url)).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


async def _fetch(client: httpx.AsyncClient, url: str):
    t0 = time.perf_counter()
    r = await client.get(url)
    elapsed = time.perf_counter() - t0
    return r, elapsed


async def run_audit(url: str) -> dict:
    """Führt den Basis-Audit aus. Wirft NotAuditableError, wenn unerreichbar."""
    start_url = _normalize(url)
    host = _host(start_url)

    findings: list[dict] = []
    not_applicable: list[dict] = []
    manual_review: list[dict] = []

    def add(cat: str, fid: str, impact: int, internal: str, customer: str):
        findings.append({
            "id": fid, "category": cat, "source": "rule",
            "score_impact": impact, "message_internal": internal, "message_customer": customer,
        })

    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT,
                                 headers={"User-Agent": UA}) as client:
        resp = None
        elapsed = 0.0
        used_http_fallback = False
        for candidate in (start_url, "http://" + host):
            try:
                resp, elapsed = await _fetch(client, candidate)
                if resp.status_code < 400:
                    used_http_fallback = candidate.startswith("http://")
                    break
            except Exception:
                resp = None
        if resp is None or resp.status_code >= 400:
            raise NotAuditableError("https+http lieferten Fehler")

        final_url = str(resp.url)
        html = resp.text
        soup = BeautifulSoup(html, "lxml")

        # ---- Technik & Performance (23) ----
        if not final_url.lower().startswith("https://") or used_http_fallback:
            add("technik_performance", "technik.no_https", -8,
                "Kein durchgängiges HTTPS (nur über http erreichbar)",
                "Ihre Seite läuft nicht durchgängig über eine sichere HTTPS-Verbindung – Besucher sehen ggf. eine Warnung.")
        if elapsed > 4.0:
            add("technik_performance", "technik.fcp_over_4s", -10,
                f"Antwortzeit {elapsed:.1f}s über 4s",
                "Die Seite braucht auf einem normalen Gerät spürbar lange, bis etwas sichtbar wird.")
        elif elapsed > 2.5:
            add("technik_performance", "technik.slow_response", -5,
                f"Antwortzeit {elapsed:.1f}s erhöht",
                "Die Seite lädt merklich langsam.")
        if not (resp.headers.get("content-encoding") or "").strip():
            add("technik_performance", "technik.no_compression", -3,
                "Keine HTTP-Komprimierung (content-encoding fehlt)",
                "Inhalte werden unkomprimiert ausgeliefert – das kostet unnötig Ladezeit.")

        # ---- Mobil & Zugänglichkeit (15) ----
        if not soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}):
            add("mobil_zugaenglichkeit", "mobil.no_viewport", -6,
                "Kein viewport-Meta-Tag",
                "Auf dem Smartphone wird die Seite nicht richtig angepasst dargestellt.")
        if not (soup.find("html") and soup.find("html").get("lang")):
            add("mobil_zugaenglichkeit", "mobil.no_lang", -3,
                "Kein lang-Attribut am <html>",
                "Die Seitensprache ist technisch nicht ausgezeichnet (schlecht für Vorlese-Hilfen und Suche).")
        manual_review.append({
            "id": "mobil.tap_targets_too_small", "category": "mobil_zugaenglichkeit",
            "message_internal": "Tap-Targets vermutlich zu klein (manuell zu prüfen)",
        })

        # Links/Text der Startseite (für Recht/Lokal/Inhalt)
        anchors = soup.find_all("a", href=True)
        text_lower = soup.get_text(" ", strip=True).lower()

        def find_link(*keywords):
            for a in anchors:
                hay = (a.get("href", "") + " " + a.get_text(" ", strip=True)).lower()
                if any(k in hay for k in keywords):
                    return urljoin(final_url, a["href"])
            return None

        # ---- Recht (20) ----
        imprint_url = find_link("impressum", "imprint", "legal")
        if not imprint_url:
            add("recht", "recht.no_imprint", -8,
                "Kein Impressum-Link gefunden",
                "Ein Impressum ist rechtlich Pflicht – ich habe auf der Seite keines gefunden.")
        if not find_link("datenschutz", "privacy"):
            add("recht", "recht.no_privacy", -6,
                "Kein Datenschutz-Link gefunden",
                "Eine Datenschutzerklärung ist Pflicht – ich habe keine gefunden.")
        if imprint_url:
            try:
                ir, _ = await _fetch(client, imprint_url)
                if ir.status_code < 400 and re.search(r"\b(TMG|RStV)\b", ir.text):
                    add("recht", "recht.outdated_legal_reference", -4,
                        "Impressum verweist auf abgelöste Normen (TMG/RStV statt DDG/MStV)",
                        "Das Impressum verweist auf inzwischen abgelöste Gesetzestexte.")
            except Exception:
                pass

        # ---- Auffindbarkeit / SEO (15) ----
        title = (soup.title.string if soup.title and soup.title.string else "").strip()
        if not title:
            add("auffindbarkeit_seo", "seo.no_title", -4,
                "Kein <title>", "Der Seitentitel für Google fehlt.")
        if not soup.find("meta", attrs={"name": re.compile("^description$", re.I)}):
            add("auffindbarkeit_seo", "seo.no_meta_description", -4,
                "Keine Meta-Description",
                "Die Kurzbeschreibung für Google-Suchergebnisse fehlt.")
        if not soup.find("link", attrs={"rel": re.compile("canonical", re.I)}):
            add("auffindbarkeit_seo", "seo.no_canonical", -2,
                "Kein canonical-Link", "Es fehlt die kanonische URL-Auszeichnung.")

        # sitemap + page_count
        page_count = {"sitemap_urls": 0, "internal_links_home": 0, "estimate": 0, "source": "home_links"}
        internal = 0
        for a in anchors:
            href = a["href"]
            if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
                continue
            if href.startswith("/") or host in href:
                internal += 1
        page_count["internal_links_home"] = internal
        sitemap_ok = False
        try:
            sm, _ = await _fetch(client, urljoin(final_url, "/sitemap.xml"))
            if sm.status_code < 400 and "<loc" in sm.text.lower():
                sitemap_ok = True
                n = len(re.findall(r"<loc>", sm.text, re.I))
                page_count.update(sitemap_urls=n, estimate=n, source="sitemap")
        except Exception:
            pass
        if not sitemap_ok:
            add("auffindbarkeit_seo", "seo.no_sitemap", -3,
                "Keine sitemap.xml", "Es fehlt eine Sitemap – Suchmaschinen finden nicht alle Unterseiten.")
            page_count["estimate"] = max(1, min(internal, 30))

        # ---- Lokale Sichtbarkeit (12) – Basis-Heuristik ----
        has_phone = bool(re.search(r"(\+?\d[\d /()\-]{6,}\d)", text_lower))
        has_plz = bool(re.search(r"\b\d{5}\b", text_lower))
        if not (has_phone or has_plz):
            add("lokale_sichtbarkeit", "lokale.no_contact_info", -5,
                "Keine Telefon/Adresse auf der Startseite erkannt",
                "Adresse und Telefon sind für die lokale Auffindbarkeit nicht klar sichtbar.")

        # ---- Inhalt & Aktualität (15) ----
        years = [int(y) for y in re.findall(r"\b(20\d{2})\b", text_lower)]
        cur = datetime.now(timezone.utc).year
        if years and max(years) < cur - 1:
            add("inhalt_aktualitaet", "inhalt.stale_content", -5,
                f"Jüngste Jahreszahl {max(years)} (>1 Jahr alt)",
                "Die Inhalte wirken seit längerem nicht aktualisiert.")
        if not any(k in text_lower for k in ("referenz", "portfolio", "projekte", "kundenstimmen", "bewertung")):
            not_applicable.append({
                "id": "inhalt.no_references_or_portfolio", "category": "inhalt_aktualitaet",
                "message_internal": "Keine Referenzen/Portfolio/Kundenstimmen auffindbar",
            })

    # ---- Kategorie-Scores aus Findings ableiten ----
    category_scores = {}
    for cat, mx in CATEGORY_MAX.items():
        deducted = sum(-f["score_impact"] for f in findings if f["category"] == cat)
        category_scores[cat] = max(0, mx - deducted)
    overall = sum(category_scores.values())

    return {
        "ruleset_version": RULESET_VERSION,
        "overall": overall,
        "domain": host,
        "business_locality": "unknown",
        "is_non_profit": False,
        "page_count": page_count,
        "category_scores": category_scores,
        "suppressed_categories": [],
        "suppressed_rules": [na["id"] for na in not_applicable],
        "findings": findings,
        "not_applicable": not_applicable,
        "manual_review_needed": manual_review,
        "unverified_external_links": [],
    }
