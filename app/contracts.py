"""Contrôle des contrats : règles métier (port fidèle de la v1) — lecture CSV DIGIFOR, évaluation, statuts, lignes d'export.

Un contrat est un dict :
  {key, source, number, type, village, surface, duration, cf, request, ownerShare, beneficiaryShare,
   actors:[{role,name,phone,id,signed,pieces[],birthDate,automatic?}], observations:[{group,text,level,origin?}],
   manual, raw:{}}
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import unicodedata
from pathlib import Path

csv.field_size_limit(50_000_000)

RULES = json.loads((Path(__file__).parent / "rules.json").read_text(encoding="utf-8"))
MANDATORY_ROLES = ("Propriétaire", "Bénéficiaire")
IDENTITY_VERSION = 3
SURFACE_KEYS = ["surface", "superficie", "Superficie", "SUPERFICIE", "Surface", "SURFACE", "area", "terrainSurface",
                "surfaceTerrain"]
COLUMNS = ["Numéro de contrat", "Fichier source", "Type", "Village", "Propriétaire", "Bénéficiaire",
           "Observations identité et CNI", "Observations signatures", "Durée du contrat", "Observations pourcentages",
           "Num_demande", "Superficie", "Autre Observations"]

_NULLISH = re.compile(r"^(null|none|nan|non renseigné)$", re.I)
_EMPTY = re.compile(r"^(null|none|nan|undefined|n[/.-]?a|néant|neant|non renseign[ée]+)$", re.I)
_NUM = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_JSNUM = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
NAN = float("nan")


# ------------------------------------------------------------------ utilitaires
def clean(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if _NULLISH.match(s) else s


def norm(v) -> str:
    s = unicodedata.normalize("NFD", clean(v))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s, flags=re.I).upper()


def present(v) -> bool:
    return norm(v) not in ("", "0", "NON", "NO", "FALSE", "FAUX", "NULL", "NONE", "NAN", "RAS", "NONRENSEIGNE")


def first(r: dict, keys: list[str]) -> str:
    for k in keys:
        v = clean(r.get(k))
        if v:
            return v
    return ""


def js_number(s) -> float:
    """Équivalent de Number(s) pour les chaînes numériques usuelles (vide -> 0, invalide -> NaN)."""
    t = str(s).strip()
    if t == "":
        return 0.0
    return float(t) if _JSNUM.match(t) else NAN


def finite(x: float) -> bool:
    return not (math.isnan(x) or math.isinf(x))


def mandatory_identity(role: str) -> bool:
    return role in MANDATORY_ROLES


def find_rule(type_: str):
    n = norm(type_)
    for key, rule in RULES["types"].items():
        if any(norm(a) == n for a in [key, *rule["alias"]]):
            return key, rule
    return None


# ------------------------------------------------------------------ CSV
def _read_table(body: str, delimiter: str | None) -> list[dict]:
    body = body.strip()
    if not body:
        return []
    if delimiter is None:
        head = body.split("\n", 1)[0]
        delimiter = max(";,\t|", key=head.count) if any(d in head for d in ";,\t|") else ","
    return [dict(r) for r in csv.DictReader(io.StringIO(body), delimiter=delimiter)]


def parse_sections(text: str) -> dict[str, list[dict]]:
    sections: dict[str, list[dict]] = {}
    markers = list(re.finditer(r"^### TABLE:\s*(\w+).*$", text, re.M))
    if markers:
        for i, m in enumerate(markers):
            end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
            lines = [l for l in text[m.end():end].split("\n") if not l.startswith("#")]
            rows = _read_table("\n".join(lines), ";")
            if rows:
                sections[m.group(1)] = rows
    else:
        sections["contracts_new"] = _read_table(text, None)
    return sections


def parse_csv(text: str, source: str) -> list[dict]:
    sections = parse_sections(text.lstrip("﻿"))
    rows = sections.get("contracts_new", [])
    sigs: list[dict] = []
    for rs in sections.values():
        if rs and "ownerSignature" in rs[0]:
            sigs = rs
            break

    def linked(table: str, id_, number: str) -> dict:
        rs = sections.get(table, []) if table else []
        cid = clean(id_)
        if cid:
            for x in rs:
                if clean(x.get("id")) == cid:
                    return x
        for x in rs:
            if x.get("codeContract") == number:
                return x
        return {}

    out: list[dict] = []
    for index, r in enumerate(rows):
        number = first(r, ["codeContract", "N_CONTRAT", "Numéro de contrat", "numeroContrat"]) or f"LIGNE-{index + 1}"
        terrain = linked("terrains", r.get("terrain_id"), number)
        raw = {**terrain, **r}
        s = next((x for x in sigs if x.get("codeContract") == number), {})
        raw["ownerSignatoryName"] = s.get("ownerName")
        raw["beneficiarySignatoryName"] = s.get("beneficiaryName")
        actors = []
        for prefix, role, table, id_ in (
                ("owner", "Propriétaire", "owners", r.get("owner_id")),
                ("beneficiary", "Bénéficiaire", "beneficiaries", r.get("beneficiary_id")),
                ("ownerWitness", "Témoin propriétaire", "", ""),
                ("beneficiaryWitness", "Témoin bénéficiaire", "", ""),
                ("cvgfrPresident", "Président CVGFR", "", "")):
            person = linked(table, id_, number)
            alt = (["BAILLEUR_NOM", "Propriétaire"] if prefix == "owner"
                   else ["PRENEUR_NOM", "Bénéficiaire"] if prefix == "beneficiary" else [])
            name = clean(person.get("name")) or clean(s.get(prefix + "Name")) or first(r, alt)
            if not name and prefix not in ("owner", "beneficiary"):
                continue
            pieces: list[str] = []
            fp = s.get(prefix + "Fingerprint") or "[]"
            try:
                val = json.loads(fp)
                pieces = [str(x) for x in val] if isinstance(val, list) else []
            except Exception:  # noqa: BLE001
                if clean(s.get(prefix + "Fingerprint")):
                    pieces = [s[prefix + "Fingerprint"]]
            actors.append({
                "role": role, "name": name,
                "phone": clean(person.get("phoneNumber")) or clean(r.get(prefix + "PhoneNumber")),
                "id": clean(person.get("idNumber")) or clean(r.get(prefix + "IdNumber")),
                "birthDate": first(person, ["birthDate", "dateOfBirth", "dateNaissance"]),
                "signed": present(s.get(prefix + "Signature") or r.get(prefix + "Signature")),
                "pieces": pieces})
        c = {
            "key": f"{source}::{number}", "source": source, "number": number,
            "type": first(r, ["type", "TYPE_CONTRAT", "Type"]), "village": first(r, ["village", "VILLAGE", "Village"]),
            "surface": first(r, SURFACE_KEYS) or first(terrain, SURFACE_KEYS),
            "duration": first(r, ["duration", "DUREE", "Durée"]),
            "cf": first(raw, [*RULES["champs_cf"], *RULES["champs_presence_cf"]]),
            "request": first(raw, RULES["champs_demande"]),
            "ownerShare": first(r, ["planterPartagerOwnerPercentage", "POURCENTAGE_BAILLEUR"]),
            "beneficiaryShare": first(r, ["planterPartagerBeneficiaryPercentage", "POURCENTAGE_PRENEUR"]),
            "actors": actors, "observations": [], "manual": "", "raw": {k: ("" if v is None else v) for k, v in raw.items()},
        }
        out.append(evaluate(c))
    return out


# ------------------------------------------------------------------ statuts
def _empty(v) -> bool:
    s = str(v if v is not None else "").strip()
    return not s or bool(_EMPTY.match(s))


def _numeric(v) -> float:
    s = re.sub(r"[\s  ]", "", str(v if v is not None else "").strip()).replace(",", ".")
    return float(s) if _NUM.match(s) else NAN


def surface_value(c: dict) -> str:
    v = c.get("surface")
    return v if v is not None else first(c["raw"], SURFACE_KEYS)


def surface_status(c: dict) -> str:
    v = surface_value(c)
    if _empty(v):
        return "Superficie non rensegnée"          # orthographe conservée (rapports v1)
    n = _numeric(v)
    return "Superficie invalide" if (not finite(n) or n <= 0) else "RAS"


def duration_status(c: dict) -> str:
    value = clean(c.get("duration"))
    if not value:
        rule = find_rule(c.get("type", ""))
        return "RAS" if rule and rule[0] in ("VENTE", "DONATION") else "Durée non précisée"
    number = js_number(re.sub(r"\s", "", value).replace(",", "."))
    if not finite(number) or number < 0:
        return "Durée invalide"
    if number == 0:
        return "Durée nulle"
    unit = clean(c["raw"].get("durationUnit") or c["raw"].get("UNITE_DUREE") or "ans")
    u = norm(unit)
    years = number / 12 if "MOIS" in u else number / 365.25 if "JOUR" in u else number / 52 if "SEMAINE" in u else number
    return "Durée invalide, sup à 99 ans" if years > 99 else f"{value} {unit}"


def percentage_status(c: dict) -> str:
    rule = find_rule(c.get("type", ""))
    if not (rule and rule[1]["partage_champs"]):
        return "RAS"
    if not clean(c.get("ownerShare")) or not clean(c.get("beneficiaryShare")):
        return "Pourcentage à preciser"
    a, b = js_number(c["ownerShare"].replace(",", ".")), js_number(c["beneficiaryShare"].replace(",", "."))
    if not finite(a + b):
        return "Pourcentage à preciser"
    if abs(a + b - 100) < .01:
        return "RAS"
    return "Pourcentage superieur à 100%" if a + b > 100 else "Pourcentage inferieur à 100%"


def _required_reason(value, kind: str = "text") -> str:
    if _empty(value):
        return "non renseigné"
    v = str(value).strip()
    if ((kind != "number" and re.match(r"^0+(?:[.,]0+)?$", v)) or re.match(r"^(?:ras|inconnu|unknown|[x?*._-]+)$", v, re.I)
            or not any(ch.isalpha() or ch.isnumeric() for ch in v)):
        return "valeur non exploitable"
    if kind == "name" and not any(ch.isalpha() for ch in v):
        return "caractères non logiques"
    if kind == "number":
        n = _numeric(v)
        if not finite(n) or n < 0:
            return "valeur non numérique ou négative"
    return ""


def required_field_issues(c: dict) -> list[str]:
    issues: list[str] = []

    def check(label, value, kind="text"):
        reason = _required_reason(value, kind)
        if reason:
            issues.append(f"{label} : {reason}")

    check("Numéro de contrat", "" if re.match(r"^LIGNE-\d+$", c["number"]) else c["number"])
    check("Type", c.get("type"), "name")
    check("Village", c.get("village"), "name")
    check("Numéro de demande", c.get("request"))
    area = surface_status(c)
    if area != "RAS":
        issues.append(area)
    rule = find_rule(c.get("type", ""))
    if not rule or rule[0] not in ("VENTE", "DONATION"):
        check("Durée du contrat", c.get("duration"), "number")
    if rule and rule[1]["partage_champs"]:
        check("Pourcentage propriétaire", c.get("ownerShare"), "number")
        check("Pourcentage bénéficiaire", c.get("beneficiaryShare"), "number")
    if rule and rule[1]["taux_recolte_a_verifier"]:
        check("Taux de récolte", first(c["raw"], ["recoltePaiementPercentage", "CLE_PARTAGE"]), "number")
    if rule and rule[1]["dates_debut_fin_requises"]:
        check("Date de début", first(c["raw"], RULES["champs_dates"]["debut"]))
        check("Date de fin", first(c["raw"], RULES["champs_dates"]["fin"]))
    for role in MANDATORY_ROLES:
        if not any(a["role"] == role for a in c["actors"]):
            issues.append(f"{role} : acteur non renseigné")
    for a in c["actors"]:
        check(f"{a['role']} — Nom et prénom", a.get("name"), "name")
        if mandatory_identity(a["role"]):
            check(f"{a['role']} — Numéro de pièce", a.get("id"))
    return list(dict.fromkeys(issues))


# ------------------------------------------------------------------ identité (contrôle automatique des pièces)
def _current(a: dict):
    chk = a.get("automatic")
    if chk and chk.get("version") == IDENTITY_VERSION and norm(chk.get("expectedName")) == norm(a.get("name")) \
            and norm(chk.get("expectedId")) == norm(a.get("id")):
        return chk
    return None


def actor_identity_issues(a: dict) -> list[str]:
    chk = _current(a)
    issues: list[str] = []
    if chk:
        if chk.get("birthMismatch"):
            issues.append("Date de naissance non conforme")
        if chk.get("idStatus") == "Numero de piece incorrect":
            issues.append("Numero de piece incorrect")
        if chk.get("nameStatus") == "Nom et prenom non conforme":
            issues.append("Nom et prenom non conforme")
    if mandatory_identity(a["role"]):
        if not clean(a.get("name")):
            issues.append("Nom et prenom non renseignés")
        if not clean(a.get("id")):
            issues.append("Numero de piece non renseigné")
        if chk:
            if not chk.get("pieces"):
                issues.append("Pièce manquante")
            elif chk.get("status") != "validé" and not issues:
                issues.append(chk.get("detail") or "Validité de la pièce non vérifiable")
        elif not a.get("pieces"):
            issues.append("Pièce manquante")
        elif not issues:
            issues.append("Contrôle de la pièce en attente")
    return list(dict.fromkeys(issues))


def optional_identity_info(a: dict) -> list[str]:
    if mandatory_identity(a["role"]):
        return []
    chk = _current(a)
    if chk:
        if not chk.get("pieces"):
            return ["Pièce absente (non bloquant)"]
        if chk.get("status") in ("validé", "non conforme"):
            return []
        return [("Numero de piece non renseigné (non bloquant)" if not a.get("id")
                 else (chk.get("detail") or "Validité de la pièce non vérifiable") + " (non bloquant)")]
    return ["Contrôle de la pièce en attente (non bloquant)" if a.get("pieces") else "Pièce absente (non bloquant)"]


def identity_remarks(c: dict) -> str:
    lines = [f"{a['role']} — {a.get('name') or 'Nom non renseigné'} : {i}"
             for a in c["actors"] for i in [*actor_identity_issues(a), *optional_identity_info(a)]]
    for role in MANDATORY_ROLES:
        if not any(a["role"] == role for a in c["actors"]):
            lines.append(f"{role} : acteur non renseigné")
    # écarts d'identité issus d'un ancien rapport (import « snapshot ») : conservés dans l'export
    lines += [o["text"] for o in c.get("observations", [])
              if o.get("origin") == "historique" and o["group"] == "Identité" and o["level"] == "écart"]
    return "\n".join(lines) or "RAS"


_KEEP_OCR = re.compile(r"CNI scannée|Fichiers référencés|Nom sur pièce|Numéro de pièce sur scan|Rapprochement des pièces", re.I)
_KEEP_OPT = re.compile(r"CNI scannée|Fichiers référencés|Numéro de pièce CSV", re.I)
_KEEP_OPT2 = re.compile(r"Nom sur pièce|Numéro de pièce sur scan|Rapprochement des pièces|Nom du fichier", re.I)


def evaluate(c: dict) -> dict:
    optional_roles = [a["role"] for a in c["actors"] if not mandatory_identity(a["role"])]
    valid_roles = [a["role"] for a in c["actors"] if (chk := a.get("automatic")) and chk.get("version") == IDENTITY_VERSION
                   and chk.get("status") == "validé" and norm(chk.get("expectedName")) == norm(a.get("name"))
                   and norm(chk.get("expectedId")) == norm(a.get("id"))]
    obs = []
    for o in c.get("observations", []):
        if not (o["group"] in ("OCR", "Import") or o.get("origin") == "historique"):
            continue
        t = o["text"]
        if any(t.startswith(r + " ·") for r in valid_roles) and _KEEP_OCR.search(t):
            continue
        if any(t.startswith(r + " ·") for r in optional_roles) and (
                _KEEP_OPT.search(t) or (o["level"] == "à vérifier" and _KEEP_OPT2.search(t))):
            continue
        obs.append(o)

    def add(group, text, level="écart"):
        obs.append({"group": group, "text": text, "level": level})

    rule = find_rule(c.get("type", ""))
    if not rule:
        add("Autres", "Type de contrat non reconnu", "à vérifier")
    area = surface_status(c)
    if area != "RAS":
        add("Superficie", area)
    for issue in required_field_issues(c):
        add("Champs obligatoires", issue)
    d = duration_status(c)
    if d in ("Durée non précisée", "Durée nulle", "Durée invalide", "Durée invalide, sup à 99 ans"):
        add("Durée", d)
    if rule and rule[1]["dates_debut_fin_requises"]:
        for label, keys in (("début", RULES["champs_dates"]["debut"]), ("fin", RULES["champs_dates"]["fin"])):
            if not first(c["raw"], keys):
                add("Durée", f"Date de {label} non renseignée")
    if not clean(c.get("request")):
        add("Demande", "Numero de demande à preciser")
    if rule and rule[1]["partage_champs"]:
        os_, bs = c.get("ownerShare", ""), c.get("beneficiaryShare", "")
        a, b = js_number(os_.replace(",", ".")), js_number(bs.replace(",", "."))
        if not os_ or not bs:
            add("Pourcentages", "Pourcentages de partage non renseignés")
        elif (not finite(a + b) or a < 0 or b < 0 or a > 100 or b > 100 or abs(a + b - 100) > .01):
            add("Pourcentages", f"Partage incohérent : {os_} % + {bs} % (attendu : 100 %)")
    if rule and rule[1]["taux_recolte_a_verifier"]:
        v = first(c["raw"], ["recoltePaiementPercentage", "CLE_PARTAGE"])
        if not v:
            add("Pourcentages", "Taux de récolte non renseigné")
        else:
            n = js_number(v.replace(",", "."))
            if n < 0 or n > 100:
                add("Pourcentages", "Taux de récolte hors de 0–100 %")
    if not c["actors"]:
        add("Signature", "Signataires non renseignés")
    for a in c["actors"]:
        if not a.get("name"):
            add("Identité", f"{a['role']} : nom non renseigné")
        if not a.get("signed"):
            add("Signature", f"{a['role']} — {a.get('name') or 'Nom absent'} — {a.get('phone') or 'Contact non renseigné'} : signature absente")
        prefix = "owner" if a["role"] == "Propriétaire" else "beneficiary" if a["role"] == "Bénéficiaire" else ""
        sig = c["raw"].get(prefix + "SignatoryName") if prefix else None
        if prefix and sig and norm(a.get("name")) != norm(sig):
            add("Identité", f"{a['role']} : nom différent du signataire ({sig})")
        if not a.get("id") and a["role"] in MANDATORY_ROLES:
            add("Identité", f"{a['role']} : numéro de pièce non renseigné")
        for issue in actor_identity_issues(a):
            add("Identité", f"{a['role']} — {a.get('name')} : {issue}",
                "à vérifier" if re.search(r"non vérifiable|en attente", issue) else "écart")
    for role in MANDATORY_ROLES:
        if not any(a["role"] == role for a in c["actors"]):
            add("Identité", f"{role} : acteur non renseigné")
    if c.get("manual", "").strip():
        add("Autres", c["manual"].strip(), "à vérifier")
    return {**c, "observations": obs}


def unsigned(c: dict) -> str:
    lines = [f"{a.get('name')} | {a.get('phone') or 'Contact non renseigné'} | {a['role']}"
             for a in c["actors"] if not a.get("signed")]
    return "\n".join(lines) or ("Signataires non renseignés" if not c["actors"] else "RAS")


def export_row(c: dict) -> list[str]:
    def actor(role):
        return next((a.get("name") for a in c["actors"] if a["role"] == role), "") or ""
    extra = [*required_field_issues(c), *([clean(c.get("manual"))] if clean(c.get("manual")) else [])]
    return [c["number"], c["source"], c.get("type", ""), c.get("village", ""), actor("Propriétaire"), actor("Bénéficiaire"),
            identity_remarks(c), unsigned(c), duration_status(c), percentage_status(c),
            "RAS" if clean(c.get("request")) else "Numero de demande à preciser", surface_status(c),
            "\n".join(extra) or "RAS"]


def from_snapshot(d: dict) -> list[dict]:
    """Ancien rapport de diagnostic (feuilles « Tous les contrats », « Acteurs et pièces », « Incohérences »)."""
    out = []
    for r in d.get("Tous les contrats", []):
        number, source = str(r.get("Numéro de contrat")), str(r.get("Fichier source"))
        belongs = lambda x: x.get("Numéro de contrat") == number and x.get("Fichier source") == source  # noqa: E731
        actors = [{"role": str(a.get("Acteur") or ""), "name": str(a.get("Nom et prénom") or ""),
                   "phone": str(a.get("Téléphone") or ""), "id": str(a.get("Numéro de pièce CSV") or ""),
                   "signed": a.get("Signature") == "Présente",
                   "pieces": [p for p in re.split(r"\s*;\s*|\s*\|\s*", str(a.get("Pièces liées") or "")) if p]}
                  for a in d.get("Acteurs et pièces", []) if belongs(a)]
        obs = []
        for x in d.get("Incohérences", []):
            if not belongs(x):
                continue
            champ = str(x.get("Champ contrôlé"))
            group = ("Signature" if re.search("signature", champ, re.I) else "Durée" if re.search("durée|pourcentage|percentage", champ, re.I)
                     else "Identité" if re.search("nom|cni|pièce|fichier|ocr", champ, re.I) else "Import")
            level = "écart" if str(x.get("Statut")) in ("Incohérence", "Discordance", "Pièce manquante", "Non conforme", "Non-conformité") else "à vérifier"
            obs.append({"origin": "historique", "group": group, "level": level,
                        "text": f"{x.get('Acteur') or ''} · {champ} : {x.get('Détail') or x.get('Statut') or ''}"})
        out.append({"key": f"{source}::{number}", "source": source, "number": number, "type": str(r.get("Type") or ""),
                    "village": str(r.get("Village") or ""), "surface": clean(r.get("Superficie") or r.get("Surface") or ""),
                    "duration": str(r.get("Durée") or ""), "cf": "", "request": "",
                    "ownerShare": str(r.get("Pourcentage propriétaire") or ""),
                    "beneficiaryShare": str(r.get("Pourcentage bénéficiaire") or ""),
                    "actors": actors, "observations": obs, "manual": "", "raw": {"snapshot": "true"}})
    return out
