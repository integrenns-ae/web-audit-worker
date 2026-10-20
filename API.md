# Audit-API — Vertrag für Aufrufer von außen

Der Worker hat zwei Nutzer: den **Website-Trichter** auf integrenns.de
(`/website-check`, über den Strato-Proxy) und ab Stufe 5 den
**Akquise-Agenten** auf dem Vilikon-VPS.

Dieses Dokument ist der Vertrag, gegen den die Vilikon-Seite baut. Es
beschreibt den Zielzustand; die Spalte **Stufe** sagt, was schon steht.

Was der Agent auf der anderen Seite tun soll — Kandidatenauswahl,
Freigabeliste, Brief- und E-Mail-Kanal, Datenquellen — steht in
[`AKQUISE-AGENT.md`](AKQUISE-AGENT.md).

| Baustein | Stufe | Stand |
|---|---|---|
| `POST /audit`, `GET /audit/{job_id}` | — | live |
| Testnetz für den Trichter-Vertrag | 0 | **fertig** |
| Mandanten (`clients.json`), Eigentumsprüfung | 1 | **fertig** |
| Mandantenpfad bei jedem Deploy geprüft | 2 | **fertig** |
| `?format=kompakt`, `?warten=`, Warteschlangen-Grenze, reservierter Slot | 3 | **fertig** |
| `keep_raw`, `absender_kandidat`, `retention_days` | 4 | **fertig** |
| Schlüssel für Vilikon | 5 | **fertig** |

---

## Adresse und Anmeldung

```
https://omr-worker.kol316.de/website-audit
```

Jeder Aufruf außer `/health` braucht das Mandanten-Geheimnis:

```
X-Shared-Secret: <geheimnis>
```

Das Geheimnis liegt **ausschließlich** in Vilikons Key Store. Auf dem Worker
steht in `clients.json` nur der SHA-256-Hash — die Datei zu lesen genügt also
nicht, um sich anzumelden.

Ab Stufe 1 bestimmt das Geheimnis, **welcher** Mandant ruft. Davon hängen
Limits, Antwortformat und Datenhaltung ab.

---

## `POST /audit` — Prüfung beauftragen

```json
{ "url": "beispiel-praxis.de" }
```

`url` darf mit oder ohne Schema kommen, höchstens 300 Zeichen.

### Antworten

**202 — angenommen, läuft**

```json
{ "job_id": "9f3c…", "status": "pending" }
```

**200 — frisches Ergebnis wiederverwendet**

```json
{ "job_id": "9f3c…", "status": "done", "cached": true }
```

Der Domain-Cooldown (für Mandanten 12 Stunden) verhindert, dass dieselbe
Seite mehrfach gecrawlt wird. Ein Treffer aus dem Cooldown **verbraucht kein
Limit**. Das Ergebnis holt man wie gewohnt per `GET`.

**400** ungültige URL · **401** falsches Geheimnis · **422** Feld fehlt oder
zu lang · **429** Limit erschöpft · **503** Worker nicht konfiguriert

Ab Stufe 3 zusätzlich **429**, wenn für den Mandanten schon `max_queued`
Aufträge laufen. Dann nicht sofort erneut senden, sondern erst die laufenden
Aufträge abholen.

---

## `GET /audit/{job_id}` — Ergebnis abholen

Ab Stufe 1 sieht ein Mandant **nur eigene** Aufträge. Fremde IDs geben
`404`, nicht `403` — eine fremde Job-ID lässt sich so nicht bestätigen.

### Parameter

| Parameter | Wirkung |
|---|---|
| `format=kompakt` | verdichtete Antwort (siehe unten) statt des vollen Ergebnisses |
| `warten=<0..60>` | hält die Verbindung bis zu N Sekunden, bis der Auftrag fertig ist |

`warten` ersetzt das Pollen: Jeder Poll ist beim Agenten ein Werkzeug-Aufruf
und kostet Tokens. Ist der Auftrag nach N Sekunden noch nicht fertig, kommt
ganz normal `{"status":"pending"}` zurück.

### Zustände

```json
{ "status": "pending" }
{ "status": "done", "result": { … } }
{ "status": "nicht_auditierbar", "grund": "robots_disallow" }
{ "status": "failed", "grund": "interner Fehler" }
```

`nicht_auditierbar` ist **kein Fehler**. Die wichtigsten Gründe:

| `grund` | Bedeutung | Reaktion des Agenten |
|---|---|---|
| `robots_disallow` | Die Seite sperrt Prüf-Bots aus | Kandidat **verwerfen**, nicht erneut versuchen |
| `Seite nicht erreichbar` | DNS, TLS oder Verbindung | verwerfen |
| `Zeitüberschreitung beim Prüfen der Seite` | über 120 s | verwerfen |

---

## Volles Ergebnis (`status: "done"`)

```json
{
  "domain": "beispiel-praxis.de",
  "ruleset_version": "2026-07-27.15",
  "overall": 42,
  "category_scores": {
    "technik_performance": 11, "mobil_zugaenglichkeit": 15, "recht": 0,
    "auffindbarkeit_seo": 8, "lokale_sichtbarkeit": 7, "inhalt_aktualitaet": 1
  },
  "business_locality": "local",
  "is_non_profit": false,
  "page_count": 7,
  "findings": [
    { "id": "recht.no_impressum", "category": "recht", "score_impact": -12,
      "message_internal": "…", "message_customer": "…" }
  ],
  "manual_review_needed": [ … ],
  "not_applicable": [ … ],
  "suppressed_categories": [], "suppressed_rules": [],
  "unverified_external_links": []
}
```

Wichtig zum Verständnis:

- **`category_scores` sind Punkte, keine Prozente.** Die Gewichte sind
  `technik_performance` 23, `recht` 20, `mobil_zugaenglichkeit` 15,
  `auffindbarkeit_seo` 15, `inhalt_aktualitaet` 15, `lokale_sichtbarkeit` 12
  (Summe 100). `recht: 0` bei Gewicht 20 ist ein Totalausfall,
  `lokale_sichtbarkeit: 7` bei Gewicht 12 nur eine Schwäche.
- **`overall` fällt nie unter 15** (Boden im Regelwerk). Ein 15er bedeutet
  „so schlecht, dass die Skala endet", nicht „genau 15".
- **`message_internal` darf niemals in ein Anschreiben.** Diese Fassung ist
  für mich, nicht für den Empfänger. Deshalb liefert das kompakte Format
  ausschließlich `message_customer`.

Für eine schlechte Seite ist das volle Ergebnis rund **7 kB** — gemessen,
nicht geschätzt. Als Text in einem Modellaufruf ist das Verschwendung.

---

## Kompaktes Format (`?format=kompakt`, ab Stufe 3)

```json
{
  "status": "done",
  "domain": "beispiel-praxis.de",
  "overall": 42,
  "kategorien": { "recht": [0, 20], "technik_performance": [11, 23] },
  "schwaechste_kategorie": "recht",
  "top_findings": [
    { "kategorie": "recht", "gewicht": -12,
      "text": "Ein Impressum ist nicht auffindbar. Das ist in Deutschland …" }
  ],
  "seiten_geschaetzt": 7,
  "befunde_gesamt": 6,
  "geschaeftstyp": "local",
  "non_profit": false,
  "heilberuf": false,
  "geprueft_am": "2026-10-20T14:03:11Z",
  "cached": false
}
```

- `kategorien` nennt je Kategorie `[erreicht, maximal]` — damit ist der
  Hebel ohne Gewichtstabelle ablesbar.
- `schwaechste_kategorie` ist **relativ** zum Gewicht bestimmt, nicht nach
  absoluten Punkten — und ist `null`, wenn die Seite in jeder Kategorie die
  volle Punktzahl hat. Eine makellose Seite hat keinen Hebel, und der Agent
  soll keinen erfinden.
- `top_findings` sind höchstens drei, nach Punktabzug sortiert, Text auf 300
  Zeichen gekürzt, **nur** `message_customer`.
- `heilberuf` ist `true` bei Arzt-, Zahnarzt-, Therapie- und
  Heilpraktiker-Praxen. Zwei Dinge folgen daraus: Die Regel „keine Referenzen
  oder Portfolio" wird dort **nicht** angewendet (sie ist sachlich unpassend
  und rechtlich heikel, siehe unten), und das Anschreiben darf keine Aussagen
  über Behandlungserfolge enthalten.
- `geprueft_am` kommt aus dem Worker (`jobs.finished_at`), nicht aus dem
  Regelwerk — im Score-Ergebnis steht kein Zeitstempel. Bei 12 Stunden
  Cooldown ließe sich frisch sonst nicht von wiederverwendet unterscheiden.

Gemessen an einer durchgefallenen Seite: **7175 Byte** voll, **842 Byte**
kompakt — Faktor 8,5. `befunde_gesamt` nennt die Zahl aller Befunde, auch wenn
`top_findings` nur die drei stärksten zeigt.

### `absender_kandidat` (nur bei `kontaktdaten: true`, ab Stufe 4)

Für den Briefkanal braucht der Agent eine Postanschrift. Sie wird aus dem
Impressum gelesen, das der Crawl ohnehin abruft:

```json
"absender_kandidat": {
  "firma": "Praxis Dr. Beispiel",
  "anschrift": "Musterweg 3, 35390 Gießen",
  "telefon": "0641 123456",
  "email": "praxis@beispiel.de",
  "quelle": "/impressum"
}
```

**Beispiel aus dem ersten echten Lauf** (integrenns.de): Anschrift, Telefon
und E-Mail waren richtig, `firma` lieferte aber `"Freelance IT consultant"` —
die Zeile über der Anschrift war dort eine Berufsbezeichnung, kein Firmenname.
Genau deshalb ist dieser Block ein Vorschlag und keine Zusage.

Gelesen wird der Impressumstext, den der Crawl ohnehin abruft — **es wird
keine zusätzliche Seite geladen**, die fremde Website wird dafür nicht ein
einziges Mal mehr angefasst.

Einzelne Felder können `null` sein; der ganze Block fehlt, wenn im Impressum
weder Anschrift noch E-Mail stand. Das ist Absicht: Eine falsche Anschrift auf
einem Brief ist schlimmer als keine. **Vor dem Druck prüfen** — Impressen sind
handgeschriebene Prosa in beliebiger Reihenfolge.

Dass diese Daten bei Praxen, Kanzleien und Einzelunternehmern personenbezogen
sind, ist der Grund für die Art.-14-Information im Erstkontakt und für
`retention_days`.

Mit `keep_raw: false` löscht der Worker nach dem Lauf das **komplette**
Lauf-Verzeichnis — HTML, Screenshot, Messdateien — und den Domain-Ordner, wenn
er leer zurückbleibt. Sonst wäre `runs/` nach einem Jahr ein Verzeichnis
fremder Firmennamen. Gelöscht wird auch dann, wenn der Lauf gescheitert ist:
Bei `NICHT AUDITIERBAR` hat der Crawl oft schon geschrieben, obwohl nie ein
Score entstand.

---

## Limits

| | Trichter (Besucher) | Mandant `vilikon` |
|---|---|---|
| Zählung je | Besucher-IP | Mandant |
| pro Stunde | 5 | 20 |
| pro Tag | 15 | 60 |
| Domain-Cooldown | 10 min | 12 h |
| gleichzeitig | bis 3 | bis 2 |
| offene Aufträge | — | 2 |

**Ein Nebenläufigkeits-Slot bleibt dem Trichter reserviert.** Ein echter
Besucher wartet nie, weil der Agent gerade 20 Seiten prüft.

Bei `429` nicht wiederholen, sondern den Tageslauf beenden. 60 Prüfungen
reichen für 3 Kandidaten bei Weitem.

---

## Betriebsfallen

1. **`clients.json` wird beim Start gelesen.** Jede Änderung braucht
   `docker compose restart audit-worker`.
2. **`rsync --delete` im Deploy würde die Datei löschen** — deshalb steht
   `--exclude='clients.json'` im Workflow. Fehlt der Ausschluss, bekommt der
   Agent nach dem nächsten Push still `401`.
3. **`?warten=` braucht nginx.** `proxy_read_timeout` steht auf `90s` in
   `/etc/nginx/sites-available/omr-worker`. Vorher waren es 45 s — damit hätte
   der Proxy bei `warten=60` vor dem Worker abgebrochen.
4. **Das Regelwerk liegt doppelt.** Quelle ist `web-audit-ruleset`, produktiv
   ist die Kopie unter `ruleset/`. Nach Regeländerungen ist `--build`
   Pflicht, sonst läuft die alte Version weiter.

---

## Höflichkeit und Recht

- Der Crawl prüft **vor** dem Zugriff `robots.txt` auf `IntegrennsAuditBot`
  und bricht bei einer Sperre ab (`grund: robots_disallow`).
- Der Bot weist sich aus und erklärt sich unter
  <https://integrenns.de/bot>.
- Eine Prüfung ist noch keine Kontaktaufnahme. Für den Kontakt gilt die
  Freigabeliste auf der Vilikon-Seite: **E-Mail nur, wenn das Unternehmen
  mich kennt oder telefonisch zugestimmt hat** — sonst Brief.
