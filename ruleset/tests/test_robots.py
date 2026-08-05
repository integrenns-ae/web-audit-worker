"""
Tests für pipeline/robots.py — das Höflichkeits-Gate vor dem Crawl.

Laufen ohne Netzwerk: fetch_robots_txt wird gemonkeypatcht, damit die reine
Entscheidungslogik geprüft wird (welche robots.txt sperrt uns aus, welche nicht).

Ausführen:
    pytest tests/ -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline import robots


def allowed_for(monkeypatch, body):
    """is_crawl_allowed() mit vorgegebenem robots.txt-Inhalt (None = keine)."""
    monkeypatch.setattr(robots, "fetch_robots_txt", lambda url: body)
    ok, _reason = robots.is_crawl_allowed("https://beispiel.de")
    return ok


# --- Fälle, die uns NICHT stoppen dürfen -------------------------------------

def test_keine_robots_txt_erlaubt(monkeypatch):
    assert allowed_for(monkeypatch, None) is True


def test_leere_robots_txt_erlaubt(monkeypatch):
    assert allowed_for(monkeypatch, "") is True


def test_offene_robots_txt_erlaubt(monkeypatch):
    assert allowed_for(monkeypatch, "User-agent: *\nDisallow:\n") is True


def test_teilpfad_sperre_erlaubt(monkeypatch):
    """"Disallow: /admin" sperrt nicht die Startseite — wir laufen weiter."""
    assert allowed_for(monkeypatch, "User-agent: *\nDisallow: /admin\nDisallow: /intern\n") is True


def test_sperre_fuer_anderen_bot_erlaubt(monkeypatch):
    """Ein Voll-Disallow, das ausdrücklich nur einen fremden Bot meint."""
    body = "User-agent: GPTBot\nDisallow: /\n"
    assert allowed_for(monkeypatch, body) is True


def test_kaputte_robots_txt_erlaubt(monkeypatch):
    assert allowed_for(monkeypatch, "<!DOCTYPE html><html><body>404</body></html>") is True


def test_wildcard_disallow_mit_expliziter_ausnahme_fuer_uns(monkeypatch):
    """Site sperrt alle Bots, gibt unseren aber ausdrücklich frei."""
    body = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: IntegrennsAuditBot\nDisallow:\n"
    )
    assert allowed_for(monkeypatch, body) is True


# --- Fälle, die uns stoppen MÜSSEN ------------------------------------------

def test_wildcard_voll_disallow_stoppt(monkeypatch):
    assert allowed_for(monkeypatch, "User-agent: *\nDisallow: /\n") is False


def test_namentliche_voll_sperre_stoppt(monkeypatch):
    body = "User-agent: IntegrennsAuditBot\nDisallow: /\n"
    assert allowed_for(monkeypatch, body) is False


def test_namentliche_sperre_gewinnt_gegen_offenen_wildcard(monkeypatch):
    """Wildcard erlaubt alles, unser Bot ist aber namentlich ausgesperrt."""
    body = (
        "User-agent: *\nDisallow:\n\n"
        "User-agent: IntegrennsAuditBot\nDisallow: /\n"
    )
    assert allowed_for(monkeypatch, body) is False


def test_begruendung_wird_geliefert(monkeypatch):
    monkeypatch.setattr(robots, "fetch_robots_txt", lambda url: "User-agent: *\nDisallow: /\n")
    ok, reason = robots.is_crawl_allowed("https://beispiel.de")
    assert ok is False
    assert reason  # nicht leer, für Log/Diagnose


# --- Bot-Name muss zum echten Crawler-UA passen ------------------------------

def test_bot_name_passt_zum_crawler_user_agent():
    """Sonst würde eine namentliche robots.txt-Regel ins Leere greifen."""
    crawl_src = (Path(__file__).parent.parent / "pipeline" / "crawl.py").read_text(encoding="utf-8")
    assert robots.BOT_NAME in crawl_src
