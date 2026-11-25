"""Tests for the read-only deployed-version field on Agent 200's /status. Alpaca is mocked."""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SHA = "d0beb8f5c55b36df7d674d55965a23b8d54ad69b"


@pytest.fixture
def main(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    sys.modules.pop("main", None)
    with patch.object(Path, "mkdir"):
        mod = importlib.import_module("main")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "agent200.db")
    mod.init_db()
    monkeypatch.setattr(mod, "shadow_regime", lambda: {"regime": "volatile", "risk_on": "no", "veto": True})
    monkeypatch.setattr(mod, "industry_note", lambda t: {})
    yield mod
    sys.modules.pop("main", None)


def client_ok():
    c = MagicMock()
    c.get_account.return_value = SimpleNamespace(equity="3000", buying_power="11000", last_equity="3000")
    c.get_all_positions.return_value = []
    c.get_orders.return_value = []
    return c


def get_status(main, client):
    with patch.object(main, "get_trading_client", return_value=client):
        return TestClient(main.app).get("/status").json()


def test_status_shows_railway_commit(main, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA)
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "dep-123")
    body = get_status(main, client_ok())
    v = body["version"]
    assert v["commit"] == SHA and v["commit_short"] == "d0beb8f" and v["deployment_id"] == "dep-123"
    assert v["started_at"]
    # existing fields are untouched
    assert body["regime"]["veto"] is True and body["max_positions"] == 8 and body["shorts_enabled"] is False


def test_status_version_unknown_without_railway_vars(main, monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.delenv("RAILWAY_DEPLOYMENT_ID", raising=False)
    v = get_status(main, client_ok())["version"]
    assert v["commit"] == "unknown" and v["commit_short"] == "unknown" and v["deployment_id"] == "unknown"


def test_status_error_path_still_reports_version(main, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", SHA)
    c = MagicMock()
    c.get_account.side_effect = Exception("broker down")
    body = get_status(main, c)
    assert body["status"] == "error" and body["version"]["commit_short"] == "d0beb8f"
