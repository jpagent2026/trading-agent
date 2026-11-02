"""Tests for /approve stop-order handling. The Alpaca clients are fully mocked; no network."""
import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def main(tmp_path, monkeypatch):
    # Env only inside tests: dummy paper creds; make sure defaults for sizing/limits apply.
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    for var in ("TEST_QTY", "RISK_PCT", "MAX_POSITION_PCT", "MIN_STOP_PCT", "MAX_TRADES_PER_DAY",
                "MAX_POSITIONS", "DAILY_LOSS_LIMIT_PCT", "SHORTS_ENABLED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    sys.modules.pop("main", None)
    # main.py creates /data at import time; avoid touching the real filesystem.
    with patch.object(Path, "mkdir"):
        mod = importlib.import_module("main")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "agent200.db")
    mod.init_db()
    # Keep log_signal offline.
    monkeypatch.setattr(mod, "shadow_regime", lambda: {"regime": None, "risk_on": None})
    monkeypatch.setattr(mod, "industry_note", lambda t: {})
    yield mod
    sys.modules.pop("main", None)


def make_order(oid, side, status, order_type, symbol="PSX", legs=None):
    return SimpleNamespace(id=oid, symbol=symbol, side=side, status=status,
                           order_type=order_type, legs=legs)


def make_client(today_orders=None, position_qty=None):
    client = MagicMock()
    client.get_clock.return_value = SimpleNamespace(is_open=True)
    client.get_account.return_value = SimpleNamespace(equity="100000", last_equity="100000")
    client.get_all_positions.return_value = []
    if position_qty is None:
        client.get_open_position.side_effect = Exception("position does not exist")
    else:
        client.get_open_position.return_value = SimpleNamespace(qty=str(position_qty))
    client.get_orders.return_value = list(today_orders or [])
    client.submit_order.return_value = SimpleNamespace(id="order-123")
    return client


def post_approve(main, client, body, trade_price=None):
    data_client = MagicMock()
    if trade_price is not None:
        data_client.get_stock_latest_trade.return_value = {body["ticker"]: SimpleNamespace(price=trade_price)}
    with patch.object(main, "get_trading_client", return_value=client), \
         patch("alpaca.data.historical.StockHistoricalDataClient", return_value=data_client):
        return TestClient(main.app).post("/approve", json=body)


def test_buy_with_valid_stop_builds_oto_gtc_with_rounded_stop(main):
    client = make_client()
    resp = post_approve(main, client, {"action": "buy", "ticker": "PSX", "stop": 97.456}, trade_price=100.0)
    body = resp.json()
    assert body["status"] == "success", body
    client.submit_order.assert_called_once()
    req = client.submit_order.call_args.args[0]
    assert req.order_class == OrderClass.OTO
    assert req.time_in_force == TimeInForce.GTC
    assert req.side == OrderSide.BUY
    assert req.type == OrderType.MARKET
    assert req.stop_loss.stop_price == 97.46
    assert req.take_profit is None
    # Sizing unchanged: 1% of 100k / 2.544 risk = 393, capped at 25% of equity / $100 = 250.
    assert req.qty == 250
    assert body["qty"] == 250


def test_same_day_sell_not_blocked_by_stop_leg_cancels_it_and_sells_full_qty(main):
    parent = make_order("parent-1", OrderSide.BUY, "filled", OrderType.MARKET)
    stop_leg = make_order("stop-leg-1", OrderSide.SELL, "new", OrderType.STOP)
    client = make_client(today_orders=[parent, stop_leg], position_qty=7)
    resp = post_approve(main, client, {"action": "sell", "ticker": "PSX"})
    body = resp.json()
    assert body["status"] == "success", body
    assert body["qty"] == 7
    client.cancel_order_by_id.assert_called_with("stop-leg-1")
    req = client.submit_order.call_args.args[0]
    assert req.side == OrderSide.SELL
    assert req.qty == 7
    assert req.time_in_force == TimeInForce.DAY
    assert req.order_class in (None, OrderClass.SIMPLE)
    # Cancel happened before the sell was submitted.
    names = [c[0] for c in client.method_calls]
    assert names.index("cancel_order_by_id") < names.index("submit_order")


def test_held_stop_leg_nested_under_parent_is_cancelled(main):
    stop_leg = make_order("held-leg", OrderSide.SELL, "held", OrderType.STOP)
    parent = make_order("parent-2", OrderSide.BUY, "new", OrderType.MARKET, legs=[stop_leg])
    client = MagicMock()
    client.get_orders.return_value = [parent]
    main.cancel_open_stops(client, "PSX")
    client.cancel_order_by_id.assert_called_once_with("held-leg")


def test_cancel_errors_are_ignored(main):
    stop_leg = make_order("stop-x", OrderSide.SELL, "held", OrderType.STOP)
    client = MagicMock()
    client.get_orders.return_value = [stop_leg]
    client.cancel_order_by_id.side_effect = Exception("boom")
    main.cancel_open_stops(client, "PSX")  # must not raise


def test_pending_non_stop_order_still_blocks(main):
    pending = make_order("p", OrderSide.BUY, "new", OrderType.MARKET)
    client = make_client(today_orders=[pending], position_qty=5)
    body = post_approve(main, client, {"action": "sell", "ticker": "PSX"}).json()
    assert body["reason"] == "pending_order"
    client.submit_order.assert_not_called()


def test_buy_without_stop_is_plain_3_share_day_market_order(main):
    assert main.TEST_QTY == 3  # code default when TEST_QTY env is unset
    client = make_client()
    body = post_approve(main, client, {"action": "buy", "ticker": "PSX"}).json()
    assert body["status"] == "success", body
    assert body["qty"] == 3
    req = client.submit_order.call_args.args[0]
    assert req.qty == 3
    assert req.side == OrderSide.BUY
    assert req.type == OrderType.MARKET
    assert req.time_in_force == TimeInForce.DAY
    assert req.order_class in (None, OrderClass.SIMPLE)
    assert req.stop_loss is None
