"""
Tests für pipeline/score.py — laufen komplett ohne Netzwerk, nur mit den
Fixture-Dateien unter tests/fixtures/. Das ist bewusst der Teil der Pipeline,
der sich in jeder Umgebung testen lässt, auch ohne Playwright/echten Crawl.

Ausführen:
    cd audit-pipeline
    pip install -r requirements.txt
    pytest tests/ -v
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.score import load_ruleset, score, CHECKS, classify_business_locality, is_non_profit

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str, domain: str):
    d = FIXTURES / name
    crawl = json.loads((d / f"{domain}_crawl.json").read_text())
    infra = json.loads((d / f"{domain}_probe_infra.json").read_text())
    perf = json.loads((d / f"{domain}_probe_perf.json").read_text())
    network = json.loads((d / f"{domain}_probe_network.json").read_text())
    return infra, perf, network, crawl


def test_ruleset_loads():
    ruleset = load_ruleset()
    assert ruleset["version"]
    assert ruleset["floor"] == 15
    assert sum(ruleset["category_weights"].values()) == 100


def test_all_automated_rules_have_check_functions():
    """Verhindert stille Lücken: jede als automated=true markierte Regel MUSS
    eine Prüf-Funktion in CHECKS haben, sonst wird sie in score() lautlos
    übersprungen und niemand merkt es."""
    ruleset = load_ruleset()
    automated_ids = {r["id"] for r in ruleset["rules"] if r.get("automated")}
    missing = [rid for rid in automated_ids if CHECKS.get(rid) is None]
    assert not missing, f"Als automatisiert markierte Regeln ohne Check-Funktion: {missing}"


def test_every_rule_id_is_unique():
    ruleset = load_ruleset()
    ids = [r["id"] for r in ruleset["rules"]]
    assert len(ids) == len(set(ids)), "Doppelte Regel-IDs im Regelwerk gefunden"


def test_clean_site_scores_100():
    infra, perf, network, crawl = load_fixture("clean_site", "clean.example")
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)
    assert result["overall"] == 100, f"Erwartete 100, bekam {result['overall']} — Findings: {result['findings']}"
    assert result["findings"] == []


def test_bad_site_triggers_expected_findings():
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)

    triggered_ids = {f["id"] for f in result["findings"]}

    expected = {
        "technik.fcp_over_4s",
        "technik.page_weight_over_5mb",
        "technik.outdated_cms_known_vulnerable",
        "mobil.viewport_blocks_zoom",
        "mobil.alt_text_missing_majority",
        "recht.impressum_missing",
        "recht.datenschutz_missing",
        "recht.third_party_before_consent",
        "seo.no_localbusiness_schema",
        "seo.no_sitemap_or_robots",
        "seo.missing_or_default_title",
        "seo.missing_meta_description",
        "seo.missing_canonical",
        "seo.missing_lang_attribute",
        "seo.missing_open_graph",
        "inhalt.construction_notice_visible",
        "inhalt.copyright_year_old",
        "inhalt.template_placeholder_in_production",
        "inhalt.no_references_or_portfolio",
        "inhalt.dead_external_link",
    }
    missing = expected - triggered_ids
    unexpected = triggered_ids - expected
    assert not missing, f"Erwartete Findings fehlen: {missing}"
    assert not unexpected, f"Unerwartete zusätzliche Findings: {unexpected}"

    # Recht-Kategorie: 20 Gewicht - 20 (impressum) - 15 (datenschutz, gedeckelt bei 20) - 12 (tracker, gedeckelt) -> 0
    assert result["category_scores"]["recht"] == 0
    assert result["overall"] < 50


def test_floor_never_undercut():
    """Auch ein Rundum-Totalausfall darf den Gesamtscore nicht unter den
    Floor aus ruleset.yaml drücken."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    ruleset = load_ruleset()
    # künstlich verschärfen: alle Kategorien auf 0 zwingen
    ruleset_copy = dict(ruleset)
    result = score(infra, perf, network, crawl, ruleset_copy)
    for cat in result["category_scores"]:
        assert result["category_scores"][cat] >= 0
    assert result["overall"] >= ruleset["floor"]


def test_third_party_deduction_is_capped():
    """recht.third_party_before_consent hat max_deduction=12 trotz 8 pro Fund -
    zwei Tracker sollten hier bereits an den Deckel stoßen (2*8=16 -> gedeckelt 12)."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)
    finding = next(f for f in result["findings"] if f["id"] == "recht.third_party_before_consent")
    assert finding["count"] == 2
    assert finding["score_impact"] == -12  # gedeckelt, nicht -16


def test_manual_review_rules_are_listed_separately():
    ruleset = load_ruleset()
    infra, perf, network, crawl = load_fixture("clean_site", "clean.example")
    result = score(infra, perf, network, crawl, ruleset)
    manual_ids = {r["id"] for r in result["manual_review_needed"]}
    assert "mobil.tap_targets_too_small" in manual_ids
    assert "seo.no_google_business_profile" in manual_ids
    assert "seo.no_location_in_title_h1" in manual_ids
    assert "inhalt.thin_content_page" in manual_ids
    # manuelle Regeln duerfen NIE als automatisierte Findings auftauchen
    finding_ids = {f["id"] for f in result["findings"]}
    assert manual_ids.isdisjoint(finding_ids)


def _crawl_with_impressum(text: str) -> dict:
    return {"legal_subpages": {"impressum": {"reachable": True, "text_content": text}}}


def test_natural_person_impressum_needs_no_authorized_rep():
    """False-Positive-Fix: eine natürliche Person / Freiberufler ohne
    Rechtsform-Marker braucht keinen Vertretungsberechtigten - die Regel darf
    hier NICHT auslösen (war der Fehlbefund bei integrenns.de)."""
    check = CHECKS["recht.impressum_no_authorized_rep"]
    crawl = _crawl_with_impressum(
        "Angaben gemäß § 5 DDG. Alexander Enns, Freelance IT consultant, "
        "Am Heiligenstock 18, 35305 Grünberg."
    )
    assert check(None, None, None, crawl) is False


def test_legal_entity_without_rep_still_flagged():
    """Gegenprobe: eine GmbH OHNE genannten Vertretungsberechtigten muss weiterhin
    auslösen - der Fix darf echte Mängel nicht verstecken."""
    check = CHECKS["recht.impressum_no_authorized_rep"]
    crawl = _crawl_with_impressum(
        "Muster Bau GmbH, Musterstraße 1, 12345 Musterstadt. Angaben gemäß § 5 DDG."
    )
    assert check(None, None, None, crawl) is True


def test_legal_entity_with_rep_not_flagged():
    check = CHECKS["recht.impressum_no_authorized_rep"]
    crawl = _crawl_with_impressum(
        "Muster Bau GmbH, vertreten durch den Geschäftsführer Max Meier."
    )
    assert check(None, None, None, crawl) is False


def test_template_placeholder_link_detected():
    """Neue Regel: Links auf johndoe/example.com/yourdomain werden erkannt -
    auch wenn sie technisch erreichbar sind (nur HTML-Scan, kein Status nötig)."""
    check = CHECKS["inhalt.template_placeholder_link"]
    html = (
        '<a href="https://linkedin.com/in/johndoe">LI</a>'
        '<a href="https://twitter.com/johndoe">TW</a>'
        '<a href="https://example.com/impressum">Beispiel</a>'
        '<a href="https://echte-firma.de/kontakt">echt</a>'
    )
    count = check(None, None, None, {"html": html})
    assert count == 3  # 2x johndoe + 1x example.com, der echte Link zählt nicht


def test_placeholder_links_not_double_counted_as_dead():
    """Ein johndoe-Link, der 403 liefert, darf NICHT zusätzlich als generisch
    toter Link zählen - sonst doppelte Bestrafung desselben Links."""
    dead_check = CHECKS["inhalt.dead_external_link"]
    infra = {"dead_external_links": [
        {"url": "https://linkedin.com/in/johndoe", "status": 403},   # Platzhalter -> ausschließen
        {"url": "https://echte-firma.de/tote-seite", "status": 404},  # echter toter Link -> zählt
    ]}
    assert dead_check(infra, None, None, {}) == 1


def test_classify_local_service_business():
    crawl = {"html": "<h1>Dachdecker Meier</h1><p>Öffnungszeiten Mo-Fr. Wir kommen zu Ihnen.</p>",
             "legal_subpages": {"impressum": {"text_content": ""}}}
    assert classify_business_locality(crawl) == "local"


def test_classify_non_local_service_business():
    crawl = {"html": "<h1>Freelance IT consultant</h1><p>Softwareentwicklung, system integration, bundesweit.</p>",
             "legal_subpages": {"impressum": {"text_content": "Freelance IT consultant"}}}
    assert classify_business_locality(crawl) == "non_local"


def test_classify_mixed_signals_is_unknown():
    """Sowohl lokal als auch nicht-lokal -> unknown, damit im Zweifel bewertet
    wird (der Klassifikator erlässt niemandem vorschnell die Lokal-Kritik)."""
    crawl = {"html": "Physiotherapie-Praxis mit Öffnungszeiten, auch Online-Beratung bundesweit.",
             "legal_subpages": {"impressum": {"text_content": ""}}}
    assert classify_business_locality(crawl) == "unknown"


def test_classify_no_signals_is_unknown():
    crawl = {"html": "<h1>Willkommen</h1>", "legal_subpages": {"impressum": {"text_content": ""}}}
    assert classify_business_locality(crawl) == "unknown"


def test_local_category_suppressed_for_non_local_business():
    """Bei einem ortsunabhängigen Anbieter zählt lokale_sichtbarkeit nicht:
    volle Punktzahl, keine Lokal-Findings, Regeln landen unter not_applicable."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    crawl = dict(crawl)
    crawl["html"] = crawl["html"] + " freelance softwareentwicklung bundesweit remote"
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)

    assert result["business_locality"] == "non_local"
    assert "lokale_sichtbarkeit" in result["suppressed_categories"]
    # volle Punktzahl trotz fehlendem LocalBusiness-Schema:
    assert result["category_scores"]["lokale_sichtbarkeit"] == ruleset["category_weights"]["lokale_sichtbarkeit"]
    # keine Lokal-Findings mehr in den Abzügen:
    assert not any(f["category"] == "lokale_sichtbarkeit" for f in result["findings"])
    # ... und auch nicht in der Manuell-Liste, sondern unter not_applicable:
    assert not any(r["category"] == "lokale_sichtbarkeit" for r in result["manual_review_needed"])
    assert any(r["category"] == "lokale_sichtbarkeit" for r in result["not_applicable"])


def test_local_category_scored_for_unknown_business():
    """Gegenprobe: unbekannter Geschäftstyp -> Kategorie wird normal bewertet
    (bad_site enthält keine Signale und muss weiter Lokal-Findings auslösen)."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)
    assert result["business_locality"] == "unknown"
    assert result["suppressed_categories"] == []
    assert any(f["id"] == "seo.no_localbusiness_schema" for f in result["findings"])


def _crawl_html(html: str) -> dict:
    return {"html": html, "legal_subpages": {"impressum": {"text_content": ""}}}


def test_default_template_title_flagged():
    for bad in ["<title>Lovable</title>", "<title>Vite App</title>", "<title>index</title>", ""]:
        assert CHECKS["seo.missing_or_default_title"](None, None, None, _crawl_html(bad)) is True


def test_real_title_not_flagged():
    ok = "<title>integrenns - KI-Integration & Expertise</title>"
    assert CHECKS["seo.missing_or_default_title"](None, None, None, _crawl_html(ok)) is False


def test_noindex_detected():
    yes = '<meta name="robots" content="noindex, nofollow">'
    no = '<meta name="robots" content="index, follow">'
    assert CHECKS["seo.noindex_detected"](None, None, None, _crawl_html(yes)) is True
    assert CHECKS["seo.noindex_detected"](None, None, None, _crawl_html(no)) is False


def test_h1_count_rule():
    assert CHECKS["seo.multiple_or_missing_h1"](None, None, None, _crawl_html("<h1>A</h1>")) is False
    assert CHECKS["seo.multiple_or_missing_h1"](None, None, None, _crawl_html("<h1>A</h1><h1>B</h1>")) is True
    assert CHECKS["seo.multiple_or_missing_h1"](None, None, None, _crawl_html("<p>kein h1</p>")) is True


def test_canonical_lang_og_checks():
    full = ('<html lang="de"><head><link rel="canonical" href="https://x.de/">'
            '<meta property="og:title" content="X"><meta property="og:description" content="Y">'
            '</head></html>')
    assert CHECKS["seo.missing_canonical"](None, None, None, _crawl_html(full)) is False
    assert CHECKS["seo.missing_lang_attribute"](None, None, None, _crawl_html(full)) is False
    assert CHECKS["seo.missing_open_graph"](None, None, None, _crawl_html(full)) is False
    empty = "<html><head></head></html>"
    assert CHECKS["seo.missing_canonical"](None, None, None, _crawl_html(empty)) is True
    assert CHECKS["seo.missing_lang_attribute"](None, None, None, _crawl_html(empty)) is True
    assert CHECKS["seo.missing_open_graph"](None, None, None, _crawl_html(empty)) is True


def test_seo_category_applies_to_non_local_business():
    """Anders als lokale_sichtbarkeit ist auffindbarkeit_seo universell: sie
    wird auch bei ortsunabhängigen Anbietern bewertet und NICHT unterdrückt."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    crawl = dict(crawl)
    crawl["html"] = crawl["html"] + " freelance softwareentwicklung bundesweit remote"
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)
    assert result["business_locality"] == "non_local"
    assert "lokale_sichtbarkeit" in result["suppressed_categories"]
    assert "auffindbarkeit_seo" not in result["suppressed_categories"]
    # SEO-Findings sind trotzdem da:
    assert any(f["category"] == "auffindbarkeit_seo" for f in result["findings"])


def test_bot_blocked_social_link_not_counted_dead():
    """Der eigentliche Fix: ein echtes LinkedIn-Profil, das dem Bot 503 liefert,
    darf NICHT als toter Link zählen. Ein echter toter Link auf einer normalen
    Domain zählt weiter."""
    dead_check = CHECKS["inhalt.dead_external_link"]
    infra = {"dead_external_links": [
        {"url": "https://www.linkedin.com/in/alexander-enns-37223a1a5/", "status": 503},
        {"url": "https://twitter.com/integrenns", "status": None},   # Timeout -> auch nicht zählen
        {"url": "https://echte-firma.de/tote-seite", "status": 404}, # echter toter Link -> zählt
    ]}
    assert dead_check(infra, None, None, {}) == 1


def test_genuine_404_on_social_domain_still_counts():
    """Gegenprobe: ein echtes 404 (nicht Anti-Bot-Status) auf einer Social-Domain
    bleibt zählbar - nur die typischen Bot-Abwehr-Status werden ausgenommen."""
    dead_check = CHECKS["inhalt.dead_external_link"]
    infra = {"dead_external_links": [
        {"url": "https://www.linkedin.com/in/geloeschtes-profil/", "status": 404},
    ]}
    assert dead_check(infra, None, None, {}) == 1


def test_unverified_links_surfaced_in_output():
    """Die bot-geblockten Links verschwinden nicht still, sondern werden separat
    ausgewiesen."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    infra = dict(infra)
    infra["dead_external_links"] = infra.get("dead_external_links", []) + [
        {"url": "https://www.linkedin.com/in/test/", "status": 999}
    ]
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)
    urls = [d["url"] for d in result.get("unverified_external_links", [])]
    assert any("linkedin.com" in u for u in urls)


def test_english_testimonials_section_satisfies_references():
    """Fix: ein englischer Testimonial-Abschnitt ('What clients say') zählt jetzt
    als Referenznachweis - vorher schlug die deutsch-only Liste hier fehl."""
    check = CHECKS["inhalt.no_references_or_portfolio"]
    html = '<section><h2>What clients say</h2><div class="testimonial">...</div></section>'
    assert check(None, None, None, {"html": html}) is False
    # Gegenprobe: gar kein Referenzteil -> feuert weiter
    assert check(None, None, None, {"html": "<h1>Willkommen</h1>"}) is True


def test_gbr_with_named_partners_not_flagged():
    """Härtung (wunderweb.de): eine GbR, die ihre Gesellschafter und den
    Inhaltsverantwortlichen nennt, hat eine erkennbare Vertretung -> kein Abzug."""
    check = CHECKS["recht.impressum_no_authorized_rep"]
    txt = ("wunderweb GbR, Alexander Bockshorn & Patrick Fratzscher, Händelstr. 6a, "
           "35625 Hüttenberg. Gesellschafter: Alexander Bockshorn, Patrick Fratzscher. "
           "Verantwortlich für den Inhalt nach § 55 Abs. 2 RStV: Alexander Bockshorn.")
    crawl = {"legal_subpages": {"impressum": {"reachable": True, "text_content": txt}}}
    assert check(None, None, None, crawl) is False


def test_local_positioning_flips_agency_to_scored():
    """Härtung: eine Web-Agentur mit Ortsname im Titel ('in Hüttenberg') gilt nicht
    mehr als rein ortsunabhängig -> lokale Sichtbarkeit wird bewertet (unknown)."""
    crawl = {
        "html": "<title>wunderweb | Internetagentur für Webentwicklung in Hüttenberg</title><h1></h1>",
        "legal_subpages": {"impressum": {"text_content": "wunderweb GbR, 35625 Hüttenberg"}},
    }
    assert classify_business_locality(crawl) == "unknown"


def test_hq_city_not_in_title_stays_non_local():
    """Gegenprobe: nennt der Titel KEINEN Ort (nur die Adresse steht im Impressum),
    bleibt ein reiner Software-Freiberufler ortsunabhängig."""
    crawl = {
        "html": "<title>integrenns - KI-Integration & Expertise</title>",
        "legal_subpages": {"impressum": {"text_content": "Alexander Enns, Freelance, 35305 Grünberg. softwareentwicklung"}},
    }
    assert classify_business_locality(crawl) == "non_local"


def test_einzelkaufmann_ek_not_flagged():
    """Härtung (.8): ein e.K. ist eine einzelne natürliche Person -> kein
    separater Vertreter nötig, darf nicht auslösen."""
    check = CHECKS["recht.impressum_no_authorized_rep"]
    crawl = {"legal_subpages": {"impressum": {"reachable": True,
             "text_content": "Max Mustermann e.K., Musterweg 3, 12345 Musterstadt."}}}
    assert check(None, None, None, crawl) is False


def test_gemeinschaftspraxis_gbr_names_only_not_flagged():
    """Härtung (.8): eine Gemeinschaftspraxis-GbR, die nur die Ärzte listet (ohne
    das Wort 'Gesellschafter'), ist eine Personengesellschaft -> kein Abzug."""
    check = CHECKS["recht.impressum_no_authorized_rep"]
    crawl = {"legal_subpages": {"impressum": {"reachable": True,
             "text_content": "Gemeinschaftspraxis Dr. Weber und Dr. Klein GbR, "
                              "Hauptstr. 1, 60311 Frankfurt. Ärztekammer Hessen."}}}
    assert check(None, None, None, crawl) is False


def test_capital_company_without_rep_still_flagged():
    """Gegenprobe (.8): GmbH und UG ohne benannten Vertreter feuern weiter."""
    check = CHECKS["recht.impressum_no_authorized_rep"]
    for txt in ["Muster GmbH, Musterstr. 1, 10115 Berlin.",
                "Muster UG (haftungsbeschränkt), 10115 Berlin."]:
        crawl = {"legal_subpages": {"impressum": {"reachable": True, "text_content": txt}}}
        assert check(None, None, None, crawl) is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_lazyload_dummy_not_a_placeholder():
    """Härtung (.9): dummy.png als Lazy-Load-Stand-in (Slider) ist KEIN echter
    Platzhalter; ein nacktes dummy.png ohne Lazy-Load-Kontext dagegen schon."""
    from pipeline.probe_network import check_placeholders
    lazy = ('<img src="images/slider/dummy.png" '
            'data-lazyload="images/slider/slider1.jpg" data-bgposition="left center">')
    assert check_placeholders(lazy) == []
    real = '<img src="dummy.png">'
    assert check_placeholders(real) != []
    # Textplatzhalter bleiben unberührt:
    assert "lorem ipsum" in " ".join(check_placeholders("<p>Lorem Ipsum dolor</p>"))


def test_organization_schema_accepted():
    """Härtung (.10): ein Verein/Non-Profit mit Organization-Schema gilt als
    valide ausgezeichnet - no_localbusiness_schema feuert nicht mehr."""
    check = CHECKS["seo.no_localbusiness_schema"]
    org = '<script type="application/ld+json">{"@type":"Organization","name":"Imkerverein"}</script>'
    assert check(None, None, None, {"html": org}) is False
    lb = '<script type="application/ld+json">{"@type":"LocalBusiness"}</script>'
    assert check(None, None, None, {"html": lb}) is False
    none = '<html><body>kein schema</body></html>'
    assert check(None, None, None, {"html": none}) is True
    # "organization" im Fließtext (ohne JSON-LD) darf NICHT als Schema zählen:
    text = '<p>We are a great organization</p>'
    assert check(None, None, None, {"html": text}) is True


def test_non_profit_detection():
    """Härtung (.11): Verein/Kirche erkannt; Unternehmen NICHT - auch wenn es
    eine Verbands-Mitgliedschaft (e.V.) im Fließtext nennt oder (als AG) einen
    Vorstand/eine Satzung hat."""
    verein = {"html": "<title>Bienenzuchtverein Grünberg</title>",
              "legal_subpages": {"impressum": {"text_content": ""}}}
    assert is_non_profit(verein) is True

    kirche = {"html": "<title>FeG Grünberg</title><p>Herzlich willkommen zum Gottesdienst</p>",
              "legal_subpages": {"impressum": {"text_content": ""}}}
    assert is_non_profit(kirche) is True

    # Unternehmen, das nur seine Innungs-Mitgliedschaft nennt -> KEIN Non-Profit:
    firma = {"html": "<title>Sanitär Meier GmbH</title><footer>Mitglied der Innung Sanitär e.V.</footer>",
             "legal_subpages": {"impressum": {"text_content": "Sanitär Meier GmbH, Geschäftsführer Max Meier"}}}
    assert is_non_profit(firma) is False

    # AG mit Vorstand und Satzung -> KEIN Non-Profit (Alleinsignale bewusst nicht genutzt):
    ag = {"html": "<title>Muster AG</title><p>Vorstand und Satzung der Gesellschaft</p>",
          "legal_subpages": {"impressum": {"text_content": "Muster AG, Vorstand: Dr. Groß"}}}
    assert is_non_profit(ag) is False


def test_references_rule_suppressed_for_non_profit():
    """Bei einem erkannten Non-Profit wird no_references_or_portfolio als
    'nicht anwendbar' geführt - kein Abzug, aber transparent ausgewiesen; andere
    Inhalt-Regeln greifen weiter."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    crawl = dict(crawl)
    crawl["html"] = "<title>Musterverein e.V.</title>" + crawl["html"]
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)
    assert result["is_non_profit"] is True
    assert "inhalt.no_references_or_portfolio" in result["suppressed_rules"]
    assert not any(f["id"] == "inhalt.no_references_or_portfolio" for f in result["findings"])
    assert any(r["id"] == "inhalt.no_references_or_portfolio" for r in result["not_applicable"])
    # andere Inhalt-Regel (construction notice) greift weiterhin:
    assert any(f["category"] == "inhalt_aktualitaet" for f in result["findings"])


def test_references_rule_active_for_business():
    """Gegenprobe: bei einem Unternehmen bleibt die Referenz-Regel aktiv."""
    infra, perf, network, crawl = load_fixture("bad_site", "bad.example")
    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl, ruleset)
    assert result["is_non_profit"] is False
    assert result["suppressed_rules"] == []
    assert any(f["id"] == "inhalt.no_references_or_portfolio" for f in result["findings"])
