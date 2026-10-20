"""Test du chemin Claude SANS appel réseau : client factice qui enregistre la requête."""
from types import SimpleNamespace

from PIL import Image

from app.extractors.claude_vision import TOOL, TOOL_NAME, ClaudeVisionExtractor


class FakeMessages:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        p = self.payloads.pop(0)
        if isinstance(p, Exception):
            raise p
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=TOOL_NAME, input=p)])


def _client(*payloads):
    m = FakeMessages(payloads)
    return SimpleNamespace(messages=m), m


GOOD = {"lisible": True, "type_piece": "CNI", "face": "RECTO", "nom": " GBRA ", "prenoms": "LIBY  NESTOR",
        "numero_piece": "CI007461947", "date_naissance": "30/01/1975",
        "confiance": {"nom": 0.98, "prenoms": 0.97, "numero_piece": 1.4}, "remarques": ""}


def test_requete_json_forcee_et_parsing():
    client, m = _client(GOOD)
    ex = ClaudeVisionExtractor("k", "claude-sonnet-5-5", client=client).extract(Image.new("RGB", (2400, 1400), "white"))
    call = m.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": TOOL_NAME}      # sortie forcée
    assert call["tools"] == [TOOL] and call["temperature"] == 0
    img = call["messages"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/jpeg"
    assert (ex.nom, ex.prenoms, ex.numero_piece) == ("GBRA", "LIBY NESTOR", "CI007461947")
    assert ex.confiance.numero_piece == 1.0  # bornée à [0,1]
    assert ex.n_fields == 3 and not ex.erreur


def test_verso_champs_null():
    bad = dict(GOOD, face="VERSO", nom=None, prenoms="N/A", numero_piece="  ")
    client, _ = _client(bad)
    ex = ClaudeVisionExtractor("k", "m", client=client).extract(Image.new("RGB", (800, 500)))
    assert ex.face == "VERSO" and ex.n_fields == 0


def test_reponse_invalide_retentee_puis_erreur():
    client, m = _client({"lisible": "pas un booleen ???", "confiance": 3}, {"lisible": [1]})
    ex = ClaudeVisionExtractor("k", "m", client=client).extract(Image.new("RGB", (800, 500)))
    assert len(m.calls) == 2 and ex.erreur and not ex.lisible


def test_erreur_reseau_non_masquee():
    client, _ = _client(RuntimeError("503"))
    ex = ClaudeVisionExtractor("k", "m", client=client).extract(Image.new("RGB", (800, 500)))
    assert ex.erreur and "503" in ex.erreur
