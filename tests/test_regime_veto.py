"""Tests for the regime veto on /approve. Alpaca is fully mocked; no network, no orders."""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from alpaca.trading.enums import OrderSide

REPO_ROOT = Path(__file__).resolve().parents[1]

RISK_OFF = {
    "regime": "volatile", "risk_on": "no", "basis": "QQQ", "last": 608.45,
    "sma20": 678.53, "five_day_pct": -10.51, "ten_day_span_pct": 13.47, "veto": True,
}
RISK_ON = {
    "regime": "trend_up", "risk_on": "yes", "basis": "QQQ", "last": 700.0,
    "sma20": 680.0, "five_day_pct": 2.1, "ten_day_span_pct": 3.2, "veto": False,
}
UNAVAILABLE = {"regime": "unknown", "risk_on": "unknown", "note": "data error", "veto": False}


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
    monkeypatch.setattr(mod, "industry_note", lambda t: {})
    yield mod
    sys.modules.pop("main", None)


def set_regime(main, monkeypatch, reading):
    monkeypatch.setattr(main, "shadow_regime", lambda: dict(reading))


def make_client(position_qty=None):
    client = MagicMock()
    client.get_clock.return_value = SimpleNamespace(is_open=True)
    client.get_account.return_value = SimpleNamespace(equity="100000", last_equity="100000", buying_power="200000")
    client.get_all_positions.return_value = []
    if position_qty is None:
        client.get_open_position.side_effect = Exception("position does not exist")
    else:
        client.get_open_position.return_value = SimpleNamespace(qty=str(position_qty))
    client.get_orders.return_value = []
    client.submit_order.return_value = SimpleNamespace(id="order-123")
    return client


def post_approve(main, client, body, trade_price=None):
    data_client = MagicMock()
    if trade_price is not None:
        data_client.get_stock_latest_trade.return_value = {body["ticker"]: SimpleNamespace(price=trade_price)}
    with patch.object(main, "get_trading_client", return_value=client), \
         patch("alpaca.data.historical.StockHistoricalDataClient", return_value=data_client):
        return TestClient(main.app).post("/approve", json=body)


# ---------- classify_regime sets veto from the existing RISK-OFF reading ----------

def test_classify_regime_veto_true_when_risk_off(main):
    closes = [100.0] * 15 + [100, 96, 92, 90, 88, 85]  # sharp drop, >6% span -> volatile
    note = main.classify_regime(closes)
    assert note["risk_on"] == "no"
    assert note["veto"] is True


def test_classify_regime_veto_false_when_trend_up(main):
    closes = [100.0 + 0.2 * i for i in range(30)]  # steady climb, small span -> trend_up
    note = main.classify_regime(closes)
    assert note["regime"] == "trend_up" and note["risk_on"] == "yes"
    assert note["veto"] is False


def test_classify_regime_not_enough_bars_never_vetoes(main):
    assert not main.classify_regime([100.0] * 5).get("veto")


# ---------- /approve ----------

def test_veto_blocks_long_buy_with_409_and_reading(main, monkeypatch, capsys):
    set_regime(main, monkeypatch, RISK_OFF)
    client = make_client()
    resp = post_approve(main, client, {"action": "buy", "ticker": "NVDA", "stop": 95}, trade_price=100.0)
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "refused"
    assert body["reason"] == "regime_veto"
    assert body["veto"] is True
    assert body["action"] == "buy" and body["ticker"] == "NVDA"
    assert body["regime"] == RISK_OFF
    assert "RISK-OFF" in body["message"] and "volatile" in body["message"]
    client.submit_order.assert_not_called()
    # The refusal is logged (stdout SIGNAL line and the persistent decision log).
    assert "reason=approve: regime_veto" in capsys.readouterr().out
    rows = main.get_recent_signals(5)
    assert rows[0]["result"] == "blocked" and "regime_veto" in rows[0]["reason"]


def test_no_veto_allows_long_buy(main, monkeypatch):
    set_regime(main, monkeypatch, RISK_ON)
    client = make_client()
    resp = post_approve(main, client, {"action": "buy", "ticker": "NVDA", "stop": 95}, trade_price=100.0)
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    client.submit_order.assert_called_once()


def test_regime_unavailable_fails_open_and_logs(main, monkeypatch, capsys):
    set_regime(main, monkeypatch, UNAVAILABLE)
    client = make_client()
    resp = post_approve(main, client, {"action": "buy", "ticker": "NVDA", "stop": 95}, trade_price=100.0)
    assert resp.status_code == 200 and resp.json()["status"] == "success"
    assert "REGIME_UNAVAILABLE" in capsys.readouterr().out


def test_veto_does_not_block_sell_exit(main, monkeypatch):
    set_regime(main, monkeypatch, RISK_OFF)
    client = make_client(position_qty=40)
    resp = post_approve(main, client, {"action": "sell", "ticker": "NVDA"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"
    req = client.submit_order.call_args.args[0]
    assert req.side == OrderSide.SELL and req.qty == 40


def test_veto_does_not_block_cover_exit(main, monkeypatch):
    set_regime(main, monkeypatch, RISK_OFF)
    client = make_client(position_qty=-10)
    resp = post_approve(main, client, {"action": "cover", "ticker": "NVDA"})
    assert resp.status_code == 200 and resp.json()["status"] == "success"
    req = client.submit_order.call_args.args[0]
    assert req.side == OrderSide.BUY and req.qty == 10


def test_veto_does_not_cancel_or_touch_existing_stop_orders_on_refused_buy(main, monkeypatch):
    set_regime(main, monkeypatch, RISK_OFF)
    client = make_client()
    post_approve(main, client, {"action": "buy", "ticker": "NVDA", "stop": 95}, trade_price=100.0)
    client.cancel_order_by_id.assert_not_called()
    client.submit_order.assert_not_called()


def test_shorts_still_off_regardless_of_regime(main, monkeypatch):
    set_regime(main, monkeypatch, RISK_ON)
    client = make_client()
    resp = post_approve(main, client, {"action": "short", "ticker": "NVDA", "stop": 105}, trade_price=100.0)
    assert resp.json()["reason"] == "shorts_disabled"
    client.submit_order.assert_not_called()


def test_status_shows_veto_true_when_risk_off(main, monkeypatch):
    set_regime(main, monkeypatch, RISK_OFF)
    client = make_client()
    with patch.object(main, "get_trading_client", return_value=client):
        body = TestClient(main.app).get("/status").json()
    assert body["regime"]["veto"] is True
