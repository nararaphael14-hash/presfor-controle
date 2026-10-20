"""Ligne de commande.

  python -m app.cli extract carte.jpg                       # JSON d'une pièce
  python -m app.cli control --csv export.csv --scans ./scans --out rapport.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import get_settings
from .csv_loader import build_rows, guess_mapping, load_csv
from .export import to_xlsx
from .extractors import get_extractor
from .pipeline import (ScanItem, collect_scans, extract_all, extract_scan, results_to_dict, run_control,
                       summarize)


def cmd_extract(a: argparse.Namespace) -> int:
    s = get_settings()
    ext = get_extractor(s, a.backend)
    scan = collect_scans([Path(a.file)])
    if not scan:
        print("fichier non pris en charge", file=sys.stderr)
        return 2
    out = [extract_scan(sc, ext, s.cache_dir).model_dump(exclude={"mrz"}) for sc in scan]
    print(json.dumps(out[0] if len(out) == 1 else out, ensure_ascii=False, indent=2))
    return 0


def cmd_control(a: argparse.Namespace) -> int:
    s = get_settings()
    loaded = load_csv(Path(a.csv).read_bytes(), a.tables)
    mapping = guess_mapping(loaded.headers)
    for kv in a.map or []:
        k, _, v = kv.partition("=")
        mapping[k.strip()] = v.strip()
    rows = build_rows(loaded, mapping)
    print(f"CSV : {len(rows)} lignes (type {loaded.kind}) — colonnes : "
          + ", ".join(f"{k}={v}" for k, v in mapping.items() if v), file=sys.stderr)
    paths = sorted(p for p in Path(a.scans).rglob("*") if p.is_file())
    scans: list[ScanItem] = collect_scans(paths)
    extractor = get_extractor(s, a.backend)
    print(f"{len(scans)} scans, extracteur : {extractor.cache_id}", file=sys.stderr)

    def prog(done: int, total: int) -> None:
        print(f"\r  extraction {done}/{total}", end="", file=sys.stderr, flush=True)

    extract_all(scans, extractor, s, prog)
    print(file=sys.stderr)
    results, orphans = run_control(rows, scans, s)
    summary = summarize(results)
    out = Path(a.out)
    out.write_bytes(to_xlsx(results_to_dict(results), orphans, summary))
    if a.json:
        Path(a.json).write_text(json.dumps({"summary": summary, "results": results_to_dict(results),
                                            "orphans": orphans}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"Rapport : {out}", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="presfor-controle")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="extraire une pièce -> JSON")
    e.add_argument("file")
    e.add_argument("--backend", default="auto", choices=["auto", "claude", "tesseract"])
    e.set_defaults(fn=cmd_extract)
    c = sub.add_parser("control", help="contrôler un CSV contre un dossier de scans")
    c.add_argument("--csv", required=True)
    c.add_argument("--scans", required=True, help="dossier de scans (récursif)")
    c.add_argument("--out", default="rapport_controle.xlsx")
    c.add_argument("--json", help="export JSON complet (optionnel)")
    c.add_argument("--backend", default="auto", choices=["auto", "claude", "tesseract"])
    c.add_argument("--tables", nargs="*", help="tables DIGIFOR à lire (défaut : owners beneficiaries)")
    c.add_argument("--map", nargs="*", metavar="champ=Colonne",
                   help="forcer le mappage, ex. numero=idNumber full=name (champs : nom prenoms full numero code type)")
    c.set_defaults(fn=cmd_control)
    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
