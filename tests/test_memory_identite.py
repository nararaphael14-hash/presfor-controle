from pathlib import Path

from app.config import get_settings
from app.identity_check import check_actor, new_verified, skippable_scans, verified_key
from app.pipeline import ScanItem


def _actor():
    return {"role": "Propriétaire", "name": "Tagro Kore Charles", "id": "CI007461947", "pieces": ["a.jpg"]}


def test_memoire_identite_valide():
    a = _actor()
    key = verified_key(a)
    assert key
    item = ScanItem(0, "a.jpg", Path("a.jpg"), 0, "", "", "sha", None)
    chk = check_actor("CT-1", a, ["a.jpg"], {}, [item], get_settings(), {key: {}})
    assert chk["status"] == "validé" and chk["state"] == "memory" and chk["pieces"] == ["a.jpg"]
    # sans mémoire : non lu -> pas validé
    chk2 = check_actor("CT-1", a, ["a.jpg"], {}, [item], get_settings(), {})
    assert chk2["status"] != "validé"
    camp = {"contracts": [{"number": "CT-1", "actors": [{**a, "automatic": chk}]}]}
    assert new_verified(camp) == {}          # un résultat issu de la mémoire n'est pas remémorisé
    assert skippable_scans({"contracts": [{"number": "CT-1", "actors": [a]}]}, ["a.jpg"], {key: {}}) == {"a.jpg"}


def test_pas_de_mémoire_sans_scan_rattaché():
    a = _actor()
    chk = check_actor("CT-1", a, [], {}, [], get_settings(), {verified_key(a): {}})
    assert chk["pieces"] == [] and chk["status"] != "validé"


def test_enregistrement_manuel_garde_analyse_recente():
    from app.campaign_api import _merge_auto
    serveur = [{"key": "k", "actors": [{"role": "Propriétaire", "name": "A", "automatic": {"version": 4, "status": "validé"}}]}]
    client = [{"key": "k", "manual": "saisie", "actors": [{"role": "Propriétaire", "name": "A modifié"}]}]
    out = _merge_auto(serveur, client)
    assert out[0]["manual"] == "saisie" and out[0]["actors"][0]["name"] == "A modifié"
    assert out[0]["actors"][0]["automatic"]["status"] == "validé"


def test_scan_importe_non_lu_est_rattache_en_attente():
    from app.identity_check import apply_identity
    a = {"role": "Propriétaire", "name": "Tagro Kore Charles", "id": "CI007461947", "pieces": ["a.jpg"]}
    camp = {"contracts": [{"number": "CT-1", "type": "PP_PLANTATION", "raw": {}, "actors": [a], "observations": []}]}
    out = apply_identity(camp, {}, Path("."), ["a.jpg"], get_settings(), only_stale=True)
    chk = out["contracts"][0]["actors"][0]["automatic"]
    assert chk["state"] == "pending" and chk["pieces"] == ["a.jpg"]
