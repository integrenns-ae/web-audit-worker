"""clients — Mandanten des Audit-Workers.

Der Worker hat zwei Nutzer: den **Website-Trichter** auf integrenns.de (über
den Strato-Proxy) und den **Akquise-Agenten** auf dem Vilikon-VPS. Beide
weisen sich mit `X-Shared-Secret` aus; welcher Mandant ruft, entscheidet das
Geheimnis. Davon haengen Limits, Datenhaltung und Antwortformat ab.

Die wichtigste Eigenschaft dieses Moduls ist eine Reihenfolge:

    Der Trichter haengt NICHT von clients.json ab.

Sein Zugang kommt weiterhin allein aus `AUDIT_SHARED_SECRET` und wird ZUERST
geprueft. Eine fehlende, leere oder kaputte clients.json fuehrt zu einer
leeren Mandantenliste und einem Fehlerprotokoll - niemals zu einem 401 fuer
einen echten Besucher. Der Trichter ist der Weg, der Geld verdient; ein
Nebenverbraucher darf ihn nicht umbringen koennen.

In clients.json stehen ausschliesslich SHA-256-Hashes. Wer die Datei lesen
kann, kann sich damit nicht anmelden. Der Klartext lebt nur in Vilikons Key
Store.

Betriebsfalle: Die Datei wird EINMAL gelesen. Jede Aenderung braucht
`docker compose restart audit-worker`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("audit-worker.clients")

# Der Trichter. Dieser Name steht als client_id in der Datenbank und ist
# deshalb fuer clients.json gesperrt.
LEGACY_CLIENT_ID = "trichter"

CLIENTS_PATH = Path(os.environ.get("AUDIT_CLIENTS_PATH", "/app/clients.json"))

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class Client:
    """Ein Aufrufer mit seinen Regeln."""

    id: str
    # "ip"     -> Limit pro Besucher-IP (Trichter: viele Menschen, ein Zugang)
    # "client" -> Limit pro Mandant (Agent: eine Maschine, immer dieselbe IP)
    limit_scope: str
    rate_hour: int
    rate_day: int
    cooldown_s: int
    # 0 = unbegrenzt. Greift ab Stufe 3.
    max_queued: int = 0
    # Nur der Trichter darf die Besucher-IP per Header melden. Sonst koennte
    # jeder Geheimnis-Inhaber sein eigenes Limit beliebig zuruecksetzen.
    trust_client_ip: bool = False
    # Rohdaten der gepruefen Seite nach dem Lauf behalten? Greift ab Stufe 4.
    keep_raw: bool = True
    # Impressumsdaten mitliefern (fuer den Briefkanal)? Greift ab Stufe 4.
    kontaktdaten: bool = False
    retention_days: int | None = None


def _legacy_secret() -> str:
    return os.environ.get("AUDIT_SHARED_SECRET", "")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        log.warning("%s ist keine Zahl - benutze %s", name, default)
        return default


def legacy_client() -> Client:
    """Der Trichter, genau mit den Werten, die er heute schon hat."""
    return Client(
        id=LEGACY_CLIENT_ID,
        limit_scope="ip",
        rate_hour=_int_env("AUDIT_RATE_HOUR", 5),
        rate_day=_int_env("AUDIT_RATE_DAY", 15),
        cooldown_s=_int_env("AUDIT_DOMAIN_COOLDOWN", 600),
        max_queued=0,
        trust_client_ip=True,
        keep_raw=True,
        kontaktdaten=False,
        retention_days=None,
    )


def legacy_configured() -> bool:
    """False, wenn kein brauchbares Shared-Secret gesetzt ist (fail-closed)."""
    s = _legacy_secret()
    return bool(s) and s not in ("changeme", "CHANGE_ME")


# --- clients.json -----------------------------------------------------------

_cache: dict[str, Client] | None = None     # Hash -> Client


def _eintrag_lesen(roh: object, nr: int) -> tuple[Client, list[str]] | None:
    """Prueft einen Eintrag streng. Bei Zweifel: verwerfen und protokollieren."""
    if not isinstance(roh, dict):
        log.error("clients.json Eintrag %d ist kein Objekt - verworfen", nr)
        return None

    kid = roh.get("id")
    if not isinstance(kid, str) or not kid.strip():
        log.error("clients.json Eintrag %d hat keine brauchbare id - verworfen", nr)
        return None
    kid = kid.strip()
    if kid == LEGACY_CLIENT_ID:
        log.error("clients.json: id %r ist fuer den Trichter reserviert - verworfen", kid)
        return None

    hashes = roh.get("secrets_sha256")
    if not isinstance(hashes, list) or not hashes:
        log.error("clients.json %r hat keine secrets_sha256-Liste - verworfen", kid)
        return None
    sauber = [h.lower() for h in hashes if isinstance(h, str) and _HEX64.match(h.strip().lower())]
    if len(sauber) != len(hashes):
        log.error("clients.json %r: nicht jeder Hash ist 64 Hex-Zeichen - verworfen", kid)
        return None

    scope = roh.get("limit_scope", "client")
    if scope not in ("ip", "client"):
        log.error("clients.json %r: limit_scope %r unbekannt - verworfen", kid, scope)
        return None
    # Nur der Trichter sitzt hinter einem Proxy, der die echte Besucher-IP
    # kennt. Ein IP-gezaehlter Fremdmandant ohne diesen Proxy wuerde sich
    # sonst hinter wechselnden Headern verstecken.
    vertraut_ip = bool(roh.get("trust_client_ip", False))
    if vertraut_ip and scope != "ip":
        log.error("clients.json %r: trust_client_ip ohne limit_scope=ip - verworfen", kid)
        return None

    def zahl(feld: str, standard: int) -> int:
        wert = roh.get(feld, standard)
        return wert if isinstance(wert, int) and not isinstance(wert, bool) and wert >= 0 else standard

    aufbewahrung = roh.get("retention_days")
    if not (isinstance(aufbewahrung, int) and not isinstance(aufbewahrung, bool) and aufbewahrung > 0):
        aufbewahrung = None

    client = Client(
        id=kid,
        limit_scope=scope,
        rate_hour=zahl("rate_hour", 20),
        rate_day=zahl("rate_day", 60),
        cooldown_s=zahl("cooldown_s", 43200),
        max_queued=zahl("max_queued", 2),
        trust_client_ip=vertraut_ip,
        keep_raw=bool(roh.get("keep_raw", False)),
        kontaktdaten=bool(roh.get("kontaktdaten", False)),
        retention_days=aufbewahrung,
    )
    return client, sauber


def _laden() -> dict[str, Client]:
    if not CLIENTS_PATH.exists():
        log.info("keine %s - nur der Trichter ist zugelassen", CLIENTS_PATH)
        return {}
    try:
        daten = json.loads(CLIENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        # Bewusst kein Abbruch: Der Trichter muss weiterlaufen.
        log.error("%s nicht lesbar (%s) - nur der Trichter ist zugelassen", CLIENTS_PATH, e)
        return {}
    if not isinstance(daten, list):
        log.error("%s ist keine Liste - nur der Trichter ist zugelassen", CLIENTS_PATH)
        return {}

    nach_hash: dict[str, Client] = {}
    for nr, roh in enumerate(daten, start=1):
        gelesen = _eintrag_lesen(roh, nr)
        if gelesen is None:
            continue
        client, hashes = gelesen
        for h in hashes:
            if h in nach_hash:
                log.error("clients.json: Hash doppelt vergeben (%r und %r) - zweiter verworfen",
                          nach_hash[h].id, client.id)
                continue
            nach_hash[h] = client
    if nach_hash:
        log.info("Mandanten geladen: %s", sorted({c.id for c in nach_hash.values()}))
    return nach_hash


def reload() -> None:
    """Cache verwerfen. Fuer Tests; im Betrieb genuegt ein Container-Restart."""
    global _cache
    _cache = None


def resolve(provided: str | None) -> Client | None:
    """Geheimnis -> Mandant, oder None bei ungueltigem Geheimnis."""
    global _cache
    if not provided:
        return None

    # 1. Der Trichter zuerst - unabhaengig davon, ob clients.json existiert
    #    oder gueltig ist. Das ist die eigentliche Zusage dieses Moduls.
    if legacy_configured():
        if hmac.compare_digest(provided.encode("utf-8"), _legacy_secret().encode("utf-8")):
            return legacy_client()

    # 2. Mandanten aus clients.json, Vergleich ueber den Hash.
    if _cache is None:
        _cache = _laden()
    if not _cache:
        return None
    digest = hashlib.sha256(provided.encode("utf-8")).hexdigest()
    for h, client in _cache.items():
        if hmac.compare_digest(digest, h):
            return client
    return None


def alle_mandanten() -> list[Client]:
    """Alle Mandanten aus clients.json (ohne den Trichter).

    Nur fuer Aufraeumarbeiten gedacht - der Trichter fehlt hier bewusst, weil
    seine Aufbewahrung global aus der Datenschutzerklaerung kommt und nicht
    je Mandant eingestellt wird.
    """
    global _cache
    if _cache is None:
        _cache = _laden()
    eindeutig = {c.id: c for c in _cache.values()}
    return [eindeutig[k] for k in sorted(eindeutig)]
