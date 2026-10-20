"""Comparaison floue (rapidfuzz) des noms, strictement encadrée pour les numéros.

Principes :
  * NOM    : tolérant aux accents, casse, ponctuation, ordre nom/prénoms, petites fautes.
  * NUMÉRO : exact après normalisation = OK. Tout écart, même d'1 caractère, est SIGNALÉ
             (« PROCHE » = faute de frappe / confusion OCR probable), jamais accepté en silence.
  * Une lecture incertaine ne produit jamais un écart confirmé : elle donne « INCERTAIN ».
"""
from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz
from rapidfuzz.distance import DamerauLevenshtein

from .normalize import collapse_confusables, name_tokens, norm_number

# Statuts de champ
OK, TOLERE, PROCHE, PARTIEL, ECART = "OK", "TOLERE", "PROCHE", "PARTIEL", "ECART"
NON_LU, MANQUANT_CSV, INCERTAIN = "NON_LU", "MANQUANT_CSV", "INCERTAIN"

# Verdicts de ligne
CONFORME, CONFORME_TOLERE, A_VERIFIER = "CONFORME", "CONFORME_TOLERE", "A_VERIFIER"
NON_CONFORME, PIECE_MANQUANTE, PIECE_INUTILISABLE = "NON_CONFORME", "PIECE_MANQUANTE", "PIECE_INUTILISABLE"


@dataclass
class FieldCheck:
    field: str
    status: str
    score: float | None = None
    csv_value: str = ""
    doc_value: str = ""
    note: str = ""


@dataclass
class NameSim:
    score: float
    exact: bool
    only_in_a: list[str]
    only_in_b: list[str]
    matched_avg: float


def _align(ta: list[str], tb: list[str], tok_thr: float = 75) -> tuple[float, list[str], list[str], float]:
    pairs = sorted(((fuzz.ratio(x, y), i, j) for i, x in enumerate(ta) for j, y in enumerate(tb)), reverse=True)
    ua, ub, matched = set(), set(), []
    for s, i, j in pairs:
        if s < tok_thr:
            break
        if i in ua or j in ub:
            continue
        ua.add(i)
        ub.add(j)
        matched.append((i, j, s))
    w = lambda i, j: max(len(ta[i]), len(tb[j]))
    num = sum(s * w(i, j) for i, j, s in matched)
    den = sum(w(i, j) for i, j, _ in matched)
    den += sum(len(ta[i]) for i in range(len(ta)) if i not in ua)
    den += sum(len(tb[j]) for j in range(len(tb)) if j not in ub)
    avg = (sum(s for _, _, s in matched) / len(matched)) if matched else 0.0
    return (num / den if den else 0.0), [ta[i] for i in range(len(ta)) if i not in ua], \
           [tb[j] for j in range(len(tb)) if j not in ub], avg


def name_similarity(a: str, b: str) -> NameSim:
    """Similarité 0-100 entre deux noms, indépendante de l'ordre des mots."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return NameSim(0.0, False, ta, tb, 0.0)
    if sorted(ta) == sorted(tb):
        return NameSim(100.0, True, [], [], 100.0)
    aligned, ma, mb, avg = _align(ta, tb)
    sa, sb = " ".join(sorted(ta)), " ".join(sorted(tb))
    score = max(aligned, fuzz.ratio(sa, sb), fuzz.ratio(sa.replace(" ", ""), sb.replace(" ", "")))
    return NameSim(round(min(score, 99.9), 1), False, ma, mb, avg)


def compare_name(csv_full: str, doc_full: str, doc_conf: float, *, tolerate: float, close: float,
                 min_conf: float, csv_sep: tuple[str, str] | None = None,
                 doc_sep: tuple[str, str] | None = None) -> FieldCheck:
    fc = FieldCheck("nom_complet", ECART, None, csv_full, doc_full)
    if not name_tokens(doc_full):
        fc.status, fc.note = NON_LU, "nom non lu sur la pièce"
        return fc
    if not name_tokens(csv_full):
        fc.status, fc.note = MANQUANT_CSV, "nom non renseigné dans le CSV"
        return fc
    sim = name_similarity(csv_full, doc_full)
    fc.score = sim.score
    if sim.exact:
        fc.status = OK
        # même mots mais nom/prénoms inversés ?
        if csv_sep and doc_sep and all(csv_sep) and all(doc_sep):
            straight = (name_similarity(csv_sep[0], doc_sep[0]).score + name_similarity(csv_sep[1], doc_sep[1]).score) / 2
            swapped = (name_similarity(csv_sep[0], doc_sep[1]).score + name_similarity(csv_sep[1], doc_sep[0]).score) / 2
            if swapped >= 85 and straight < 60:
                fc.status, fc.score, fc.note = PROCHE, swapped, "nom et prénoms inversés dans le CSV"
        return fc
    diffs = []
    if sim.only_in_a:
        diffs.append(f"saisi seulement : {' '.join(sim.only_in_a)}")
    if sim.only_in_b:
        diffs.append(f"pièce seulement : {' '.join(sim.only_in_b)}")
    fc.note = " | ".join(diffs)
    one_sided = bool(sim.only_in_a) != bool(sim.only_in_b)
    if one_sided and sim.matched_avg >= 88:
        fc.status = PARTIEL
        fc.note = ("prénom/nom manquant dans le CSV (" if sim.only_in_b else "mot en trop dans le CSV (") + \
                  (" ".join(sim.only_in_b) or " ".join(sim.only_in_a)) + ")"
    elif sim.score >= tolerate:
        fc.status = TOLERE
        fc.note = fc.note or "petite variation d'orthographe"
    elif sim.score >= close:
        fc.status = PROCHE
    else:
        fc.status = ECART
    if fc.status == ECART and doc_conf < min_conf:
        fc.status, fc.note = INCERTAIN, f"lecture incertaine (confiance {doc_conf:.2f}) ; " + fc.note
    return fc


def compare_number(csv_num: str, doc_num: str, doc_conf: float, *, min_conf: float) -> FieldCheck:
    a, b = norm_number(csv_num), norm_number(doc_num)
    fc = FieldCheck("numero_piece", ECART, None, csv_num or "", doc_num or "")
    if not b:
        fc.status, fc.note = NON_LU, "numéro non lu sur la pièce"
        return fc
    if not a:
        fc.status, fc.note = MANQUANT_CSV, "numéro non renseigné dans le CSV"
        return fc
    if a == b:
        fc.status, fc.score = OK, 100.0
        return fc
    fc.score = round(fuzz.ratio(a, b), 1)
    ca, cb = collapse_confusables(a), collapse_confusables(b)
    if ca == cb:
        fc.status, fc.note = PROCHE, "confusion de caractères (O/0, I/1, S/5, B/8…)"
        return fc
    d = min(DamerauLevenshtein.distance(a, b), DamerauLevenshtein.distance(ca, cb))
    n = max(len(a), len(b))
    if d == 1 or (d == 2 and n >= 10):
        fc.status, fc.note = PROCHE, f"{d} caractère(s) d'écart : faute de frappe ou lecture probable"
    else:
        fc.status, fc.note = ECART, f"numéros différents ({d} caractères d'écart)"
        if doc_conf < min_conf:
            fc.status, fc.note = INCERTAIN, f"lecture incertaine (confiance {doc_conf:.2f}) ; {d} caractères d'écart"
    return fc


def verdict(name: FieldCheck, number: FieldCheck) -> tuple[str, list[str]]:
    checks = (name, number)
    reasons = [f"{c.field}: {c.status}" + (f" ({c.note})" if c.note else "") for c in checks if c.status != OK]
    st = {c.status for c in checks}
    if ECART in st or any(c.status == MANQUANT_CSV and c.doc_value for c in checks):
        return NON_CONFORME, reasons
    if st & {PROCHE, PARTIEL, NON_LU, INCERTAIN, MANQUANT_CSV} or (number.status != OK):
        return A_VERIFIER, reasons
    if TOLERE in st:
        return CONFORME_TOLERE, reasons
    return CONFORME, reasons
