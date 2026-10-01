import os
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="sealtest-")


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("SEAL_DB_PATH", str(db))
    monkeypatch.setenv("SWEEP_INTERVAL_SECONDS", "0.05")
    yield db


@pytest.fixture
def sweep():
    """手动触发一次过期扫描（确定性等待到期）。"""
    from app.main import ExpirySweeper

    return ExpirySweeper.sweep_once

@pytest.fixture
def client():
    # 惰性导入，确保环境变量已生效
    from app.main import app

    with TestClient(app) as c:
        yield c


def register(client, name):
    r = client.post("/register", json={"username": name})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def auth(key):
    return {"X-API-Key": key}


def digest_of(body: str) -> str:
    import hashlib

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def seal(client, owner_key, body, witnesses):
    r = client.post(
        "/documents",
        json={"body": body, "witnesses": witnesses},
        headers=auth(owner_key),
    )
    assert r.status_code == 201, r.text
    return r.json()
