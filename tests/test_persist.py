import os
import shutil

import boto3
from moto import mock_aws

from app import persist


def _env(monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "presfor-test")
    monkeypatch.setenv("S3_ACCESS_KEY", "k")
    monkeypatch.setenv("S3_SECRET_KEY", "s")
    monkeypatch.setenv("S3_ENDPOINT", "")
    monkeypatch.setenv("S3_REGION", "us-east-1")
    persist._state.update(client=None, root=None, restored=set(), synced={}, thread=None)


@mock_aws
def test_sauvegarde_et_restauration(tmp_path, monkeypatch):
    _env(monkeypatch)
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="presfor-test")
    root = tmp_path / "data"
    (root / "campaigns/c1/scans").mkdir(parents=True)
    (root / "campaigns/c1/campaign.json").write_text('{"id":"c1"}')
    (root / "campaigns/c1/scans/a.jpg").write_bytes(b"IMG")
    (root / "campaigns/c1/_tmp").mkdir()
    (root / "campaigns/c1/_tmp/x.zip").write_bytes(b"Z")
    persist._state["root"] = root
    assert persist.push_all(root) == 2            # json + image ; _tmp ignoré
    assert persist.push_all(root) == 0            # rien de modifié
    shutil.rmtree(root)                           # « redémarrage » : disque vide
    persist._state.update(synced={}, restored=set(), thread=None)
    assert persist.start(root, every=3600)
    assert (root / "campaigns/c1/campaign.json").read_text() == '{"id":"c1"}'
    assert not (root / "campaigns/c1/scans/a.jpg").exists()   # image : à la demande
    persist.ensure(root / "campaigns/c1/scans")
    assert (root / "campaigns/c1/scans/a.jpg").read_bytes() == b"IMG"
    persist.delete_prefix(root / "campaigns/c1")
    assert list(persist._list(persist._prefix())) == []


def test_desactive_sans_configuration(monkeypatch, tmp_path):
    for k in ("S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert persist.start(tmp_path) is False
    persist.ensure(tmp_path / "x")            # sans effet
