"""
pipeline/crawl.py

Layer 1 - Code (deterministisch). Läuft mit echtem Netzwerkzugriff, also NICHT
in dieser Sandbox lauffähig - gedacht für den Hetzner-VPS oder lokale Ausführung.

Nutzt Playwright statt requests/BeautifulSoup, weil client-seitig gerenderte
Seiten (React/Vue-SPAs, z.B. integrenns.de) sonst als leere Huelle ankommen.

Output: crawl/<domain>.json mit:
  - html: das gerenderte HTML nach Netzwerk-Ruhe
  - requests: Liste aller Netzwerk-Requests (url, resource_type, size, status)
  - response_headers: Header der Hauptantwort
  - screenshot_path
  - timing: FCP und weitere Performance-Timings (siehe probe_perf.py)
"""

import asyncio
import os
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


async def crawl(url: str, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    domain = urlparse(url).netloc.lower().removeprefix("www.")

    requests_log = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # AUDIT_INSECURE_TLS=1 nur setzen, wenn die Umgebung einen TLS-terminierenden
        # Egress-Proxy mit eigener CA benutzt (z.B. die Claude-Sandbox). Auf dem
        # Hetzner-VPS/lokal NICHT setzen - dort gilt normale Zertifikatsprüfung.
        _insecure_tls = os.environ.get("AUDIT_INSECURE_TLS") == "1"
        context = await browser.new_context(
            ignore_https_errors=_insecure_tls,
            viewport={"width": 390, "height": 844},  # mobile-first, wie die Zielgruppe surft
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 "
                "IntegrennsAuditBot/1.0 (+https://integrenns.de/bot)"
            ),
        )
        page = await context.new_page()

        def on_request_finished(request):
            requests_log.append({
                "url": request.url,
                "resource_type": request.resource_type,
                "method": request.method,
                "timestamp": time.time(),
            })

        page.on("requestfinished", on_request_finished)

        response = await page.goto(url, wait_until="networkidle", timeout=30_000)
        main_headers = dict(response.headers) if response else {}
        main_status = response.status if response else None

        # kurze Wartezeit für spät nachladende Skripte (CSR-Apps)
        await page.wait_for_timeout(1500)

        html = await page.content()

        # Performance-Timings direkt aus der Navigation/Paint Timing API
        timing = await page.evaluate("""
            () => {
                const nav = performance.getEntriesByType('navigation')[0] || {};
                const paint = performance.getEntriesByType('paint');
                const fcp = paint.find(p => p.name === 'first-contentful-paint');
                return {
                    dom_content_loaded_ms: nav.domContentLoadedEventEnd || null,
                    load_event_ms: nav.loadEventEnd || null,
                    first_contentful_paint_ms: fcp ? fcp.startTime : null,
                    transfer_size_bytes: nav.transferSize || null,
                };
            }
        """)

        screenshot_path = out_dir / f"{domain}_screenshot.png"
        await page.screenshot(path=str(screenshot_path), full_page=True)

        # Wichtig für client-seitig geroutete SPAs (z.B. integrenns.de auf Lovable/
        # React): ein rohes requests.get(/impressum) sieht bei solchen Seiten oft nur
        # die leere App-Huelle. Playwright navigiert wie ein echter Browser und lässt
        # das Client-Routing tatsächlich greifen - deshalb hier zusätzlich per
        # Browser-Navigation prüfen, nicht nur per HTTP-Request in probe_infra.
        legal_subpages = {}
        candidate_paths = {
            "impressum": ["/impressum", "/impressum.html", "/legal-notice"],
            "datenschutz": ["/datenschutz", "/datenschutzerklaerung", "/privacy-policy"],
        }
        base = url.rstrip("/")
        for key, paths in candidate_paths.items():
            legal_subpages[key] = {"reachable": False, "path": None, "word_count": 0, "text_content": ""}
            for path in paths:
                try:
                    resp = await page.goto(base + path, wait_until="networkidle", timeout=15_000)
                    await page.wait_for_timeout(800)
                    text_only = await page.evaluate("() => document.body.innerText || ''")
                    word_count = len(text_only.split())
                    if resp and resp.status < 400 and word_count > 40:
                        legal_subpages[key] = {
                            "reachable": True, "path": path, "word_count": word_count,
                            "text_content": text_only,
                        }
                        break
                except Exception:
                    continue
        # zurueck zur Startseite navigieren, falls weitere Schritte im selben Context folgen
        await page.goto(url, wait_until="domcontentloaded", timeout=15_000)

        await browser.close()

    result = {
        "url": url,
        "domain": domain,
        "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "html": html,
        "main_status": main_status,
        "main_headers": main_headers,
        "requests": requests_log,
        "timing": timing,
        "screenshot_path": str(screenshot_path),
        "legal_subpages": legal_subpages,
    }

    out_path = out_dir / f"{domain}_crawl.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "https://integrenns.de"
    out = asyncio.run(crawl(target, Path("runs/_manual_crawl_test")))
    print(f"Crawled {out['domain']}: {len(out['requests'])} requests, "
          f"FCP={out['timing'].get('first_contentful_paint_ms')}ms")
