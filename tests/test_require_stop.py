"""Tests for 'no buy without a stop' on /approve. Alpaca is fully mocked; no network, no orders."""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def main(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    for var in ("TEST_QTY", "RISK_PCT", "MAX_POSITION_PCT", "MIN_STOP_PCT", "MAX_TRADES_PER_DAY",
                "MAX_POSITIONS", "DAILY_LOSS_LIMIT_PCT", "SHORTS_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    sys.modules.pop("main", None)
    with patch.object(Path, "mkdir"):
        mod = importlib.import_module("main")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "agent200.db")
    mod.init_db()
    # Neutral (risk-on) regime so these tests isolate the stop rule.
    monkeypatch.setattr(mod, "shadow_regime", lambda: {"regime": "trend_up", "risk_on": "yes", "veto": False})
    monkeypatch.setattr(mod, "industry_note", lambda t: {})
    yield mod
    sys.modules.pop("main", None)


def make_client(equity="100000", position_qty=None, open_positions=0):
    client = MagicMock()
    client.get_clock.return_value = SimpleNamespace(is_open=True)
    client.get_account.return_value = SimpleNamespace(equity=equity, last_equity=equity)
    client.get_all_positions.return_value = [object()] * open_positions
    if position_qty is None:
        client.get_open_position.side_effect = Exception("position does not exist")
    else:
        client.get_open_position.return_value = SimpleNamespace(qty=str(position_qty))
    client.get_orders.return_value = []
    client.submit_order.return_value = SimpleNamespace(id="order-123")
    return client


def post_approve(main, client, body, trade_price=100.0):
    data_client = MagicMock()
    data_client.get_stock_latest_trade.return_value = {body["ticker"]: SimpleNamespace(price=trade_price)}
    with patch.object(main, "get_trading_client", return_value=client), \
         patch("alpaca.data.historical.StockHistoricalDataClient", return_value=data_client):
        return TestClient(main.app).post("/approve", json=body)


def test_risk_rules_unchanged(main):
    assert main.RISK_PCT == 0.01
    assert main.MAX_POSITION_PCT == 0.25
    assert main.MAX_POSITIONS == 8
    assert main.SHORTS_ENABLED is False
    assert main.DAILY_LOSS_LIMIT_PCT == 3.0
    assert main.MIN_STOP_PCT == 0.015


@pytest.mark.parametrize("payload", [
    {"action": "buy", "ticker": "AAPL"},
    {"action": "buy", "ticker": "AAPL", "stop": None},
    {"action": "buy", "ticker": "AAPL", "stop": ""},
    {"action": "buy", "ticker": "AAPL", "stop": "   "},
])
def test_buy_without_stop_rejected_422(main, payload, capsys):
    client = make_client()
    resp = post_approve(main, client, payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "stop_required"
    assert body["action"] == "buy" and body["ticker"] == "AAPL"
    assert "must include a stop" in body["message"]
    client.submit_order.assert_not_called()
    assert "approve: stop_required" in capsys.readouterr().out


@pytest.mark.parametrize("stop,fragment", [
    ("abc", "must be a number"),
    (True, "must be a number"),
    ([95], "must be a number"),
    (0, "greater than 0"),
    (-5, "greater than 0"),
    ("NaN", "finite"),
    ("inf", "finite"),
])
def test_buy_with_invalid_stop_rejected_422(main, stop, fragment):
    client = make_client()
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": stop})
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == "rejected" and body["reason"] == "bad_stop", body
    assert fragment in body["message"]
    client.submit_order.assert_not_called()


@pytest.mark.parametrize("stop", [100.0, 100.01, 150])
def test_buy_with_stop_at_or_above_price_rejected_422(main, stop):
    client = make_client()
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": stop}, trade_price=100.0)
    assert resp.status_code == 422
    body = resp.json()
    assert body["reason"] == "bad_stop"
    assert body["entry_price"] == 100.0
    assert "below the current price" in body["message"]
    client.submit_order.assert_not_called()


def test_stop_too_tight_still_rejected_by_existing_min_stop_rule(main):
    client = make_client()
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": 99.0}, trade_price=100.0)
    assert resp.status_code == 422
    assert resp.json()["reason"] == "bad_stop"
    client.submit_order.assert_not_called()


def test_valid_stop_sizes_at_1pct_risk(main):
    # $10,000 equity, entry 100, stop 90: 1% risk = $100 / $10 per share = 10 shares.
    # 25% cap = $2,500 / $100 = 25 shares, so risk sizing (10) wins.
    client = make_client(equity="10000")
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": 90}, trade_price=100.0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success" and body["qty"] == 10
    req = client.submit_order.call_args.args[0]
    assert req.qty == 10
    assert req.side == OrderSide.BUY
    assert req.order_class == OrderClass.OTO
    assert req.time_in_force == TimeInForce.GTC
    assert req.stop_loss.stop_price == 90.0


def test_valid_stop_respects_25pct_cap(main):
    # $100,000 equity, entry 100, stop 98: 1% risk = $1,000 / $2 = 500 shares,
    # but 25% cap = $25,000 / $100 = 250 shares.
    client = make_client(equity="100000")
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": "98"}, trade_price=100.0)
    assert resp.status_code == 200
    assert resp.json()["qty"] == 250
    assert client.submit_order.call_args.args[0].stop_loss.stop_price == 98.0


def test_max_8_positions_unchanged(main):
    client = make_client(open_positions=8)
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": 90})
    assert resp.json()["reason"] == "max_positions"
    client.submit_order.assert_not_called()


def test_seven_positions_still_allows_a_buy(main):
    client = make_client(open_positions=7)
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": 90})
    assert resp.json()["status"] == "success"


def test_shorts_still_off(main):
    client = make_client()
    resp = post_approve(main, client, {"action": "short", "ticker": "AAPL", "stop": 110})
    assert resp.json()["reason"] == "shorts_disabled"
    client.submit_order.assert_not_called()


def test_sell_needs_no_stop(main):
    client = make_client(position_qty=12)
    resp = post_approve(main, client, {"action": "sell", "ticker": "AAPL"})
    assert resp.status_code == 200 and resp.json()["status"] == "success"
    req = client.submit_order.call_args.args[0]
    assert req.side == OrderSide.SELL and req.qty == 12


def test_daily_loss_limit_unchanged(main):
    client = make_client()
    client.get_account.return_value = SimpleNamespace(equity="96000", last_equity="100000")
    resp = post_approve(main, client, {"action": "buy", "ticker": "AAPL", "stop": 90})
    assert resp.json()["reason"] == "daily_loss_limit"
    client.submit_order.assert_not_called()
