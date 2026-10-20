"""Gemeinsame Vorbereitung fuer die Worker-Tests.

Wichtig: app.py und store.py lesen ihre Konfiguration beim IMPORT aus der
Umgebung. Die Variablen werden deshalb hier gesetzt - bevor irgendein Test
die Module importiert - und nicht in den Tests selbst.

Diese Tests sichern den Vertrag des Website-Trichters ab. Sie sind entstanden,
bevor der Worker mandantenfaehig wurde: Wenn hier etwas rot wird, ist der Weg
kaputt, den ein echter Besucher auf integrenns.de nimmt.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_SECRET = "pytest-shared-secret-0123456789"
AUTH = {"X-Shared-Secret": TEST_SECRET}

os.environ["AUDIT_SHARED_SECRET"] = TEST_SECRET
os.environ["AUDIT_RATE_HOUR"] = "5"
os.environ["AUDIT_RATE_DAY"] = "15"
os.environ["AUDIT_DOMAIN_COOLDOWN"] = "600"
os.environ["AUDIT_MAX_CONCURRENT"] = "3"
# Absichtlich unbrauchbar: Jeder Test bekommt per db-Fixture eine eigene Datei.
# Vergisst ein Test die Fixture, soll er scheitern - und nicht die echte
# Produktionsdatenbank /data/audit.db anfassen.
os.environ["AUDIT_DB_PATH"] = "/dev/null/niemals-benutzen/audit.db"

import pytest  # noqa: E402

FAKE_RESULT = {
    "overall": 73,
    "categories": {"technik": 80, "inhalt": 66},
    "findings": [],
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Frische SQLite-Datei je Test, danach Verbindung schliessen."""
    import store

    monkeypatch.setattr(store, "DB_PATH", str(tmp_path / "audit.db"))
    monkeypatch.setattr(store, "_conn", None)
    yield store
    if store._conn is not None:
        store._conn.close()


@pytest.fixture
def client(db, monkeypatch):
    """Worker ohne Hintergrundlauf - deterministisch fuer Auth und Limits."""
    from fastapi.testclient import TestClient

    import app as app_mod

    async def _kein_lauf(job_id: str, url: str) -> None:
        return None

    monkeypatch.setattr(app_mod, "_run_job", _kein_lauf)
    with TestClient(app_mod.app) as c:
        yield c


@pytest.fixture
def client_mit_audit(db, monkeypatch):
    """Echter _run_job, aber run_audit ist gefaelscht - kein Netz, kein Chromium."""
    from fastapi.testclient import TestClient

    import app as app_mod

    async def _fake_audit(url: str) -> dict:
        return dict(FAKE_RESULT)

    monkeypatch.setattr(app_mod, "run_audit", _fake_audit)
    with TestClient(app_mod.app) as c:
        yield c


def warte_auf_ende(c, job_id: str, timeout: float = 10.0) -> dict:
    """Pollt bis der Job nicht mehr pending ist. Gibt den Antwortkoerper zurueck."""
    ende = time.time() + timeout
    while time.time() < ende:
        r = c.get(f"/audit/{job_id}", headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        if body.get("status") != "pending":
            return body
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} blieb laenger als {timeout}s pending")
