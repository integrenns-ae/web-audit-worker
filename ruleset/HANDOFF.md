# HANDOFF.md — Audit-Pipeline für integrenns.de

Stand: 27.07.2026, Übergabe aus einer Vorgänger-Unterhaltung wegen Netzwerk-
Restriktion (siehe unten). Diese Datei existiert, damit die neue Session
nahtlos weiterlaufen kann, ohne dass der Nutzer den Kontext erneut erklären muss.

## Worum es geht

Manuell getriggerte Website-Audit-Pipeline: Playwright-Crawl → deterministische
Regel-Bewertung (kein LLM) → Bericht. Gebaut, um zuerst `integrenns.de` (eigene
Seite des Nutzers) zu bewerten, zu verbessern, erneut zu bewerten — als
Praxistest, bevor dieselbe Pipeline später für Handwerker-Kundenwebsites
automatisiert wird (Places-API-Leads, Formular-Leads, Freigabe-Gates — das ist
eine SPÄTERE Ausbaustufe, NICHT Teil dieses Pakets hier).

## Warum diese Übergabe nötig war

Die Vorgänger-Session lief in einer Sandbox mit Netzwerk-Einschränkung
("Domain-Zulassungsliste: nur Paketmanager"). Der Nutzer hat in
claude.ai/settings/capabilities → Fähigkeiten → "Ausgehenden Netzwerkverkehr
erlauben" → Domain-Zulassungsliste bereits auf **"Alle Domains"** gestellt.
Das griff aber nicht rückwirkend auf die laufende Session (bestätigt durch
Kontrolltest gegen `example.com`, ebenfalls blockiert). Eine neue Session
sollte die Einstellung von Anfang an korrekt übernehmen.

## Sofort zu tun, in dieser Reihenfolge

1. ZIP entpacken (liegt neben dieser Datei im selben Archiv).
2. Kurzer Verbindungstest: `curl -I https://integrenns.de` — darf **nicht**
   mehr `x-deny-reason: host_not_allowed` zeigen. Falls doch: Netzwerk-Problem
   besteht weiter, dann erst klären, bevor der Rest Sinn ergibt.
3. `pip install -r requirements.txt && playwright install chromium --with-deps`
4. `pytest tests/ -v` — sollte weiterhin 8/8 grün sein (reine Bestätigung,
   braucht kein Netzwerk, testet nur `score.py` gegen Fixtures).
5. **Erster echter Lauf:** `python audit.py https://integrenns.de`
6. Ergebnis mit dem Nutzer durchgehen: Gesamtscore, Kategoriewerte, Findings,
   und die separate "noch manuell zu prüfen"-Liste (5 Regeln, siehe README).
7. Nutzer verbessert die Seite.
8. `python audit.py https://integrenns.de` erneut.
9. `python compare.py integrenns.de` — zeigt Score-Delta und behobene/neue/
   weiterhin offene Findings.

## Wichtiger Hintergrund zu integrenns.de selbst

- Ist eine **Lovable-gebaute React/Vite-SPA** (client-seitig gerendert) — ein
  einfacher HTTP-Fetch zeigt fast nur eine leere Hülle plus Meta-Tags. Deshalb
  crawlt `crawl.py` mit echtem Playwright-Browser, nicht mit `requests`.
- `probe_infra.py::CMS_FINGERPRINTS` enthält bereits `lovable-tagger` /
  `lovable.dev` als Erkennungsmuster.
- Unklar war bisher, ob Impressum/Datenschutz überhaupt vorhanden/auffindbar
  sind (CSR-Routing kann das für einfache Prüfmethoden verstecken) — das war
  der ursprüngliche Auslöser für den Bau dieser Pipeline.
- Möglicher Folgekontext, falls er aufkommt: Der Nutzer denkt über eine
  Migration von Lovable auf Astro (statisches Build-Ziel) nach, u.a. wegen
  besserer SEO/Ladezeit-Werte. Das ist Hintergrund, keine offene Aufgabe hier.

## Architektur-Entscheidungen (falls Code angefasst wird)

- `score.py` ist bewusst rein regelbasiert (kein LLM) — Reproduzierbarkeit ist
  Kernanforderung für den Vorher/Nachher-Vergleich.
- `ruleset.yaml` ist die einzige Quelle für Gewichte/Abzüge, versioniert
  (aktuell `2026-07-27.1`). Bei Änderungen: Version hochzählen, danach
  `pytest tests/ -v` laufen lassen, bevor der nächste echte Audit folgt.
- 19 von 24 Regeln sind automatisiert, 5 bewusst nicht (Begründung jeweils
  direkt in `ruleset.yaml` und in der Tabelle in `README.md`).
- `probe_perf.py` nutzt Playwright-eigene Navigation-/Paint-Timing-Werte statt
  einer separaten Lighthouse-CLI-Installation — bewusste v1-Vereinfachung,
  sauber austauschbar, kein Rewrite nötig.
- Eine LLM-Schicht (kundentaugliche Umformulierung der Befunde) ist bewusst
  NICHT Teil dieser Stufe — Score/Findings sollen zuerst als deterministisches
  Fundament stehen.

## Bekannte, bereits dokumentierte Lücken (kein Grund zur Beunruhigung)

- Tracker-Erkennung zählt nur tatsächlich geladene Requests. Schlägt ein
  Tracker-Script fehl (Netzwerk, Blocker), wird er nicht erkannt — Grenze der
  v1-Heuristik, kein Bug.
- Kein simulierter Cookie-Banner-Klick — alle Drittanbieter-Requests beim
  initialen Laden gelten als "vor Consent" (siehe Kommentar in
  `probe_network.py`).
- Es wird nur die Startseite plus Impressum/Datenschutz gecrawlt, kein
  Mehrseiten-Crawl der ganzen Domain (macht `inhalt.thin_content_page`
  bewusst noch nicht automatisiert).

## Ton/Arbeitsweise mit diesem Nutzer

- Sachlich, direkt, ohne übertriebene Begeisterung oder Superlative.
- Schätzt Ehrlichkeit bei Unsicherheiten/Schätzungen mehr als glatte Zahlen
  (siehe: mehrfache Korrektur einer Preiskalkulation in einem anderen
  Arbeitsstrom — Grundhaltung gilt genauso hier).
- Durchgängig Deutsch.
- Bereits mehrfach bewährt: erst prüfen/testen, dann behaupten — nichts als
  "fertig" melden, ohne es tatsächlich laufen lassen zu haben.
