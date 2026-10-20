from app.matching import (A_VERIFIER, CONFORME, CONFORME_TOLERE, ECART, INCERTAIN, MANQUANT_CSV, NON_CONFORME,
                          NON_LU, OK, PARTIEL, PROCHE, TOLERE, compare_name, compare_number, name_similarity,
                          verdict)
from app.normalize import collapse_confusables, norm_date, norm_number, norm_text, name_tokens, parse_scan_filename

KW = dict(tolerate=90, close=80, min_conf=0.6)


def test_normalisation():
    assert norm_text("Noël  N'Da-Kouamé") == "NOEL NDA KOUAME"
    assert name_tokens("Mme Aya  Koné") == ["AYA", "KONE"]
    assert norm_number("N° CI 007-461.947") == "CI007461947"
    assert norm_date("30/01/1975") == "1975-01-30" and norm_date("1975-1-30") == "1975-01-30"
    assert norm_date("30 JAN 1975") == "1975-01-30" and norm_date("31/02/abc") == ""
    assert collapse_confusables("CIOO7461947") == collapse_confusables("CI007461947")


def test_nom_exact_accents_ordre():
    s = name_similarity("Kouadio Yao Noël", "NOEL KOUADIO YAO")
    assert s.exact and s.score == 100
    c = compare_name("Kouadio Yao Noël", "KOUADIO YAO NOEL", 0.9, **KW)
    assert c.status == OK


def test_nom_faute_de_frappe_toleree():
    c = compare_name("Gnonka Guehi Boniface Matthurin", "GNONKA GUEHI BONIFACE MATHURIN", 0.9, **KW)
    assert c.status == TOLERE and c.score >= 90


def test_nom_partiel_prenom_manquant():
    c = compare_name("Gnonka Guehi Mathurin", "GNONKA GUEHI BONIFACE MATHURIN", 0.9, **KW)
    assert c.status == PARTIEL and "BONIFACE" in c.note


def test_nom_different():
    c = compare_name("Bolou Dafla Emile", "GBRA LIBY NESTOR", 0.9, **KW)
    assert c.status == ECART
    c2 = compare_name("Bolou Dafla Emile", "GBRA LIBY NESTOR", 0.3, **KW)
    assert c2.status == INCERTAIN  # lecture douteuse : jamais d'écart confirmé


def test_inversion_nom_prenoms():
    c = compare_name("Gbra Liby Nestor", "GBRA LIBY NESTOR", 0.9, csv_sep=("Liby Nestor", "Gbra"),
                     doc_sep=("GBRA", "LIBY NESTOR"), **KW)
    assert c.status == PROCHE and "inversés" in c.note


def test_nom_manquants():
    assert compare_name("Abc Def", "", 0.9, **KW).status == NON_LU
    assert compare_name("", "ABC DEF", 0.9, **KW).status == MANQUANT_CSV


def test_numero_exact_normalise():
    assert compare_number("CI 007461947", "ci007461947", 0.9, min_conf=0.6).status == OK


def test_numero_confusion_ocr_signalee():
    c = compare_number("CI007461947", "CIOO7461947", 0.9, min_conf=0.6)
    assert c.status == PROCHE and "confusion" in c.note


def test_numero_un_caractere_signale_jamais_ok():
    c = compare_number("CI007461947", "CI007461948", 0.9, min_conf=0.6)
    assert c.status == PROCHE
    c = compare_number("C0091489890", "C0019489890", 0.9, min_conf=0.6)  # transposition
    assert c.status == PROCHE


def test_numero_different():
    c = compare_number("CI007461947", "CI005038480", 0.95, min_conf=0.6)
    assert c.status == ECART
    assert compare_number("CI007461947", "CI005038480", 0.2, min_conf=0.6).status == INCERTAIN
    assert compare_number("CI007461947", "", 0.0, min_conf=0.6).status == NON_LU
    assert compare_number("", "CI007461947", 0.9, min_conf=0.6).status == MANQUANT_CSV


def test_verdicts():
    ok = compare_number("A1234567", "A1234567", 1, min_conf=.6)
    nm_ok = compare_name("A B", "B A", 1, **KW)
    assert verdict(nm_ok, ok)[0] == CONFORME
    nm_tol = compare_name("Gnonka Guehi Boniface Matthurin", "GNONKA GUEHI BONIFACE MATHURIN", 1, **KW)
    assert verdict(nm_tol, ok)[0] == CONFORME_TOLERE
    bad = compare_number("A1234567", "A7654321", 1, min_conf=.6)
    assert verdict(nm_ok, bad)[0] == NON_CONFORME
    close = compare_number("A1234567", "A1234568", 1, min_conf=.6)
    assert verdict(nm_ok, close)[0] == A_VERIFIER
    assert verdict(nm_ok, compare_number("A1", "", 0, min_conf=.6))[0] == A_VERIFIER
    assert verdict(nm_ok, compare_number("", "A1234567", 1, min_conf=.6))[0] == NON_CONFORME


def test_nom_fichier():
    assert parse_scan_filename("CT-375-009-002102-03-PRE_Gbra_Liby_Nestor_1773744001149_0.jpg") == \
        ("CT-375-009-002102-03-PRE", "Gbra Liby Nestor", 0)
    assert parse_scan_filename("_Gbla_Guehi_Alphonse_1771587570422_0.jpg") == ("", "Gbla Guehi Alphonse", 0)
    assert parse_scan_filename("CT-375-009-002120-03-PRE_Toili_Tade_Zepherin__1775902193590_1.jpg")[1] == \
        "Toili Tade Zepherin"
