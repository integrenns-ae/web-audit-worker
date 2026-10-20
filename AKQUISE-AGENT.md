# Akquise-Agent — Vorgabe für die Vilikon-Seite

Dieses Dokument gehört zur Audit-API, beschreibt aber die **andere** Seite:
den Agenten auf dem Vilikon-VPS, der die API benutzt. Es steht hier, weil der
Vertrag beider Seiten zusammengehört.

**Die API ist fertig** (siehe [`API.md`](API.md)). Was hier steht, ist noch
nicht gebaut.

## Auftrag

Täglich lokale Unternehmen, Ärzte und Praxen suchen, deren Websites prüfen
lassen und **drei Kandidaten** mit fertigem Anschreiben vorlegen.

```
1. Suchen        OpenStreetMap/Overpass → Kandidaten mit Website
2. Prüfen        Audit-API → Score + Befunde + Absenderdaten
3. Auswählen     3 Kandidaten nach Hebel
4. Schreiben     Dossier + Anschreiben-Entwurf
5. Kanal         Freigabeliste entscheidet: E-Mail-Entwurf oder Brief-PDF
6. Entscheiden   Alexander gibt frei, bearbeitet oder verwirft
```

---

## Die Freigabeliste ist das Herzstück

Alexanders Regel:

> „Ich sende E-Mails nur, wenn ein Unternehmen mich kennt oder ich vorher per
> Telefon um Erlaubnis angefragt habe."

Das löst das zentrale Rechtsproblem. **§ 7 Abs. 2 Nr. 2 UWG** verlangt für
E-Mail-Werbung ausdrückliche vorherige Einwilligung — auch gegenüber
Unternehmen. Eine im Impressum veröffentlichte Adresse ist nach BGH I ZR
201/07 keine Einwilligung, und AG Düsseldorf 38 C 135/25 hat „gezielte
Ansprache nach Marktanalyse" schon bei einer einzigen Mail verworfen. Mit
vorherigem Anruf greift die Einwilligung; beim Telefonat selbst genügt
gegenüber Unternehmen die mutmaßliche Einwilligung (§ 7 Abs. 2 Nr. 1); Brief
fällt ohnehin nur unter § 7 Abs. 1.

| Zustand | Bedeutung | Erlaubter Kanal |
|---|---|---|
| `unbekannt` (Standard) | kein Kontakt bisher | **nur Brief** |
| `telefonisch_erlaubt` | angerufen, Zusendung zugesagt (mit Datum und Notiz) | E-Mail **und** Brief |
| `kennt_mich` | bestehender Kontakt oder Kunde | E-Mail und Brief |
| `gesperrt` | Widerspruch nach Art. 21 DSGVO | **kein** Kontakt |

**Technisch erzwingen, nicht in den Prompt schreiben.** `mail_entwerfen` darf
für diesen Agenten nur erreichbar sein, wenn der Kandidat
`telefonisch_erlaubt` oder `kennt_mich` ist. Im Zweifel Brief. Ein Prompt, der
eine Regel „beachten soll", ist keine Durchsetzung — und eine Vertragsstrafe
kommt genau dann, wenn das Modell einmal nicht aufpasst.

Der Sperrzustand ist nicht optional: **Art. 21 Abs. 2 DSGVO** gewährt ein
absolutes Widerspruchsrecht gegen Direktwerbung, ohne Abwägung. Die Sperrliste
muss vor **jedem** Kontakt geprüft werden und darf als einzige Liste nie
gelöscht werden.

---

## Anbindung an die API

Neues Backend-Werkzeug nach dem Muster von `backend/app/ocr.py`: feste
URL-Konstante, `httpx.AsyncClient`, `Authorization`/`X-Shared-Secret` aus dem
Key Store, `transport=`-Parameter für Tests. Registriert über
`create_sdk_mcp_server`, damit der Agent zwei gewöhnliche Werkzeuge sieht:

- `audit_starten(url)` → `POST /audit`
- `audit_ergebnis(job_id)` → `GET /audit/{id}?format=kompakt&warten=60`

Dazu eine neue `Berechtigung` in `permissions.py` — ohne sie erreicht das
Werkzeug keinen Lauf.

**Immer `format=kompakt` benutzen.** Das volle Ergebnis ist rund 7 kB, das
kompakte 842 Byte. **Immer `warten=` benutzen**, statt zu pollen: Jeder Poll
ist ein Werkzeug-Aufruf und kostet Tokens.

Zugangsdaten: Credstore-Eintrag `audit-worker-agent`
(`AUDIT_BASE_URL`, `AUDIT_AGENT_SECRET`) → Vilikons Key Store. Nie über den
Chat, nie in eine Repo-Datei.

Limits: 20/Stunde, 60/Tag, 2 offene Aufträge. Bei `429` den Tageslauf beenden,
nicht wiederholen — 60 Prüfungen reichen für drei Kandidaten bei Weitem.

Täglicher Lauf: eine `Aufgabe`-Zeile (Wochentage + Uhrzeit), der vorhandene
APScheduler erledigt den Rest.

---

## Kandidaten auswählen

**Der schlechteste Score ist nicht der beste Kandidat.** Eine Seite mit 20
Punkten gehört oft jemandem, der keine Website will. Besser ist die
Kombination aus:

- mittlerem `overall` (nicht am Boden — der Boden ist 15 und bedeutet „die
  Skala endet hier")
- `geschaeftstyp: "local"` — lokal agierend, also im Zielbild
- `non_profit: false`
- **einem** deutlichen Hebel: `schwaechste_kategorie` mit großem Gewicht.
  `kategorien` liefert `[erreicht, maximal]`, der Hebel ist also direkt
  ablesbar. Ist `schwaechste_kategorie` `null`, ist die Seite makellos —
  Kandidat verwerfen.
- `befunde_gesamt` moderat: Eine Seite mit 20 Befunden braucht einen Neubau,
  eine mit 3 ein Angebot.

`grund: "robots_disallow"` → Kandidat **endgültig** verwerfen und nicht
erneut versuchen. Die Seite hat Prüf-Bots ausgesperrt; das wird respektiert.

---

## Zwei Kanäle, ein Text

Der Agent erzeugt **einen** Anschreibentext. Der Kanal entscheidet nur über
die Verpackung:

- **E-Mail:** `mail_entwerfen` → Freigabe-Karte im Cockpit → Versand erst nach
  Alexanders Ja. Die Karte ist bearbeitbar; damit ist „als Entwurf" ohne neue
  Infrastruktur erfüllt.
- **Brief:** PDF über `pypdf` in die Ablage des Mitarbeiters. Das Beiblatt mit
  der Art.-14-Information gehört in **dieselbe** Datei, sonst geht es
  verloren.

Beides zusätzlich als Briefing-Kachel (`ergebnis_typ="briefing"`): drei
Kandidaten mit Score, stärkstem Befund und vorgeschlagenem Kanal auf einen
Blick.

Für den Brief liefert die API `absender_kandidat` (Firma, Anschrift, Telefon,
E-Mail aus dem Impressum). Felder können `null` sein, der Block kann fehlen.
**Vor dem Druck prüfen** — das ist eine Textextraktion aus handgeschriebener
Prosa.

### Google Drive: bewusst später

Vilikon hat **keine** Google-Anbindung (Integrationstypen: `imap`, `caldav`,
`github`). Drive bräuchte OAuth2 samt Redirect-Handler, Token-Erneuerung und
Scope-Verwaltung — ein eigenes Vorhaben.

Der Gewinn gegenüber dem obigen Weg wäre allein: Bearbeiten im Browser von
überall. Für E-Mails leistet die Freigabe-Karte das schon. Für Briefe bliebe
der Unterschied, das PDF nicht herunterladen zu müssen.

**Empfehlung:** erst den Ablauf mit Freigabe-Karte und PDF laufen lassen.
Stört das Herunterladen nach zwei Wochen, Drive gezielt nachrüsten — dann ist
auch klar, welche Felder das Dokument wirklich braucht.

---

## Datenquellen

**OpenStreetMap/Overpass** ist lizenzrechtlich sauber: kommerzielle Nutzung
ausdrücklich erlaubt, Share-Alike greift nur bei öffentlicher Weitergabe, eine
interne Liste fällt nicht darunter. Eigene Overpass-Instanz oder
Geofabrik-Extrakte statt der öffentlichen Instanz.

**Nicht verwenden:** Google Places untersagt das Speichern für eigene
Lead-Datenbanken (Maps-Bedingungen 10.5(d), Caching höchstens 30 Tage). Gelbe
Seiten und Das Örtliche untersagen gewerbliche Adressverwertung ausdrücklich
und genießen zusätzlich Datenbankschutz.

---

## Datenschutz

- **Art.-14-Hinweis** gehört in **jeden** Erstkontakt, Brief wie E-Mail:
  Verantwortlicher, Zweck, Rechtsgrundlage (berechtigtes Interesse),
  **Quelle der Daten**, Speicherdauer, Widerspruchsrecht.
- **Löschen statt horten.** Kandidaten, die nicht angeschrieben werden,
  binnen Tagen verwerfen. Wer nur die drei Angeschriebenen behält, hat das
  Problem strukturell nicht. Der Worker tut dasselbe: `keep_raw: false`
  löscht die Rohdaten sofort, `retention_days: 30` die Aufträge nach einem
  Monat.
- **Ausnahme:** Die Sperrliste (Art. 21) bleibt dauerhaft.

## Offene Punkte für Alexander

- **Die Freigabeliste braucht einen Startbestand.** Ohne gepflegte Liste läuft
  anfangs alles über Brief — rechtlich richtig, aber langsamer.
- **Art.-14-Text** formulieren. Entwurf ist machbar, die Freigabe gehört zum
  Anwalt.
- **Rechtliche Durchsicht:** Dieselbe Runde, die AGB und
  Datenschutzerklärung prüft, sollte den Akquise-Ablauf mitbewerten —
  insbesondere, ob der Telefonanruf als Einwilligungsgrundlage in dieser
  Ausgestaltung trägt.
