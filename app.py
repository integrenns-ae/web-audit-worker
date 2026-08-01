"""audit-worker — FastAPI-Gegenpart zum /website-check-Trichter.

Vertrag (2026-07-28):
  POST /audit            {url}            -> 202 {job_id, status:"pending"}
  GET  /audit/{job_id}                    -> 200 {status, result?|grund?}
  GET  /health                            -> {status:"ok"}

Aufruf server-seitig vom Strato-PHP-Proxy mit Header X-Shared-Secret.
Scoring steckt in audit.run_audit() (Integrationsnaht für das echte Regelwerk).
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import store
from audit import NotAuditableError, _host, run_audit

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("audit-worker")

SHARED_SECRET = os.environ.get("AUDIT_SHARED_SECRET", "")
RATE_HOUR = int(os.environ.get("AUDIT_RATE_HOUR", "5"))
RATE_DAY = int(os.environ.get("AUDIT_RATE_DAY", "15"))
DOMAIN_COOLDOWN = int(os.environ.get("AUDIT_DOMAIN_COOLDOWN", "600"))   # 10 min
MAX_CONCURRENT = int(os.environ.get("AUDIT_MAX_CONCURRENT", "3"))
MAX_URL = 300

SEM = asyncio.Semaphore(MAX_CONCURRENT)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.mark_stale_pending_failed(older_than_s=900)   # Crash-Recovery
    store.cleanup_req_log(older_than_s=86400)
    yield


app = FastAPI(title="audit-worker", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class AuditReq(BaseModel):
    url: str = Field(..., min_length=3, max_length=MAX_URL)


def _check_secret(provided: str | None) -> None:
    if not SHARED_SECRET or SHARED_SECRET in ("", "changeme", "CHANGE_ME"):
        raise HTTPException(status_code=503, detail="Service nicht konfiguriert.")
    if not provided or not hmac.compare_digest(provided, SHARED_SECRET):
        raise HTTPException(status_code=401, detail="Ungueltiges oder fehlendes Token.")


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


async def _run_job(job_id: str, url: str) -> None:
    async with SEM:   # globale Obergrenze gleichzeitiger Audits
        try:
            result = await run_audit(url)
            await asyncio.to_thread(store.finish_job, job_id, "done", result=result)
            log.info("job %s done overall=%s", job_id, result.get("overall"))
        except NotAuditableError as e:
            await asyncio.to_thread(store.finish_job, job_id, "nicht_auditierbar", grund=str(e))
            log.info("job %s nicht_auditierbar", job_id)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            await asyncio.to_thread(store.finish_job, job_id, "failed", grund="interner Fehler")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/audit")
async def enqueue(req: AuditReq, request: Request, x_shared_secret: str | None = Header(default=None)):
    _check_secret(x_shared_secret)
    ip = _client_ip(request)
    domain = _host(req.url)
    if not domain or "." not in domain:
        raise HTTPException(status_code=400, detail="Ungueltige URL.")

    # Domain-Cooldown: frisches Ergebnis wiederverwenden (spart Kosten, kein Limit-Verbrauch).
    cached = await asyncio.to_thread(store.recent_done_for_domain, domain, DOMAIN_COOLDOWN)
    if cached:
        return JSONResponse(status_code=200, content={"job_id": cached["id"], "status": "done", "cached": True})

    # Rate-Limit pro IP (Stunde + Tag).
    per_hour = await asyncio.to_thread(store.count_requests, ip, 3600)
    per_day = await asyncio.to_thread(store.count_requests, ip, 86400)
    if per_hour >= RATE_HOUR or per_day >= RATE_DAY:
        raise HTTPException(status_code=429,
            detail="Zu viele Pruefungen. Bitte kontaktieren Sie uns direkt.")

    await asyncio.to_thread(store.log_request, ip)
    job_id = secrets.token_hex(4)
    await asyncio.to_thread(store.create_job, job_id, req.url, domain)
    asyncio.create_task(_run_job(job_id, req.url))
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "pending"})


@app.get("/audit/{job_id}")
async def status(job_id: str, x_shared_secret: str | None = Header(default=None)):
    _check_secret(x_shared_secret)
    job = await asyncio.to_thread(store.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unbekannter Job.")
    s = job["status"]
    if s == "done":
        return {"status": "done", "result": job["result"]}
    if s in ("nicht_auditierbar", "failed"):
        return {"status": s, "grund": job.get("grund") or ""}
    return {"status": "pending"}
