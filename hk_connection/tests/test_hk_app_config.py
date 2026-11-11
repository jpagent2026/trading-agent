from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hk_connection.app import Runtime, create_app
from hk_connection.config import Settings
from hk_connection.ledger import utcnow


@pytest.fixture
def api(settings, ledger, broker, engine):
    rt = Runtime()
    rt.settings, rt.ledger, rt.broker, rt.engine = settings, ledger, broker, engine
    return TestClient(create_app(rt, start_engine=False))


def test_status_reports_paper_mode_capital_and_streak(api, ledger):
    r = api.get("/status").json()
    assert r["mode"] == "paper" and r["capital_base"] == {"value": 2000.0, "source": "account_equity"}
    assert r["account"]["equity"] == 2000.0 and r["account"]["buying_power"] == 8000.0
    assert r["losing_streak"]["consecutive_losses"] == 0 and r["current_risk_pct"] == 0.003
    assert r["open_trades"] == 0 and r["rules"]["trim"]["r_levels"] == [3.0, 5.0]


def test_positions_merges_broker_and_ledger(api, ledger, client):
    ledger.create_trade(symbol="ABC", signal_symbol="ABC", direction="long", bias="long", setup="pullback_long",
                        status="open", entry_price=100, qty_initial=4, qty_open=4, stop_price=99.9, stop_placed_at=utcnow())
    client.get_all_positions.return_value = [SimpleNamespace(symbol="ABC", qty="4", side="long", avg_entry_price="100",
                                                             current_price="101", market_value="404",
                                                             unrealized_pl="4", unrealized_plpc="0.01")]
    r = api.get("/positions").json()
    assert r["mode"] == "paper" and r["positions"][0]["qty"] == 4.0
    assert r["positions"][0]["ledger"]["stop_price"] == 99.9


def test_no_write_endpoints(api):
    for path in ("/status", "/positions", "/trades", "/watchlist", "/events"):
        assert api.post(path).status_code == 405
    assert api.get("/trades").status_code == 200 and api.get("/watchlist").status_code == 200


def test_capital_base_env_is_optional(monkeypatch):
    monkeypatch.setenv("HK_ALPACA_PAPER_KEY_ID", "hk")
    monkeypatch.setenv("HK_ALPACA_PAPER_SECRET_KEY", "s")
    monkeypatch.delenv("CAPITAL_BASE", raising=False)
    assert Settings.from_env().capital_base is None
    monkeypatch.setenv("CAPITAL_BASE", "")
    assert Settings.from_env().capital_base is None
    monkeypatch.setenv("CAPITAL_BASE", "1500")
    assert Settings.from_env().capital_base == 1500.0


def test_refuses_agent200_keys(monkeypatch):
    monkeypatch.setenv("HK_ALPACA_PAPER_KEY_ID", "same")
    monkeypatch.setenv("HK_ALPACA_PAPER_SECRET_KEY", "s")
    monkeypatch.setenv("ALPACA_API_KEY", "same")
    with pytest.raises(RuntimeError):
        Settings.from_env()
