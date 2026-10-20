"""Extraction 100 % locale (Tesseract) avec analyse SPATIALE des libellés.

Repli quand aucune clé API n'est disponible. Moins fiable que la vision Claude :
les valeurs lues ont une confiance plafonnée et un doute produit « À vérifier »,
jamais une conformité ni un écart confirmé.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# 1 thread par appel Tesseract : le parallélisme est assuré par WORKERS (évite la sur-souscription CPU)
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

from PIL import Image, ImageFilter, ImageOps
from rapidfuzz import fuzz

from ..normalize import norm_text
from ..schemas import Extraction
from . import mrz as mrzmod


@dataclass
class Line:
    text: str
    x0: int
    y0: int
    x1: int
    y1: int
    conf: float

    @property
    def h(self) -> int:
        return max(1, self.y1 - self.y0)


# ---------------------------------------------------------------- OCR
def _preprocess(img: Image.Image, variant: int) -> Image.Image:
    """0 = gris simple ; 1 = contraste ; 2 = zone de texte + suppression du fond/filigrane (OpenCV)."""
    if variant == 2:
        try:
            import cv2
            import numpy as np

            g = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
            g = g[:, int(g.shape[1] * 0.30):]                   # colonne de texte (évite la photo)
            f = 1800 / max(g.shape)
            if f > 1.0:
                g = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
            c = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g)
            return Image.fromarray(cv2.divide(c, cv2.medianBlur(c, 31), scale=255))
        except ImportError:
            variant = 1
    g = ImageOps.grayscale(img)
    w, h = g.size
    scale = 1800 / max(w, h)
    if scale > 1.0 or scale < 0.6:
        g = g.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    if variant == 1:
        g = ImageOps.autocontrast(g, cutoff=2).filter(ImageFilter.SHARPEN)
    return g


def ocr_lines(img: Image.Image, lang: str, psm: int) -> list[Line]:
    import pytesseract

    data = pytesseract.image_to_data(img, lang=lang, config=f"--oem 1 --psm {psm}",
                                     output_type=pytesseract.Output.DICT)
    groups: dict[tuple, list[int]] = {}
    for i, txt in enumerate(data["text"]):
        if txt and txt.strip() and float(data["conf"][i]) >= 0:
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            groups.setdefault(key, []).append(i)
    lines: list[Line] = []
    for idxs in groups.values():
        idxs.sort(key=lambda i: data["left"][i])
        # sépare une « ligne » Tesseract en segments si grand trou horizontal
        seg: list[int] = [idxs[0]]
        segs = []
        for i in idxs[1:]:
            prev = seg[-1]
            gap = data["left"][i] - (data["left"][prev] + data["width"][prev])
            if gap > 2.5 * max(data["height"][prev], 12):
                segs.append(seg)
                seg = [i]
            else:
                seg.append(i)
        segs.append(seg)
        for s in segs:
            lines.append(Line(
                text=" ".join(data["text"][i] for i in s),
                x0=min(data["left"][i] for i in s), y0=min(data["top"][i] for i in s),
                x1=max(data["left"][i] + data["width"][i] for i in s),
                y1=max(data["top"][i] + data["height"][i] for i in s),
                conf=sum(float(data["conf"][i]) for i in s) / len(s) / 100,
            ))
    lines.sort(key=lambda l: (l.y0, l.x0))
    return lines


# ---------------------------------------------------------------- analyse
def _is_label(word: str, targets: tuple[str, ...], thr: int = 82) -> bool:
    w = norm_text(word).replace(" ", "")
    return any(fuzz.ratio(w, t) >= thr for t in targets)


LABELS = {
    "prenoms": ("PRENOMS", "PRENOM", "GIVENNAMES", "GIVENNAME", "FIRSTNAMES"),
    "nom": ("NOM", "SURNAME", "LASTNAME", "NOMDEFAMILLE"),
}
STOP_WORDS = {"NOM", "PRENOM", "PRENOMS", "S", "DATE", "NAISSANCE", "SEXE", "TAILLE", "NATIONALITE",
              "LIEU", "SIGNATURE", "TITULAIRE", "REPUBLIQUE", "COTE", "IVOIRE", "CARTE", "NATIONALE",
              "IDENTITE", "EXPIRATION", "SURNAME", "GIVEN", "NAMES", "NAME", "PERE", "MERE", "DOMICILE",
              "PROFESSION", "DELIVRE", "SOUS", "PREFET", "VALIDE", "JUSQU", "AU", "N", "NE", "LE"}
FILIATION = re.compile(r"^(?:NOM\s+DU\s+PERE|NOM\s+DE\s+LA\s+MERE|PERE|MERE|FATHER|MOTHER|FILIATION)\b")
# « Numéro de série » (verso) n'est PAS le numéro de pièce ; « Immatriculation » (anciennes CNI) l'est.
NUM_AFTER_LABEL = re.compile(
    r"(?:\bN\s*[°º0o]\s*|\bNUM[EÉ]RO(?!\s*DE\s*S[EÉ]RIE)\s*(?:DE\s+(?!S[EÉ]RIE)\w+\s*)?[:.]?\s*|\bNO\s*[:.]\s*"
    r"|\bIMMATRICULATION\s*[:.]?\s*|PASSPORT\s*N[o°]?\s*[:.]?\s*)"
    r"([A-Z]{0,3}\s?[0-9][A-Z0-9 \-]{5,19})", re.I)
# Numéros imprimés des CNI ivoiriennes : « CI » + 9 chiffres, ou « C » + 10 chiffres. Les autres suites de
# chiffres (NNI, numéro de série, téléphone…) NE sont PAS prises comme numéro de pièce par le moteur local.
ID_TOKEN = re.compile(r"\bC[IL1]?[0-9OQ]{9,10}\b")
LOCAL_MAX_CONF = 0.55   # < MIN_CONFIDENCE (0.6) : une lecture locale ne confirme jamais un écart


def _name_value(text: str) -> str | None:
    t = re.sub(r"[^A-Za-zÀ-ÿ'’\- ]", " ", text)
    toks = [x for x in t.split() if len(x.strip("-'’")) >= 3]          # mots de 1-2 lettres = bruit
    toks = [x for x in toks if norm_text(x) not in STOP_WORDS
            and not any(fuzz.ratio(norm_text(x), w) >= 80 for w in ("PRENOMS", "PRENOM", "SURNAME", "NAISSANCE"))]
    if not toks:
        return None
    return " ".join(toks).upper()


def _value_for(label: Line, lines: list[Line], remainder: str, width: int) -> tuple[str | None, float]:
    v = _name_value(remainder) if remainder else None
    if v:
        return v, label.conf
    tol = 0.06 * width
    # valeur sur la ligne en dessous, alignée à gauche du libellé (lignes bruitées ignorées)
    cy = (label.y0 + label.y1) / 2
    dy = lambda l: (l.y0 + l.y1) / 2 - cy          # écart vertical entre centres (boîtes OCR parfois chevauchantes)
    below = [l for l in lines if 0.45 * label.h < dy(l) < 3.0 * label.h + 10 and abs(l.x0 - label.x0) < tol
             and l is not label and l.conf >= 0.5]
    below.sort(key=dy)
    for l in below[:2]:
        if any(_is_label(w, LABELS["nom"] + LABELS["prenoms"]) for w in l.text.split()[:1]):
            continue
        v = _name_value(l.text)
        if v:
            return v, l.conf
    # modèles anciens : valeur AU-DESSUS du libellé
    above = [l for l in lines if 0.45 * label.h < -dy(l) < 3.0 * label.h + 10 and abs(l.x0 - label.x0) < tol
             and l.conf >= 0.5]
    above.sort(key=lambda l: -dy(l))
    for l in above[:1]:
        v = _name_value(l.text)
        if v:
            return v, l.conf
    return None, 0.0


def parse_lines(lines: list[Line], width: int) -> dict:
    out: dict = {"nom": None, "prenoms": None, "numero": None, "conf": {}, "mrz": None}
    in_filiation = False
    for ln in lines:
        up = norm_text(ln.text)
        if FILIATION.match(up):
            in_filiation = True
            continue
        if re.match(r"^(?:TITULAIRE|DATE|SEXE|NOM\b|PRENOM)", up):
            in_filiation = False
        if in_filiation:
            continue
        words = ln.text.split()
        if not words:
            continue
        first = re.sub(r"[^\wÀ-ÿ()]", "", words[0])
        for fld in ("prenoms", "nom"):
            if out[fld] is None and _is_label(re.sub(r"\(.*?\)", "", first), LABELS[fld], 80 if fld == "prenoms" else 100):
                # « Nom et prénoms : … » -> champ combiné, traité comme nom complet
                rem = re.sub(r"^\S+\s*(?:\(s\)|\(S\))?\s*[:=\-]?\s*", "", ln.text, count=1)
                rem = re.sub(r"^(?:ET\s+PRENOMS?|DE\s+FAMILLE|/\s*\w+)\s*[:=]?\s*", "", rem, flags=re.I)
                val, vconf = _value_for(ln, lines, rem, width)
                if val:
                    out[fld] = val
                    out["conf"][fld] = min(LOCAL_MAX_CONF, vconf)
                break
    if out["nom"] and out["nom"] == out["prenoms"]:      # même ligne lue pour les 2 libellés : nom non fiable
        out["nom"] = None
        out["conf"].pop("nom", None)
    # numéro : d'abord après un libellé « n° », sinon motif d'identifiant
    for ln in lines:
        m = NUM_AFTER_LABEL.search(ln.text)
        if m and re.search(r"\d", m.group(1)):
            cand = re.sub(r"[\s\-]", "", m.group(1)).upper()
            if 6 <= len(cand) <= 20 and not re.fullmatch(r"\d{8}", cand):
                out["numero"], out["conf"]["numero_piece"] = cand, min(LOCAL_MAX_CONF, ln.conf)
                break
    if out["numero"] is None:
        best = None
        for ln in lines:
            for m in ID_TOKEN.finditer(ln.text.upper().replace(" ", "")):
                tok = m.group(0)
                head = "CI" if tok.startswith("CI") or tok.startswith(("CL", "C1")) else "C"
                tail = re.sub(r"[OQ]", "0", tok[len(head):] if head == "C" else tok[2:])
                tok = head + tail
                if best is None or ln.conf > best[1]:
                    best = (tok, ln.conf)
        if best:
            out["numero"], out["conf"]["numero_piece"] = best[0], min(LOCAL_MAX_CONF, best[1])
    # MRZ
    m = mrzmod.parse_mrz([l.text for l in lines])
    if m:
        out["mrz"] = m
    return out


def _doc_type(text: str) -> str:
    t = norm_text(text)
    if re.search(r"\b(PASSEPORT|PASSPORT)\b", t):
        return "PASSEPORT"
    if "PERMIS DE CONDUIRE" in t or "DRIVING LICEN" in t:
        return "PERMIS"
    if "COUVERTURE MALADIE" in t or "CMU" in t.split():
        return "CARTE_CMU"
    if re.search(r"EXTRAIT.*(NAISSANCE|ACTE)|ACTE DE NAISSANCE", t):
        return "EXTRAIT_NAISSANCE"
    if "CARTE CONSULAIRE" in t:
        return "CARTE_CONSULAIRE"
    if re.search(r"CARTE NATIONALE|IDENTITE|\bCNI\b", t):
        return "CNI"
    return "AUTRE"


class TesseractExtractor:
    name = "tesseract"

    def __init__(self, lang: str = "fra+eng"):
        import pytesseract

        available = set(pytesseract.get_languages(config=""))
        wanted = [l for l in lang.split("+") if l in available]
        self.lang = "+".join(wanted) or "eng"
        self.has_fra = "fra" in wanted

    @property
    def cache_id(self) -> str:
        return f"tesseract-{self.lang}-v7"

    def extract(self, img: Image.Image) -> Extraction:
        # Plusieurs tentatives (prétraitements/modes OCR) par ordre de coût ; les champs sont FUSIONNÉS
        # (premier lu gagne) et on s'arrête dès que les 3 champs sont obtenus.
        p: dict = {"nom": None, "prenoms": None, "numero": None, "conf": {}, "mrz": None}
        texts: list[str] = []
        try:
            prepared: dict[int, Image.Image] = {}
            for variant, psm in ((0, 11), (0, 6), (2, 11), (1, 11)):
                if variant not in prepared:
                    prepared[variant] = _preprocess(img, variant)
                lines = ocr_lines(prepared[variant], self.lang, psm)
                texts.append(" ".join(l.text for l in lines))
                got = parse_lines(lines, prepared[variant].size[0])
                for k, ck in (("nom", "nom"), ("prenoms", "prenoms"), ("numero", "numero_piece")):
                    if not p[k] and got[k]:
                        p[k], p["conf"][ck] = got[k], got["conf"].get(ck, 0.0)
                if got["mrz"] and (not p["mrz"] or (got["mrz"]["valid"] and not p["mrz"]["valid"])):
                    p["mrz"] = got["mrz"]
                if p["nom"] and p["prenoms"] and p["numero"]:
                    break
        except Exception as e:  # tesseract absent ou image corrompue
            return Extraction(lisible=False, type_piece="ILLISIBLE", backend=self.cache_id,
                              erreur=f"{type(e).__name__}: {e}")
        text = " ".join(texts)
        m = p["mrz"]
        nom, prenoms, numero = p["nom"], p["prenoms"], p["numero"]
        conf = dict(p["conf"])
        remarques = ["OCR local (Tesseract)"] + ([] if self.has_fra else ["langue fra absente : accents perdus"])
        face = "RECTO" if (nom or prenoms or numero) else "VERSO"
        if m and m["valid"]:
            # MRZ valide : source la plus fiable pour le nom/prénoms ; le numéro MRZ n'est utilisé
            # que pour les passeports (TD3) où il coïncide avec le numéro imprimé.
            nom, prenoms = m["nom"] or nom, m["prenoms"] or prenoms
            conf["nom"] = conf["prenoms"] = 0.95
            if m["format"] == "TD3":
                numero, conf["numero_piece"] = m["numero"], 0.95
            remarques.append(f"MRZ {m['format']} valide")
            face = "RECTO" if face == "VERSO" and m["format"] == "TD3" else face
        elif m:
            remarques.append("MRZ détectée mais chiffres de contrôle invalides (non utilisée)")
        return Extraction(
            lisible=bool(text.strip()), type_piece=_doc_type(text), face=face,
            nom=nom, prenoms=prenoms, numero_piece=numero,
            confiance={"nom": conf.get("nom"), "prenoms": conf.get("prenoms"),
                       "numero_piece": conf.get("numero_piece")},
            remarques="; ".join(remarques), backend=self.cache_id, mrz=m,
        )
