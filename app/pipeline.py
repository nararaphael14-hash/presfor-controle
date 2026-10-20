"""Pipeline : extraction (avec cache) -> rattachement scans/lignes -> comparaison -> verdict."""
from __future__ import annotations

import os
import threading

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .config import Settings
from .csv_loader import Row
from .images import SUPPORTED_EXT, count_pages, load_page
from .matching import (A_VERIFIER, CONFORME, CONFORME_TOLERE, NON_CONFORME, PIECE_INUTILISABLE,
                       PIECE_MANQUANTE, FieldCheck, compare_name, compare_number, name_similarity, verdict)
from .normalize import canon_doc_type, collapse_confusables, name_tokens, norm_number, parse_scan_filename
from .schemas import Extraction

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ scans
@dataclass
class ScanItem:
    id: int
    filename: str          # nom affiché (avec #pN pour les PDF multi-pages)
    path: Path
    page_index: int
    code: str
    hint: str
    sha: str
    extraction: Extraction | None = None


def collect_scans(paths: list[Path]) -> list[ScanItem]:
    items: list[ScanItem] = []
    for p in sorted(paths, key=lambda x: x.name.lower()):
        if p.suffix.lower() not in SUPPORTED_EXT or not p.is_file():
            continue
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        try:
            n = count_pages(p)
        except Exception as e:  # PDF corrompu
            log.warning("illisible %s: %s", p.name, e)
            n = 1
        code, hint, _ = parse_scan_filename(p.name)
        for k in range(n):
            name = p.name if n == 1 else f"{p.name}#p{k + 1}"
            items.append(ScanItem(len(items), name, p, k, code, hint, sha))
    return items


def _cache_file(cache_dir: Path, scan: ScanItem, cache_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-." else "_" for c in cache_id)
    return cache_dir / f"{scan.sha[:32]}_p{scan.page_index}_{safe}.json"


# Plafond global d'extractions simultanées, tous contrôles confondus (contrats + CF) : l'hébergement gratuit n'a que 512 Mo
_SLOTS = threading.BoundedSemaphore(int(os.getenv("GLOBAL_WORKERS", "3")))


def extract_scan(scan: ScanItem, extractor, cache_dir: Path) -> Extraction:
    with _SLOTS:
        return _extract_scan(scan, extractor, cache_dir)


def _extract_scan(scan: ScanItem, extractor, cache_dir: Path) -> Extraction:
    cf = _cache_file(cache_dir, scan, extractor.cache_id)
    if cf.is_file():
        try:
            return Extraction.model_validate_json(cf.read_text(encoding="utf-8"))
        except Exception:
            cf.unlink(missing_ok=True)
    try:
        ex = extractor.extract(load_page(scan.path, scan.page_index))
    except Exception as e:
        return Extraction(lisible=False, type_piece="ILLISIBLE", backend=extractor.cache_id,
                          erreur=f"{type(e).__name__}: {e}")
    if not ex.erreur:  # on ne met JAMAIS une erreur en cache : elle sera retentée
        cache_dir.mkdir(parents=True, exist_ok=True)
        cf.write_text(ex.model_dump_json(indent=1), encoding="utf-8")
    return ex


def extract_all(scans: list[ScanItem], extractor, settings: Settings,
                progress: Callable[[int, int], None] | None = None) -> None:
    """Lit chaque image UNE seule fois : les scans identiques (même empreinte) partagent le même résultat.
    Dans les exports, la même photo est très souvent réutilisée (recto/verso doublés, même titulaire sur N demandes)."""
    from concurrent.futures import as_completed

    total, done = len(scans), 0
    groups: dict[tuple, list[ScanItem]] = {}
    for sc in scans:
        groups.setdefault((sc.sha, sc.page_index), []).append(sc)
    if progress:
        progress(0, total)
    with ThreadPoolExecutor(max_workers=max(1, settings.workers)) as pool:
        futures = {pool.submit(extract_scan, g[0], extractor, settings.cache_dir): g for g in groups.values()}
        for fut in as_completed(futures):
            ex = fut.result()
            for sc in futures[fut]:
                sc.extraction = ex
                done += 1
            if progress:
                progress(done, total)


# ------------------------------------------------------------------ rattachement
@dataclass
class Link:
    scan: ScanItem
    score: float
    reasons: list[str]
    inter_contrat: bool


def _full(ex: Extraction) -> str:
    return " ".join(x for x in (ex.nom, ex.prenoms) if x)


@lru_cache(maxsize=None)
def _prefixes(s: str) -> frozenset[str]:
    return frozenset(t[:3] for t in name_tokens(s))


def _maybe(a: str, b: str) -> bool:
    """Préfiltre bon marché : au moins un mot en commun (3 premières lettres) avant tout calcul flou."""
    return bool(_prefixes(a) & _prefixes(b))


def link_scan(row: Row, scan: ScanItem, settings: Settings) -> Link | None:
    ex = scan.extraction
    thr = settings.link_threshold
    diff_code = bool(row.code and scan.code and row.code != scan.code)
    reasons: list[str] = []
    score = 0.0
    if row.fichier and row.fichier.strip().lower() in (scan.filename.lower(), scan.path.name.lower()):
        return Link(scan, 100.0, ["fichier indiqué dans le CSV"], False)
    rn = norm_number(row.numero)
    if ex and ex.numero_piece and len(rn) >= 5 and norm_number(ex.numero_piece) == rn:
        score = 100.0
        reasons.append("numéro identique")
    if not diff_code and scan.hint and row.full and _maybe(row.full, scan.hint):
        s = name_similarity(row.full, scan.hint).score
        if s >= thr:
            reasons.append(f"nom du fichier {s:.0f}")
            score = max(score, s)
    if ex and _full(ex) and row.full and _maybe(row.full, _full(ex)):
        s = name_similarity(row.full, _full(ex)).score
        if s >= (thr if not diff_code else max(90.0, thr)):
            reasons.append(f"nom lu sur la pièce {s:.0f}")
            score = max(score, s)
    if not reasons:
        return None
    return Link(scan, score, reasons, inter_contrat=diff_code)


# ------------------------------------------------------------------ résultats
@dataclass
class RowResult:
    row_index: int
    code: str
    role: str
    csv: dict
    verdict: str = PIECE_MANQUANTE
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    extracted: dict = field(default_factory=dict)
    scans: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def _scan_summary(link: Link) -> dict:
    ex = link.scan.extraction
    return {
        "id": link.scan.id, "fichier": link.scan.filename, "lien_score": round(link.score, 1),
        "lien": link.reasons, "inter_contrat": link.inter_contrat,
        "extraction": ex.model_dump(exclude={"mrz"}) if ex else None,
    }


def _mean_conf(ex: Extraction) -> float:
    c = [ex.conf(k) for k in ("nom", "prenoms", "numero_piece") if getattr(ex, k)]
    return sum(c) / len(c) if c else 0.0


def evaluate_row(row: Row, links: list[Link], settings: Settings) -> RowResult:
    res = RowResult(row.idx, row.code, row.role,
                    {"nom": row.nom, "prenoms": row.prenoms, "nom_complet": row.full, "numero": row.numero,
                     "type": row.type, "date_naissance": row.birth, "fichier": row.fichier})
    if not links:
        res.verdict, res.reasons = PIECE_MANQUANTE, ["aucun scan rattaché à cette ligne"]
        return res
    res.scans = [_scan_summary(l) for l in sorted(links, key=lambda l: -l.score)]
    usable = [l for l in links if l.scan.extraction and not l.scan.extraction.erreur and l.scan.extraction.lisible
              and l.scan.extraction.n_fields > 0]
    if not usable:
        errs = [l.scan.extraction.erreur for l in links if l.scan.extraction and l.scan.extraction.erreur]
        faces = {l.scan.extraction.face for l in links if l.scan.extraction}
        res.verdict = PIECE_INUTILISABLE
        res.reasons = [f"erreur d'extraction : {errs[0]}"] if errs else [
            "seul le verso est fourni" if faces <= {"VERSO"} else "aucun champ lisible sur les scans rattachés"]
        return res

    # même contrat d'abord, puis pièces d'un autre contrat ; puis plus de champs lus / meilleure confiance
    order = sorted(usable, key=lambda l: (l.inter_contrat, -l.scan.extraction.n_fields, -_mean_conf(l.scan.extraction)))
    same = [l for l in order if not l.inter_contrat]
    pool = same or order
    ident = next((l for l in pool if l.scan.extraction.nom or l.scan.extraction.prenoms), None)
    numl = next((l for l in pool if l.scan.extraction.numero_piece), None)
    ex_i = ident.scan.extraction if ident else None
    ex_n = numl.scan.extraction if numl else None
    doc_full = _full(ex_i) if ex_i else ""
    doc_num = ex_n.numero_piece if ex_n and ex_n.numero_piece else ""
    name_conf = min([ex_i.conf(k) for k in ("nom", "prenoms") if ex_i and getattr(ex_i, k)] or [0.0])
    num_conf = ex_n.conf("numero_piece") if ex_n else 0.0
    res.extracted = {
        "nom": ex_i.nom if ex_i else None, "prenoms": ex_i.prenoms if ex_i else None,
        "numero_piece": doc_num or None,
        "type_piece": (ex_n or ex_i).type_piece if (ex_n or ex_i) else None,
        "date_naissance": next((l.scan.extraction.date_naissance for l in pool if l.scan.extraction.date_naissance), None),
        "source_identite": ident.scan.filename if ident else None,
        "source_numero": numl.scan.filename if numl else None,
        "confiance_nom": round(name_conf, 2), "confiance_numero": round(num_conf, 2),
    }
    kw = dict(tolerate=settings.name_tolerate, close=settings.name_close, min_conf=settings.min_confidence)
    csv_sep = (row.nom, row.prenoms) if row.nom and row.prenoms else None
    doc_sep = (ex_i.nom or "", ex_i.prenoms or "") if ex_i and ex_i.nom and ex_i.prenoms else None
    name_chk = compare_name(row.full, doc_full, name_conf, csv_sep=csv_sep, doc_sep=doc_sep, **kw)
    num_chk = compare_number(row.numero, doc_num, num_conf, min_conf=settings.min_confidence)
    res.checks = {"nom_complet": asdict(name_chk), "numero_piece": asdict(num_chk)}
    if row.nom and ex_i and ex_i.nom:      # diagnostics détaillés (n'influencent pas le verdict)
        res.checks["nom"] = asdict(compare_name(row.nom, ex_i.nom, name_conf, **kw))
    if row.prenoms and ex_i and ex_i.prenoms:
        res.checks["prenoms"] = asdict(compare_name(row.prenoms, ex_i.prenoms, name_conf, **kw))
    res.verdict, res.reasons = verdict(name_chk, num_chk)

    # signaux complémentaires
    nums = {collapse_confusables(norm_number(l.scan.extraction.numero_piece)): l.scan.extraction.numero_piece
            for l in same if l.scan.extraction.numero_piece}
    if len(nums) > 1:
        res.flags.append("numéros différents selon les scans : " + " / ".join(nums.values()))
    if not same:
        res.flags.append("pièce issue d'un autre contrat (même identité)")
    if res.verdict in (CONFORME, CONFORME_TOLERE) and res.flags and any("numéros différents" in f for f in res.flags):
        res.verdict = A_VERIFIER
    if row.type and ex_n and ex_n.type_piece not in ("AUTRE", "ILLISIBLE"):
        want, got = canon_doc_type(row.type), ex_n.type_piece
        res.info["type_declare"], res.info["type_lu"] = want or row.type, got
        res.info["type_coherent"] = (want == got) if want not in ("", "AUTRE") else None
    return res


def run_control(rows: list[Row], scans: list[ScanItem], settings: Settings) -> tuple[list[RowResult], list[dict]]:
    """Retourne (résultats par ligne, scans orphelins). Les extractions doivent déjà être faites."""
    results: list[RowResult] = []
    attached: set[int] = set()
    for row in rows:
        links = [lk for s in scans if (lk := link_scan(row, s, settings))]
        attached.update(l.scan.id for l in links)
        results.append(evaluate_row(row, links, settings))
    # signal : le numéro lu sur la pièce appartient (exactement) à une AUTRE ligne du CSV
    by_num: dict[str, list[int]] = {}
    for r in rows:
        n = norm_number(r.numero)
        if len(n) >= 5:
            by_num.setdefault(n, []).append(r.idx)
    for res in results:
        n = norm_number(res.extracted.get("numero_piece") or "")
        if n and res.checks.get("numero_piece", {}).get("status") != "OK":
            others = [i for i in by_num.get(n, []) if i != res.row_index]
            if others:
                res.flags.append(f"le numéro lu sur la pièce est celui de la ligne CSV {others[0] + 2}")
    orphans = []
    for s in scans:
        if s.id in attached:
            continue
        ex = s.extraction
        best = None
        if ex and _full(ex):
            cand = max(rows, key=lambda r: name_similarity(r.full, _full(ex)).score, default=None)
            if cand:
                sc = name_similarity(cand.full, _full(ex)).score
                best = {"ligne": cand.idx, "nom": cand.full, "code": cand.code, "score": sc} if sc >= 60 else None
        orphans.append({"id": s.id, "fichier": s.filename, "code": s.code, "indice_nom": s.hint,
                        "extraction": ex.model_dump(exclude={"mrz"}) if ex else None, "plus_proche": best})
    return results, orphans


def summarize(results: list[RowResult]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in results:
        out[r.verdict] = out.get(r.verdict, 0) + 1
    out["TOTAL"] = len(results)
    return out


def results_to_dict(results: list[RowResult]) -> list[dict]:
    return [asdict(r) for r in results]


def dump_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
