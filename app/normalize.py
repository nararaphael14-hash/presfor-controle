"""Normalisation de texte, noms, numéros, dates et types de pièces."""
from __future__ import annotations

import re
import unicodedata

_APOS = re.compile(r"[’'`´ʼ]")
_HONORIFICS = {"M", "MR", "MME", "MLLE", "MONSIEUR", "MADAME", "MADEMOISELLE"}


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm_text(s: object) -> str:
    """MAJUSCULES, sans accents ni ponctuation. L'apostrophe est supprimée (N'DA -> NDA)."""
    if s is None:
        return ""
    s = _APOS.sub("", str(s))
    s = strip_accents(s).upper().replace("Œ", "OE").replace("Æ", "AE")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_tokens(s: object) -> list[str]:
    toks = norm_text(s).split()
    while toks and toks[0] in _HONORIFICS and len(toks) > 1:
        toks = toks[1:]
    return toks


_NUM_PREFIX = re.compile(r"^\s*(?:N\s*[°º]|NUM(?:ERO)?\s*[:.]|NUMERO\s)\s*[:.]?\s*", re.I)


def norm_number(s: object) -> str:
    """Numéro de pièce : majuscules, uniquement lettres/chiffres."""
    if s is None:
        return ""
    s = _NUM_PREFIX.sub("", str(s))
    return re.sub(r"[^A-Z0-9]", "", strip_accents(s).upper())


# Confusions classiques d'OCR / de saisie : O/0, I/1, L/1, Z/2, S/5, B/8, G/6
_CONFUSABLE = str.maketrans({"O": "0", "Q": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})


def collapse_confusables(s: str) -> str:
    return s.translate(_CONFUSABLE)


_MONTHS = {
    "JAN": 1, "FEV": 2, "FEB": 2, "MAR": 3, "AVR": 4, "APR": 4, "MAI": 5, "MAY": 5, "JUN": 6, "JUI": 6,
    "JUL": 7, "AOU": 8, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def norm_date(s: object) -> str:
    """Retourne AAAA-MM-JJ ou '' si non interprétable."""
    if not s:
        return ""
    t = strip_accents(str(s)).upper().strip()
    m = re.match(r"^(\d{4})[-/. ](\d{1,2})[-/. ](\d{1,2})", t)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.match(r"^(\d{1,2})[-/. ]+(\d{1,2})[-/. ]+(\d{4})", t)
        if m:
            d, mo, y = int(m[1]), int(m[2]), int(m[3])
        else:
            m = re.match(r"^(\d{1,2})[-/. ]*([A-Z]{3})[A-Z]*[-/. ]*(\d{4})", t)
            if not m or m[2] not in _MONTHS:
                return ""
            d, mo, y = int(m[1]), _MONTHS[m[2]], int(m[3])
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 1850 <= y <= 2100):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def canon_doc_type(s: object) -> str:
    t = norm_text(s)
    if not t:
        return ""
    if "PASSEPORT" in t or "PASSPORT" in t:
        return "PASSEPORT"
    if "PERMIS" in t:
        return "PERMIS"
    if "CMU" in t:
        return "CARTE_CMU"
    if "CONSULAIRE" in t:
        return "CARTE_CONSULAIRE"
    if "EXTRAIT" in t or "NAISSANCE" in t or t.startswith("ACTE"):
        return "EXTRAIT_NAISSANCE"
    if "ATTESTATION" in t:
        return "ATTESTATION"
    if t == "CNI" or "IDENTITE" in t or "CARTE NATIONALE" in t:
        return "CNI"
    return "AUTRE"


_CODE_RE = re.compile(r"(CT-\d+(?:-\d+)*-[A-Z]{2,4})", re.I)


def parse_scan_filename(filename: str) -> tuple[str, str, int | None]:
    """'CT-375-009-002102-03-PRE_Gbra_Liby_Nestor_1773744001149_0.jpg'
    -> (code_contrat, 'Gbra Liby Nestor', page)"""
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename.rsplit("/", 1)[-1])
    stem = re.sub(r"#p\d+$", "", stem)
    code = ""
    m = _CODE_RE.search(stem)
    if m:
        code = m.group(1).upper()
        stem = stem.replace(m.group(1), "", 1)
    parts = [p for p in re.split(r"[_]+", stem) if p]
    page = None
    if parts and re.fullmatch(r"\d{1,2}", parts[-1]):
        page = int(parts.pop())
    parts = [p for p in parts if not re.fullmatch(r"\d{9,}", p)]
    hint = " ".join(p.replace("-", " ") for p in parts).strip()
    return code, hint, page
