"""Tests für pipeline/probe_network.py — Erkennung verlinkter Rechtsseiten.

Hintergrund (v.17): Die Erkennung prüfte nur die Adresse eines Links, nie
seine Beschriftung. Dadurch meldete das Regelwerk reihenweise „kein Impressum
auffindbar" auf Seiten, die ein vollständiges Impressum haben — es nur nicht
unter /impressum ablegen. Vier echte Fälle sind unten als Test festgehalten.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.probe_network import check_legal_pages_linked  # noqa: E402


# --- Die Fehlbefunde, die zu dieser Änderung geführt haben -----------------

def test_beschriftung_zaehlt_auch_ohne_passende_adresse():
    """Alle vier Seiten hatten ein vollständiges Impressum und wurden
    trotzdem mit -20 Punkten abgestraft."""
    echte_faelle = {
        "zahnarzt-argus.de": '<a href="#js-c641" uk-toggle>Impressum</a>',
        "tierarzt-hahn.de": '<a href="/about/">Impressum</a>',
        "gaststaette-lapp.de": '<a href="/about/">Impressum</a>',
        "recht-giessen.de": '<a href="#imprint" class="tara-menuitem">Impressum</a>',
    }
    for domain, html in echte_faelle.items():
        assert check_legal_pages_linked(html)["impressum_linked"] is True, domain


def test_adresse_zaehlt_weiterhin():
    """Gegenprobe: Das bisherige Verhalten bleibt erhalten."""
    h = '<a href="/impressum">Rechtliches</a>'
    assert check_legal_pages_linked(h)["impressum_linked"] is True


def test_englische_beschriftungen():
    for h in ('<a href="/about">Legal Notice</a>',
              '<a href="/x">Imprint</a>',
              '<a href="/y">Anbieterkennzeichnung</a>'):
        assert check_legal_pages_linked(h)["impressum_linked"] is True, h


def test_datenschutz_getrennt_erkannt():
    h = '<a href="/privacy-policy/">Datenschutz</a>'
    e = check_legal_pages_linked(h)
    assert e["datenschutz_linked"] is True
    assert e["impressum_linked"] is False


def test_privacy_als_adresse_und_als_text():
    assert check_legal_pages_linked('<a href="/privacy">Hinweise</a>')["datenschutz_linked"]
    assert check_legal_pages_linked('<a href="/x">Privacy</a>')["datenschutz_linked"]


# --- Was NICHT als Rechtslink gelten darf ----------------------------------

def test_ohne_rechtslink_bleibt_es_bei_false():
    h = '<a href="/kontakt">Kontakt</a><a href="/leistungen">Leistungen</a>'
    e = check_legal_pages_linked(h)
    assert e == {"impressum_linked": False, "datenschutz_linked": False}


def test_langer_fliesstext_ist_kein_navigationslink():
    """Sonst würde ein Blogbeitrag über Impressumspflichten als Impressum
    durchgehen. Die Längengrenze trennt Navigation von Fließtext."""
    h = ('<a href="/blog/1">Was alles in ein Impressum gehört und warum das '
         'für jede Website wichtig ist</a>')
    assert check_legal_pages_linked(h)["impressum_linked"] is False


def test_adresse_schlaegt_laengengrenze():
    """Zeigt die Adresse auf /impressum, ist die Textlänge egal."""
    h = ('<a href="/impressum">Hier finden Sie alle gesetzlich '
         'vorgeschriebenen Angaben zu unserem Unternehmen</a>')
    assert check_legal_pages_linked(h)["impressum_linked"] is True


# --- Robustheit -------------------------------------------------------------

def test_html_entities_in_der_beschriftung():
    h = '<a href="/x">Impressum&nbsp;&amp;&nbsp;Datenschutz</a>'
    e = check_legal_pages_linked(h)
    assert e["impressum_linked"] is True
    assert e["datenschutz_linked"] is True


def test_verschachtelte_elemente_in_der_beschriftung():
    h = '<a href="/x"><span class="i"></span> <strong>Impressum</strong></a>'
    assert check_legal_pages_linked(h)["impressum_linked"] is True


def test_grossschreibung_egal():
    assert check_legal_pages_linked('<a HREF="/X">IMPRESSUM</a>')["impressum_linked"] is True


def test_mehrzeiliger_link():
    h = '<a\n  href="/about/"\n  class="footer"\n>\n  Impressum\n</a>'
    assert check_legal_pages_linked(h)["impressum_linked"] is True


def test_leeres_html_stuerzt_nicht_ab():
    assert check_legal_pages_linked("") == {
        "impressum_linked": False, "datenschutz_linked": False}
