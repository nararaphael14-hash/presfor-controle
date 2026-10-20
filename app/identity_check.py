"""Contrôle automatique des pièces d'une campagne : moteur d'identité v2 branché sur les acteurs des contrats."""
from __future__ import annotations

import re
from pathlib import Path

from .config import Settings
from .contracts import evaluate, norm
from .csv_loader import Row
from .matching import A_VERIFIER, CONFORME, CONFORME_TOLERE, NON_CONFORME, PIECE_INUTILISABLE, PIECE_MANQUANTE
from .normalize import norm_date, parse_scan_filename
from .pipeline import Link, ScanItem, evaluate_row, link_scan
from .schemas import Extraction

ROLE_CODES = {"Propriétaire": "PROP", "Bénéficiaire": "BEN", "Témoin propriétaire": "TP",
              "Témoin bénéficiaire": "TB", "Président CVGFR": "PCVGFR"}
_CODE = re.compile(r"CT-\d{3}-\d{3}-\d{6}-\d{2}-[a-z]+", re.I)
VERSION = 3

STATUS_FR = {"PROCHE": "proche (à vérifier)", "PARTIEL": "partiel (à vérifier)", "ECART": "différent",
             "NON_LU": "non lu sur la pièce", "MANQUANT_CSV": "non renseigné dans le CSV",
             "INCERTAIN": "lecture incertaine", "TOLERE": "toléré"}


def basename(name: str) -> str:
    return name.replace("\\", "/").split("/")[-1] or name


def resolve_scan(name: str, names: list[str], aliases: dict[str, str] | None = None) -> str | None:
    aliases = aliases or {}
    if name in names:
        return name
    alias = aliases.get(name) or aliases.get(basename(name))
    if alias and alias in names:
        return alias
    m = [n for n in names if basename(n) == basename(name)]
    return m[0] if len(m) == 1 else None


def inventory_aliases(rows: list[dict], names: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in rows:
        orig, target = r.get("Fichier original"), r.get("Pièce renommée")
        if not orig or not target:
            continue
        exact = next((n for n in names if n == basename(target)), None)
        if exact:
            out[orig] = out[basename(orig)] = exact
    return out


def actor_candidates(actor: dict, contract: str, names: list[str], aliases: dict[str, str]) -> list[str]:
    exact = [r for p in actor.get("pieces", []) if (r := resolve_scan(p, names, aliases))]
    safe_contract = re.sub(r"[^a-zA-Z0-9_-]+", "_", contract)
    other = []
    nm = norm(actor.get("name"))
    code = ROLE_CODES.get(actor["role"])
    for n in names:
        m = _CODE.search(n)
        if m and norm(m.group(0)) != norm(contract):
            continue
        if (len(nm) > 5 and nm in norm(basename(n))) or (code and safe_contract in n and f"_{code}_" in n):
            other.append(n)
    return list(dict.fromkeys([*exact, *other]))


def items_from_ocr(ocr: dict, scans_dir: Path) -> list[ScanItem]:
    items: list[ScanItem] = []
    for key in sorted(ocr):
        rec = ocr[key]
        fname, _, page = key.partition("#p")
        code, hint, _p = parse_scan_filename(fname)
        try:
            ex = Extraction.model_validate(rec.get("extraction") or {})
        except Exception:  # noqa: BLE001
            ex = None
        items.append(ScanItem(len(items), key, scans_dir / fname, int(page) - 1 if page else 0, code, hint,
                              rec.get("sha", ""), ex))
    return items


def _fs(res, key: str, bad: str) -> str:
    s = res.checks.get(key, {}).get("status")
    if s in ("OK", "TOLERE"):
        return "RAS"
    return bad if s == "ECART" else "Non vérifiable"


def _detail(res) -> str:
    parts = []
    for key, label in (("nom_complet", "Nom"), ("numero_piece", "N° de pièce")):
        c = res.checks.get(key)
        if c and c["status"] not in ("OK",):
            parts.append(f"{label} {STATUS_FR.get(c['status'], c['status'])}" + (f" — {c['note']}" if c.get("note") else ""))
    return " ; ".join([*parts, *res.flags]) or "; ".join(res.reasons)


def check_actor(contract: str, actor: dict, names: list[str], aliases: dict[str, str], items: list[ScanItem],
                settings: Settings) -> dict:
    by_file: dict[str, list[ScanItem]] = {}
    for it in items:
        by_file.setdefault(it.path.name, []).append(it)
    cand = actor_candidates(actor, contract, names, aliases)
    links = [Link(it, 100.0, ["pièce rattachée"], False) for f in cand for it in by_file.get(f, [])]
    row = Row(0, code=contract, role=actor["role"], full=actor.get("name") or "", numero=actor.get("id") or "",
              birth=actor.get("birthDate") or "")
    by_content = False
    if not links:
        links = [lk for s in items if (lk := link_scan(row, s, settings))]
        by_content = bool(links)
    base = {"version": VERSION, "expectedName": actor.get("name") or "", "expectedId": actor.get("id") or ""}
    files = list(dict.fromkeys(l.scan.path.name for l in links))
    if not links:
        return {**base, "status": "non vérifiable", "state": "missing", "nameStatus": "Non vérifiable",
                "idStatus": "Non vérifiable", "nameMatched": False, "idMatched": False, "pieces": [],
                "detail": "Pièce absente", "confidence": 0, "score": 0}
    res = evaluate_row(row, links, settings)
    ex = res.extracted or {}
    obs_name = " ".join(x for x in (ex.get("nom"), ex.get("prenoms")) if x)
    conf = round(100 * max(ex.get("confiance_nom") or 0, ex.get("confiance_numero") or 0))
    score = round((res.checks.get("nom_complet") or {}).get("score") or 0)
    birth_bad = False
    want = norm_date(actor.get("birthDate"))
    for l in links:
        e = l.scan.extraction
        if want and e and e.date_naissance and (e.backend or "").startswith("claude") and norm_date(e.date_naissance) != want:
            birth_bad = True
    out = {**base, "pieces": files, "observedName": obs_name, "observedId": ex.get("numero_piece") or "",
           "confidence": conf, "score": score, "birthMismatch": birth_bad,
           "documentTypes": [ex["type_piece"]] if ex.get("type_piece") else [],
           "nameStatus": _fs(res, "nom_complet", "Nom et prenom non conforme"),
           "idStatus": _fs(res, "numero_piece", "Numero de piece incorrect"),
           "nameMatched": res.checks.get("nom_complet", {}).get("status") in ("OK", "TOLERE"),
           "idMatched": res.checks.get("numero_piece", {}).get("status") == "OK"}
    suffix = " (rattachée d'après son contenu)" if by_content else ""
    if res.verdict in (CONFORME, CONFORME_TOLERE) and not birth_bad:
        out.update(status="validé", state="analysed", detail="Nom et numéro de pièce concordants"
                   + (" (nom toléré)" if res.verdict == CONFORME_TOLERE else "") + suffix)
    elif birth_bad or out["nameStatus"] == "Nom et prenom non conforme" or out["idStatus"] == "Numero de piece incorrect":
        d = _detail(res)
        if birth_bad:
            d = (d + " ; " if d else "") + "Date de naissance non conforme"
        out.update(status="non conforme", state="analysed", detail=d + suffix)
    elif res.verdict == PIECE_INUTILISABLE:
        out.update(status="non vérifiable", state="error" if any("erreur" in r for r in res.reasons) else "insufficient",
                   detail="; ".join(res.reasons) + suffix)
    else:
        amb = any("numéros différents" in f for f in res.flags)
        out.update(status="non vérifiable", state="ambiguous" if amb else "insufficient",
                   detail=("Rattachement ambigu — " if amb else "") + (_detail(res) or "Lecture insuffisante") + suffix)
    return out


def apply_identity(campaign: dict, ocr: dict, scans_dir: Path, names: list[str], settings: Settings,
                   only_stale: bool = False) -> dict:
    """Recalcule `automatic` de chaque acteur à partir des extractions déjà faites (aucun appel OCR/API)."""
    items = items_from_ocr(ocr, scans_dir)
    aliases = campaign.get("aliases", {})
    all_names = list(dict.fromkeys([*names, *[i.path.name for i in items]]))
    contracts = []
    for c in campaign["contracts"]:
        actors = []
        for a in c["actors"]:
            chk = a.get("automatic")
            fresh = (chk and chk.get("version") == VERSION and norm(chk.get("expectedName")) == norm(a.get("name"))
                     and norm(chk.get("expectedId")) == norm(a.get("id")))
            if only_stale and fresh:
                actors.append(a)
                continue
            actors.append({**a, "automatic": check_actor(c["number"], a, all_names, aliases, items, settings)})
        contracts.append(evaluate({**c, "actors": actors}))
    return {**campaign, "contracts": contracts}
