"""vilikon — meldet Website-Checks des Trichters als Lead an das Vilikon-Cockpit.

Nach jedem fertigen Check geht ein Lead im Format „vilikon.lead/1" an
`POST {VILIKON_URL}/api/leads` (Vertrag: ki-cockpit/docs/LEADS-API.md):

- **Check ohne Formular** -> `art: "info"`, eine Spur ohne Aufgabe. Die
  gepruefte Seite mit Score, Hebel und den drei staerksten Befunden.
- **Anfrage zum Check** (Umbau-Formular, KI-Beratung) -> `art: "anfrage"`,
  ein To-do fuer den Vertrieb, mit Kontakt, Nachricht und derselben Bewertung.
  Mit Bewertung prueft das Cockpit die Seite nicht ein zweites Mal.

Drei Zusagen sind wichtiger als der Lead selbst:

1. **Best effort.** Kurzer Timeout, wenige Versuche, eigener Task. Ein nicht
   erreichbares Cockpit darf den Check fuer den Besucher weder verzoegern noch
   kaputtmachen - hier wird keine Ausnahme nach draussen gereicht.
2. **Keine Besucherdaten.** Weder IP noch sonst etwas ueber den Besucher. Die
   Anfrage traegt nur, was er selbst ins Formular geschrieben hat.
3. **Der Schluessel steht nirgends sonst.** Er kommt aus der Umgebung und geht
   nur in den Authorization-Header - nie ins Log, auch nicht in Fehlertexten.

Ohne `VILIKON_URL` und `VILIKON_LEADS_KEY` ist das Ganze aus.

Die beiden Leads tragen VERSCHIEDENE `externe_id`s (`audit-<job>` und
`audit-<job>-anfrage`). Das Cockpit verwirft eine zweite Lieferung mit
derselben id als „bekannt" - auch dann, wenn sie eine Anfrage ist. Mit
eigener id findet es die Spur ueber die Domain und stuft sie zum To-do hoch.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

import kompakt
from audit import _normalize

log = logging.getLogger("audit-worker.vilikon")

STANDARD = "vilikon.lead/1"

# Pro Versuch. Das Cockpit nimmt einen Lead in Millisekunden an; wer laenger
# braucht, ist gerade nicht da.
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
# Pausen zwischen den Versuchen -> drei Versuche insgesamt.
PAUSEN_S = (1.0, 4.0)

# Fuer Tests: httpx.MockTransport statt echtem Netz.
TRANSPORT: httpx.AsyncBaseTransport | None = None

# Kundenfassung der Kategorien - dieselben Namen, die der Besucher im
# Ergebnis auf integrenns.de gesehen hat (CATEGORY_META in auditApi.ts).
BEREICHE = {
    "technik_performance": "Technik & Performance",
    "mobil_zugaenglichkeit": "Mobil & Zugänglichkeit",
    "recht": "Recht",
    "auffindbarkeit_seo": "Auffindbarkeit (SEO)",
    "lokale_sichtbarkeit": "Lokale Sichtbarkeit",
    "inhalt_aktualitaet": "Inhalt & Aktualität",
}

# Betreff wie in der Web3Forms-Mail, damit beide Wege gleich aussehen.
BETREFF = {
    "umbau": "Website-Check: Umbau-Anfrage (mit Audit)",
    "ki-beratung": "Website-Check: Anfrage KI-Beratung (Seite ohne Befunde)",
}
BETREFF_SONST = "Website-Check: Anfrage (mit Audit)"

# Laufende Zustellungen. Ohne Referenz koennte der Garbage Collector einen
# Task mitten in der Zustellung einsammeln.
_LAEUFT: set[asyncio.Task] = set()


def _ziel() -> tuple[str, str]:
    # Bei jedem Aufruf gelesen (wie clients._legacy_secret) - so greift ein
    # Test mit monkeypatch.setenv, und im Betrieb aendert sich nichts.
    return (os.environ.get("VILIKON_URL", "").strip(),
            os.environ.get("VILIKON_LEADS_KEY", "").strip())


def aktiv() -> bool:
    """Nur mit Adresse UND Schluessel. Eines allein ist ein halber Eintrag."""
    url, key = _ziel()
    return bool(url and key)


def _bereich(kategorie) -> str:
    return BEREICHE.get(str(kategorie), str(kategorie or ""))


def bewertung(k: dict) -> dict:
    """Kompakte Antwort (kompakt.verdichten) -> `bewertung` des Leads.

    Ueber das kompakte Format, damit dieselben Zusagen gelten wie fuer den
    Akquise-Agenten: nur `message_customer`, hoechstens drei Befunde, Hebel
    relativ zum Gewicht - und bei einer makellosen Seite gar keiner.
    """
    kategorien = k.get("kategorien") or {}
    schwach = k.get("schwaechste_kategorie")
    hebel = ""
    if schwach in kategorien:
        erreicht, maximal = kategorien[schwach]
        hebel = f"{_bereich(schwach)} {erreicht}/{maximal}"
    # Alle Kategorien in der Reihenfolge des Trichters - so sieht der
    # Vertrieb auf einen Blick, was der Besucher gesehen hat.
    reihenfolge = [n for n in BEREICHE if n in kategorien] + \
                  [n for n in kategorien if n not in BEREICHE]
    zusammenfassung = " · ".join(
        f"{_bereich(n)} {kategorien[n][0]}/{kategorien[n][1]}" for n in reihenfolge)
    overall = k.get("overall")
    return {
        "score": overall if isinstance(overall, (int, float)) and not isinstance(overall, bool) else None,
        "skala": 100,
        "hebel": hebel,
        "zusammenfassung": zusammenfassung[:1000],
        "befunde": [{"bereich": _bereich(f.get("kategorie")), "text": f.get("text", "")}
                    for f in (k.get("top_findings") or [])[:kompakt.MAX_FINDINGS]],
    }


def merkmale(k: dict) -> dict:
    """Was der Vertrieb ausser der Bewertung wissen sollte. Nichts ueber den Besucher."""
    m = {
        # Bei Heilberufen gilt das HWG - der Vertrieb muss das vor dem
        # ersten Satz wissen, nicht erst aus der Seite erraten.
        "heilberuf": k.get("heilberuf"),
        "befunde_gesamt": k.get("befunde_gesamt"),
        "geschaeftstyp": k.get("geschaeftstyp"),
        "regelwerk": k.get("regelwerk"),
    }
    return {name: wert for name, wert in m.items() if isinstance(wert, (str, int, float, bool))}


def info_lead(job: dict) -> dict:
    """Fertiger Check ohne Formular -> Spur (`art: "info"`)."""
    k = kompakt.verdichten(job.get("result") or {}, geprueft_am=job.get("finished_at"))
    lead = {
        "standard": STANDARD,
        "externe_id": f"audit-{job['id']}",
        "art": "info",
        "kontakt": {"website": _normalize(job["url"])},
        "bewertung": bewertung(k),
        "merkmale": merkmale(k),
    }
    if k["geprueft_am"]:
        lead["zeitpunkt"] = k["geprueft_am"]
    return lead


def anfrage_lead(job: dict, anfrage: dict) -> dict:
    """Formular zum Check -> To-do (`art: "anfrage"`).

    `anfrage` ist genau das, was der Besucher ins Formular geschrieben hat.
    Die Bewertung kommt mit, wenn der Check fertig ist - sonst prueft das
    Cockpit die Seite selbst.
    """
    typ = anfrage.get("typ") or "umbau"
    telefon = (anfrage.get("telefon") or "").strip()
    lead = {
        "standard": STANDARD,
        "externe_id": f"audit-{job['id']}-anfrage",
        "art": "anfrage",
        "person": (anfrage.get("name") or "").strip(),
        "kontakt": {
            "email": (anfrage.get("email") or "").strip(),
            "telefon": [telefon] if telefon else [],
            "website": _normalize(job["url"]),
        },
        "anliegen": {
            "betreff": BETREFF.get(typ, BETREFF_SONST),
            "nachricht": (anfrage.get("nachricht") or "").strip(),
            "kategorie": typ,
        },
    }
    extra: dict = {}
    if job.get("status") == "done" and job.get("result"):
        k = kompakt.verdichten(job["result"])
        lead["bewertung"] = bewertung(k)
        extra.update(merkmale(k))
    for feld in ("paket_wunsch", "sprache"):
        wert = (anfrage.get(feld) or "").strip()
        if wert:
            extra[feld] = wert
    if extra:
        lead["merkmale"] = extra
    return lead


def _feldnamen(antwort: httpx.Response) -> str:
    """Bei 422 nur WELCHE Felder, nicht ihr Inhalt - der waere Besucherdaten."""
    try:
        detail = antwort.json().get("detail")
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(detail, list):
        return ""
    orte = {".".join(str(x) for x in d.get("loc", [])) for d in detail if isinstance(d, dict)}
    return ", ".join(sorted(o for o in orte if o))


async def senden(lead: dict, *, transport: httpx.AsyncBaseTransport | None = None) -> bool:
    """Einen Lead zustellen. True bei 200/201. Wirft nie."""
    url, key = _ziel()
    if not (url and key):
        return False
    ziel = url.rstrip("/") + "/api/leads"
    kennung = lead.get("externe_id", "?")
    grund = ""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport or TRANSPORT) as c:
            for versuch in range(len(PAUSEN_S) + 1):
                if versuch:
                    await asyncio.sleep(PAUSEN_S[versuch - 1])
                try:
                    r = await c.post(ziel, json=lead,
                                     headers={"Authorization": f"Bearer {key}"})
                except httpx.HTTPError as e:
                    # Nur die Art des Fehlers. Der Text einer Ausnahme ist
                    # fremder Code - was darin steht, weiss man nicht.
                    grund = type(e).__name__
                    continue
                if r.status_code in (200, 201):
                    log.info("Lead %s an Vilikon: %s", kennung, r.status_code)
                    return True
                grund = f"HTTP {r.status_code}"
                # 401/403/422 werden durch Wiederholen nicht besser.
                if r.status_code != 429 and r.status_code < 500:
                    felder = _feldnamen(r) if r.status_code == 422 else ""
                    log.warning("Lead %s von Vilikon abgelehnt: %s%s", kennung, grund,
                                f" ({felder})" if felder else "")
                    return False
    except Exception as e:  # noqa: BLE001
        grund = type(e).__name__
    log.warning("Lead %s nicht zugestellt (%s)", kennung, grund)
    return False


async def _sicher_senden(lead: dict) -> None:
    try:
        await senden(lead)
    except Exception as e:  # noqa: BLE001 - senden wirft nicht; doppelt haelt besser
        log.warning("Lead %s: %s", lead.get("externe_id", "?"), type(e).__name__)


def melden(baue, *args) -> None:
    """Lead bauen und im Hintergrund zustellen. Kehrt sofort zurueck, wirft nie.

    `baue` ist info_lead oder anfrage_lead. Auch das Bauen steckt hier drin:
    Ein unerwartet geformtes Ergebnis darf den fertigen Check nicht nachtraeglich
    in "failed" verwandeln.
    """
    if not aktiv():
        return
    try:
        lead = baue(*args)
        task = asyncio.get_running_loop().create_task(_sicher_senden(lead))
    except Exception as e:  # noqa: BLE001
        log.warning("Lead nicht gebaut: %s", type(e).__name__)
        return
    _LAEUFT.add(task)
    task.add_done_callback(_LAEUFT.discard)
