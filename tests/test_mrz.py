from app.extractors.mrz import check_digit, parse_mrz

# Spécimen officiel OACI 9303 (passeport TD3)
TD3 = ["P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<<",
       "L898902C36UTO7408122F1204159ZE184226B<<<<<10"]
# Spécimen OACI TD1
TD1 = ["I<UTOD231458907<<<<<<<<<<<<<<<",
       "7408122F1204159UTO<<<<<<<<<<<6",
       "ERIKSSON<<ANNA<MARIA<<<<<<<<<<"]


def test_check_digit():
    assert check_digit("L898902C3") == 6
    assert check_digit("740812") == 2


def test_td3_valide():
    r = parse_mrz(TD3)
    assert r and r["format"] == "TD3" and r["valid"]
    assert r["numero"] == "L898902C3" and r["nom"] == "ERIKSSON" and r["prenoms"] == "ANNA MARIA"


def test_td1_valide():
    r = parse_mrz(TD1)
    assert r and r["format"] == "TD1" and r["valid"]
    assert r["numero"] == "D23145890" and r["nom"] == "ERIKSSON"


def test_mrz_corrompue_invalide():
    bad = [TD3[0], TD3[1].replace("L898902C3", "L898902C8")]
    r = parse_mrz(bad)
    assert r and not r["valid"] and not r["checks"]["numero"]


def test_pas_de_mrz():
    assert parse_mrz(["REPUBLIQUE DE COTE D'IVOIRE", "Nom GBRA"]) is None
