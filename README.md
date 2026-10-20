# web-audit-worker

> **Gesamt-Übergabe:** Dieses Repo ist Teil des Website-Check-Systems von integrenns.de.
> Die vollständige Dokumentation — Architektur der Kette, Zugänge, Deploy-Prozeduren für alle
> drei Repos, Gotchas und offene Punkte — steht zentral in
> [`expert-showcase-site/HANDOFF.md`](https://github.com/integrenns-ae/expert-showcase-site/blob/main/HANDOFF.md).
> **Vor substanzieller Arbeit dort zuerst lesen.**


FastAPI-Worker, der Websites auditiert und einen `score_result` liefert. Gegenpart
zum `/website-check`-Trichter auf [integrenns.de](https://integrenns.de/website-check).

Läuft auf dem Hetzner-VPS `omr-worker` in `/opt/audit-worker/` als Docker-Container
(`127.0.0.1:8003`), hinter nginx (geplant: `audit.integrenns.de`). Server-zu-Server-
Auth über `X-Shared-Secret`; aufgerufen wird er vom Strato-PHP-Proxy, nie direkt aus
dem Browser.

## API-Vertrag

```
POST /audit            {url}          -> 202 {job_id, status:"pending"}
GET  /audit/{job_id}                  -> 200 {status, result?|grund?}
GET  /health                          -> {status:"ok"}
```

`status`: `pending` | `done` (+`result`) | `nicht_auditierbar` (+`grund`) | `failed` (+`grund`).
Alle Aufrufe (außer `/health`) erfordern Header `X-Shared-Secret`.

**Für Aufrufer von außen** (Akquise-Agent auf dem Vilikon-VPS) ist
[`API.md`](API.md) der verbindliche Vertrag: Mandanten, Limits, kompaktes
Antwortformat, Fehlergründe und die Betriebsfallen.

Der Audit läuft asynchron (~30–60 s realer Crawl). Enqueue → `job_id` → Status pollen.

## `result`-Struktur

Feste Kategorie-Nenner (Summe = 100): technik_performance 23, mobil_zugaenglichkeit 15,
recht 20, auffindbarkeit_seo 15, lokale_sichtbarkeit 12, inhalt_aktualitaet 15.
Felder u. a. `overall`, `category_scores` (Objekt), `findings[]` (mit `score_impact`
negativ, `message_customer` = kundentauglich, `message_internal` = intern), `page_count`,
`not_applicable`, `manual_review_needed`, `unverified_external_links`.

## Scoring-Naht

`audit.run_audit(url) -> result` ist die **einzige** Stelle für die Score-Logik.
Dort läuft das vendorte **Regelwerk** (`ruleset/`) als Subprozess; gelesen wird
anschließend das erzeugte `<domain>_score_result.json`.

Das Regelwerk liegt **doppelt**: Quelle ist das Repo `web-audit-ruleset`,
produktiv ist allein die Kopie unter `ruleset/`. Das Dockerfile kopiert sie beim
Bauen nach `/opt/ruleset` — nach Regeländerungen ist `docker compose up -d
--build` deshalb Pflicht, ein `restart` ließe die alte Version laufen.

## Schutz / Limits

- Rate-Limit pro IP: 5/Stunde, 15/Tag.
- Domain-Cooldown 10 min mit Ergebnis-Cache (spart Kosten, kein Limit-Verbrauch).
- Global max. 3 gleichzeitige Audits (Semaphore, Rest wartet).
- SQLite-Job-Store (persistent, überlebt Neustarts).

## Tests

```sh
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -q          # Worker: Trichter-Vertrag (43)
cd ruleset && python3 -m pytest tests/ -q   # Regelwerk (54)
```

`tests/` sichert den Vertrag ab, auf den sich `/website-check` verlässt: Auth,
Rate-Limit pro Absender, Domain-Cooldown ohne Limit-Verbrauch, Statusabfrage,
Crash-Recovery und die Löschfristen, die die Datenschutzerklärung zusagt. Beide
Suiten laufen **ohne Netzwerk und ohne Chromium** und sind Teil der CI — ein
Push mit rotem Test deployt nicht.

## Deploy

```sh
# Code auf den VPS spiegeln (ohne .env) und Container neu starten:
rsync -az --exclude='.env' --exclude='__pycache__' ./ omr-worker:/opt/audit-worker/
ssh omr-worker 'cd /opt/audit-worker && docker compose up -d --build'
```

`.env` (nur auf dem VPS, nie im Repo) aus `.env.example` erzeugen; `AUDIT_SHARED_SECRET`
muss identisch im Strato-Proxy stehen.
