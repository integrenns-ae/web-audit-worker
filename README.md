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

## Mandanten

Das Shared-Secret entscheidet, **wer** ruft (`clients.py`):

- **Trichter** — Zugang aus `AUDIT_SHARED_SECRET`, Limit pro Besucher-IP
  (5/Stunde, 15/Tag), Cooldown 10 min, darf die echte Besucher-IP per
  `X-Client-IP` melden.
- **Weitere Mandanten** — optional in `clients.json` (nicht im Repo), Limit
  pro Mandant, eigene Limits und Cooldowns.

Der Trichter wird **zuerst** geprüft, unabhängig davon, ob `clients.json`
existiert oder gültig ist. Eine kaputte Datei führt zu einem Fehlereintrag im
Log und einer leeren Mandantenliste — **nie** zu einem 401 für einen echten
Besucher. In der Datei stehen ausschließlich SHA-256-Hashes; sie zu lesen
genügt nicht, um sich anzumelden.

Ein Mandant sieht nur **eigene** Aufträge. Fremde Job-IDs geben `404`, nicht
`403` — sonst wäre die Antwort ein Orakel über existierende IDs.

Der Domain-Cooldown gilt **mandantenübergreifend**: Hat der Trichter eine
Domain gerade geprüft, erbt ein Mandant das Ergebnis als eigenen Auftrag statt
die fremde Seite ein zweites Mal zu crawlen.

## Datenhaltung je Mandant

- `keep_raw: false` — nach dem Lauf wird das komplette Lauf-Verzeichnis
  gelöscht (HTML, Screenshot, Messdateien), der leere Domain-Ordner mit. Auch
  bei gescheitertem Lauf, denn bei `NICHT AUDITIERBAR` hat der Crawl oft schon
  geschrieben. Der Trichter behält seine Läufe — `compare.py` braucht sie.
- `kontaktdaten: true` — Absenderdaten für den Briefkanal werden aus dem
  bereits gecrawlten Impressum gelesen (keine zusätzliche Seitenabfrage).
- `retention_days` — kürzere Aufbewahrung als die globalen zwölf Monate.

Ein Lauf je Domain gleichzeitig (`audit._nur_einmal_pro_domain`): Die fremde
Seite wird nie doppelt gecrawlt, und beim Löschen ist eindeutig, welche
Verzeichnisse zu welchem Lauf gehören.

## Schutz / Limits

- Rate-Limit je Absender (IP oder Mandant): Stunde + Tag, siehe oben.
- Domain-Cooldown mit Ergebnis-Cache (spart Kosten, kein Limit-Verbrauch).
- Global max. 3 gleichzeitige Audits (Semaphore, Rest wartet).
- SQLite-Job-Store (persistent, überlebt Neustarts, Schema wird migriert).

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
