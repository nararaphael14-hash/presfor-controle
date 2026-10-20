"""Lecture du CSV des agents : CSV simple (; , tab |) ou export multi-tables DIGIFOR (### TABLE: …)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .normalize import norm_text

csv.field_size_limit(50_000_000)

FIELDS = ("code", "role", "nom", "prenoms", "full", "numero", "type", "birth", "fichier")

# Alias (en-têtes normalisés, sans espaces/accents/ponctuation), par ordre de préférence
ALIASES = {
    "nom": ["NOM", "NOMDEFAMILLE", "NOMFAMILLE", "LASTNAME", "SURNAME", "NOMTITULAIRE"],
    "prenoms": ["PRENOMS", "PRENOM", "FIRSTNAME", "FIRSTNAMES", "GIVENNAMES", "PRENOMTITULAIRE"],
    "full": ["NOMETPRENOMS", "NOMPRENOMS", "NOMCOMPLET", "FULLNAME", "NAME", "OWNERNAME", "BENEFICIARYNAME",
             "NOMDUSIGNATAIRE", "SIGNATAIRE"],
    "numero": ["IDNUMBER", "IDENTITYDOCUMENTNUMBER", "NUMEROPIECE", "NUMPIECE", "NUMEROCNI", "NUMEROPIECEIDENTITE",
               "NPIECE", "NUMERO", "NUMEROIDENTITE", "NCNI", "PIECENUMERO", "DOCUMENTNUMBER"],
    "type": ["IDTYPE", "IDENTITYDOCUMENTTYPE", "TYPEPIECE", "TYPEDEPIECE", "TYPEIDENTITE", "TYPEPIECEIDENTITE"],
    "code": ["CODECONTRACT", "CODECONTRAT", "CONTRAT", "CONTRACT", "NUMEROCONTRAT", "REFCONTRAT"],
    "birth": ["BIRTHDATE", "DATENAISSANCE", "DATEDENAISSANCE", "NEDATE", "DATENAIS"],
    "fichier": ["FICHIER", "FILE", "FILENAME", "SCAN", "PIECEJOINTE", "NOMFICHIER", "FICHIERSCAN"],
    "role": ["ROLE", "QUALITE", "STATUT"],
}

ROLE_BY_TABLE = {"owners": "PROPRIETAIRE", "beneficiaries": "BENEFICIAIRE"}
DEFAULT_TABLES = ["owners", "beneficiaries"]


@dataclass
class Row:
    idx: int
    code: str = ""
    role: str = ""
    nom: str = ""
    prenoms: str = ""
    full: str = ""
    numero: str = ""
    type: str = ""
    birth: str = ""
    fichier: str = ""
    raw: dict = field(default_factory=dict)


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def is_digifor(text: str) -> bool:
    return "### TABLE:" in text[:20000] or "### TABLE:" in text


def parse_digifor(text: str) -> dict[str, tuple[list[str], list[list[str]]]]:
    tables: dict[str, tuple[list[str], list[list[str]]]] = {}
    name = None
    hdr: list[str] | None = None
    for values in csv.reader(io.StringIO(text), delimiter=";"):
        if not values or not any(v.strip() for v in values):
            continue
        first = values[0].strip()
        if first.startswith("### TABLE:"):
            m = re.match(r"### TABLE:\s*(\w+)", first)
            name = m.group(1) if m else None
            hdr = None
            continue
        if first.startswith("#"):
            continue
        if name is None:
            continue
        if hdr is None:
            hdr = [h.strip() for h in values]
            tables[name] = (hdr, [])
            continue
        tables[name][1].append(values)
    return tables


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:5])
    counts = {d: sample.count(d) for d in (";", ",", "\t", "|")}
    return max(counts, key=counts.get)


@dataclass
class LoadedCsv:
    kind: str                      # "simple" | "digifor"
    headers: list[str]
    records: list[dict]            # une entrée par ligne, clés = en-têtes (+ "__table")
    tables: list[str] = field(default_factory=list)   # tables disponibles (digifor)
    selected_tables: list[str] = field(default_factory=list)


def load_csv(data: bytes, tables: list[str] | None = None) -> LoadedCsv:
    text = decode(data)
    if is_digifor(text):
        parsed = parse_digifor(text)
        usable = [t for t, (_, rows) in parsed.items() if rows]
        wanted = [t for t in (tables or DEFAULT_TABLES) if t in parsed and parsed[t][1]]
        if not wanted:
            wanted = [t for t in usable if t in ROLE_BY_TABLE] or usable[:1]
        records, headers = [], []
        for t in wanted:
            h, rows = parsed[t]
            for c in h:
                if c not in headers:
                    headers.append(c)
            for r in rows:
                d = {h[i]: (r[i].strip() if i < len(r) else "") for i in range(len(h))}
                d["__table"] = t
                records.append(d)
        return LoadedCsv("digifor", headers, records, usable, wanted)
    delim = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        return LoadedCsv("simple", [], [])
    headers = [h.strip() for h in rows[0]]
    records = [{headers[i]: (r[i].strip() if i < len(r) else "") for i in range(len(headers))} for r in rows[1:]]
    return LoadedCsv("simple", headers, records)


def guess_mapping(headers: list[str]) -> dict[str, str]:
    compact = {re.sub(r"\s", "", norm_text(h)): h for h in headers}
    mapping: dict[str, str] = {}
    for key, aliases in ALIASES.items():
        mapping[key] = next((compact[a] for a in aliases if a in compact), "")
    if mapping["nom"] and mapping["prenoms"]:
        mapping["full"] = ""  # colonnes séparées : elles priment
    return mapping


def build_rows(loaded: LoadedCsv, mapping: dict[str, str]) -> list[Row]:
    g = lambda rec, k: (rec.get(mapping.get(k) or "", "") or "").strip()
    rows: list[Row] = []
    for i, rec in enumerate(loaded.records):
        nom, pre, full = g(rec, "nom"), g(rec, "prenoms"), g(rec, "full")
        if not full:
            full = f"{nom} {pre}".strip()
        role = g(rec, "role") or ROLE_BY_TABLE.get(rec.get("__table", ""), "")
        row = Row(idx=i, code=g(rec, "code").upper(), role=role, nom=nom, prenoms=pre, full=full,
                  numero=g(rec, "numero"), type=g(rec, "type"), birth=g(rec, "birth"),
                  fichier=g(rec, "fichier"),
                  raw={k: v for k, v in rec.items() if k != "__table" and len(v) < 120})
        if row.full or row.numero:
            rows.append(row)
    return rows
