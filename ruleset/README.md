# Audit-Pipeline (v1 — manuell getriggert)

Automatisierter Website-Audit: Technik, Recht, lokale Sichtbarkeit, Inhalt.
Score ist deterministisch (Code, kein LLM) und reproduzierbar — derselbe
Crawl-Stand ergibt immer denselben Score. Das ist Absicht, siehe `ruleset.yaml`.

## Wichtig: Läuft NICHT in eingeschränkten Sandboxen

Playwright startet einen echten Chromium-Browser und braucht Zugriff auf die
Zielseite. Ausführen auf deinem Hetzner-VPS oder lokal — nicht in einer
Umgebung mit eingeschränktem Netzwerk-Egress.

## Setup (einmalig)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium --with-deps
```

## Nutzung

Einen Audit durchführen:
```bash
python audit.py https://integrenns.de
```

Ergebnis liegt danach unter `runs/integrenns.de/<timestamp>/`:
- `<domain>_crawl.json` — rohes HTML, Requests, Timings, Screenshot
- `<domain>_probe_infra.json` — TLS, DNS, CMS, Rechtsseiten-Erreichbarkeit, tote Links
- `<domain>_probe_perf.json` — Performance-Kennzahlen
- `<domain>_probe_network.json` — Tracker, Viewport, Platzhalter, Copyright
- `<domain>_score_result.json` — Kategoriewerte, Gesamtscore, Findings
- `<domain>_report.md` — lesbarer Bericht

Nach Verbesserungen an der Seite erneut auditieren:
```bash
python audit.py https://integrenns.de
```

Beide Läufe vergleichen (Vorher/Nachher):
```bash
python compare.py integrenns.de
```
Zeigt Score-Delta pro Kategorie sowie behobene/neue/weiterhin offene Findings.

## Tests (laufen überall, kein Netzwerk nötig)

```bash
pip install pytest
pytest tests/ -v
```

Die Tests prüfen ausschließlich `score.py` gegen feste Fixture-Daten unter
`tests/fixtures/` — nicht die Crawl-/Probe-Module selbst, die brauchen echtes
Netzwerk und werden hier bewusst nicht gemockt (Mocking von Playwright bringt
wenig Vertrauen; besser echt gegen eine Testseite laufen lassen).

**`test_all_automated_rules_have_check_functions` ist der wichtigste Test**:
er schlägt fehl, sobald `ruleset.yaml` eine Regel als `automated: true` markiert,
für die keine Prüf-Funktion in `score.py` existiert. Das verhindert die stille
Lücke zwischen "Regelwerk verspricht" und "Code liefert tatsächlich".

## Aktueller Stand: automatisiert vs. manuell

37 Regeln insgesamt, davon **30 automatisiert**, **7 noch manuell** zu prüfen
(im Bericht unter "Noch manuell zu prüfen" aufgelistet, fließen nicht in den
Score ein):

| Regel | Warum noch manuell |
|---|---|
| `mobil.tap_targets_too_small` | braucht Layout-Messung gerenderter Elemente |
| `mobil.low_contrast_text` | braucht Farbkontrast-Berechnung auf gerenderten Farben |
| `recht.form_without_privacy_notice` | braucht Formularerkennung + Textnähe-Analyse |
| `seo.no_location_in_title_h1` | braucht einen Standort-Parameter pro Kunde (Ort ist nicht generisch ableitbar) |
| `seo.nap_inconsistent` | braucht Places-API-Abgleich |
| `seo.no_google_business_profile` | braucht Places-API-Abgleich |
| `inhalt.thin_content_page` | braucht Mehrseiten-Crawl der ganzen Domain (v1 crawlt nur die Startseite + Rechtsseiten) |

## Nächste Ausbaustufen (nicht Teil von v1)

1. **Mehrseiten-Crawl** — `crawl.py` folgt aktuell nur Impressum/Datenschutz
   gezielt. Ein echter Sitemap-/Link-basierter Crawl über ~20-30 Seiten würde
   `inhalt.thin_content_page` automatisierbar machen.
2. **Places-API-Anbindung** — für `seo.nap_inconsistent` und
   `seo.no_google_business_profile`.
3. **Echtes Lighthouse** statt der Playwright-eigenen Timing-Werte, für
   präzise Core-Web-Vitals (LCP/CLS/INP nach offizieller Definition).
4. **LLM-Schicht obendrauf** (siehe frühere Pipeline-Beschreibung: `extract-business`,
   `detect-stale`, `phrase-findings`) — wandelt die technischen Rohbefunde in
   kundentaugliche Sprache um. Bewusst NICHT Teil dieser ersten Stufe, weil
   Score und Findings zuerst als deterministisches Fundament stehen sollten,
   bevor Sprachmodell-Schichten obendrauf kommen.
5. **Automatisierung** (Timer-Trigger, Formular-Webhook, Postgres-Zustand,
   Freigabe-Gates) — bewusst noch nicht Teil dieser Version, da explizit
   "erst einmal manuell getriggert" gewünscht war.

## Regelwerk pflegen

`ruleset.yaml` ist die einzige Quelle für Gewichte/Abzüge. Bei Änderungen:
1. `version` hochzählen (Datum + laufende Nummer)
2. Falls eine neue Regel `automated: true` bekommt: passende Funktion in
   `pipeline/score.py::CHECKS` ergänzen — sonst schlägt der Test fehl
3. `pytest tests/ -v` laufen lassen, bevor der nächste echte Audit folgt
