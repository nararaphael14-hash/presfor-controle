"""Pipeline + API de bout en bout avec un extracteur factice (pas de réseau, pas d'OCR)."""
import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import server
from app.config import Settings
from app.csv_loader import build_rows, guess_mapping, load_csv
from app.pipeline import collect_scans, extract_all, run_control
from app.schemas import Extraction

DIGIFOR = """###############################################
# EXPORT TEST
###############################################

### TABLE: Declarant (VIDE)

###############################################
### TABLE: owners (3 lignes)
###############################################
id;codeContract;name;partnerName;birthDate;idType;idNumber
1;CT-1-PRE;Gbra Liby Nestor;;30/01/1975;CNI;CI007461947
2;CT-1-PRE;Bolou Dafla Emile;;03/07/1946;CNI;CI005038480
3;CT-2-PRE;Yepe Dogbo Jean Jacques;;01/01/1970;CNI;CI0099999
###############################################
### TABLE: beneficiaries (1 lignes)
###############################################
id;codeContract;name;partnerName;birthDate;idType;idNumber
1;CT-1-PRE;Kouadio Yao Noël;;25/05/2000;permis de conduire;
"""

SCANS = {  # nom de fichier -> extraction factice
    "CT-1-PRE_Gbra_Liby_Nestor_1_0.jpg": dict(nom="GBRA", prenoms="LIBY NESTOR", numero_piece="CI007461947", face="RECTO"),
    "CT-1-PRE_Gbra_Liby_Nestor_1_1.jpg": dict(face="VERSO"),
    "CT-1-PRE_Bolou_Dafla_Emile_2_0.jpg": dict(nom="BOLOU", prenoms="DAFLA EMILE", numero_piece="CI005038489", face="RECTO"),
    "CT-1-PRE_Kouadio_Yao_Noel_3_0.jpg": dict(nom="KOUADIO", prenoms="YAO NOEL", numero_piece="KOUA09-23-1", face="RECTO",
                                              type_piece="PERMIS"),
    "CT-9-PRE_Zzz_Autre_Personne_4_0.jpg": dict(nom="ZZZ", prenoms="AUTRE PERSONNE", numero_piece="X1234567", face="RECTO"),
}


class FakeExtractor:
    cache_id = "fake-v1"

    def extract(self, img):
        return Extraction(lisible=True, type_piece=self._cur.get("type_piece", "CNI"),
                          confiance={"nom": .95, "prenoms": .95, "numero_piece": .95},
                          **{k: v for k, v in self._cur.items() if k != "type_piece"})


def _img_bytes():
    b = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(b, "JPEG")
    return b.getvalue()


class PerFileExtractor(FakeExtractor):
    """Associe l'extraction au nom de fichier via la taille unique de l'image (astuce de test)."""

    def __init__(self, order):
        self.order = order
        self.n = 0

    def extract(self, img):
        w = img.size[0]
        self._cur = SCANS[self.order[w - 100]]
        return super().extract(img)


@pytest.fixture()
def workdir(tmp_path):
    names = list(SCANS)
    for i, n in enumerate(names):
        Image.new("RGB", (100 + i, 80), "white").save(tmp_path / n, "JPEG")  # largeur = 100 + indice
    return tmp_path, names


def test_csv_digifor():
    loaded = load_csv(DIGIFOR.encode())
    assert loaded.kind == "digifor" and loaded.selected_tables == ["owners", "beneficiaries"]
    m = guess_mapping(loaded.headers)
    assert m["full"] == "name" and m["numero"] == "idNumber" and m["code"] == "codeContract"
    rows = build_rows(loaded, m)
    assert len(rows) == 4 and rows[0].role == "PROPRIETAIRE" and rows[3].role == "BENEFICIAIRE"


def test_csv_simple_colonnes_separees():
    csvb = "Nom;Prénoms;N° pièce\nGBRA;Liby Nestor;CI007461947\n".encode("cp1252")
    loaded = load_csv(csvb)
    m = guess_mapping(loaded.headers)
    assert m["nom"] == "Nom" and m["prenoms"] == "Prénoms" and m["numero"] == "N° pièce"
    r = build_rows(loaded, m)[0]
    assert r.full == "GBRA Liby Nestor" and r.numero == "CI007461947"


def test_pipeline_verdicts(workdir):
    path, names = workdir
    settings = Settings(workers=2, cache_dir=path / "cache")
    scans = collect_scans(sorted(path.glob("*.jpg")))
    ext = PerFileExtractor(names)  # l'extraction est retrouvée via la largeur d'image (100 + indice)
    extract_all(scans, ext, settings)
    rows = build_rows(load_csv(DIGIFOR.encode()), guess_mapping(load_csv(DIGIFOR.encode()).headers))
    results, orphans = run_control(rows, scans, settings)
    by = {r.csv["nom_complet"]: r for r in results}
    assert by["Gbra Liby Nestor"].verdict == "CONFORME"
    assert by["Gbra Liby Nestor"].extracted["source_identite"].endswith("_0.jpg")  # le recto, pas le verso
    r = by["Bolou Dafla Emile"]
    assert r.verdict == "A_VERIFIER" and r.checks["numero_piece"]["status"] == "PROCHE"   # 1 caractère d'écart
    r = by["Kouadio Yao Noël"]
    assert r.checks["numero_piece"]["status"] == "MANQUANT_CSV" and r.verdict == "NON_CONFORME"
    assert by["Yepe Dogbo Jean Jacques"].verdict == "PIECE_MANQUANTE"
    assert [o["fichier"] for o in orphans] == ["CT-9-PRE_Zzz_Autre_Personne_4_0.jpg"]


def test_cache_extraction(workdir):
    path, names = workdir
    settings = Settings(cache_dir=path / "cache")
    scans = collect_scans(sorted(path.glob("*.jpg")))
    calls = []

    class Counting(PerFileExtractor):
        def extract(self, img):
            calls.append(1)
            return super().extract(img)

    e = Counting(names)
    extract_all(scans, e, settings)
    n1 = len(calls)
    extract_all(scans, e, settings)
    assert n1 == len(scans) and len(calls) == n1  # 2e passage : tout vient du cache


def test_api_bout_en_bout(workdir, monkeypatch):
    path, names = workdir
    monkeypatch.setattr(server, "get_extractor", lambda s, b=None: PerFileExtractor(names))
    monkeypatch.setattr(server, "get_settings", lambda: Settings(workers=2, cache_dir=path / "c", jobs_dir=path / "j"))
    c = TestClient(server.app)
    assert c.get("/api/health").json()["ok"]
    pv = c.post("/api/csv/preview", files={"csv_file": ("x.csv", DIGIFOR.encode())}).json()
    assert pv["kind"] == "digifor" and pv["n_rows"] == 4 and pv["mapping"]["numero"] == "idNumber"
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        for n in names:
            z.write(path / n, n)
        z.writestr("../evil.jpg", _img_bytes())   # tentative de zip-slip
    r = c.post("/api/jobs", files=[("csv_file", ("x.csv", DIGIFOR.encode())), ("scans", ("scans.zip", zbuf.getvalue()))],
               data={"backend": "tesseract"})
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for _ in range(100):
        s = c.get(f"/api/jobs/{jid}").json()
        if s["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert s["status"] == "done", s
    assert s["summary"]["CONFORME"] == 1 and s["summary"]["PIECE_MANQUANTE"] == 1
    assert not (path / "j" / "evil.jpg").exists() and not (path.parent / "evil.jpg").exists()
    x = c.get(f"/api/jobs/{jid}/export.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert c.get(f"/api/jobs/{jid}/scan/0").headers["content-type"] == "image/jpeg"
    assert c.get("/api/jobs/inconnu").status_code == 404
    assert "PRESFOR" in c.get("/").text


def test_auth_basic(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "secret")
    c = TestClient(server.app)
    assert c.get("/api/health").status_code == 401
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/health", auth=("admin", "secret")).status_code == 200
    assert c.get("/api/health", auth=("admin", "mauvais")).status_code == 401
