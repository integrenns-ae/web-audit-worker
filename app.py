"""audit-worker — FastAPI-Gegenpart zum /website-check-Trichter.

Vertrag:
  POST /audit            {url}            -> 202 {job_id, status:"pending"}
  GET  /audit/{job_id}                    -> 200 {status, result?|grund?}
  POST /audit/{job_id}   {name, email, …} -> 202 (Anfrage zum Check, nur Trichter)
  GET  /health                            -> {status:"ok"}

Aufruf mit Header X-Shared-Secret. Das Geheimnis entscheidet, WELCHER Mandant
ruft (siehe clients.py): der Website-Trichter über den Strato-PHP-Proxy oder
der Akquise-Agent auf dem Vilikon-VPS. Limits, Cooldown und Datenhaltung
haengen am Mandanten.

Der vollstaendige Vertrag fuer Aufrufer von aussen steht in API.md.
Scoring steckt in audit.run_audit() (Integrationsnaht für das echte Regelwerk).
Checks und Anfragen des Trichters gehen als Lead an Vilikon (vilikon.py).
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import clients
import kompakt
import store
import vilikon
from audit import NotAuditableError, _host, run_audit
from clients import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("audit-worker")

MAX_CONCURRENT = int(os.environ.get("AUDIT_MAX_CONCURRENT", "3"))
MAX_URL = 300

SEM = asyncio.Semaphore(MAX_CONCURRENT)
# Ein Slot bleibt dem Trichter reserviert: Hintergrund-Mandanten duerfen
# hoechstens MAX_CONCURRENT-1 gleichzeitig belegen. Ein echter Besucher wartet
# damit nie darauf, dass der Akquise-Agent gerade 20 Seiten durchprueft.
SEM_HINTERGRUND = asyncio.Semaphore(max(1, MAX_CONCURRENT - 1))

# Taktung des Long-Polls (?warten=). Haeufiger abzufragen bringt nichts, weil
# ein Audit 30-60 Sekunden dauert.
POLL_INTERVALL_S = 0.5

RUNS_DIR = Path(os.environ.get("RULESET_DIR", "/opt/ruleset")) / "runs"
RUNS_MAX_AGE_S = int(os.environ.get("AUDIT_RUNS_MAX_AGE_DAYS", "360")) * 86400


def prune_old_runs() -> None:
    """Löscht Audit-Läufe älter als AUDIT_RUNS_MAX_AGE_DAYS (Standard 360 Tage)."""
    if not RUNS_DIR.exists():
        return
    cutoff = time.time() - RUNS_MAX_AGE_S
    for domain_dir in RUNS_DIR.iterdir():
        if not domain_dir.is_dir():
            continue
        for run_dir in domain_dir.iterdir():
            try:
                if run_dir.is_dir() and run_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(run_dir, ignore_errors=True)
            except OSError:
                pass


# Loeschfristen - muessen zur Datenschutzerklaerung passen (/datenschutz):
# IP-Adressen hoechstens 24 Stunden, geprüfte Adressen und Ergebnisse 12 Monate.
IP_MAX_AGE_S = 86400
JOBS_MAX_AGE_S = 365 * 86400


def _aufraeumen() -> None:
    """Alle Loeschfristen in einem Durchgang."""
    store.cleanup_req_log(IP_MAX_AGE_S)
    store.cleanup_jobs(JOBS_MAX_AGE_S)
    # Mandanten mit kuerzerer Aufbewahrung (retention_days) zuerst - der Agent
    # prueft fremde Firmenseiten, die nicht monatelang liegen bleiben sollen.
    for client in clients.alle_mandanten():
        if client.retention_days:
            weg = store.cleanup_jobs_for_client(client.id, client.retention_days * 86400)
            if weg:
                log.info("Mandant %s: %d alte Auftraege geloescht", client.id, weg)


async def _prune_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(prune_old_runs)
        except Exception:  # noqa: BLE001
            log.exception("runs-cleanup fehlgeschlagen")
        # Beide Loeschungen gehoeren in den taeglichen Lauf. Vorher liefen sie
        # nur beim Start - bei wochenlang laufendem Container wurden IP-Adressen
        # damit faktisch nie geloescht.
        try:
            await asyncio.to_thread(_aufraeumen)
        except Exception:  # noqa: BLE001
            log.exception("datenbank-cleanup fehlgeschlagen")
        await asyncio.sleep(86400)   # täglich


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Crash-Recovery: Nach einem Neustart ist JEDER pending-Job verwaist -
    # die zugehoerige Task ist mit dem Prozess gestorben. Mit dem frueheren
    # Grenzwert von 900s blieben jung gestorbene Jobs fuer immer pending,
    # und der Trichter pollte sie endlos.
    store.mark_stale_pending_failed(older_than_s=0)
    _aufraeumen()
    prune_task = asyncio.create_task(_prune_loop())
    yield
    prune_task.cancel()


app = FastAPI(title="audit-worker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class AuditReq(BaseModel):
    url: str = Field(..., min_length=3, max_length=MAX_URL)


def _resolve_client(provided: str | None) -> Client:
    """Geheimnis -> Mandant. 401 bei falschem, 503 bei unkonfiguriertem Worker."""
    client = clients.resolve(provided)
    if client is not None:
        return client
    # Fail-closed: Ohne brauchbares Shared-Secret ist der Worker nicht
    # konfiguriert - das ist ein Betriebsfehler, kein Tippfehler des Aufrufers.
    if not clients.legacy_configured():
        raise HTTPException(status_code=503, detail="Service nicht konfiguriert.")
    raise HTTPException(status_code=401, detail="Ungueltiges oder fehlendes Token.")


def _subject(request: Request, client: Client) -> str:
    """Wogegen das Rate-Limit zaehlt.

    Fuer den Trichter die echte Besucher-IP, die der Strato-Proxy als
    X-Client-IP meldet - sonst wuerde ein einziger Besucher den Check fuer
    alle sperren.

    Fuer Maschinen-Mandanten der Mandantenname. Die IP waere immer die des
    Vilikon-VPS und damit nutzlos; sie wird deshalb gar nicht gespeichert.

    Entscheidend ist, dass der Header NUR fuer trust_client_ip ausgewertet
    wird. Vorher konnte jeder Geheimnis-Inhaber durch eine frei erfundene
    X-Client-IP sein eigenes Limit beliebig oft zuruecksetzen.
    """
    if client.limit_scope == "client":
        return f"client:{client.id}"
    if client.trust_client_ip:
        gemeldet = request.headers.get("x-client-ip") or request.headers.get("x-real-ip")
        if gemeldet:
            return gemeldet[:64]
    return request.client.host if request.client else "?"


async def _run_job(job_id: str, url: str, hintergrund: bool = False,
                   kontaktdaten: bool = False, keep_raw: bool = True,
                   melden: bool = False) -> None:
    async with AsyncExitStack() as slots:
        # Reihenfolge ist wichtig: zuerst der Hintergrund-Slot, dann der
        # globale. Umgekehrt haelt ein Agentenlauf einen globalen Slot fest,
        # waehrend er noch auf seinen Hintergrund-Slot wartet - und blockiert
        # genau den Besucher, den die Reservierung schuetzen soll.
        if hintergrund:
            await slots.enter_async_context(SEM_HINTERGRUND)
        await slots.enter_async_context(SEM)
        try:
            result = await run_audit(url, kontaktdaten=kontaktdaten,
                                     keep_raw=keep_raw)
            await asyncio.to_thread(store.finish_job, job_id, "done", result=result)
            log.info("job %s done overall=%s", job_id, result.get("overall"))
            if melden:
                # Kehrt sofort zurueck und wirft nie - die Zustellung laeuft
                # in einem eigenen Task, der Job ist hier schon fertig.
                vilikon.melden(vilikon.info_lead, {"id": job_id, "url": url, "result": result,
                                                   "finished_at": time.time()})
        except NotAuditableError as e:
            await asyncio.to_thread(store.finish_job, job_id, "nicht_auditierbar", grund=str(e))
            log.info("job %s nicht_auditierbar", job_id)
        except Exception:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            await asyncio.to_thread(store.finish_job, job_id, "failed", grund="interner Fehler")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/audit")
async def enqueue(req: AuditReq, request: Request, x_shared_secret: str | None = Header(default=None)):
    client = _resolve_client(x_shared_secret)
    subject = _subject(request, client)
    domain = _host(req.url)
    if not domain or "." not in domain:
        raise HTTPException(status_code=400, detail="Ungueltige URL.")

    # Domain-Cooldown: frisches Ergebnis wiederverwenden (spart Kosten, kein
    # Limit-Verbrauch). Die Suche laeuft ueber alle Mandanten - eine fremde
    # Seite soll nicht zweimal gecrawlt werden, nur weil zwei Mandanten sie
    # kurz hintereinander anfragen.
    cached = await asyncio.to_thread(store.recent_done_for_domain, domain, client.cooldown_s)
    if cached:
        if (cached.get("client_id") or clients.LEGACY_CLIENT_ID) == client.id:
            return JSONResponse(status_code=200,
                content={"job_id": cached["id"], "status": "done", "cached": True})
        # Ergebnis eines anderen Mandanten: dem Aufrufer als eigenen Auftrag
        # gutschreiben. Sonst wuerde die Eigentumspruefung auf GET die gerade
        # ausgelieferte job_id mit 404 abweisen.
        eigene_id = secrets.token_hex(16)
        await asyncio.to_thread(store.clone_job_for_client, cached, eigene_id, client.id)
        if _meldet(client):
            # Ein Besucher hat die Seite geprueft, auch wenn das Ergebnis
            # geerbt ist - fuer Vilikon ist das ein Check wie jeder andere.
            vilikon.melden(vilikon.info_lead, {**cached, "id": eigene_id})
        return JSONResponse(status_code=200,
            content={"job_id": eigene_id, "status": "done", "cached": True})

    # Warteschlangen-Grenze: Wer seine Ergebnisse nicht abholt, soll nicht
    # weiter Auftraege auftuermen. Vor dem Rate-Limit geprueft, damit ein
    # ueberfuelltes Postfach nicht zusaetzlich das Tageslimit verbraucht.
    if client.max_queued:
        offen = await asyncio.to_thread(store.count_pending_for_client, client.id)
        if offen >= client.max_queued:
            raise HTTPException(status_code=429,
                detail=f"Es laufen schon {offen} Auftraege. Bitte erst Ergebnisse abholen.")

    # Rate-Limit (Stunde + Tag), gezaehlt gegen subject.
    per_hour = await asyncio.to_thread(store.count_requests, subject, 3600)
    per_day = await asyncio.to_thread(store.count_requests, subject, 86400)
    if per_hour >= client.rate_hour or per_day >= client.rate_day:
        raise HTTPException(status_code=429,
            detail="Zu viele Pruefungen. Bitte kontaktieren Sie uns direkt.")

    await asyncio.to_thread(store.log_request, subject)
    # Die Job-ID ist der einzige Schutz davor, fremde Ergebnisse zu erraten.
    job_id = secrets.token_hex(16)
    await asyncio.to_thread(store.create_job, job_id, req.url, domain, client.id)
    asyncio.create_task(_run_job(job_id, req.url,
                                 hintergrund=client.hintergrund,
                                 kontaktdaten=client.kontaktdaten,
                                 keep_raw=client.keep_raw,
                                 melden=_meldet(client)))
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "pending"})


def _meldet(client: Client) -> bool:
    """Nur Checks des Trichters gehen an Vilikon.

    Die Auftraege des Akquise-Agenten hat Vilikon selbst erteilt - sie
    zurueckzumelden waere ein Kreis.
    """
    return client.id == clients.LEGACY_CLIENT_ID


def _gehoert(job: dict | None, client: Client) -> bool:
    if not job:
        return False
    return (job.get("client_id") or clients.LEGACY_CLIENT_ID) == client.id


@app.get("/audit/{job_id}")
async def status(
    job_id: str,
    antwortformat: str = Query("voll", alias="format", pattern="^(voll|kompakt)$"),
    warten: int = Query(0, ge=0, le=60),
    x_shared_secret: str | None = Header(default=None),
):
    client = _resolve_client(x_shared_secret)
    job = await asyncio.to_thread(store.get_job, job_id)
    # Fremde Auftraege sehen aus wie unbekannte: 404, nicht 403. Ein 403 waere
    # ein Orakel - es wuerde bestaetigen, dass es diese job_id gibt.
    if not _gehoert(job, client):
        raise HTTPException(status_code=404, detail="Unbekannter Job.")

    # Long-Poll. Jeder Poll ist beim Agenten ein Werkzeug-Aufruf und kostet
    # Tokens; ein Audit dauert 30-60 Sekunden. Die Eigentumspruefung ist
    # bewusst VORHER gelaufen - eine fremde job_id haelt die Verbindung nicht.
    if warten and job["status"] == "pending":
        frist = time.monotonic() + warten
        while job["status"] == "pending" and time.monotonic() < frist:
            await asyncio.sleep(POLL_INTERVALL_S)
            job = await asyncio.to_thread(store.get_job, job_id)
            if not _gehoert(job, client):
                raise HTTPException(status_code=404, detail="Unbekannter Job.")

    s = job["status"]
    if s == "done":
        if antwortformat == "kompakt":
            # Ein geerbtes Ergebnis wurde spaeter angelegt als es fertig
            # wurde - daran ist eine Wiederverwendung erkennbar, ohne sie
            # zusaetzlich speichern zu muessen.
            geerbt = (job.get("created_at") or 0) > (job.get("finished_at") or 0)
            return kompakt.verdichten(job["result"] or {},
                                      geprueft_am=job.get("finished_at"),
                                      cached=geerbt)
        return {"status": "done", "result": job["result"]}
    if s in ("nicht_auditierbar", "failed"):
        return {"status": s, "grund": job.get("grund") or ""}
    return {"status": "pending"}


class AnfrageReq(BaseModel):
    """Was der Besucher ins Formular zum Check geschrieben hat - nicht mehr.

    Die Grenzen folgen dem Lead-Format des Cockpits (LEADS-API.md); der
    Strato-Proxy laesst ohnehin hoechstens 8 kB durch.
    """
    model_config = ConfigDict(extra="ignore")

    # "umbau" | "ki-beratung" - wie `typ` in der Web3Forms-Mail.
    typ: str = Field("umbau", max_length=40, pattern=r"^[a-z0-9-]+$")
    name: str = Field("", max_length=200)
    email: str = Field("", max_length=254)
    telefon: str = Field("", max_length=60)
    nachricht: str = Field("", max_length=5000)
    paket_wunsch: str = Field("", max_length=120)
    sprache: str = Field("", max_length=5)


@app.post("/audit/{job_id}", status_code=202)
async def anfrage(job_id: str, req: AnfrageReq, request: Request,
                  x_shared_secret: str | None = Header(default=None)):
    """Anfrage zu einem Check (Formular „Umbau-Anfrage (mit Audit)").

    Antwortet sofort mit 202, ob das Cockpit erreichbar ist oder nicht - die
    Web3Forms-Mail laeuft daneben weiter, der Besucher merkt von Vilikon nichts.
    """
    client = _resolve_client(x_shared_secret)
    job = await asyncio.to_thread(store.get_job, job_id)
    # Wie beim Abholen: fremd und unbekannt sehen gleich aus. Anfragen gibt es
    # nur zu Checks des Trichters.
    if not _meldet(client) or not _gehoert(job, client):
        raise HTTPException(status_code=404, detail="Unbekannter Job.")
    if not req.email.strip() and not req.telefon.strip():
        raise HTTPException(status_code=422, detail="E-Mail oder Telefon fehlt.")

    # Eigener Zaehler neben dem fuer Pruefungen: Jede Anfrage ist ein Aufruf
    # beim Cockpit, und dessen Limit teilen sich alle Checks.
    subject = "anfrage:" + _subject(request, client)
    per_hour = await asyncio.to_thread(store.count_requests, subject, 3600)
    per_day = await asyncio.to_thread(store.count_requests, subject, 86400)
    if per_hour >= client.rate_hour or per_day >= client.rate_day:
        raise HTTPException(status_code=429,
            detail="Zu viele Anfragen. Bitte kontaktieren Sie uns direkt.")
    await asyncio.to_thread(store.log_request, subject)

    vilikon.melden(vilikon.anfrage_lead, job, req.model_dump())
    return {"status": "angenommen"}
