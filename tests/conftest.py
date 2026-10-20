"""Gemeinsame Vorbereitung fuer die Worker-Tests.

Wichtig: store.py liest DB_PATH beim IMPORT aus der Umgebung, clients.py liest
das Shared-Secret bei JEDEM Aufruf. Die Variablen werden deshalb hier gesetzt -
bevor irgendein Test die Module importiert - und nicht in den Tests selbst.

Diese Tests sichern den Vertrag des Website-Trichters ab. Wenn hier etwas rot
wird, ist der Weg kaputt, den ein echter Besucher auf integrenns.de nimmt.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TEST_SECRET = "pytest-shared-secret-0123456789"
AUTH = {"X-Shared-Secret": TEST_SECRET}

# Geheimnis eines zweiten Mandanten (steht fuer den Akquise-Agenten).
AGENT_SECRET = "pytest-agent-secret-9876543210"
AGENT_AUTH = {"X-Shared-Secret": AGENT_SECRET}

os.environ["AUDIT_SHARED_SECRET"] = TEST_SECRET
os.environ["AUDIT_RATE_HOUR"] = "5"
os.environ["AUDIT_RATE_DAY"] = "15"
os.environ["AUDIT_DOMAIN_COOLDOWN"] = "600"
os.environ["AUDIT_MAX_CONCURRENT"] = "3"
# Absichtlich unbrauchbar: Jeder Test bekommt per db-Fixture eine eigene Datei.
# Vergisst ein Test die Fixture, soll er scheitern - und nicht die echte
# Produktionsdatenbank /data/audit.db anfassen.
os.environ["AUDIT_DB_PATH"] = "/dev/null/niemals-benutzen/audit.db"
os.environ["AUDIT_CLIENTS_PATH"] = "/dev/null/niemals-benutzen/clients.json"
# Echtes Regelwerk aus dem Repo - damit der Drift-Test in test_kompakt.py die
# Gewichtstabelle gegen ruleset.yaml pruefen kann.
os.environ["RULESET_DIR"] = str(REPO / "ruleset")

import pytest  # noqa: E402

FAKE_RESULT = {
    "overall": 73,
    "category_scores": {"technik_performance": 20, "recht": 0},
    "findings": [],
}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tmp_pfad_runs() -> Path:
    """Leeres Verzeichnis, damit prune_old_runs() nie echte Laeufe anfasst."""
    import tempfile

    return Path(tempfile.mkdtemp(prefix="runs-test-"))


@pytest.fixture(autouse=True)
def _mandanten_cache_leeren():
    """clients.py haelt die geladene clients.json in einem Modul-Cache.
    Ohne dieses Zuruecksetzen wuerde ein Test den naechsten beeinflussen."""
    import clients

    import kompakt

    clients.reload()
    kompakt.reload()
    yield
    clients.reload()
    kompakt.reload()


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
def mandanten_datei(tmp_path, monkeypatch):
    """Schreibt eine clients.json und richtet clients.py darauf aus.

    Nimmt entweder eine Liste von Eintraegen oder rohen Text (fuer kaputte
    Dateien).
    """
    import clients

    pfad = tmp_path / "clients.json"
    monkeypatch.setattr(clients, "CLIENTS_PATH", pfad)

    def schreiben(inhalt) -> Path:
        pfad.write_text(
            inhalt if isinstance(inhalt, str) else json.dumps(inhalt),
            encoding="utf-8",
        )
        clients.reload()
        return pfad

    return schreiben


AGENT_EINTRAG = {
    "id": "vilikon",
    "secrets_sha256": [sha256(AGENT_SECRET)],
    "limit_scope": "client",
    "rate_hour": 20,
    "rate_day": 60,
    "cooldown_s": 43200,
    "max_queued": 2,
    "trust_client_ip": False,
    "keep_raw": False,
    "kontaktdaten": True,
    "retention_days": 30,
}


@pytest.fixture
def client(db, monkeypatch):
    """Worker ohne Hintergrundlauf - deterministisch fuer Auth und Limits."""
    from fastapi.testclient import TestClient

    import app as app_mod

    async def _kein_lauf(job_id: str, url: str, **_egal) -> None:
        return None

    monkeypatch.setattr(app_mod, "_run_job", _kein_lauf)
    monkeypatch.setattr(app_mod, "RUNS_DIR", tmp_pfad_runs())
    with TestClient(app_mod.app) as c:
        yield c


@pytest.fixture
def client_mit_audit(db, monkeypatch):
    """Echter _run_job, aber run_audit ist gefaelscht - kein Netz, kein Chromium."""
    from fastapi.testclient import TestClient

    import app as app_mod

    async def _fake_audit(url: str, **_egal) -> dict:
        return dict(FAKE_RESULT)

    monkeypatch.setattr(app_mod, "run_audit", _fake_audit)
    monkeypatch.setattr(app_mod, "RUNS_DIR", tmp_pfad_runs())
    with TestClient(app_mod.app) as c:
        yield c


def warte_auf_ende(c, job_id: str, kopf: dict | None = None, timeout: float = 10.0) -> dict:
    """Pollt bis der Job nicht mehr pending ist. Gibt den Antwortkoerper zurueck."""
    ende = time.time() + timeout
    while time.time() < ende:
        r = c.get(f"/audit/{job_id}", headers=kopf or AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        if body.get("status") != "pending":
            return body
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} blieb laenger als {timeout}s pending")
