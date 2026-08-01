"""
pipeline/probe_infra.py

Layer 1 - Code. Prueft TLS-Zertifikat, DNS-Aufloesung, CMS/Framework-Fingerprint
und ob robots.txt/sitemap.xml erreichbar sind. Braucht ebenfalls echten
Netzwerkzugriff (ssl-Handshake, HTTP-Requests) - nicht in dieser Sandbox lauffaehig.
"""

import json
import re
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path

import requests

# bekannte Fingerprints: (Suchstring im HTML/Headern) -> Label
CMS_FINGERPRINTS = {
    "wp-content": "WordPress",
    "wp-includes": "WordPress",
    "Joomla!": "Joomla",
    "/media/jui/": "Joomla",
    "Drupal.settings": "Drupal",
    "lovable-tagger": "Lovable (React/Vite SPA)",
    "lovable.dev": "Lovable (React/Vite SPA)",
    "wix.com": "Wix",
    "jimdo": "Jimdo",
    "squarespace": "Squarespace",
}

# Versionen mit bekannten kritischen CVEs - Liste ist bewusst klein gehalten,
# soll regelmaessig gepflegt werden (siehe Kommentar am Ende der Datei)
KNOWN_VULNERABLE_PATTERNS = [
    "wp-content/themes/twentyseventeen",  # Platzhalter-Beispiel, echte Liste ergaenzen
]


def check_tls(domain: str) -> dict:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((domain, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
                not_after = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
                not_after = not_after.replace(tzinfo=timezone.utc)
                return {
                    "valid": True,
                    "expires_at": not_after.isoformat(),
                    "expired": not_after < datetime.now(timezone.utc),
                }
    except Exception as e:
        return {"valid": False, "error": str(e), "expired": None}


def check_dns(domain: str) -> dict:
    try:
        ip = socket.gethostbyname(domain)
        return {"resolves": True, "ip": ip}
    except Exception as e:
        return {"resolves": False, "error": str(e)}


def check_robots_sitemap(base_url: str) -> dict:
    result = {"robots_txt": False, "sitemap_xml": False}
    for path, key in (("/robots.txt", "robots_txt"), ("/sitemap.xml", "sitemap_xml")):
        try:
            resp = requests.get(base_url.rstrip("/") + path, timeout=8)
            result[key] = resp.status_code == 200 and len(resp.text.strip()) > 0
        except Exception:
            result[key] = False
    return result


# Häufige Pfad-Varianten für die beiden Pflichtseiten. Eine reine "ist verlinkt"-
# Prüfung reicht nicht (kaputte/relative Links, JS-Routing) - hier wird aktiv
# nachgeschaut, ob unter einem der üblichen Pfade tatsächlich Inhalt liegt.
LEGAL_PAGE_PATH_CANDIDATES = {
    "impressum": ["/impressum", "/impressum.html", "/impressum.php", "/de/impressum", "/legal-notice"],
    "datenschutz": ["/datenschutz", "/datenschutz.html", "/datenschutz.php", "/de/datenschutz", "/privacy-policy", "/datenschutzerklaerung"],
}


def check_legal_pages_reachable(base_url: str) -> dict:
    result = {}
    for key, paths in LEGAL_PAGE_PATH_CANDIDATES.items():
        reachable = False
        found_path = None
        for path in paths:
            try:
                resp = requests.get(base_url.rstrip("/") + path, timeout=8, allow_redirects=True)
                if resp.status_code == 200 and len(resp.text.strip()) > 200:
                    reachable = True
                    found_path = path
                    break
            except Exception:
                continue
        result[key] = {"reachable": reachable, "path": found_path}
    return result


def detect_cms(html: str, headers: dict) -> dict:
    haystack = html + " " + " ".join(f"{k}:{v}" for k, v in headers.items())
    matches = [label for needle, label in CMS_FINGERPRINTS.items() if needle.lower() in haystack.lower()]
    vulnerable = any(pat.lower() in haystack.lower() for pat in KNOWN_VULNERABLE_PATTERNS)
    return {
        "detected": sorted(set(matches)) or ["unbekannt / vermutlich Individualentwicklung"],
        "known_vulnerable_version": vulnerable,
    }


def check_external_links(html: str, own_domain: str, max_links: int = 20) -> list:
    """Prüft bis zu max_links externe Links per HEAD-Request auf Fehlerstatus.
    Absichtlich gedeckelt, um den Audit nicht durch hunderte Links zu verlangsamen -
    das ist ein Stichprobenverfahren, keine vollständige Linkprüfung."""
    import re
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    external = [h for h in hrefs if h.startswith("http") and own_domain not in h]
    external = list(dict.fromkeys(external))[:max_links]  # dedupe, Reihenfolge erhalten

    dead = []
    for link in external:
        try:
            resp = requests.head(link, timeout=6, allow_redirects=True)
            if resp.status_code >= 400:
                dead.append({"url": link, "status": resp.status_code})
        except Exception as e:
            dead.append({"url": link, "status": None, "error": str(e)})
    return dead


def _page_locs(xml_text: str) -> set:
    """Seiten-URLs aus einer Sitemap; Bild-/PDF-/Asset-URLs werden ausgefiltert."""
    locs = re.findall(r"<url>.*?<loc>\s*([^<\s]+)\s*</loc>", xml_text, re.I | re.S)
    if not locs:
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml_text, re.I | re.S)
    return {u for u in locs
            if not re.search(r"\.(jpg|jpeg|png|gif|webp|svg|pdf|zip|xml|css|js)(\?|$)", u, re.I)}


def _collect_sitemap_urls(base_url: str, max_sub: int = 12) -> int:
    """Anzahl Seiten laut Sitemap. Löst Sitemap-Index-Dateien (die auf Unter-
    Sitemaps zeigen) auf. Gibt None zurück, wenn keine Sitemap erreichbar ist."""
    def fetch(url):
        try:
            r = requests.get(url, timeout=8)
            return r.text if r.status_code == 200 and r.text.strip() else None
        except Exception:
            return None

    root = fetch(base_url.rstrip("/") + "/sitemap.xml") or fetch(base_url.rstrip("/") + "/sitemap_index.xml")
    if not root:  # robots.txt kann auf eine abweichende Sitemap verweisen
        try:
            rb = requests.get(base_url.rstrip("/") + "/robots.txt", timeout=8)
            m = re.search(r"(?i)sitemap:\s*(\S+)", rb.text)
            if m:
                root = fetch(m.group(1).strip())
        except Exception:
            pass
    if not root:
        return None

    if re.search(r"<sitemapindex", root, re.I):
        subs = re.findall(r"<sitemap>.*?<loc>\s*([^<\s]+)\s*</loc>", root, re.I | re.S)
        urls = set()
        for loc in subs[:max_sub]:
            body = fetch(loc.strip())
            if body:
                urls.update(_page_locs(body))
        return len(urls) or None
    return len(_page_locs(root)) or None


def _count_internal_page_links(html: str, domain: str) -> int:
    """Anzahl unterschiedlicher interner Seiten, die von der Startseite verlinkt
    sind (Navigation + Body). Untergrenze für die Größe des Auftritts."""
    paths = set()
    for h in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        h = h.strip()
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        if h.startswith("http"):
            if domain not in h:
                continue
            path = re.sub(r"^https?://[^/]+", "", h)
        else:
            path = h
        path = path.split("#")[0].split("?")[0].rstrip("/") or "/"
        if re.search(r"\.(jpg|jpeg|png|gif|webp|svg|pdf|zip|css|js|ico|mp4|woff2?)$", path, re.I):
            continue
        paths.add(path.lower())
    return len(paths)


def estimate_page_count(base_url: str, html: str, domain: str) -> dict:
    """Schätzt die Zahl einzelner Seiten des Web-Auftritts. Informativ (kein
    Abzug). Sitemap zählt als vollständigste Quelle, sonst die Startseiten-Links."""
    result = {"sitemap_urls": None, "internal_links_home": None,
              "estimate": None, "source": None}
    try:
        result["sitemap_urls"] = _collect_sitemap_urls(base_url)
    except Exception:
        pass
    try:
        result["internal_links_home"] = _count_internal_page_links(html, domain)
    except Exception:
        pass
    if result["sitemap_urls"]:
        result["estimate"], result["source"] = result["sitemap_urls"], "sitemap"
    elif result["internal_links_home"] is not None:
        result["estimate"], result["source"] = result["internal_links_home"], "startseiten-links"
    return result


def probe_infra(crawl_result: dict, out_dir: Path) -> dict:
    domain = crawl_result["domain"]
    base_url = crawl_result["url"]

    result = {
        "domain": domain,
        "tls": check_tls(domain),
        "dns": check_dns(domain),
        "robots_sitemap": check_robots_sitemap(base_url),
        "legal_pages": check_legal_pages_reachable(base_url),
        "cms": detect_cms(crawl_result["html"], crawl_result.get("main_headers", {})),
        "dead_external_links": check_external_links(crawl_result["html"], domain),
        "page_count": estimate_page_count(base_url, crawl_result["html"], domain),
    }

    out_path = out_dir / f"{domain}_probe_infra.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    import sys
    crawl_path = Path(sys.argv[1])
    crawl_result = json.loads(crawl_path.read_text())
    out = probe_infra(crawl_result, crawl_path.parent)
    print(json.dumps(out, indent=2, ensure_ascii=False))

# Pflegehinweis (Nachhaltigkeit): CMS_FINGERPRINTS und KNOWN_VULNERABLE_PATTERNS
# sind Wörterbücher, die über die Zeit wachsen sollen - jedes Mal, wenn ein Audit
# eine neue Software/Version erkennt, hier ergänzen statt im Code zu verstreuen.
