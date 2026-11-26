"""Tests for Agent 200's daily GTC stop renewal. Alpaca is fully mocked; no network, no orders."""
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import pytz
from fastapi.testclient import TestClient

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
ET = pytz.timezone("America/New_York")
NOW = ET.localize(datetime(2026, 11, 30, 9, 0))  # a Monday, pre-market


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
    mod.init_stop_renewal_db()
    monkeypatch.setattr(mod, "shadow_regime", lambda: {"regime": None, "risk_on": None})
    monkeypatch.setattr(mod, "industry_note", lambda t: {})
    monkeypatch.setattr(mod, "latest_trade_price", lambda t: 100.0)
    yield mod
    sys.modules.pop("main", None)


def stop_order(oid="old-stop", symbol="ORLY", qty="8", stop="88.45", side="sell", age_days=None,
               expires_in_days=None, status="new", filled="0"):
    created = NOW - timedelta(days=age_days) if age_days is not None else None
    expires = NOW + timedelta(days=expires_in_days) if expires_in_days is not None else None
    return SimpleNamespace(id=oid, symbol=symbol, qty=qty, filled_qty=filled, stop_price=stop,
                           side=SimpleNamespace(value=side), status=SimpleNamespace(value=status),
                           order_type=SimpleNamespace(value="stop"), created_at=created, expires_at=expires,
                           legs=None)


def make_client(orders, position_qty=8, after_cancel="canceled"):
    client = MagicMock()
    client.get_orders.return_value = orders
    client.get_open_position.return_value = SimpleNamespace(qty=str(position_qty))
    client.get_order_by_id.return_value = SimpleNamespace(status=SimpleNamespace(value=after_cancel))
    client.submit_order.return_value = SimpleNamespace(id="new-stop")
    return client


def submitted(client):
    return [c.args[0] for c in client.submit_order.call_args_list]


def test_due_rules(main):
    assert main.stop_renewal_due(stop_order(expires_in_days=9), NOW)[0] is True
    assert main.stop_renewal_due(stop_order(expires_in_days=30), NOW)[0] is False
    assert main.stop_renewal_due(stop_order(age_days=81), NOW)[0] is True     # no expires_at: age rule
    assert main.stop_renewal_due(stop_order(age_days=20), NOW)[0] is False
    assert main.stop_renewal_due(stop_order(), NOW)[0] is False               # no timestamps: never touched
    # expires_at wins over age when both are present
    assert main.stop_renewal_due(stop_order(age_days=85, expires_in_days=40), NOW)[0] is False


def test_old_stop_is_cancelled_then_replaced_same_price_same_qty(main):
    client = make_client([stop_order(age_days=82)])
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    client.cancel_order_by_id.assert_called_once_with("old-stop")
    reqs = submitted(client)
    assert len(reqs) == 1 and isinstance(reqs[0], StopOrderRequest)
    r = reqs[0]
    assert r.symbol == "ORLY" and float(r.qty) == 8 and r.side == OrderSide.SELL
    assert r.time_in_force == TimeInForce.GTC and r.stop_price == 88.45
    assert out["results"][0]["result"] == "renewed" and out["results"][0]["new_order_id"] == "new-stop"
    # cancel happened before the new stop went in
    names = [c[0] for c in client.method_calls]
    assert names.index("cancel_order_by_id") < names.index("submit_order")


def test_fresh_stops_are_left_alone(main):
    client = make_client([stop_order(age_days=10, expires_in_days=80)])
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    client.cancel_order_by_id.assert_not_called()
    client.submit_order.assert_not_called()
    assert out["stops_checked"] == 1 and out["renewed_or_handled"] == 0


def test_held_legs_and_non_stops_are_ignored(main):
    held = stop_order(oid="held-leg", age_days=85, status="held")
    limit = stop_order(oid="lim", age_days=85)
    limit.order_type = SimpleNamespace(value="limit")
    client = make_client([held, limit])
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    assert out["stops_checked"] == 0
    client.cancel_order_by_id.assert_not_called()


def test_price_already_through_stop_leaves_old_stop_in_place(main, monkeypatch):
    monkeypatch.setattr(main, "latest_trade_price", lambda t: 87.0)  # below the 88.45 sell stop
    client = make_client([stop_order(expires_in_days=5)])
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    client.cancel_order_by_id.assert_not_called()
    client.submit_order.assert_not_called()
    assert out["results"][0]["result"] == "skipped_price_through_stop"


def test_old_stop_filled_during_cancel_places_nothing(main):
    client = make_client([stop_order(age_days=85)], after_cancel="filled")
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    client.submit_order.assert_not_called()
    assert out["results"][0]["result"] == "old_stop_filled"


def test_new_stop_rejected_through_market_falls_back_to_market_exit(main):
    client = make_client([stop_order(age_days=85)])
    client.submit_order.side_effect = [Exception("stop price must be less than current price"),
                                       SimpleNamespace(id="exit-1")]
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    reqs = submitted(client)
    assert isinstance(reqs[0], StopOrderRequest)
    assert isinstance(reqs[1], MarketOrderRequest) and reqs[1].side == OrderSide.SELL and float(reqs[1].qty) == 8
    assert reqs[1].time_in_force == TimeInForce.DAY
    assert out["results"][0]["result"] == "stop_through_market_exit"
    assert main.pending_stop_repairs() == []


def test_other_failure_is_saved_and_retried_until_placed(main):
    client = make_client([stop_order(age_days=85)])
    client.submit_order.side_effect = Exception("503 service unavailable")
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    assert out["results"][0]["result"] == "place_failed_pending_retry"
    assert client.submit_order.call_count == 2  # one immediate retry
    pend = main.pending_stop_repairs()
    assert len(pend) == 1 and pend[0]["symbol"] == "ORLY" and pend[0]["stop_price"] == 88.45 and pend[0]["qty"] == 8
    # next minute: broker is back
    client.submit_order.side_effect = None
    client.submit_order.return_value = SimpleNamespace(id="new-stop-2")
    rows = main.retry_pending_repairs(client)
    assert rows[0]["result"] == "repaired" and rows[0]["new_order_id"] == "new-stop-2"
    assert submitted(client)[-1].stop_price == 88.45
    assert main.pending_stop_repairs() == []


def test_qty_never_exceeds_shares_held(main):
    client = make_client([stop_order(qty="10", age_days=85)], position_qty=6)
    main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    assert float(submitted(client)[0].qty) == 6


def test_stop_without_position_is_not_touched(main):
    client = make_client([stop_order(age_days=85)], position_qty=0)
    out = main.run_stop_renewal(client, NOW, sleep=lambda s: None)
    client.cancel_order_by_id.assert_not_called()
    assert out["results"][0]["result"] == "skipped_no_position"


def test_schedule_runs_once_per_weekday_in_window(main, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_stop_renewal", lambda client=None, now=None: calls.append(now))
    assert main.stop_renewal_tick(ET.localize(datetime(2026, 11, 30, 8, 30))) == []        # too early
    assert main.stop_renewal_tick(ET.localize(datetime(2026, 11, 30, 9, 0))) == ["renewal"]
    assert main.stop_renewal_tick(ET.localize(datetime(2026, 11, 30, 9, 1))) == []         # once a day
    assert main.stop_renewal_tick(ET.localize(datetime(2026, 12, 5, 9, 0))) == []          # Saturday
    assert main.stop_renewal_tick(ET.localize(datetime(2026, 12, 1, 15, 0))) == ["renewal"]  # catch-up
    assert main.stop_renewal_tick(ET.localize(datetime(2026, 12, 2, 16, 30))) == []        # after window
    assert len(calls) == 2


def test_startup_starts_one_daemon_thread(main, monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            started.append((name, daemon))

        def start(self):
            pass

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    main.start_stop_renewal_thread()
    main.start_stop_renewal_thread()
    assert started == [("stop-renewal", True)]


def test_renewal_endpoint_is_read_only(main):
    resp = TestClient(main.app).get("/stops/renewal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["renew_after_days"] == 80 and body["renew_within_days_of_expiry"] == 10
    assert body["pending_repairs"] == [] and isinstance(body["recent"], list)


def test_risk_settings_unchanged(main):
    assert main.RISK_PCT == 0.01 and main.MAX_POSITION_PCT == 0.25 and main.MAX_POSITIONS == 8
    assert main.SHORTS_ENABLED is False and main.DAILY_LOSS_LIMIT_PCT == 3.0
