"""Contrôle des pièces CF : une demande = un demandeur (table « applicant ») + ses scans recto/verso.

Seules les pièces VALIDES entrent dans la base (SQLite + fichiers) ; l'Excel ne contient que les pièces INVALIDÉES
(floues, illisibles, mal scannées, scannées en double, numéro différent du CSV).
"""
from __future__ import annotations

import io
import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Callable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .campaigns import data_dir, now
from .cf_quality import hamming, measure, similar_views
from .config import Settings
from .contracts import parse_sections
from .matching import FieldCheck, INCERTAIN, OK, compare_number
from .normalize import norm_number
from .pipeline import collect_scans, extract_all

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
# Seuils (réglables) — calibrés sur l'export CF de la région Nawa
SHARP_MIN = 2.3        # netteté normalisée (cf_quality.sharpness)
MIN_SIDE = 500         # px, plus petit côté
DARK, BRIGHT, FLAT = 55.0, 235.0, 22.0
ORB_DUP = 60           # correspondances ORB cohérentes => même face photographiée deux fois
DHASH_DUP = 5
PREFIX_OK = {"", "C", "I", "1", "L", "CL", "C1", "CI", "LI", "IC"}


# ------------------------------------------------------------------ données de l'export
def parse_export(text: str) -> dict:
    sec = parse_sections(text.lstrip("﻿"))
    if "applicant" not in sec:
        raise ValueError("table « applicant » absente de l'export")
    villages = {r.get("codeVillage", "").strip(): r.get("label", "").strip() for r in sec.get("village", [])}
    sprefs = {r.get("codeSubPrefecture", "").strip(): r.get("label", "").strip() for r in sec.get("subprefecture", [])}
    requests = {r.get("requestNumber", "").strip(): r for r in sec.get("request", [])}
    out = []
    for a in sec["applicant"]:
        num = (a.get("applicantNumber") or "").strip()
        req = num[3:] if num.upper().startswith("APT") else num
        parts = req.split("-")
        sp_code = parts[0] if parts else ""
        v_code = "-".join(parts[:2]) if len(parts) > 1 else ""
        try:
            refs = [Path(str(x).replace("\\", "/")).name for x in json.loads(a.get("photoidentity") or "[]")]
        except Exception:  # noqa: BLE001
            refs = []
        out.append({
            "demande": req, "applicant": num, "code_sous_prefecture": sp_code, "code_village": v_code,
            "sous_prefecture": sprefs.get(sp_code, "Non trouvée"), "village": villages.get(v_code, "Non trouvé"),
            "nom": (a.get("firstname") or "").strip(), "prenoms": (a.get("lastname") or "").strip(),
            "type_piece": (a.get("identityDocumentType") or "").strip(),
            "numero_csv": (a.get("identityDocumentNumber") or "").strip(),
            "ce": (requests.get(req, {}).get("nomPrenomCE") or "").strip(), "refs": refs})
    return {"applicants": out, "n_villages": len(villages), "n_sprefs": len(sprefs), "villages": villages,
            "sprefs": sprefs, "requests": set(requests)}


def list_images(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.is_file() and p.suffix.lower() in IMG_EXT) if d.is_dir() else []


def scans_for(app: dict, files: list[str]) -> list[str]:
    fs = set(files)
    found = [r for r in app["refs"] if r in fs]
    found += [f for f in files if (f.startswith(app["demande"] + "_") or app["applicant"] and app["applicant"] + "_" in f)
              and f not in found]
    return sorted(dict.fromkeys(found), key=lambda n: (re.sub(r"_\d+(\.\w+)$", "", n), n))


def request_of_file(name: str) -> str:
    """380-014-041001_0.jpg -> 380-014-041001 (le suffixe _0 / _1 désigne seulement le recto / verso)."""
    m = re.search(r"(\d{3}-\d{3}-\d{6})", name)
    return m.group(1) if m else re.sub(r"_\d+(\.\w+)?$", "", Path(name).stem)


def side_from_name(name: str) -> str:
    m = re.search(r"_(\d+)\.\w+$", name)
    return {"0": "RECTO", "1": "VERSO"}.get(m.group(1), "INCONNUE") if m else "INCONNUE"


# ------------------------------------------------------------------ numéro
def compare_cf_number(csv_num: str, read: str, conf: float) -> FieldCheck:
    fc = compare_number(csv_num, read, conf, min_conf=0.6)
    if fc.status != OK:
        a, b = norm_number(csv_num), norm_number(read)
        if re.fullmatch(r"CI\d{9}", a) and len(b) >= 9 and b[-9:] == a[2:] and b[:-9] in PREFIX_OK:
            return FieldCheck("numero_piece", OK, 100.0, csv_num, read, "préfixe « CI » mal lu, 9 chiffres identiques")
    return fc


def best_number(csv_num: str, exts: list[tuple[str, object]]) -> tuple[FieldCheck | None, str]:
    best, src = None, ""
    rank = {OK: 0, INCERTAIN: 3}
    for name, ex in exts:
        if not ex or not ex.numero_piece:
            continue
        fc = compare_cf_number(csv_num, ex.numero_piece, ex.conf("numero_piece"))
        if best is None or rank.get(fc.status, 1) < rank.get(best.status, 1):
            best, src = fc, name
    return best, src


# ------------------------------------------------------------------ analyse
def quality_motifs(m: dict) -> list[str]:
    if not m["ok"]:
        return ["image illisible (fichier corrompu)"]
    out = []
    if min(m["width"], m["height"]) < MIN_SIDE:
        out.append(f"résolution insuffisante ({m['width']}×{m['height']})")
    if m["sharp"] < SHARP_MIN:
        out.append("image floue")
    if m["mean"] < DARK:
        out.append("image trop sombre")
    elif m["mean"] > BRIGHT:
        out.append("image surexposée")
    if m["std"] < FLAT:
        out.append("contraste insuffisant (image quasi vide)")
    return out


def analyse_request(app: dict, names: list[str], sc: dict) -> dict:
    """sc : nom -> {metrics, extraction(Extraction|None), sha, gray}. Retourne le résultat de la demande."""
    motifs: list[str] = []
    detail: list[str] = []
    faces: dict[str, str] = {}
    scans = []
    for n in names:
        s = sc[n]
        ex = s["extraction"]
        face = ex.face if ex and ex.face in ("RECTO", "VERSO") and (ex.nom or ex.numero_piece or ex.face == "VERSO") else side_from_name(n)
        faces[n] = face
        q = quality_motifs(s["metrics"])
        for x in q:
            motifs.append(f"{x} ({n})")
        scans.append({"fichier": n, "face": face, "net": s["metrics"].get("sharp"), "largeur": s["metrics"].get("width"),
                      "hauteur": s["metrics"].get("height"), "problemes": q,
                      "numero_lu": ex.numero_piece if ex else None})
    # --- doublons dans la même demande
    seen_pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            sa, sb = sc[a], sc[b]
            why = ""
            if sa["sha"] == sb["sha"]:
                why = "même fichier"
            elif sa["metrics"]["ok"] and sb["metrics"]["ok"]:
                if hamming(sa["metrics"]["dhash"], sb["metrics"]["dhash"]) <= DHASH_DUP:
                    why = "images quasi identiques"
                elif sa["gray"] is not None and sb["gray"] is not None and similar_views(sa["gray"], sb["gray"]) >= ORB_DUP:
                    why = "même face photographiée deux fois"
            ea, eb = sa["extraction"], sb["extraction"]
            if not why and ea and eb and ea.face == eb.face == "RECTO" and (ea.nom or ea.numero_piece) and (eb.nom or eb.numero_piece):
                why = "recto scanné deux fois"
            if why:
                seen_pairs.add((a, b))
                side = faces[a].lower() if faces[a] == faces[b] else "la même face est présente en recto et en verso"
                motifs.append(f"pièce scannée en double — {side} ({why}) : {a} et {b}")
    # --- faces manquantes (CNI : recto + verso attendus)
    cni = "identit" in app["type_piece"].lower() and "attestation" not in app["type_piece"].lower()
    if not names:
        motifs.append("aucun scan trouvé pour cette demande")
    elif cni and not seen_pairs:
        have = set(faces.values())
        if len(names) == 1 or not ({"RECTO", "VERSO"} <= have):
            miss = "verso" if "RECTO" in have and "VERSO" not in have else "recto" if "VERSO" in have and "RECTO" not in have else "une face"
            motifs.append(f"face manquante : {miss} non scanné")
    elif len(names) > 2 and not seen_pairs:
        detail.append(f"{len(names)} scans pour une seule demande")
    # --- numéro de pièce du scan = numéro de la table applicant
    nb, src = best_number(app["numero_csv"], [(n, sc[n]["extraction"]) for n in names])
    read = (sc[src]["extraction"].numero_piece if src else "") or ""
    if not app["numero_csv"]:
        motifs.append("numéro de pièce absent de la table applicant")
        num_status = "MANQUANT_CSV"
    elif nb is None:
        num_status = "NON_LU"
    else:
        num_status = nb.status
    if num_status == "ECART":
        motifs.append(f"numéro de pièce du scan ({read}) différent de la table applicant ({app['numero_csv']})")
    unconfirmed = num_status in ("NON_LU", "INCERTAIN", "PROCHE")
    if nb and nb.note and nb.status != OK:
        detail.append("numéro : " + nb.note)
    elif nb and nb.note:
        detail.append("numéro : " + nb.note)
    if motifs:
        statut = "INVALIDE"
    elif num_status == OK:
        statut = "VALIDE"
    else:
        statut = "A_VERIFIER"
        motifs_v = {"NON_LU": "numéro de pièce non lu sur le scan", "INCERTAIN": "lecture du numéro incertaine",
                    "PROCHE": f"numéro lu ({read}) proche mais différent du CSV ({app['numero_csv']})"}
        detail.insert(0, motifs_v.get(num_status, "numéro non confirmé"))
    return {**{k: app[k] for k in ("demande", "applicant", "sous_prefecture", "village", "nom", "prenoms", "type_piece",
                                    "numero_csv", "ce")},
            "numero_lu": read, "statut": statut, "motifs": motifs, "details": detail, "numero_statut": num_status,
            "scans": scans}


def run_session(sdir: Path, settings: Settings, extractor, progress: Callable[[int, int, str], None] | None = None) -> dict:
    csvs = sorted(sdir.glob("*.csv"))
    if not csvs:
        raise ValueError("aucun CSV dans l'export")
    from .csv_loader import decode
    exp = parse_export(decode(csvs[0].read_bytes()))
    files = list_images(sdir / "Images")
    items = collect_scans([sdir / "Images" / f for f in files])
    by_name = {i.filename: i for i in items}
    if progress:
        progress(0, len(items), "Lecture des pièces")
    extract_all(items, extractor, settings, lambda d, t: progress and progress(d, t, "Lecture des pièces"))
    sc: dict[str, dict] = {}
    for n, it in by_name.items():
        m, g = measure(it.path)
        sc[n] = {"metrics": m.to_dict(), "gray": g, "extraction": it.extraction, "sha": it.sha}
    results, used = [], set()
    for app in exp["applicants"]:
        names = [n for n in scans_for(app, files) if n in sc]
        used.update(names)
        results.append(analyse_request(app, names, sc))
    for r in results:
        r["_n"] = len(r["scans"])
    orphans = []
    for n in files:
        if n in used or n not in sc:
            continue
        dem = request_of_file(n)               # « _0 » / « _1 » = recto / verso, pas une partie du numéro
        parts = dem.split("-")
        sp, vl = parts[0], "-".join(parts[:2])
        known = dem in exp["requests"]
        orphans.append({"fichier": n, "demande": dem, "face": side_from_name(n),
                        "sous_prefecture": exp["sprefs"].get(sp, "Non trouvée"), "village": exp["villages"].get(vl, "Non trouvé"),
                        "net": sc[n]["metrics"].get("sharp"),
                        "numero_lu": sc[n]["extraction"].numero_piece if sc[n]["extraction"] else None,
                        "motif": (f"demande {dem} présente dans la table request mais sans ligne dans la table applicant" if known else
                                  f"demande {dem} absente des tables applicant et request de cet export")})
    summary = {k: sum(1 for r in results if r["statut"] == k) for k in ("VALIDE", "INVALIDE", "A_VERIFIER")}
    summary.update(DEMANDES=len(results), IMAGES=len(files), ORPHELINS=len(orphans),
                   MOTIFS=_count_motifs(results))
    return {"results": results, "orphans": orphans, "summary": summary, "backend": getattr(extractor, "cache_id", ""),
            "analysed": now()}


def _count_motifs(results: list[dict]) -> dict:
    c: dict[str, int] = {}
    for r in results:
        if r["statut"] != "INVALIDE":
            continue
        seen = set()
        for m in r["motifs"]:
            k = re.sub(r"\s*\(.*$|\s*:.*$", "", m).strip()
            k = re.sub(r"numéro de pièce du scan.*", "numéro différent du CSV", k)
            if k not in seen:
                seen.add(k)
                c[k] = c.get(k, 0) + 1
    return dict(sorted(c.items(), key=lambda x: -x[1]))


# ------------------------------------------------------------------ base des pièces validées
def valid_root() -> Path:
    d = data_dir() / "cf_valid"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(valid_root() / "pieces_valides.db")
    con.execute("""CREATE TABLE IF NOT EXISTS pieces_valides(
        demande TEXT PRIMARY KEY, applicant TEXT, sous_prefecture TEXT, village TEXT, nom TEXT, prenoms TEXT,
        type_piece TEXT, numero_piece TEXT, recto TEXT, verso TEXT, valide_le TEXT, session TEXT)""")
    return con


def store_valid(results: list[dict], sdir: Path, session: str) -> int:
    n = 0
    with _db() as con:
        for r in results:
            if r["statut"] != "VALIDE":
                continue
            dest = valid_root() / "pieces" / re.sub(r"[^\w-]+", "_", r["village"]) / r["demande"]
            dest.mkdir(parents=True, exist_ok=True)
            for old in dest.iterdir():
                old.unlink()
            stored = {}
            for s in r["scans"]:
                side = "recto" if s["face"] == "RECTO" else "verso" if s["face"] == "VERSO" else "autre"
                if side in stored:
                    continue
                src = sdir / "Images" / s["fichier"]
                target = dest / f"{r['demande']}_{side}{src.suffix.lower()}"
                shutil.copyfile(src, target)
                stored[side] = str(target.relative_to(valid_root()))
            con.execute("INSERT OR REPLACE INTO pieces_valides VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (r["demande"], r["applicant"], r["sous_prefecture"], r["village"], r["nom"], r["prenoms"],
                         r["type_piece"], r["numero_csv"], stored.get("recto", ""), stored.get("verso", ""), now(), session))
            n += 1
    return n


def valid_rows() -> list[dict]:
    with _db() as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute("SELECT * FROM pieces_valides ORDER BY village, demande")]


# ------------------------------------------------------------------ Excel (uniquement les invalidées)
def _sheet(wb: Workbook, title: str, heads: list[str], rows: list[list], first: bool = False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(heads)
    for r in rows:
        ws.append([("'" + v if isinstance(v, str) and v[:1] and v[0] in "=+-@" else v) for v in r])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFFFF")
        c.fill = PatternFill("solid", fgColor="163F38")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(ws.columns):
        ws.column_dimensions[col[0].column_letter].width = 60 if heads[i] in ("Motif(s)", "Détail", "Fichiers") else 24
        for cell in col:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def invalid_workbook(data: dict) -> bytes:
    R = data["results"]
    wb = Workbook()
    heads = ["Numéro de demande", "Sous-prefecture", "Village", "Demandeur", "Type de pièce", "N° pièce (table applicant)",
             "N° pièce lu sur le scan", "Motif(s)", "Détail", "Fichiers"]
    inv = [[r["demande"], r["sous_prefecture"], r["village"], f"{r['nom']} {r['prenoms']}".strip(), r["type_piece"],
            r["numero_csv"], r["numero_lu"], "\n".join(r["motifs"]), "\n".join(r["details"]),
            "\n".join(s["fichier"] for s in r["scans"])] for r in R if r["statut"] == "INVALIDE"]
    _sheet(wb, "Invalidées", heads, inv, first=True)
    ver = [[r["demande"], r["sous_prefecture"], r["village"], f"{r['nom']} {r['prenoms']}".strip(), r["type_piece"],
            r["numero_csv"], r["numero_lu"], "\n".join(r["details"]), "\n".join(s["fichier"] for s in r["scans"])]
           for r in R if r["statut"] == "A_VERIFIER"]
    _sheet(wb, "À vérifier (n° non confirmé)", [*heads[:7], "Détail", "Fichiers"], ver)
    grp: dict[str, dict] = {}
    for o in data["orphans"]:
        g = grp.setdefault(o["demande"], {"o": o, "RECTO": [], "VERSO": [], "INCONNUE": [], "lus": []})
        g[o["face"]].append(o["fichier"])
        if o["numero_lu"] and o["numero_lu"] not in g["lus"]:
            g["lus"].append(o["numero_lu"])
    _sheet(wb, "Scans sans demande", ["Numéro de demande", "Sous-prefecture", "Village", "Recto", "Verso", "N° lu", "Motif(s)"],
           [[d, g["o"]["sous_prefecture"], g["o"]["village"], "\n".join(g["RECTO"] + g["INCONNUE"]), "\n".join(g["VERSO"]),
             ", ".join(g["lus"]), g["o"]["motif"]] for d, g in sorted(grp.items())])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
