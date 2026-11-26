"""HK Connection /status shows the deployed commit (read-only). No engine, no network."""
from fastapi.testclient import TestClient

from hk_connection.app import Runtime, create_app

SHA = "3c8c5d0aaaabbbbccccddddeeeeffff000011112"


def status():
    return TestClient(create_app(Runtime(), start_engine=False)).get("/status").json()


def test_hk_status_shows_railway_commit(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA)
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "dep-hk")
    v = status()["version"]
    assert v["commit"] == SHA and v["commit_short"] == "3c8c5d0" and v["deployment_id"] == "dep-hk" and v["started_at"]


def test_hk_status_version_unknown_without_railway_vars(monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_DEPLOYMENT_ID", raising=False)
    body = status()
    assert body["version"] == {**body["version"], "commit": "unknown", "commit_short": "unknown", "deployment_id": "unknown"}
    assert body["service"] == "hk_connection" and body["mode"] == "paper"
