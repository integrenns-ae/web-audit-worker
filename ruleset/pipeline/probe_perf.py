"""
pipeline/probe_perf.py

Layer 1 - Code. Bewusst OHNE separate Lighthouse-CLI-Installation gebaut:
Playwright liefert beim Crawl bereits Navigation- und Paint-Timing-API-Werte
sowie die vollstaendige Request-Liste. Das reicht fuer v1 aus und spart eine
schwere Zusatzabhaengigkeit (Node + Headless Chrome extra fuer Lighthouse).

Aufwertung auf echtes Lighthouse (fuer LCP/CLS/INP nach Core-Web-Vitals-Definition)
ist ein sauberer spaeterer Schritt, kein Rewrite - dieses Modul liesse sich 1:1
ersetzen, das Ausgabeformat (Dict mit denselben Schluesseln) bliebe gleich.
"""

import json
from pathlib import Path
from urllib.parse import urlparse


def probe_perf(crawl_result: dict, out_dir: Path) -> dict:
    domain = crawl_result["domain"]
    requests_log = crawl_result.get("requests", [])
    timing = crawl_result.get("timing", {})

    image_requests = [r for r in requests_log if r.get("resource_type") == "image"]
    script_requests = [r for r in requests_log if r.get("resource_type") == "script"]

    result = {
        "domain": domain,
        "first_contentful_paint_ms": timing.get("first_contentful_paint_ms"),
        "dom_content_loaded_ms": timing.get("dom_content_loaded_ms"),
        "transfer_size_bytes": timing.get("transfer_size_bytes"),
        "total_requests": len(requests_log),
        "image_requests": len(image_requests),
        "script_requests": len(script_requests),
        # Einzelne Bildgroessen sind aus requestfinished-Events in Playwright
        # nicht direkt verfuegbar (dafuer braucht es Response-Body-Zugriff) -
        # als v2-Ausbau vermerkt, siehe README.
        "large_image_over_1mb_detected": None,  # v2
    }

    out_path = out_dir / f"{domain}_probe_perf.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    import sys
    crawl_path = Path(sys.argv[1])
    crawl_result = json.loads(crawl_path.read_text())
    out = probe_perf(crawl_result, crawl_path.parent)
    print(json.dumps(out, indent=2, ensure_ascii=False))
