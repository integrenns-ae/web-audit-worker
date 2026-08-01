#!/usr/bin/env python3
"""
compare.py - vergleicht zwei Audit-Läufe derselben Domain.

Nutzung:
    python compare.py integrenns.de
        -> vergleicht die zwei neuesten Läufe automatisch

    python compare.py integrenns.de 20260727-091500 20260810-143000
        -> vergleicht zwei konkrete Läufe explizit

Genau der Anwendungsfall "bewerten -> verbessern -> wieder bewerten":
zeigt, welche Findings verschwunden/neu sind und wie sich der Score
pro Kategorie verändert hat.
"""

import json
import sys
from pathlib import Path


def load_run(run_dir: Path) -> dict:
    domain_files = list(run_dir.glob("*_score_result.json"))
    domain = domain_files[0].stem.replace("_score_result", "")
    return json.loads((run_dir / f"{domain}_score_result.json").read_text())


def compare(domain: str, run_a: str = None, run_b: str = None):
    domain_dir = Path("runs") / domain
    if not domain_dir.exists():
        print(f"Keine Läufe für {domain} gefunden unter {domain_dir}/")
        sys.exit(1)

    all_runs = sorted([d for d in domain_dir.iterdir() if d.is_dir()])
    if len(all_runs) < 2 and not (run_a and run_b):
        print(f"Nur {len(all_runs)} Lauf/Läufe vorhanden — brauche mindestens zwei zum Vergleichen.")
        sys.exit(1)

    if run_a and run_b:
        older_dir = domain_dir / run_a
        newer_dir = domain_dir / run_b
    else:
        older_dir, newer_dir = all_runs[-2], all_runs[-1]

    older = load_run(older_dir)
    newer = load_run(newer_dir)

    print(f"Vergleich für {domain}")
    print(f"  Vorher: {older_dir.name}  (Score: {older['overall']})")
    print(f"  Nachher: {newer_dir.name}  (Score: {newer['overall']})")
    print()

    delta = newer["overall"] - older["overall"]
    sign = "+" if delta >= 0 else ""
    print(f"Gesamtscore: {older['overall']} → {newer['overall']}  ({sign}{delta})")
    print()

    print("Pro Kategorie:")
    for cat in older["category_scores"]:
        o, n = older["category_scores"][cat], newer["category_scores"].get(cat, 0)
        d = n - o
        sign = "+" if d >= 0 else ""
        print(f"  {cat:25s} {o:3d} → {n:3d}  ({sign}{d})")
    print()

    older_ids = {f["id"] for f in older["findings"]}
    newer_ids = {f["id"] for f in newer["findings"]}

    resolved = older_ids - newer_ids
    new_findings = newer_ids - older_ids
    still_open = older_ids & newer_ids

    if resolved:
        print(f"Behoben ({len(resolved)}):")
        for f in older["findings"]:
            if f["id"] in resolved:
                print(f"  ✓ {f['message_internal']}")
        print()

    if new_findings:
        print(f"Neu aufgetreten ({len(new_findings)}):")
        for f in newer["findings"]:
            if f["id"] in new_findings:
                print(f"  ! {f['message_internal']}")
        print()

    if still_open:
        print(f"Weiterhin offen ({len(still_open)}):")
        for f in newer["findings"]:
            if f["id"] in still_open:
                print(f"  - {f['message_internal']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Nutzung: python compare.py <domain> [<lauf_a> <lauf_b>]")
        sys.exit(1)
    domain = sys.argv[1]
    run_a = sys.argv[2] if len(sys.argv) > 2 else None
    run_b = sys.argv[3] if len(sys.argv) > 3 else None
    compare(domain, run_a, run_b)
