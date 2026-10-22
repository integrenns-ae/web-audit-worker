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
POST /audit/{job_id}   {name, email, telefon, nachricht, …}
                                      -> 202 {status:"angenommen"}  (Anfrage zum Check)
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

## Leads an Vilikon

Jeder Check des Trichters geht als Lead an das Vilikon-Cockpit
(`POST {VILIKON_URL}/api/leads`, Format „vilikon.lead/1", Vertrag:
[`ki-cockpit/docs/LEADS-API.md`](https://github.com/integrenns-ae/ki-cockpit/blob/main/docs/LEADS-API.md)).
Code: `vilikon.py`.

| Was | Lead | Inhalt |
|---|---|---|
| Fertiger Check (`done`) | `art: "info"`, `externe_id: "audit-<job_id>"` | `kontakt.website`, `bewertung`, `merkmale` |
| Anfrage zum Check (`POST /audit/{job_id}`) | `art: "anfrage"`, `externe_id: "audit-<job_id>-anfrage"` | dazu `person`, `kontakt.email`/`telefon`, `anliegen` |

`bewertung` kommt aus dem kompakten Format: `score` = `overall` (Skala 100),
`hebel` = schwächste Kategorie mit Punkten („Recht 0/20"), `zusammenfassung` =
alle Kategorien, `befunde` = die drei stärksten, nur `message_customer`. Die
Kategorienamen sind die, die der Besucher auf integrenns.de gesehen hat. Mit
Bewertung prüft das Cockpit die Seite nicht noch einmal. `merkmale` trägt
`heilberuf` (HWG!), `befunde_gesamt`, `geschaeftstyp`, `regelwerk`, bei der
Anfrage auch `paket_wunsch` und `sprache`. Einen Link zum Bericht gibt es
nicht, weil es keine Berichtsseite je Auftrag gibt.

**Zwei verschiedene `externe_id`s, mit Absicht:** Das Cockpit verwirft eine
zweite Lieferung mit derselben id als „bekannt" — auch dann, wenn sie eine
Anfrage ist. Mit eigener id findet es die Spur über die Domain und stuft sie
zum To-do hoch.

Gemeldet wird **nur der Trichter**. Aufträge des Akquise-Agenten hat Vilikon
selbst erteilt. Ein geerbtes Ergebnis (Cooldown, anderer Mandant) zählt als
eigener Check; ein wiederverwendetes eigenes (dieselbe `job_id`) nicht noch
einmal. Nie dabei: die Besucher-IP oder sonst etwas über den Besucher — die
Anfrage trägt nur, was er selbst ins Formular geschrieben hat.

**Best effort.** Die Zustellung läuft in einem eigenen Task: 5 s Timeout je
Versuch (3 s Verbindungsaufbau), drei Versuche bei Netzfehler, `429` und `5xx`,
keiner bei `401`/`403`/`422`. Der Check und die Antwort an den Besucher warten
nie darauf. Fehlschläge stehen im Log mit `externe_id` und Statuscode — ohne
Schlüssel, ohne Fehlertexte fremder Bibliotheken, bei `422` nur die Feldnamen.

### Die beiden Variablen

| Variable | Inhalt |
|---|---|
| `VILIKON_URL` | Adresse des Cockpits **ohne** `/api/leads`, z. B. `https://cockpit.example` |
| `VILIKON_LEADS_KEY` | Schlüssel `vlk_…` einer API-Quelle im Cockpit |

Beide leer (oder eine davon) = aus; der Worker läuft dann genau wie vorher.
Den Schlüssel legt Alexander im Cockpit an: **Akquise → Quellen → „Kanal
anbinden" → Schnittstelle (API)**. Er wird dort nur einmal angezeigt und
gehört in den Credstore-Eintrag `audit-worker-vilikon` (Felder `VILIKON_URL`,
`VILIKON_LEADS_KEY`) — von dort in `/opt/audit-worker/.env` auf dem VPS. Nie
ins Repo, in den Chat oder in ein Log.

`.env` wird beim **Anlegen** des Containers gelesen: Nach einer Änderung
`docker compose up -d` (legt ihn neu an), ein `restart` reicht **nicht**.

### Anfrage zum Check

`POST /audit/{job_id}` ist für das Formular „Umbau-Anfrage (mit Audit)" (und
„KI-Beratung") auf `/website-check`. Der Strato-Proxy leitet `POST
/api/audit/{id}` schon heute weiter (Route `audit/<id>`, Methode `POST`),
samt Geheimnis und Besucher-IP:

```json
{"typ": "umbau", "name": "Sabine Sommer", "email": "praxis@example.com",
 "telefon": "0641 7654321", "nachricht": "…", "paket_wunsch": "schaufenster", "sprache": "de"}
```

`typ` (`umbau` | `ki-beratung` | …) wird zu Betreff und Kategorie, wie in der
Web3Forms-Mail. E-Mail **oder** Telefon muss dabei sein (sonst `422`). Antwort
immer sofort `202 {"status":"angenommen"}` — auch wenn das Cockpit aus oder
nicht erreichbar ist. `404` für fremde oder unbekannte Aufträge und für jeden
Mandanten außer dem Trichter; `429` nach 5 Anfragen/Stunde bzw. 15/Tag je
Besucher-IP (eigener Zähler, getrennt von den Prüfungen). Die Web3Forms-Mail
bleibt vorerst daneben bestehen; das Cockpit entdoppelt über Domain und E-Mail.

## Tests

```sh
python3 -m pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -q          # Worker: Trichter-Vertrag, Mandanten, Leads (192)
cd ruleset && python3 -m pytest tests/ -q   # Regelwerk (59)
```

`tests/` sichert den Vertrag ab, auf den sich `/website-check` verlässt: Auth,
Rate-Limit pro Absender, Domain-Cooldown ohne Limit-Verbrauch, Statusabfrage,
Crash-Recovery und die Löschfristen, die die Datenschutzerklärung zusagt.
`tests/test_vilikon.py` prüft die Leads: Ergebnis → Lead-JSON, aus ohne
Variablen, Fehlschläge ohne Folgen für den Check und ohne Schlüssel im Log. Beide
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
