"""Tests for Agent 200's in-app exit checks (stop / 2R target / 10-day time stop).

Alpaca is fully mocked by FakeAlpaca below: no network, no real orders."""
import importlib
import itertools
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytz
from fastapi.testclient import TestClient

from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
ET = pytz.timezone("America/New_York")
UTC = pytz.utc

HOLIDAYS = {date(2026, 11, 26), date(2026, 12, 25)}
EARLY_CLOSE = {date(2026, 11, 27): (13, 0), date(2026, 12, 24): (13, 0)}


def trading_day(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def et(y, m, d, hh, mm):
    return ET.localize(datetime(y, m, d, hh, mm))


LIVE = ("new", "accepted", "pending_new", "partially_filled", "held")


class FakeAlpaca:
    """Small stateful stand-in for alpaca-py's TradingClient."""

    def __init__(self, is_open=True, equity=3000.0, last_equity=3000.0, cancel_result="canceled",
                 fill_sells=False, reject_sells=False):
        self.positions = {}
        self.orders = []
        self.is_open = is_open
        self.equity, self.last_equity = equity, last_equity
        self.cancel_result = cancel_result
        self.fill_sells = fill_sells
        self.reject_sells = reject_sells
        self.submitted = []
        self.cancelled = []
        self.calls = []          # ordered log of ("cancel", id) / ("submit", req)
        self._ids = itertools.count(1)

    # --- setup helpers
    def add_position(self, symbol, qty, avg_entry, current=None):
        self.positions[symbol] = SimpleNamespace(symbol=symbol, qty=str(qty), avg_entry_price=str(avg_entry),
                                                 current_price=str(current or avg_entry))

    def add_order(self, **kw):
        o = SimpleNamespace(id=kw.pop("id", f"o{next(self._ids)}"), symbol=kw.pop("symbol"),
                            side=SimpleNamespace(value=kw.pop("side", "sell")),
                            status=SimpleNamespace(value=kw.pop("status", "new")),
                            order_type=SimpleNamespace(value=kw.pop("order_type", "stop")),
                            stop_price=kw.pop("stop_price", None), qty=str(kw.pop("qty", 0)),
                            filled_qty=str(kw.pop("filled_qty", 0)), filled_at=kw.pop("filled_at", None),
                            filled_avg_price=kw.pop("filled_avg_price", None), legs=kw.pop("legs", None),
                            client_order_id=kw.pop("client_order_id", None),
                            created_at=kw.pop("created_at", None), expires_at=None)
        assert not kw, kw
        self.orders.append(o)
        return o

    # --- TradingClient surface
    def get_clock(self):
        return SimpleNamespace(is_open=self.is_open)

    def get_account(self):
        return SimpleNamespace(equity=str(self.equity), last_equity=str(self.last_equity), buying_power="9000")

    def get_all_positions(self):
        return list(self.positions.values())

    def get_open_position(self, symbol):
        if symbol not in self.positions:
            raise Exception("position does not exist")
        return self.positions[symbol]

    def _all(self):
        for o in self.orders:
            yield o
            for leg in o.legs or []:
                yield leg

    def get_orders(self, req=None):
        status = getattr(req, "status", None)
        symbols = getattr(req, "symbols", None)
        out = []
        for o in self.orders:
            if symbols and o.symbol not in symbols:
                continue
            st = o.status.value
            if status == QueryOrderStatus.OPEN and st not in LIVE:
                continue
            if status == QueryOrderStatus.CLOSED and st in LIVE:
                continue
            out.append(o)
        return out

    def get_order_by_id(self, oid):
        for o in self._all():
            if str(o.id) == str(oid):
                return o
        raise Exception("order not found")

    def get_order_by_client_id(self, coid):
        for o in self._all():
            if o.client_order_id and o.client_order_id == coid:
                return o
        raise Exception("order not found")

    def cancel_order_by_id(self, oid):
        self.calls.append(("cancel", str(oid)))
        self.cancelled.append(str(oid))
        o = self.get_order_by_id(oid)
        if self.cancel_result == "filled":   # the stop fired while we were cancelling
            o.status = SimpleNamespace(value="filled")
            self.positions.pop(o.symbol, None)
        else:
            o.status = SimpleNamespace(value=self.cancel_result)

    def submit_order(self, req):
        self.calls.append(("submit", req))
        coid = getattr(req, "client_order_id", None)
        if coid and any(o.client_order_id == coid for o in self._all()):
            raise Exception("client_order_id must be unique")
        if isinstance(req, MarketOrderRequest) and req.side == OrderSide.SELL and self.reject_sells:
            raise Exception("insufficient qty available for order")
        self.submitted.append(req)
        is_stop = isinstance(req, StopOrderRequest)
        o = self.add_order(symbol=req.symbol, side=req.side.value, order_type="stop" if is_stop else "market",
                           status="new" if is_stop else "accepted", stop_price=getattr(req, "stop_price", None),
                           qty=req.qty, client_order_id=coid)
        if not is_stop and req.side == OrderSide.SELL and self.fill_sells:
            o.status = SimpleNamespace(value="filled")
            o.filled_avg_price = "97.20"
            self.positions.pop(req.symbol, None)
        return o

    def get_calendar(self, req):
        out, d = [], req.start
        while d <= req.end:
            if trading_day(d):
                hh, mm = EARLY_CLOSE.get(d, (16, 0))
                out.append(SimpleNamespace(date=d, open=datetime(d.year, d.month, d.day, 9, 30),
                                           close=datetime(d.year, d.month, d.day, hh, mm)))
            d += timedelta(days=1)
        return out

    # --- assertions helpers
    def sells(self):
        return [r for r in self.submitted if isinstance(r, MarketOrderRequest)]


@pytest.fixture
def main(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.delenv("EXIT_CHECKS_ENABLED", raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    sys.modules.pop("main", None)
    with patch.object(Path, "mkdir"):
        mod = importlib.import_module("main")
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "agent200.db")
    mod.init_db()
    mod.init_stop_renewal_db()
    mod.init_exit_checks_db()
    monkeypatch.setattr(mod, "shadow_regime", lambda: {"regime": None, "risk_on": None})
    monkeypatch.setattr(mod, "industry_note", lambda t: {})
    mod._prices = {}
    monkeypatch.setattr(mod, "latest_trade_price", lambda t: mod._prices.get(t))
    yield mod
    sys.modules.pop("main", None)


NOSLEEP = lambda s: None  # noqa: E731


def orly(main, fake, price, stop_live=True, entry_day=date(2026, 11, 12), plan=True):
    """ORLY 8 sh @ 91.33 bought Nov 12 with stop 88.45 (the real position), 2R target 97.09."""
    fake.add_position("ORLY", 8, 91.33, price)
    entry = fake.add_order(id="entry-orly", symbol="ORLY", side="buy", order_type="market", status="filled",
                           qty=8, filled_qty=8, filled_avg_price="91.33",
                           filled_at=ET.localize(datetime.combine(entry_day, datetime.min.time()) + timedelta(hours=10)),
                           legs=[])
    stop = None
    if stop_live:
        stop = fake.add_order(id="stop-orly", symbol="ORLY", side="sell", order_type="stop", status="new",
                              stop_price="88.45", qty=8)
    if plan:
        main.save_plan("entry-orly", "ORLY", "entry-orly", entry_day.isoformat(), 91.33, 88.45, "approve_fill")
    main._prices["ORLY"] = price
    return entry, stop


def run(main, fake, now, enabled=True):
    return main.run_exit_checks(fake, now, slot="test", enabled=enabled, sleep=NOSLEEP)


MON_AM = et(2026, 11, 30, 9, 45)    # Monday after Thanksgiving week
TUE_PM = et(2026, 11, 17, 14, 0)    # 3 sessions after entry: no time stop


# ---------------------------------------------------------------- rules

def test_target_formula_uses_original_stop(main):
    assert main.target_price(91.33, 88.45) == 97.09      # matches the ledger's ORLY 2R target
    assert main.target_price(167.57, 158.0) == 186.71    # ICE
    assert main.target_price(100, 100) is None and main.target_price(None, 90) is None


def test_stop_exit_when_no_broker_stop(main):
    fake = FakeAlpaca()
    orly(main, fake, price=88.00, stop_live=False)
    out = run(main, fake, TUE_PM)
    sells = fake.sells()
    assert out["exits_sent"] == 1 and len(sells) == 1
    s = sells[0]
    assert s.symbol == "ORLY" and float(s.qty) == 8 and s.side == OrderSide.SELL and s.time_in_force == TimeInForce.DAY
    row = main.recent_exits()[0]
    assert row["reason"] == "stop" and row["trigger_price"] == 88.0 and row["qty"] == 8 and row["order_id"]


def test_stop_through_but_broker_stop_working_is_left_to_the_broker(main):
    fake = FakeAlpaca()
    orly(main, fake, price=88.00, stop_live=True)
    out = run(main, fake, TUE_PM)
    assert fake.submitted == [] and fake.cancelled == []
    assert any("broker_stop_working" in s.get("detail", "") for s in out["skipped"])


def test_target_2R_cancels_working_stop_before_selling(main):
    fake = FakeAlpaca()
    orly(main, fake, price=97.10)
    out = run(main, fake, TUE_PM)
    assert out["exits_sent"] == 1
    kinds = [c[0] for c in fake.calls]
    assert kinds == ["cancel", "submit"], kinds            # stop cancelled first, then one sell
    assert fake.calls[0][1] == "stop-orly"
    assert main.recent_exits()[0]["reason"] == "target_2R"
    assert main.recent_exits()[0]["target"] == 97.09


def test_time_stop_after_10_full_sessions(main):
    # Entered Nov 12. Sessions after entry: Nov 13,16,17,18,19,20,23,24,25,27 (Thanksgiving skipped) = 10.
    fake = FakeAlpaca()
    orly(main, fake, price=95.00)
    out = run(main, fake, et(2026, 11, 27, 12, 45))   # during the 10th session: only 9 completed
    assert fake.submitted == [] and out["skipped"][0]["sessions_held"] == 9
    out = run(main, fake, MON_AM)                       # first check after the 10th close
    assert out["exits_sent"] == 1 and main.recent_exits()[0]["reason"] == "time_stop"
    assert [c[0] for c in fake.calls] == ["cancel", "submit"]


def test_time_stop_matches_routine_history_jnj(main):
    fake = FakeAlpaca()
    assert len(main.trading_dates_between(fake, "2026-10-29", date(2026, 11, 12))) == 9   # no sell on Nov 12
    assert len(main.trading_dates_between(fake, "2026-10-29", date(2026, 11, 13))) == 10  # sold morning Nov 13


def test_no_exit_when_none_applies(main):
    fake = FakeAlpaca()
    orly(main, fake, price=93.00)
    out = run(main, fake, TUE_PM)
    assert fake.submitted == [] and fake.cancelled == []
    assert out["positions_checked"] == 1 and out["exits_sent"] == 0
    assert out["skipped"][0]["reason"] == "no_exit_applies"


# ---------------------------------------------------------------- sell-only

def test_sell_only_across_scenarios(main):
    for price in (80.0, 97.5, 95.0):
        fake = FakeAlpaca()
        main.init_exit_checks_db()
        orly(main, fake, price=price, stop_live=price != 80.0)
        fake.add_position("SQQQ", -5, 20.0, 10.0)        # a short: must be ignored, never covered
        main._prices["SQQQ"] = 10.0
        out = run(main, fake, MON_AM)
        assert all(r.side == OrderSide.SELL for r in fake.submitted)
        assert all(r.symbol != "SQQQ" for r in fake.submitted)
        assert {"symbol": "SQQQ", "reason": "not_a_long_position"} in out["skipped"]
        with main.STOP_RENEWAL_LOCK:
            pass
        main._db_exec("DELETE FROM exits")
        main._db_exec("DELETE FROM position_plans")


def test_exit_submitter_refuses_more_than_held_or_no_position(main):
    fake = FakeAlpaca()
    with pytest.raises(ValueError):
        main._submit_exit_sell(fake, "ORLY", 9, 8, "x")
    with pytest.raises(ValueError):
        main._submit_exit_sell(fake, "ORLY", 1, 0, "x")
    assert fake.submitted == []


def test_exit_code_contains_no_buy_or_short_path(main):
    src = (REPO_ROOT / "main.py").read_text()
    section = src.split("# === EXIT CHECKS (BEGIN) ===")[1].split("# === EXIT CHECKS (END) ===")[0]
    assert "OrderSide.BUY" not in section
    assert "OrderClass" not in section
    assert re.search(r"side\s*=\s*OrderSide\.SELL", section)


# ---------------------------------------------------------------- idempotency

def test_two_runs_and_a_restart_give_one_sell(main, tmp_path, monkeypatch):
    fake = FakeAlpaca()            # sells are accepted but NOT filled, so the position is still there
    orly(main, fake, price=97.50)
    run(main, fake, TUE_PM)
    run(main, fake, TUE_PM + timedelta(minutes=1))
    # "restart": fresh module state, same sqlite file
    db = main.DB_PATH
    sys.modules.pop("main", None)
    with patch.object(Path, "mkdir"):
        fresh = importlib.import_module("main")
    monkeypatch.setattr(fresh, "DB_PATH", db)
    fresh.init_exit_checks_db()
    monkeypatch.setattr(fresh, "latest_trade_price", lambda t: 97.50)
    monkeypatch.setattr(fresh, "shadow_regime", lambda: {"regime": None, "risk_on": None})
    monkeypatch.setattr(fresh, "industry_note", lambda t: {})
    out = fresh.run_exit_checks(fake, TUE_PM + timedelta(hours=1), slot="test", enabled=True, sleep=NOSLEEP)
    assert len(fake.sells()) == 1
    assert any("already_exited" in s.get("detail", "") for s in out["skipped"])
    assert len(fresh.recent_exits()) == 1


def test_crash_between_submit_and_db_update_does_not_resell(main):
    fake = FakeAlpaca()
    orly(main, fake, price=97.50)
    real_update = main.update_exit
    calls = {"n": 0}

    def crash_once(coid, **fields):
        if fields.get("order_id") and calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("process killed")
        return real_update(coid, **fields)

    with patch.object(main, "update_exit", crash_once):
        out = run(main, fake, TUE_PM)
    assert out["errors"]                          # the crash surfaced
    out = run(main, fake, TUE_PM + timedelta(minutes=5))
    assert len(fake.sells()) == 1                 # found by client_order_id; no second sell
    row = main.recent_exits()[0]
    assert row["order_id"] and row["status"] == "accepted"


def test_crash_after_write_ahead_before_submit_retries_with_same_id(main):
    fake = FakeAlpaca()
    orly(main, fake, price=97.50)
    with patch.object(main, "_submit_exit_sell", side_effect=SystemExit("killed")):
        with pytest.raises(SystemExit):
            run(main, fake, TUE_PM)
    pending = main.recent_exits()[0]
    assert pending["status"] == "pending" and not pending["order_id"]
    # the stop was cancelled before the crash; put the shares back to a realistic state (stop gone)
    run(main, fake, TUE_PM + timedelta(minutes=5))
    sells = fake.sells()
    assert len(sells) == 1 and sells[0].client_order_id == pending["client_order_id"]


def test_open_sell_order_blocks_a_second_sell(main):
    fake = FakeAlpaca()
    orly(main, fake, price=97.50)
    fake.add_order(symbol="ORLY", side="sell", order_type="market", status="accepted", qty=8)
    out = run(main, fake, TUE_PM)
    assert fake.submitted == [] and fake.cancelled == []
    assert any(s.get("detail") == "open_sell_order_exists" for s in out["skipped"])


def test_dead_exit_order_gets_a_new_attempt(main):
    fake = FakeAlpaca()
    orly(main, fake, price=97.50)
    run(main, fake, TUE_PM)
    first = fake.sells()[0]
    fake.get_order_by_client_id(first.client_order_id).status = SimpleNamespace(value="expired")
    fake.add_order(id="stop-orly-2", symbol="ORLY", side="sell", order_type="stop", status="new",
                   stop_price="88.45", qty=8)
    run(main, fake, TUE_PM + timedelta(days=1))
    sells = fake.sells()
    assert len(sells) == 2 and sells[1].client_order_id.endswith("-a2")


# ---------------------------------------------------------------- flag off

def test_flag_off_by_default_places_nothing_but_previews(main):
    assert main.exit_checks_enabled() is False
    fake = FakeAlpaca()
    orly(main, fake, price=97.50)
    out = main.run_exit_checks(fake, TUE_PM, slot="test", sleep=NOSLEEP)   # enabled read from env (unset)
    assert out["enabled"] is False and out["dry_run"] is True
    assert fake.submitted == [] and fake.cancelled == []
    assert [w["reason"] for w in out["would_exit"]] == ["target_2R"]
    assert main.recent_exits() == []


def test_flag_values(main, monkeypatch):
    for v, want in (("true", True), ("1", True), ("YES", True), ("false", False), ("", False), ("0", False)):
        monkeypatch.setenv("EXIT_CHECKS_ENABLED", v)
        assert main.exit_checks_enabled() is want


# ---------------------------------------------------------------- broker stop races

def test_broker_stop_filled_during_cancel_gives_no_sell(main):
    fake = FakeAlpaca(cancel_result="filled")
    orly(main, fake, price=97.50)
    out = run(main, fake, TUE_PM)
    assert fake.sells() == []
    assert any(s.get("detail") == "broker_stop_filled" for s in out["skipped"])


def test_position_already_stopped_out_gives_no_sell(main):
    fake = FakeAlpaca()
    orly(main, fake, price=87.0, stop_live=False)
    fake.positions.clear()                           # broker stop filled before the check
    out = run(main, fake, TUE_PM)
    assert fake.submitted == [] and out["positions_checked"] == 0


def test_unconfirmed_cancel_leaves_stop_and_does_not_sell(main):
    fake = FakeAlpaca(cancel_result="pending_cancel")
    orly(main, fake, price=97.50)
    out = run(main, fake, TUE_PM)
    assert fake.sells() == []
    assert any("stop_cancel_unconfirmed" in s.get("detail", "") for s in out["skipped"])


def test_rejected_sell_puts_the_stop_back(main):
    fake = FakeAlpaca(reject_sells=True)
    orly(main, fake, price=97.50)
    out = run(main, fake, TUE_PM)
    assert out["errors"] and "sell_failed_stop_restored" in out["errors"][0]["detail"]
    stops = [r for r in fake.submitted if isinstance(r, StopOrderRequest)]
    assert len(stops) == 1 and stops[0].stop_price == 88.45 and float(stops[0].qty) == 8
    assert stops[0].side == OrderSide.SELL and stops[0].time_in_force == TimeInForce.GTC


# ---------------------------------------------------------------- halt + market gates

def test_daily_loss_halt_still_applies_to_exits(main):
    fake = FakeAlpaca(equity=2900.0, last_equity=3000.0)   # -3.33% <= -3% halt
    orly(main, fake, price=97.50)
    out = run(main, fake, TUE_PM)
    assert fake.submitted == [] and fake.cancelled == []
    assert any("daily_loss_halt" in s.get("detail", "") for s in out["skipped"])


def test_market_closed_sends_nothing(main):
    fake = FakeAlpaca(is_open=False)
    orly(main, fake, price=97.50)
    out = run(main, fake, TUE_PM)
    assert fake.submitted == [] and fake.cancelled == []
    assert any(s.get("detail") == "market_closed" for s in out["skipped"])


# ---------------------------------------------------------------- schedule

def test_schedule_normal_day(main):
    s = main.exit_schedule_for(FakeAlpaca(), date(2026, 11, 30))
    assert s["morning"] == et(2026, 11, 30, 9, 45) and s["afternoon"] == et(2026, 11, 30, 14, 0)
    assert s["early_close"] is False
    assert main.due_exit_slot(s, et(2026, 11, 30, 9, 44)) is None
    assert main.due_exit_slot(s, et(2026, 11, 30, 9, 45)) == "morning"
    assert main.due_exit_slot(s, et(2026, 11, 30, 13, 59)) == "morning"
    assert main.due_exit_slot(s, et(2026, 11, 30, 14, 0)) == "afternoon"
    assert main.due_exit_slot(s, et(2026, 11, 30, 15, 56)) is None


def test_non_trading_days_do_not_run(main, monkeypatch):
    monkeypatch.setenv("EXIT_CHECKS_ENABLED", "true")
    fake = FakeAlpaca()
    orly(main, fake, price=97.50)
    with patch.object(main, "run_exit_checks") as rec:
        for now in (et(2026, 11, 26, 9, 45), et(2026, 11, 26, 14, 0),     # Thanksgiving
                    et(2026, 11, 28, 9, 45), et(2026, 11, 29, 14, 0)):    # weekend
            main.EXIT_CHECK_STATE.pop("_schedule_cache", None)
            assert main.exit_check_tick(now, fake) is None
        rec.assert_not_called()
    assert main.EXIT_CHECK_STATE["schedule_today"]["trading_day"] is False
    assert fake.submitted == []


def test_early_close_runs_afternoon_check_before_the_close(main, monkeypatch):
    monkeypatch.setenv("EXIT_CHECKS_ENABLED", "true")
    fake = FakeAlpaca()
    s = main.exit_schedule_for(fake, date(2026, 11, 27))     # 1:00 PM ET close
    assert s["early_close"] is True and s["afternoon"] == et(2026, 11, 27, 12, 45)
    orly(main, fake, price=97.50)
    assert main.exit_check_tick(et(2026, 11, 27, 12, 46), fake) == "afternoon"
    assert len(fake.sells()) == 1
    assert main.exit_check_tick(et(2026, 11, 27, 12, 47), fake) is None   # once per slot
    assert main.exit_check_tick(et(2026, 11, 27, 14, 0), fake) is None    # after the close: nothing


def test_tick_runs_each_slot_once_per_day(main, monkeypatch):
    fake = FakeAlpaca()
    orly(main, fake, price=93.0)
    with patch.object(main, "run_exit_checks", wraps=main.run_exit_checks) as rec:
        assert main.exit_check_tick(et(2026, 11, 30, 9, 45), fake) == "morning"
        assert main.exit_check_tick(et(2026, 11, 30, 9, 46), fake) is None
        assert main.exit_check_tick(et(2026, 11, 30, 14, 0), fake) == "afternoon"
        assert main.exit_check_tick(et(2026, 11, 30, 14, 1), fake) is None
        assert rec.call_count == 2


def test_failed_slot_is_retried(main):
    fake = FakeAlpaca()
    with patch.object(fake, "get_all_positions", side_effect=Exception("alpaca 503")):
        assert main.exit_check_tick(et(2026, 11, 30, 9, 45), fake) is None
    assert "alpaca 503" in main.EXIT_CHECK_STATE["last_error"]
    assert main.exit_check_tick(et(2026, 11, 30, 9, 46), fake) == "morning"


# ---------------------------------------------------------------- plans (new and legacy)

def test_approved_plan_switches_to_fill_price(main):
    fake = FakeAlpaca()
    fake.add_position("DD", 5, 151.40, 152)
    fake.add_order(id="buy-dd", symbol="DD", side="buy", order_type="market", status="filled", qty=5,
                   filled_qty=5, filled_avg_price="151.40", filled_at=et(2026, 11, 30, 9, 50))
    fake.add_order(id="stop-dd", symbol="DD", side="sell", order_type="stop", status="new", stop_price="147.40", qty=5)
    main.record_entry_plan("DD", "buy-dd", 151.25, 147.40)
    assert main.get_plan("buy-dd")["status"] == "approved"
    main._prices["DD"] = 152.0
    run(main, fake, et(2026, 12, 1, 9, 45))
    p = main.get_plan("buy-dd")
    assert p["status"] == "open" and p["entry_price"] == 151.40 and p["original_stop"] == 147.40
    assert p["target"] == round(151.40 + 2 * (151.40 - 147.40), 2) and p["entry_date"] == "2026-11-30"


def test_legacy_position_derived_from_oto_leg(main):
    fake = FakeAlpaca()
    fake.add_position("ICE", 3, 167.57, 170)
    leg = SimpleNamespace(id="leg1", symbol="ICE", side=SimpleNamespace(value="sell"),
                          status=SimpleNamespace(value="new"), order_type=SimpleNamespace(value="stop"),
                          stop_price="158.00", qty="3", filled_qty="0", legs=None, client_order_id=None)
    fake.add_order(id="buy-ice", symbol="ICE", side="buy", order_type="market", status="filled", qty=3,
                   filled_qty=3, filled_avg_price="167.57", filled_at=UTC.localize(datetime(2026, 10, 30, 13, 50)),
                   legs=[leg])
    main._prices["ICE"] = 170.0
    out = run(main, fake, et(2026, 11, 2, 9, 45))
    p = main.get_plan("buy-ice")
    assert p["source"] == "derived_oto_leg" and p["entry_date"] == "2026-10-30"
    assert p["original_stop"] == 158.0 and p["target"] == 186.71
    assert out["skipped"][0]["reason"] == "no_exit_applies"


def test_legacy_position_falls_back_to_working_stop(main):
    fake = FakeAlpaca()
    orly(main, fake, price=93.0, plan=False)
    run(main, fake, TUE_PM)
    p = main.get_plan("entry-orly")
    assert p["source"] == "derived_working_stop" and p["original_stop"] == 88.45 and p["target"] == 97.09


def test_flag_off_does_not_write_plans(main):
    fake = FakeAlpaca()
    orly(main, fake, price=93.0, plan=False)
    run(main, fake, TUE_PM, enabled=False)
    assert main.get_plan("entry-orly") is None


# ---------------------------------------------------------------- endpoints + risk settings

def test_exits_status_endpoint_and_status_summary(main):
    fake = FakeAlpaca()
    fake.add_position("ORLY", 8, 91.33)
    with patch.object(main, "get_trading_client", return_value=fake):
        body = TestClient(main.app).get("/exits/status").json()
        status = TestClient(main.app).get("/status").json()
    for k in ("enabled", "thread_started", "last_run_at", "last_result", "recent_exits", "last_error",
              "schedule_today", "open_plans"):
        assert k in body
    assert body["enabled"] is False
    for k in ("equity", "buying_power", "open_positions", "trades_today", "watchlist", "regime", "industry",
              "shorts_enabled", "max_positions", "version"):
        assert k in status
    assert status["exit_checks"]["enabled"] is False
    assert fake.submitted == []


def test_risk_settings_unchanged(main):
    assert main.MAX_POSITIONS == 8
    assert main.MAX_TRADES_PER_DAY == 3
    assert main.DAILY_LOSS_LIMIT_PCT == 3.0
    assert main.RISK_PCT == 0.01
    assert main.MAX_POSITION_PCT == 0.25
    assert main.MIN_STOP_PCT == 0.015
    assert main.SHORTS_ENABLED is False
    assert main.SHORT_RISK_PCT == 0.005
    assert main.compute_qty(3000, 151.25, 147.40, main.RISK_PCT) == 4
    with patch.object(main, "TradingClient") as tc:
        main.get_trading_client()
        assert tc.call_args.kwargs["paper"] is True


def test_buy_via_approve_unchanged_and_records_plan(main):
    fake = FakeAlpaca()
    fake.add_position("XOM", 1, 100)  # unrelated position so counts are realistic

    class Trade(SimpleNamespace):
        pass

    data = SimpleNamespace(get_stock_latest_trade=lambda req: {"DD": Trade(price=151.25)})
    with patch.object(main, "get_trading_client", return_value=fake), \
         patch("alpaca.data.historical.StockHistoricalDataClient", return_value=data):
        resp = TestClient(main.app).post("/approve", json={"action": "buy", "ticker": "DD", "stop": 147.40}).json()
    assert resp["status"] == "success" and resp["qty"] == 4
    assert set(resp) == {"status", "message", "order_id", "qty"}
    req = fake.submitted[0]
    assert req.side == OrderSide.BUY and req.order_class == OrderClass.OTO and req.stop_loss.stop_price == 147.4
    p = main.get_plan(resp["order_id"])
    assert p["original_stop"] == 147.40 and p["entry_price"] == 151.25 and p["status"] == "approved"


def test_plan_write_failure_never_breaks_a_buy(main):
    with patch.object(main, "save_plan", side_effect=Exception("disk full")):
        main.record_entry_plan("DD", "id", 151.25, 147.40)   # must not raise
