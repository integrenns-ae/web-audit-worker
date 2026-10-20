"""kompakt — verdichtete Antwort für Maschinen-Mandanten.

Das volle Audit-Ergebnis ist für eine schlechte Seite rund **7 kB** (gemessen,
nicht geschätzt). Beim Akquise-Agenten landet diese Antwort als Text in einem
Modellaufruf. Dort ist fast alles davon Verschwendung: `manual_review_needed`,
`not_applicable`, `suppressed_*` und die internen Regelbezeichnungen helfen
beim Schreiben eines Anschreibens nicht.

Zwei Entscheidungen sind hier wichtiger als die Größenersparnis:

1. **Nur `message_customer` geht nach draußen.** Das Regelwerk führt zu jedem
   Befund zwei Fassungen: eine interne und eine kundentaugliche. Käme die
   interne in den Modellkontext, würde der Agent sie irgendwann in einem
   Anschreiben zitieren.
2. **Kategorien kommen als `[erreicht, maximal]`.** `recht: 0` allein ist
   nicht lesbar; `recht: [0, 20]` ist ein Totalausfall, `lokale_sichtbarkeit:
   [7, 12]` nur eine Schwäche. Ohne das Maximum müsste der Agent die
   Gewichtstabelle kennen.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("audit-worker.kompakt")

MAX_FINDINGS = 3
MAX_TEXT = 300

# Fallback, falls ruleset.yaml nicht lesbar ist. Dass dieser Stand mit dem
# Regelwerk übereinstimmt, prüft ein Test - sonst würde eine Gewichtsänderung
# hier unbemerkt veralten.
GEWICHTE_FALLBACK = {
    "technik_performance": 23,
    "mobil_zugaenglichkeit": 15,
    "recht": 20,
    "auffindbarkeit_seo": 15,
    "lokale_sichtbarkeit": 12,
    "inhalt_aktualitaet": 15,
}

_gewichte_cache: dict[str, int] | None = None


def gewichte() -> dict[str, int]:
    """Kategoriegewichte aus dem Regelwerk, mit Fallback."""
    global _gewichte_cache
    if _gewichte_cache is not None:
        return _gewichte_cache
    pfad = Path(os.environ.get("RULESET_DIR", "/opt/ruleset")) / "ruleset.yaml"
    try:
        import yaml

        daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
        gelesen = daten.get("category_weights") if isinstance(daten, dict) else None
        if isinstance(gelesen, dict) and gelesen:
            _gewichte_cache = {str(k): int(v) for k, v in gelesen.items()}
            return _gewichte_cache
        log.warning("%s hat keine category_weights - benutze Fallback", pfad)
    except Exception as e:  # noqa: BLE001
        log.warning("%s nicht lesbar (%s) - benutze Fallback", pfad, e)
    _gewichte_cache = dict(GEWICHTE_FALLBACK)
    return _gewichte_cache


def reload() -> None:
    """Cache verwerfen (fuer Tests)."""
    global _gewichte_cache
    _gewichte_cache = None


def _zeitstempel(epoche: float | None) -> str | None:
    if not epoche:
        return None
    return datetime.fromtimestamp(epoche, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _kuerzen(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_TEXT:
        return text
    # An der letzten Wortgrenze abschneiden, damit kein Wort zerfetzt wird.
    schnitt = text[:MAX_TEXT].rsplit(" ", 1)[0].rstrip(" ,;:.")
    return (schnitt or text[:MAX_TEXT]) + "…"


def _schwaechste(kategorien: dict[str, list[int]]) -> str | None:
    """Relativ zum Gewicht, nicht nach absoluten Punkten.

    Sonst waere `lokale_sichtbarkeit: [5, 12]` schlechter als
    `recht: [6, 20]` - obwohl Letzteres der groessere Hebel ist. Bei
    Gleichstand gewinnt die Kategorie mit dem groesseren Gewicht.
    """
    if not kategorien:
        return None
    def schluessel(eintrag):
        name, (erreicht, maximal) = eintrag
        anteil = erreicht / maximal if maximal else 1.0
        return (anteil, -maximal, name)
    return min(kategorien.items(), key=schluessel)[0]


def verdichten(result: dict, *, geprueft_am: float | None = None,
               cached: bool = False) -> dict:
    """Volles Audit-Ergebnis -> kompakte Antwort."""
    gew = gewichte()

    kategorien: dict[str, list[int]] = {}
    for name, erreicht in (result.get("category_scores") or {}).items():
        try:
            kategorien[str(name)] = [int(erreicht), int(gew.get(str(name), 0))]
        except (TypeError, ValueError):
            continue

    befunde = []
    for f in result.get("findings") or []:
        if not isinstance(f, dict):
            continue
        text = f.get("message_customer")
        # Ohne kundentaugliche Fassung bleibt der Befund draussen. Die interne
        # Fassung ist hier bewusst KEIN Ersatz.
        if not isinstance(text, str) or not text.strip():
            continue
        wirkung = f.get("score_impact")
        befunde.append({
            "kategorie": f.get("category"),
            "gewicht": wirkung if isinstance(wirkung, (int, float)) else 0,
            "text": _kuerzen(text),
        })
    befunde.sort(key=lambda b: b["gewicht"])

    antwort = {
        "status": "done",
        "domain": result.get("domain"),
        "overall": result.get("overall"),
        "kategorien": kategorien,
        "schwaechste_kategorie": _schwaechste(kategorien),
        "top_findings": befunde[:MAX_FINDINGS],
        "befunde_gesamt": len(befunde),
        "seiten_geschaetzt": result.get("page_count"),
        "geschaeftstyp": result.get("business_locality"),
        "non_profit": bool(result.get("is_non_profit")),
        "regelwerk": result.get("ruleset_version"),
        "geprueft_am": _zeitstempel(geprueft_am),
        "cached": bool(cached),
    }
    return antwort
