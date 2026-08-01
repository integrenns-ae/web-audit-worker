"""
pipeline/probe_network.py

Layer 1 - Code. Zwei Aufgaben:
1. Welche Drittanbieter-Requests laufen, ohne dass eine Consent-Interaktion
   stattgefunden hat (v1-Heuristik: JEDER Analytics/Tracking-Request beim
   initialen Seitenaufruf gilt als "vor Consent", da wir in v1 keinen
   Cookie-Banner-Klick simulieren).
2. Direkte HTML-Prüfungen, die kein separates Rendering brauchen: Viewport-Tag,
   Impressum/Datenschutz-Linktext, Copyright-Jahr, Platzhalter-Muster.

v2-Ausbau: Consent-Banner tatsächlich klicken und zwei Request-Logs (vorher/
nachher) vergleichen - siehe README.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

KNOWN_TRACKERS = {
    "google-analytics.com": "Google Analytics",
    "googletagmanager.com": "Google Tag Manager",
    "facebook.net": "Meta/Facebook Pixel",
    "connect.facebook.net": "Meta/Facebook Pixel",
    "doubleclick.net": "Google Ads/DoubleClick",
    "hotjar.com": "Hotjar",
    "maps.googleapis.com": "Google Maps",
    "maps.google.com": "Google Maps",
}

PLACEHOLDER_PATTERNS = [
    r"lorem ipsum",
    r"theme[- ]?credits?",
    r"edit with lovable",
]

# Platzhalter-Bilddateien (dummy/placeholder) werden separat und LAZY-LOAD-BEWUSST
# geprüft: Slider/Lazy-Load-Bibliotheken setzen dummy.png als src, während das
# echte Bild in data-lazyload/data-src steht - das ist KEIN echter Platzhalter.
_IMAGE_PLACEHOLDER_RE = re.compile(r"(?:dummy|placeholder)\.(?:png|jpg|jpeg)", re.I)
# Marker für Lazy-Load- oder Slider-Frameworks. Wenn IRGENDWO auf der Seite eines
# davon vorkommt, sind dummy/placeholder-Bilder technische Stand-ins (Slider
# Revolution liefert dummy.png genau dafür mit; das echte Bild lädt JS nach) -
# kein unfertiger Inhalt. Global geprüft, weil dieselbe Seite dummy.png sowohl in
# <img src> als auch in CSS background:url() an vielen Stellen einsetzt.
_LAZYLOAD_MARKERS = (
    "data-lazyload", "data-src", "data-original", "data-lazydone",
    "data-bgposition", "data-bg", "lazyload", "lazy-load", "lazyestload",
    "rev_slider", "revslider", "tp-banner", "tp-arr-", "tp-bullets", "tp-loader",
)

CONSTRUCTION_PATTERNS = [
    r"befindet sich (noch |gerade )?im aufbau",
    r"seite wird (gerade )?überarbeitet",
    r"under construction",
    r"coming soon",
    r"demnächst verfügbar",
]


def detect_trackers_before_consent(requests_log: list) -> list:
    found = []
    for req in requests_log:
        host = urlparse(req["url"]).netloc.lower()
        for needle, label in KNOWN_TRACKERS.items():
            if needle in host and label not in [f["label"] for f in found]:
                found.append({"label": label, "url": req["url"]})
    return found


def check_viewport(html: str) -> dict:
    m = re.search(r'<meta[^>]+name=["\']viewport["\'][^>]*content=["\']([^"\']+)["\']', html, re.I)
    if not m:
        return {"present": False, "blocks_zoom": False}
    content = m.group(1).lower()
    blocks_zoom = "user-scalable=no" in content.replace(" ", "") or "maximum-scale=1" in content.replace(" ", "")
    return {"present": True, "content": content, "blocks_zoom": blocks_zoom}


def check_legal_pages_linked(html: str) -> dict:
    lower = html.lower()
    return {
        "impressum_linked": bool(re.search(r'href=["\'][^"\']*impressum', lower)),
        "datenschutz_linked": bool(re.search(r'href=["\'][^"\']*datenschutz', lower)),
    }


def _real_image_placeholder(lower_html: str) -> bool:
    """True nur, wenn ein dummy/placeholder-Bild als ECHTER Platzhalter auftaucht -
    NICHT als Lazy-Loading-/Slider-Stand-in. Nutzt die Seite irgendwo ein Lazy-Load-
    oder Slider-Framework, sind dummy.png-Referenzen technische Stand-ins."""
    if not _IMAGE_PLACEHOLDER_RE.search(lower_html):
        return False
    if any(mark in lower_html for mark in _LAZYLOAD_MARKERS):
        return False  # Lazy-Load/Slider-Kontext -> kein unfertiger Inhalt
    return True


def check_placeholders(html: str) -> list:
    lower = html.lower()
    found = [pat for pat in PLACEHOLDER_PATTERNS if re.search(pat, lower)]
    if _real_image_placeholder(lower):
        found.append("dummy/placeholder-bild (kein lazy-load)")
    return found


def check_construction_notice(html: str) -> bool:
    lower = html.lower()
    return any(re.search(pat, lower) for pat in CONSTRUCTION_PATTERNS)


def check_copyright_year(html: str) -> dict:
    years = re.findall(r"(?:©|copyright)\s*(\d{4})", html, re.I)
    if not years:
        return {"found": False}
    year = max(int(y) for y in years)
    return {"found": True, "year": year, "older_than_2_years": (datetime.now().year - year) > 2}


def check_alt_texts(html: str) -> dict:
    img_tags = re.findall(r"<img\b[^>]*>", html, re.I)
    if not img_tags:
        return {"total_images": 0, "missing_alt": 0, "majority_missing": False}
    missing = sum(1 for tag in img_tags if not re.search(r'alt=["\'][^"\']+["\']', tag, re.I))
    return {
        "total_images": len(img_tags),
        "missing_alt": missing,
        "majority_missing": missing / len(img_tags) > 0.5,
    }


def probe_network(crawl_result: dict, out_dir: Path) -> dict:
    domain = crawl_result["domain"]
    html = crawl_result["html"]
    requests_log = crawl_result.get("requests", [])

    result = {
        "domain": domain,
        "trackers_before_consent": detect_trackers_before_consent(requests_log),
        "viewport": check_viewport(html),
        "legal_pages_linked": check_legal_pages_linked(html),
        "placeholders_found": check_placeholders(html),
        "construction_notice": check_construction_notice(html),
        "copyright": check_copyright_year(html),
        "alt_texts": check_alt_texts(html),
    }

    out_path = out_dir / f"{domain}_probe_network.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    import sys
    crawl_path = Path(sys.argv[1])
    crawl_result = json.loads(crawl_path.read_text())
    out = probe_network(crawl_result, crawl_path.parent)
    print(json.dumps(out, indent=2, ensure_ascii=False))

# Pflegehinweis (Nachhaltigkeit): KNOWN_TRACKERS, PLACEHOLDER_PATTERNS und
# CONSTRUCTION_PATTERNS sind bewusst als Listen ausgelagert (nicht verstreut im
# Code) - jedes neue Muster, das ein Audit von Hand findet, gehört hier ergänzt.
