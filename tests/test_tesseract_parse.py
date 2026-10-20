"""Analyse spatiale du repli Tesseract, testée sur des lignes OCR synthétiques (sans OCR réel)."""
from app.extractors.tesseract_local import LOCAL_MAX_CONF, Line, parse_lines

W = 1800


def L(text, x, y, conf=0.9, h=36):
    return Line(text, x, y, x + 12 * len(text), y + h, conf)


def test_cni_libelle_au_dessus_et_bruit_ignore():
    lines = [L("REPUBLIQUE DE CÔTE D' IVOIRE", 438, 103), L("n° CI005769623", 686, 256),
             L("Prénom(s)", 677, 365), L("GUEHI ALPHONSE", 679, 416),
             L("Nom", 677, 483), L("oN", 362, 515, conf=0.33), L("GBLA", 679, 528),   # bruit plus proche que la valeur
             L("Date de Naissance", 678, 668), L("386251", 1327, 1023)]
    p = parse_lines(lines, W)
    assert (p["nom"], p["prenoms"], p["numero"]) == ("GBLA", "GUEHI ALPHONSE", "CI005769623")
    assert all(v <= LOCAL_MAX_CONF for v in p["conf"].values())   # jamais de « certitude » locale


def test_valeur_sur_la_meme_ligne():
    p = parse_lines([L("Nom : KOUAME", 100, 100), L("Prénoms : YAO NOEL", 100, 160), L("N° C0091489890", 100, 40)], W)
    assert (p["nom"], p["prenoms"], p["numero"]) == ("KOUAME", "YAO NOEL", "C0091489890")


def test_numero_nni_non_pris_pour_numero_de_piece():
    p = parse_lines([L("11918126249", 300, 900), L("058ARE08600043053", 300, 960)], W)
    assert p["numero"] is None


def test_bruit_de_deux_lettres_rejete_et_filiation_ignoree():
    lines = [L("Nom", 100, 100), L("WY IL", 100, 150), L("Père : LOUE GNALY", 100, 400), L("Né le : ND", 100, 450),
             L("Mère : MAMBO LEZA", 100, 500)]
    p = parse_lines(lines, W)
    assert p["nom"] is None and p["prenoms"] is None   # ni bruit, ni nom d'un parent


def test_numero_confusion_o_zero_corrige():
    p = parse_lines([L("CIOO5769623", 300, 900)], W)
    assert p["numero"] == "CI005769623"


def test_immatriculation_ancien_modele_et_numero_de_serie_ignore():
    p = parse_lines([L("Immatriculation : C 0090 4420 54", 100, 100), L("Numéro de série 058 0198 086 0004395", 100, 900)], W)
    assert p["numero"] == "C0090442054"
    p = parse_lines([L("Numéro de série 058 0198 086 0004395", 100, 900)], W)
    assert p["numero"] is None


def test_nom_identique_au_prenoms_rejete():
    p = parse_lines([L("Prénom(s)", 100, 100), L("GNOBLE SERAPHIN", 100, 150), L("Nom", 100, 300)], W)
    assert p["prenoms"] == "GNOBLE SERAPHIN" and p["nom"] is None
