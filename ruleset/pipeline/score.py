"""
pipeline/score.py

Layer 1 - Code, deterministisch. KEIN Sprachmodell beteiligt - das ist hier
bewusst so, damit derselbe Input immer denselben Score ergibt, egal wann
und wie oft er berechnet wird (Voraussetzung fuer den Vorher/Nachher-Vergleich,
den compare.py später macht).

Ablauf:
  1. ruleset.yaml laden (Gewichte, Deckel, Version)
  2. jede Regel-ID gegen eine kleine Pruef-Funktion mappen (siehe CHECKS unten)
  3. pro Kategorie Abzuege aufsummieren, bei 0 deckeln, vom Kategorie-Gewicht abziehen
  4. Gesamtscore = Summe der Kategoriewerte, nach unten hin auf ruleset["floor"] gedeckelt

Braucht KEINEN Netzwerkzugriff - laeuft rein auf den bereits gespeicherten
Probe-JSON-Dateien. Das ist der Teil, der sich in dieser Sandbox testen laesst.
"""

import json
import re
from pathlib import Path

import yaml


def load_ruleset(path: Path = Path(__file__).parent.parent / "ruleset.yaml") -> dict:
    return yaml.safe_load(path.read_text())


# ---------------------------------------------------------------------------
# Gemeinsame Muster (von mehreren Prüf-Funktionen genutzt)
# ---------------------------------------------------------------------------

# Kapitalgesellschaften: NUR sie haben ein gesetzliches Vertretungsorgan
# (Geschäftsführer/Vorstand) getrennt von den Eigentümern, das im Impressum
# benannt sein muss. Personengesellschaften (GbR/OHG/KG/PartG) und Einzelformen
# (e.K., Freiberufler, Einzelpraxis) nennen ihre Personen selbst und werden wie
# natürliche Personen behandelt - sonst entstehen False-Positives (z.B. bei einer
# Gemeinschaftspraxis-GbR oder einem e.K.). Word-Boundaries, damit kurze Formen
# (ag, ug) nicht in normalen Wörtern matchen.
_CAPITAL_COMPANY_RE = re.compile(
    r"\bgmbh\b|\bmbh\b|\bug\b|\bag\b|\bkgaa\b|"
    r"aktiengesellschaft|haftungsbeschränkt|genossenschaft|limited|\bltd\b",
    re.IGNORECASE,
)

# Klassische Template-Platzhalter, die in Links stehen bleiben, wenn eine
# Vorlage nie fertig eingerichtet wurde. Bewusst spezifisch gehalten
# (example.com statt bloß "example"), damit reservierte .example-TLDs in
# echten Testlinks nicht fälschlich als Platzhalter gelten.
_PLACEHOLDER_LINK_TOKENS = (
    "johndoe", "john-doe", "john.doe", "janedoe", "jane-doe",
    "max-mustermann", "mustermann",
    "example.com", "example.org", "example.net",
    "yourdomain", "your-domain", "yourcompany", "your-company",
    "yourusername", "your-username", "/username", "/user-name",
    "placeholder",
)


def _extract_hrefs(html: str) -> list:
    return re.findall(r'href=["\']([^"\']+)["\']', html or "", re.I)


def _is_placeholder_url(url: str) -> bool:
    u = (url or "").lower()
    return any(tok in u for tok in _PLACEHOLDER_LINK_TOKENS)


# Social-/Portal-Domains, die automatisierte Zugriffe (HEAD/Bot) systematisch mit
# Anti-Bot-Status abweisen (403/429/503/999) oder ins Leere laufen lassen, obwohl
# die Seite für echte Besucher funktioniert. Ein "Fehlerstatus" von diesen
# Domains ist daher KEIN verlässlicher Beleg für einen toten Link - er darf nicht
# als Abzug zählen (sonst False-Positive wie beim echten LinkedIn-Profil).
_BOT_HOSTILE_DOMAINS = (
    "linkedin.com", "twitter.com", "x.com", "instagram.com", "facebook.com",
    "xing.com", "tiktok.com", "medium.com",
)
_ANTI_BOT_STATUSES = {403, 429, 503, 999}


def _is_unverifiable_link(entry: dict) -> bool:
    """Nicht verlässlich als 'tot' wertbar (-> surface statt Abzug):
      - kein HTTP-Status (Timeout/Verbindungsfehler): transiente Langsamkeit,
        Rate-Limit oder DNS-Hänger sind kein Beleg für einen echten Fehler
        (z.B. wunderweb.de, das beim Audit nur langsam antwortete);
      - bot-feindliche Portale (LinkedIn/Xing/...) mit typischem Anti-Bot-Status.
    Ein echtes 4xx/5xx auf einer normalen Domain bleibt zählbar."""
    status = entry.get("status")
    if status is None:
        return True
    url = (entry.get("url") or "").lower()
    if any(dom in url for dom in _BOT_HOSTILE_DOMAINS) and status in _ANTI_BOT_STATUSES:
        return True
    return False


# ---------------------------------------------------------------------------
# Geschäftstyp-Klassifikation (lokal vs. ortsunabhängig)
#
# Rein stichwortbasiert und bewusst KONSERVATIV: "non_local" wird nur vergeben,
# wenn ein positives Nicht-lokal-Signal vorliegt UND kein Lokal-Signal. Sonst
# "local" oder "unknown" - beide führen dazu, dass die Kategorie lokale_sicht-
# barkeit ganz normal bewertet wird. So kann der Klassifikator einem echten
# lokalen Betrieb nie fälschlich die Lokal-Kritik erlassen; der teurere Fehler
# ist damit ausgeschlossen. Die Wörterbücher sollen mit der Zeit wachsen
# (gleiche Pflege-Idee wie CMS_FINGERPRINTS in probe_infra.py).
# ---------------------------------------------------------------------------

# Signale für einen ortsGEBUNDENEN Dienstleister (Branche, Ladenlokal/Praxis,
# Einzugsgebiet). Substrings, kleingeschrieben.
_LOCAL_SIGNAL_TOKENS = (
    # Handwerk / handwerksnahe Betriebe
    "dachdecker", "elektroinstall", "elektriker", "installateur", "sanitär", "heizungsbau",
    "klempner", "malerbetrieb", "malermeister", "lackierer", "tischlerei", "schreinerei",
    "zimmerei", "fliesenleger", "gerüstbau", "galabau", "landschaftsbau", "trockenbau",
    "estrich", "stuckateur", "schornsteinfeger", "metallbau", "schlosserei", "glaserei",
    "rollladen", "parkettleger", "bodenleger", "gebäudereinigung", "hausmeisterservice",
    "umzugsunternehmen", "autowerkstatt", "kfz-werkstatt", "reifenservice",
    # Gesundheit / Praxen
    "arztpraxis", "zahnarzt", "zahnärzt", "kieferorthopäd", "hausarzt", "facharzt",
    "physiotherapie", "ergotherapie", "logopäd", "heilpraktiker", "apotheke", "tierarzt",
    "tierärzt", "sprechzeiten", "sprechstunde",
    # lokale Konsum-/Ladendienste
    "friseursalon", "friseur", "kosmetikstudio", "nagelstudio", "barbershop", "restaurant",
    "gaststätte", "bäckerei", "konditorei", "metzgerei", "fleischerei", "fahrschule",
    "fitnessstudio", "bestattung", "floristik",
    # Ladenlokal / Einzugsgebiet / Anfahrt
    "öffnungszeiten", "vor ort", "in ihrer nähe", "in ihrer region", "im umkreis",
    "einzugsgebiet", "anfahrtsweg", "wir kommen zu ihnen", "hausbesuch", "notdienst",
)

# Signale für einen ortsUNABHÄNGIGEN Dienstleister (bundesweit/remote oder rein
# digital erbringbare Leistung).
_NONLOCAL_SIGNAL_TOKENS = (
    "bundesweit", "deutschlandweit", "europaweit", "weltweit", "international tätig",
    "remote", "ortsunabhängig", "standortunabhängig", "online-beratung", "homeoffice",
    "home office", "freelance", "freelancer",
    "softwareentwicklung", "software development", "system integration", "systemintegration",
    "webentwicklung", "web development", "app-entwicklung", "it-beratung", "it consultant",
    "it-consultant", "saas", "digitalagentur", "online-marketing", "seo-agentur",
    "übersetzungsbüro", "virtuelle assistenz",
)


def _local_positioning(crawl_meta: dict) -> bool:
    """Explizite lokale Positionierung: der Ort aus dem Impressum (nach der PLZ)
    taucht im Seitentitel oder in einer H1 auf - z.B. 'Internetagentur ... in
    Hüttenberg'. Das ist ein starkes Lokal-Signal, das auch ortsunabhängige
    Leistungen (Webdesign o.ä.) lokal verankert."""
    imp = crawl_meta.get("legal_subpages", {}).get("impressum", {}).get("text_content", "") or ""
    m = re.search(r"\b\d{5}\s+([A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß.\-]{3,})", imp)
    if not m:
        return False
    city = m.group(1).strip().lower()
    if len(city) < 4:
        return False
    html = crawl_meta.get("html", "") or ""
    title = (_extract_title(html) or "").lower()
    h1s = re.sub(r"<[^>]+>", " ",
                 " ".join(re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.I | re.S))).lower()
    return city in title or city in h1s


def is_non_profit(crawl_meta: dict) -> bool:
    """Non-Profit-Organisation (Verein, Kirchengemeinde, Stiftung, Kommune)?
    Für sie passen geschäftsspezifische Regeln (Referenzen/Portfolio) inhaltlich
    nicht. Bewusst KONSERVATIV und identitätsbasiert:
      1) Titel = Name der Seite (zuverlässigster Ort): 'verein', 'e.V.',
         'Kirchengemeinde', 'Freikirche' usw.
      2) eindeutige Rechtsform-/Status-Marker im Impressum ('eingetragener
         Verein', 'Vereinsregister', 'gemeinnützig') - die beschreiben die
         eigene Organisation, nicht bloß eine erwähnte.
      3) stark religiöse Aktivitätsbegriffe im Text ('Gottesdienst', 'Predigt')
         - die hat kein Unternehmen.
    Bewusst NICHT als Alleinsignal: 'Vorstand'/'Satzung' (haben AGs auch) oder
    ein beiläufiges 'e.V.' im Fließtext (Unternehmen nennen ihre Verbands-
    Mitgliedschaften) - das vermeidet False-Positives auf Unternehmen."""
    html = crawl_meta.get("html", "") or ""
    title = (_extract_title(html) or "").lower()
    imp_l = (crawl_meta.get("legal_subpages", {}).get("impressum", {})
             .get("text_content", "") or "").lower()
    body_l = html.lower()

    title_signals = ("verein", "e.v.", "e. v.", "kirchengemeinde", "kirchgemeinde",
                     "freikirche", "pfarrgemeinde", "pfarrei")
    if any(s in title for s in title_signals):
        return True
    if any(s in imp_l for s in ("eingetragener verein", "vereinsregister", "gemeinnützig")):
        return True
    if any(s in body_l for s in ("gottesdienst", "predigt", "bibelstunde")):
        return True
    return False


def classify_business_locality(crawl_meta: dict) -> str:
    """Gibt "local", "non_local" oder "unknown" zurück. Siehe Kommentar oben:
    "non_local" nur bei positivem Nicht-lokal-Signal UND ohne Lokal-Signal."""
    html = crawl_meta.get("html", "") or ""
    imp = crawl_meta.get("legal_subpages", {}).get("impressum", {}).get("text_content", "") or ""
    hay = (html + " " + imp).lower()

    has_local = any(t in hay for t in _LOCAL_SIGNAL_TOKENS) or _local_positioning(crawl_meta)
    has_nonlocal = any(t in hay for t in _NONLOCAL_SIGNAL_TOKENS)

    if has_nonlocal and not has_local:
        return "non_local"
    if has_local and not has_nonlocal:
        return "local"
    return "unknown"


# ---------------------------------------------------------------------------
# Pruef-Funktionen: eine pro automatisierter Regel-ID aus ruleset.yaml.
# Jede Funktion bekommt die drei rohen Probe-Dicts und gibt zurueck:
#   - für Einzel-Regeln: True/False (ausgelöst oder nicht)
#   - für "_per_unit"-Regeln: eine Ganzzahl (wie oft ausgelöst)
# ---------------------------------------------------------------------------

def _check_no_https(infra, perf, network, crawl_meta):
    return crawl_meta.get("url", "").startswith("http://")


def _check_cert_expired(infra, perf, network, crawl_meta):
    return bool(infra.get("tls", {}).get("expired"))


def _check_fcp_over_4s(infra, perf, network, crawl_meta):
    fcp = perf.get("first_contentful_paint_ms")
    return fcp is not None and fcp > 4000


def _check_fcp_2_5_to_4s(infra, perf, network, crawl_meta):
    fcp = perf.get("first_contentful_paint_ms")
    return fcp is not None and 2500 <= fcp <= 4000


def _check_page_weight_over_5mb(infra, perf, network, crawl_meta):
    size = perf.get("transfer_size_bytes")
    return size is not None and size > 5 * 1024 * 1024


def _check_large_uncompressed_images(infra, perf, network, crawl_meta):
    return bool(perf.get("large_image_over_1mb_detected"))  # v2, aktuell immer None -> False


def _check_outdated_cms_known_vulnerable(infra, perf, network, crawl_meta):
    return bool(infra.get("cms", {}).get("known_vulnerable_version"))


def _check_no_responsive_viewport(infra, perf, network, crawl_meta):
    return not network.get("viewport", {}).get("present", False)


def _check_viewport_blocks_zoom(infra, perf, network, crawl_meta):
    return bool(network.get("viewport", {}).get("blocks_zoom"))


def _check_alt_text_missing_majority(infra, perf, network, crawl_meta):
    return bool(network.get("alt_texts", {}).get("majority_missing"))


def _check_impressum_missing(infra, perf, network, crawl_meta):
    linked = network.get("legal_pages_linked", {}).get("impressum_linked", False)
    reachable_http = infra.get("legal_pages", {}).get("impressum", {}).get("reachable", False)
    reachable_spa = crawl_meta.get("legal_subpages", {}).get("impressum", {}).get("reachable", False)
    return not (linked or reachable_http or reachable_spa)


def _check_datenschutz_missing(infra, perf, network, crawl_meta):
    linked = network.get("legal_pages_linked", {}).get("datenschutz_linked", False)
    reachable_http = infra.get("legal_pages", {}).get("datenschutz", {}).get("reachable", False)
    reachable_spa = crawl_meta.get("legal_subpages", {}).get("datenschutz", {}).get("reachable", False)
    return not (linked or reachable_http or reachable_spa)


def _check_third_party_before_consent(infra, perf, network, crawl_meta):
    return len(network.get("trackers_before_consent", []))  # per_unit


def _check_no_localbusiness_schema(infra, perf, network, crawl_meta):
    html = crawl_meta.get("html", "")
    blocks = re.findall(r"<script[^>]+ld\+json[^>]*>(.*?)</script>", html, re.I | re.S)
    if not blocks:
        return True  # gar keine strukturierten Daten
    lower = " ".join(blocks).lower()
    # LocalBusiness ODER Organization (inkl. Subtypen wie NGO/SportsOrganization)
    # gelten als valide Entitäts-Auszeichnung. Ein Verein/Non-Profit nutzt korrekt
    # "Organization" statt "LocalBusiness" - das darf nicht als Mangel zählen.
    return not ("localbusiness" in lower or "organization" in lower)


def _check_no_sitemap_or_robots(infra, perf, network, crawl_meta):
    rs = infra.get("robots_sitemap", {})
    return not (rs.get("robots_txt") or rs.get("sitemap_xml"))


# ---------------------------------------------------------------------------
# Allgemeines On-Page-SEO. Alle Prüfungen lesen das gerenderte HTML
# (crawl_meta["html"]); Titel/Meta/OG/lang/canonical stehen bei Vite-/Lovable-
# Seiten in der index.html und bleiben im gerenderten DOM, die H1 kommt erst
# nach der Hydration dazu - beides steckt im gerenderten HTML.
# ---------------------------------------------------------------------------

# Vorlagen-Standardtitel, die auf eine unbearbeitete Vorlage hindeuten.
_DEFAULT_TITLES = {
    "lovable", "lovable app", "vite app", "vite + react", "vite + react + ts",
    "react app", "create react app", "index", "home", "startseite", "document",
    "untitled", "untitled document", "my app", "new project", "webflow site",
    "site", "app", "webseite", "website",
}


def _extract_title(html: str):
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return m.group(1).strip() if m else None


def _check_missing_or_default_title(infra, perf, network, crawl_meta):
    title = _extract_title(crawl_meta.get("html", ""))
    if not title:
        return True
    return title.lower().strip() in _DEFAULT_TITLES


def _check_missing_meta_description(infra, perf, network, crawl_meta):
    html = crawl_meta.get("html", "") or ""
    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
        html, re.I)
    if not m:
        # auch die umgekehrte Attribut-Reihenfolge (content vor name) prüfen
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*name=["\']description["\']',
            html, re.I)
    return not m or len(m.group(1).strip()) < 20


def _check_noindex_detected(infra, perf, network, crawl_meta):
    # Meta-Robots-noindex. (X-Robots-Tag-Header wird vom Crawler derzeit nicht
    # erfasst - dokumentierte Grenze, hier bewusst nur der Meta-Tag.)
    html = crawl_meta.get("html", "") or ""
    m = re.search(r'<meta[^>]+name=["\']robots["\'][^>]*content=["\']([^"\']*)["\']',
                  html, re.I)
    return bool(m and "noindex" in m.group(1).lower())


def _check_multiple_or_missing_h1(infra, perf, network, crawl_meta):
    html = crawl_meta.get("html", "") or ""
    n = len(re.findall(r"<h1[\s>]", html, re.I))
    return n != 1


def _check_missing_canonical(infra, perf, network, crawl_meta):
    html = crawl_meta.get("html", "") or ""
    return not re.search(r'<link[^>]+rel=["\']canonical["\']', html, re.I)


def _check_missing_lang_attribute(infra, perf, network, crawl_meta):
    html = crawl_meta.get("html", "") or ""
    m = re.search(r"<html[^>]*\blang=[\"']([^\"']*)[\"']", html, re.I)
    return not (m and m.group(1).strip())


def _check_missing_open_graph(infra, perf, network, crawl_meta):
    html = crawl_meta.get("html", "") or ""
    has_title = re.search(r'property=["\']og:title["\']', html, re.I)
    has_desc = re.search(r'property=["\']og:description["\']', html, re.I)
    return not (has_title and has_desc)


def _check_construction_notice_visible(infra, perf, network, crawl_meta):
    return bool(network.get("construction_notice"))


def _check_copyright_year_old(infra, perf, network, crawl_meta):
    cr = network.get("copyright", {})
    return bool(cr.get("found") and cr.get("older_than_2_years"))


def _check_template_placeholder_in_production(infra, perf, network, crawl_meta):
    return len(network.get("placeholders_found", [])) > 0


def _check_no_references_or_portfolio(infra, perf, network, crawl_meta):
    html_lower = crawl_meta.get("html", "").lower()
    # Deutsch UND Englisch, plus die gängige Testimonial-/Review-Vokabel. Die
    # frühere Liste war deutsch-only und übersah "Testimonials"/"What clients say"
    # -> False-Positive auf Seiten mit englischem/anders benanntem Referenzteil.
    # Bewusst spezifische Phrasen (nicht bloß "review"/"erfahrungen"), um keine
    # neuen Fehlbefunde auf unverwandten Inhalten zu erzeugen.
    keywords = [
        "referenz", "portfolio", "kundenstimme", "unsere projekte", "case stud",
        "testimonial", "clients say", "what clients", "kunden sagen", "was kunden",
        "erfahrungsbericht", "bewertungen von", "das sagen",
    ]
    return not any(k in html_lower for k in keywords)


def _check_outdated_legal_reference(infra, perf, network, crawl_meta):
    impressum_text = crawl_meta.get("legal_subpages", {}).get("impressum", {}).get("text_content", "")
    lower = impressum_text.lower()
    return ("rstv" in lower or "§ 55" in lower or "tmg" in lower) and "mstv" not in lower and "ddg" not in lower


def _check_impressum_no_authorized_rep(infra, perf, network, crawl_meta):
    impressum = crawl_meta.get("legal_subpages", {}).get("impressum", {})
    if not impressum.get("reachable"):
        return False  # wird bereits durch impressum_missing abgedeckt, keine Doppelbestrafung
    lower = impressum.get("text_content", "").lower()

    role_keywords = ["geschäftsführer", "inhaber", "vertreten durch",
                     "vertretungsberechtigt", "vorstand",
                     # Personengesellschaften (GbR/OHG) nennen "Gesellschafter";
                     # §18 MStV / §55 RStV verlangen "Verantwortlich für den Inhalt".
                     # Sind solche Personen benannt, ist die Vertretung erkennbar.
                     "gesellschafter", "verantwortlich"]
    if any(k in lower for k in role_keywords):
        return False  # Vertretungsberechtigter ist genannt -> alles gut

    # Kein Vertretungsberechtigter genannt: das ist NUR bei Kapitalgesellschaften
    # (GmbH/UG/AG/KGaA/eG) ein Mangel - nur sie haben ein gesetzliches Vertretungs-
    # organ getrennt von den Eigentümern. Natürliche Personen (Freiberufler, e.K.,
    # Einzelpraxis) und Personengesellschaften (GbR/OHG/KG/PartG) nennen ihre
    # Personen selbst; dort darf die Regel nicht auslösen (sonst False-Positives
    # z.B. bei einer Gemeinschaftspraxis-GbR oder einem e.K.).
    return bool(_CAPITAL_COMPANY_RE.search(lower))


def _check_template_placeholder_link(infra, perf, network, crawl_meta):
    """Zählt Links, die noch auf Template-Platzhalter zeigen (johndoe,
    example.com, yourdomain ...). Scannt das gerenderte HTML, nicht nur die
    toten Links - erfasst damit AUCH Platzhalter-Links, die technisch erreichbar
    sind (Status 200) und deshalb gar nicht in infra.dead_external_links landen.
    Das ist der aussagekräftigere Befund: nicht 'Link kaputt', sondern 'Vorlage
    nie fertig eingerichtet'."""
    hrefs = _extract_hrefs(crawl_meta.get("html", ""))
    placeholders = [h for h in dict.fromkeys(hrefs) if _is_placeholder_url(h)]
    return len(placeholders)  # per_unit


def _check_dead_external_link(infra, perf, network, crawl_meta):
    # Zwei Ausnahmen, die NICHT als toter Link zählen:
    #  1. Platzhalter-Links (johndoe etc.) - werden separat/härter durch
    #     inhalt.template_placeholder_link erfasst (keine Doppelbestrafung).
    #  2. Nicht verifizierbare Social-Links (LinkedIn & Co.), die nur den Bot
    #     abweisen - kein verlässlicher Beleg für einen echten Fehler.
    dead = infra.get("dead_external_links", [])
    real_dead = [
        d for d in dead
        if not _is_placeholder_url(d.get("url", ""))
        and not _is_unverifiable_link(d)
    ]
    return len(real_dead)  # per_unit


# nicht implementierte (automated: false) Regeln brauchen keinen Eintrag hier -
# score() ueberspringt sie automatisch und markiert sie im Ergebnis als "manual_review"

CHECKS = {
    "technik.no_https": _check_no_https,
    "technik.cert_expired": _check_cert_expired,
    "technik.fcp_over_4s": _check_fcp_over_4s,
    "technik.fcp_2_5_to_4s": _check_fcp_2_5_to_4s,
    "technik.page_weight_over_5mb": _check_page_weight_over_5mb,
    "technik.large_uncompressed_images": _check_large_uncompressed_images,
    "technik.outdated_cms_known_vulnerable": _check_outdated_cms_known_vulnerable,
    "mobil.no_responsive_viewport": _check_no_responsive_viewport,
    "mobil.viewport_blocks_zoom": _check_viewport_blocks_zoom,
    "mobil.alt_text_missing_majority": _check_alt_text_missing_majority,
    "recht.impressum_missing": _check_impressum_missing,
    "recht.datenschutz_missing": _check_datenschutz_missing,
    "recht.third_party_before_consent": _check_third_party_before_consent,
    "seo.no_localbusiness_schema": _check_no_localbusiness_schema,
    "seo.missing_or_default_title": _check_missing_or_default_title,
    "seo.missing_meta_description": _check_missing_meta_description,
    "seo.noindex_detected": _check_noindex_detected,
    "seo.multiple_or_missing_h1": _check_multiple_or_missing_h1,
    "seo.missing_canonical": _check_missing_canonical,
    "seo.missing_lang_attribute": _check_missing_lang_attribute,
    "seo.missing_open_graph": _check_missing_open_graph,
    "seo.no_sitemap_or_robots": _check_no_sitemap_or_robots,
    "inhalt.construction_notice_visible": _check_construction_notice_visible,
    "inhalt.copyright_year_old": _check_copyright_year_old,
    "inhalt.template_placeholder_in_production": _check_template_placeholder_in_production,
    "inhalt.no_references_or_portfolio": _check_no_references_or_portfolio,
    "recht.outdated_legal_reference": _check_outdated_legal_reference,
    "recht.impressum_no_authorized_rep": _check_impressum_no_authorized_rep,
    "inhalt.template_placeholder_link": _check_template_placeholder_link,
    "inhalt.dead_external_link": _check_dead_external_link,
    # Diese zwei bleiben in v1 ehrlich unautomatisiert (siehe ruleset.yaml,
    # dort jetzt automated: false mit Begründung) - sie brauchen mehr als eine
    # Einzelseiten-Prüfung: einen Standort-Parameter pro Kunde bzw. einen
    # Mehrseiten-Crawl der ganzen Domain, nicht nur der Startseite.
}


def score(infra: dict, perf: dict, network: dict, crawl_meta: dict, ruleset: dict) -> dict:
    category_totals = {cat: 0 for cat in ruleset["category_weights"]}
    findings = []

    # Bedingte Kategorien: welche werden für diesen Geschäftstyp unterdrückt?
    locality = classify_business_locality(crawl_meta)
    suppressed = set()
    for cat, cfg in ruleset.get("conditional_categories", {}).items():
        if cfg.get("suppress_when") == locality:
            suppressed.add(cat)

    # Bedingte EINZELREGELN: z.B. Referenzen/Portfolio bei Non-Profits (Vereine,
    # Kirchengemeinden) - die Regel sitzt in einer Kategorie, die sonst gilt,
    # daher regel- statt kategorieweise unterdrückt.
    non_profit = is_non_profit(crawl_meta)
    suppressed_rules = set()
    for rid, cfg in ruleset.get("conditional_rules", {}).items():
        if cfg.get("suppress_when") == "non_profit" and non_profit:
            suppressed_rules.add(rid)

    not_applicable = []

    for rule in ruleset["rules"]:
        rid = rule["id"]
        if rule["category"] in suppressed or rid in suppressed_rules:
            # zählt für diesen Organisationstyp nicht - weder als Abzug noch als
            # "manuell zu prüfen", sondern separat ausgewiesen.
            not_applicable.append({
                "id": rid, "category": rule["category"],
                "message_internal": rule["message_internal"],
            })
            continue

        if not rule.get("automated", False):
            continue  # v2-Regeln werden separat als "manual_review" ausgewiesen, s.u.

        check_fn = CHECKS.get(rid)
        if check_fn is None:
            continue  # sollte durch Tests abgefangen werden, s.o.

        outcome = check_fn(infra, perf, network, crawl_meta)

        if "deduction_per_unit" in rule:
            count = int(outcome)
            if count > 0:
                impact = min(count * rule["deduction_per_unit"], rule["max_deduction"])
                category_totals[rule["category"]] += impact
                findings.append({
                    "id": rid, "category": rule["category"], "source": "rule",
                    "count": count, "score_impact": -impact,
                    "message_internal": rule["message_internal"],
                    "message_customer": rule.get("message_customer"),
                })
        else:
            if outcome:
                impact = rule["deduction"]
                category_totals[rule["category"]] += impact
                findings.append({
                    "id": rid, "category": rule["category"], "source": "rule",
                    "score_impact": -impact,
                    "message_internal": rule["message_internal"],
                    "message_customer": rule.get("message_customer"),
                })

    category_scores = {}
    for cat, weight in ruleset["category_weights"].items():
        if cat in suppressed:
            # nicht anwendbar -> volle Punktzahl, damit der Geschäftstyp nicht
            # durch eine für ihn irrelevante Kategorie abgewertet wird.
            category_scores[cat] = weight
        else:
            category_scores[cat] = max(0, weight - category_totals[cat])

    overall = max(ruleset["floor"], sum(category_scores.values()))

    manual_review_rules = [
        {"id": r["id"], "category": r["category"], "message_internal": r["message_internal"]}
        for r in ruleset["rules"]
        if not r.get("automated", False)
        and r["category"] not in suppressed
        and r["id"] not in suppressed_rules
    ]

    # Externe Links, die der Bot nicht verlässlich prüfen konnte (Anti-Bot-Schutz
    # der Zielseite) - kein Abzug, aber transparent zur manuellen Prüfung.
    unverified_links = [
        d for d in infra.get("dead_external_links", [])
        if _is_unverifiable_link(d)
    ]

    return {
        "ruleset_version": ruleset["version"],
        "business_locality": locality,
        "is_non_profit": non_profit,
        "page_count": infra.get("page_count"),
        "suppressed_categories": sorted(suppressed),
        "suppressed_rules": sorted(suppressed_rules),
        "category_scores": category_scores,
        "overall": overall,
        "findings": findings,
        "manual_review_needed": manual_review_rules,
        "not_applicable": not_applicable,
        "unverified_external_links": unverified_links,
    }


def run_score(run_dir: Path) -> dict:
    """Liest crawl/probe-Dateien aus run_dir und schreibt score_result.json."""
    domain_files = list(run_dir.glob("*_crawl.json"))
    if not domain_files:
        raise FileNotFoundError(f"Keine *_crawl.json in {run_dir} gefunden")
    domain = domain_files[0].stem.replace("_crawl", "")

    crawl_meta = json.loads((run_dir / f"{domain}_crawl.json").read_text())
    infra = json.loads((run_dir / f"{domain}_probe_infra.json").read_text())
    perf = json.loads((run_dir / f"{domain}_probe_perf.json").read_text())
    network = json.loads((run_dir / f"{domain}_probe_network.json").read_text())

    ruleset = load_ruleset()
    result = score(infra, perf, network, crawl_meta, ruleset)
    result["domain"] = domain

    out_path = run_dir / f"{domain}_score_result.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


if __name__ == "__main__":
    import sys
    result = run_score(Path(sys.argv[1]))
    print(json.dumps(result, indent=2, ensure_ascii=False))
