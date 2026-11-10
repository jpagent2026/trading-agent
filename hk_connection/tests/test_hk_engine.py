from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce

from hk_connection import rules
from hk_connection.ledger import utcnow
from hk_connection.tests.conftest import SESSION, mbar

LONG_ITEM = {"symbol": "ABC", "bias": "long", "trade_symbol": "ABC", "levels": [100.0], "adr_pct": 6.0,
             "market_cap": 20e9}
SHORT_ITEM = {"symbol": "XYZ", "bias": "short", "trade_symbol": "XYZ", "levels": [50.0], "adr_pct": 6.0,
              "market_cap": 20e9}
QQQ_ITEM = {"symbol": "QQQ", "bias": "short", "trade_symbol": "SQQQ", "levels": [500.0], "adr_pct": 1.5,
            "trade_adr_pct": 5.0, "market_cap": None}


def entry_orders(client):
    return [c.args[0] for c in client.submit_order.call_args_list]


def run_long_flush(engine, last_close=100.7, last_high=100.8):
    engine.load_watchlist(SESSION.isoformat(), [dict(LONG_ITEM)])
    engine.on_bar("ABC", mbar(9, 30, 101.0, 101.2, 100.4, 100.6))   # opens into the 9 EMA area
    engine.on_bar("ABC", mbar(9, 31, 100.6, 100.7, 99.9, 100.0))    # flush: new low of day at support
    engine.on_bar("ABC", mbar(9, 32, 100.0, last_high, 99.95, last_close))  # breaks prior 1-min high


def skip_reasons(ledger):
    return [e["detail"].get("reason") for e in ledger.events(50) if e["kind"] == "entry_skipped"]


def test_pullback_long_fires_oto_gtc_entry_with_lod_stop_sized_from_account_equity(engine, client, ledger):
    run_long_flush(engine)
    orders = entry_orders(client)
    assert len(orders) == 1
    o = orders[0]
    assert o.symbol == "ABC" and o.side == OrderSide.BUY and o.order_class == OrderClass.OTO
    assert o.time_in_force == TimeInForce.GTC and o.stop_loss.stop_price == 99.9
    # equity 2000 (no CAPITAL_BASE): risk $6 / 0.80 = 7 sh, 25% cap = $500 / 100.7 = 4 sh -> 4
    assert o.qty == 4
    t = ledger.open_trades()[0]
    assert t["status"] == "pending" and t["stop_basis"] == "low_of_day" and t["capital_base"] == 2000
    assert t["stop_order_id"] == "stop-leg-1" and t["timeframe"] == 1
    assert "account_equity" in t["notes"]


def test_capital_base_env_override_changes_sizing(engine, client, settings):
    settings.capital_base = 1000.0
    assert engine.capital_base() == (1000.0, "CAPITAL_BASE")
    run_long_flush(engine)
    assert entry_orders(client)[0].qty == 2   # 25% of 1000 = $250 / 100.7


def test_capital_base_defaults_to_live_equity(engine, client):
    client.get_account.return_value = SimpleNamespace(equity="2345.67", cash="0", buying_power="9000")
    assert engine.capital_base() == (2345.67, "account_equity")


def test_no_entry_more_than_3pct_above_low_of_day(engine, client, ledger):
    run_long_flush(engine, last_close=103.1, last_high=103.2)
    assert entry_orders(client) == []
    assert "more than 3% above low of day" in skip_reasons(ledger)


def test_earnings_within_5_days_blocks_entry(engine, client, ledger):
    engine.set_earnings({"ABC": {"date": SESSION + timedelta(days=3), "hour": "amc"}}, True)
    run_long_flush(engine)
    assert entry_orders(client) == [] and "earnings within 5 days" in skip_reasons(ledger)


def test_unknown_earnings_calendar_fails_closed_for_stocks(engine, client, ledger):
    engine.set_earnings({}, False)
    run_long_flush(engine)
    assert entry_orders(client) == []


def test_losing_streak_pause_blocks_entry(engine, client, ledger):
    ledger.save_streak(rules.StreakState(paused_until=(SESSION + timedelta(days=2)).isoformat(), reduced_risk=True))
    run_long_flush(engine)
    assert entry_orders(client) == [] and "losing-streak pause" in skip_reasons(ledger)


def test_one_direction_at_a_time(engine, client, ledger):
    ledger.create_trade(symbol="XYZ", signal_symbol="XYZ", direction="short", bias="short", setup="pullback_short",
                        status="open", entry_price=50, qty_initial=5, qty_open=5, stop_price=51)
    run_long_flush(engine)
    assert entry_orders(client) == [] and "one direction at a time" in skip_reasons(ledger)


def test_multiple_entries_per_day_allowed(engine, client, ledger):
    run_long_flush(engine)
    other = dict(LONG_ITEM, symbol="DEF", trade_symbol="DEF")
    engine.load_watchlist(SESSION.isoformat(), [dict(LONG_ITEM), other])
    engine.on_bar("DEF", mbar(9, 33, 101.0, 101.2, 100.4, 100.6))
    engine.on_bar("DEF", mbar(9, 34, 100.6, 100.7, 99.9, 100.0))
    engine.on_bar("DEF", mbar(9, 35, 100.0, 100.8, 99.95, 100.7))
    assert [o.symbol for o in entry_orders(client)] == ["ABC", "DEF"]


def test_pullback_short_into_declining_ema_stop_at_high_of_day(engine, client, ledger):
    engine.load_watchlist(SESSION.isoformat(), [dict(SHORT_ITEM)])
    engine.on_bar("XYZ", mbar(9, 30, 49.5, 49.9, 49.4, 49.6))
    engine.on_bar("XYZ", mbar(9, 31, 49.6, 50.1, 49.55, 49.9))   # bounce into the declining EMA
    engine.on_bar("XYZ", mbar(9, 32, 49.9, 50.0, 49.5, 49.52))   # breaks prior 1-min low
    o = entry_orders(client)[0]
    assert o.side == OrderSide.SELL and o.order_class == OrderClass.OTO and o.stop_loss.stop_price == 50.1
    assert o.time_in_force == TimeInForce.GTC
    client.get_asset.assert_called_with("XYZ")
    assert ledger.open_trades()[0]["direction"] == "short"


def test_short_skipped_when_not_shortable(engine, client, ledger):
    client.get_asset.return_value = SimpleNamespace(shortable=False, easy_to_borrow=False)
    engine.load_watchlist(SESSION.isoformat(), [dict(SHORT_ITEM)])
    engine.on_bar("XYZ", mbar(9, 30, 49.5, 49.9, 49.4, 49.6))
    engine.on_bar("XYZ", mbar(9, 31, 49.6, 50.1, 49.55, 49.9))
    engine.on_bar("XYZ", mbar(9, 32, 49.9, 50.0, 49.5, 49.52))
    assert entry_orders(client) == [] and "not shortable / easy to borrow" in skip_reasons(ledger)


def test_index_short_is_expressed_by_buying_inverse_etf(engine, client, ledger):
    engine.set_earnings({}, False)  # ETFs don't need the earnings calendar
    engine.load_watchlist(SESSION.isoformat(), [dict(QQQ_ITEM)])
    assert engine.stream_symbols() == ["QQQ", "SQQQ"]
    engine.on_bar("SQQQ", mbar(9, 30, 20.0, 20.1, 19.9, 20.0))
    engine.on_bar("QQQ", mbar(9, 30, 498.0, 499.5, 497.5, 499.0))
    engine.on_bar("SQQQ", mbar(9, 31, 20.0, 20.05, 19.85, 19.9))
    engine.on_bar("QQQ", mbar(9, 31, 499.0, 500.2, 498.8, 500.0))
    engine.on_bar("SQQQ", mbar(9, 32, 19.9, 20.2, 19.88, 20.15))
    engine.on_bar("QQQ", mbar(9, 32, 500.0, 500.1, 498.5, 498.7))
    o = entry_orders(client)[0]
    assert o.symbol == "SQQQ" and o.side == OrderSide.BUY and o.order_class == OrderClass.OTO
    assert o.stop_loss.stop_price == 19.85
    t = ledger.open_trades()[0]
    assert (t["direction"], t["bias"], t["signal_symbol"]) == ("long", "short", "QQQ")
    client.get_asset.assert_not_called()


def _open_trade(ledger, **kw):
    f = dict(symbol="ABC", signal_symbol="ABC", direction="long", bias="long", setup="pullback_long", status="open",
             entry_price=100.0, qty_initial=20, qty_open=20, stop_price=98.5, stop_order_id="stop-0",
             stop_placed_at=utcnow())
    f.update(kw)
    return ledger.create_trade(**f)


def test_trims_15pct_at_3r_and_5r_with_cancel_sell_replace(engine, client, ledger):
    tid = _open_trade(ledger)
    client.get_orders.return_value = [SimpleNamespace(id="stop-0", symbol="ABC", order_type="stop", status="new", legs=None)]
    engine.on_bar("ABC", mbar(9, 40, 104.0, 104.7, 103.9, 104.6))   # +3R = 104.5
    calls = [c[0] for c in client.method_calls if c[0] in ("cancel_order_by_id", "submit_order")]
    assert calls == ["cancel_order_by_id", "submit_order", "submit_order"]
    sell, stop = entry_orders(client)
    assert sell.qty == 3 and sell.side == OrderSide.SELL and sell.type == OrderType.MARKET
    assert stop.qty == 17 and stop.type == OrderType.STOP and stop.stop_price == 98.5 and stop.time_in_force == TimeInForce.GTC
    t = ledger.get_trade(tid)
    assert t["qty_open"] == 17 and t["trims_done"] == 1 and round(t["realized_pnl"], 2) == 13.8
    engine.on_bar("ABC", mbar(9, 41, 104.6, 105.0, 104.5, 104.8))   # below +5R: nothing
    assert len(entry_orders(client)) == 2
    engine.on_bar("ABC", mbar(9, 42, 107.0, 107.8, 106.9, 107.6))   # +5R = 107.5
    assert entry_orders(client)[2].qty == 3 and entry_orders(client)[3].qty == 14
    assert ledger.get_trade(tid)["trims_done"] == 2


def test_reconcile_fill_then_stop_out_updates_pnl_and_streak(engine, client, ledger):
    tid = _open_trade(ledger, status="pending", entry_order_id="e1", stop_order_id="s1")
    orders = {"e1": SimpleNamespace(status="filled", filled_avg_price="100.10", filled_qty="20", legs=None),
              "s1": SimpleNamespace(status="new", filled_avg_price=None, filled_qty=None, legs=None)}
    client.get_order_by_id.side_effect = lambda oid: orders[oid]
    engine.reconcile()
    t = ledger.get_trade(tid)
    assert t["status"] == "open" and t["entry_price"] == 100.10
    orders["s1"] = SimpleNamespace(status="filled", filled_avg_price="98.50", filled_qty="20", legs=None)
    engine.reconcile()  # position gone (get_open_position raises) -> closed by the stop
    t = ledger.get_trade(tid)
    assert t["status"] == "closed" and t["exit_reason"] == "stop" and round(t["realized_pnl"], 2) == -32.0
    assert ledger.load_streak().consecutive_losses == 1


def test_ten_straight_losses_pause_entries(engine, client, ledger):
    client.get_order_by_id.side_effect = lambda oid: SimpleNamespace(status="filled", filled_avg_price="98.5",
                                                                     filled_qty="20", legs=None)
    for i in range(10):
        _open_trade(ledger, symbol=f"L{i}", stop_order_id=f"s{i}")
        engine.reconcile()
    s = ledger.load_streak()
    assert s.paused_until and s.reduced_risk and rules.is_paused(s, SESSION)
    run_long_flush(engine)
    assert "losing-streak pause" in skip_reasons(ledger)


def _daily(closes):
    return [{"t": datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(days=i), "o": c, "h": c, "l": c, "c": c,
             "v": 1e6} for i, c in enumerate(closes)]


def test_eod_trail_flags_close_below_9ema_and_open_job_exits(engine, client, ledger):
    tid = _open_trade(ledger)
    keep = _open_trade(ledger, symbol="DEF")
    bars = {"ABC": _daily([100 + i for i in range(30)] + [120.0]),     # close well below the 9 EMA
            "DEF": _daily([100 + i for i in range(31)])}               # still above
    assert engine.eod_trail(bars) == [tid]
    assert "9 EMA" in ledger.get_trade(tid)["pending_exit"] and not ledger.get_trade(keep)["pending_exit"]
    client.get_open_position.side_effect = None
    client.get_open_position.return_value = SimpleNamespace(qty="20")
    client.get_orders.return_value = [SimpleNamespace(id="stop-0", symbol="ABC", order_type="stop", status="new", legs=None)]
    engine.open_job()
    calls = [c[0] for c in client.method_calls if c[0] in ("cancel_order_by_id", "submit_order")]
    assert calls == ["cancel_order_by_id", "submit_order"]
    ex = entry_orders(client)[0]
    assert ex.symbol == "ABC" and ex.qty == 20 and ex.side == OrderSide.SELL and ex.type == OrderType.MARKET
    t = ledger.get_trade(tid)
    assert t["pending_exit"] is None and t["exit_reason"].startswith("daily close below 9 EMA") and t["exit_order_id"]


def test_trail_can_use_21_ema(engine, ledger, settings):
    settings.trail_ema = 21
    tid = _open_trade(ledger)
    # Below the 9 EMA but above the 21 EMA: no exit with the 21 EMA trail
    assert engine.eod_trail({"ABC": _daily([100 + i for i in range(30)] + [124.0])}) == []


def test_gtc_stops_refreshed_before_90_days_and_when_not_live(engine, client, ledger, clock):
    old = _open_trade(ledger, symbol="OLD", stop_order_id="s-old",
                      stop_placed_at=(clock.t - timedelta(days=81)).astimezone(timezone.utc).isoformat())
    fresh = _open_trade(ledger, symbol="NEW", stop_order_id="s-new",
                        stop_placed_at=(clock.t - timedelta(days=10)).astimezone(timezone.utc).isoformat())
    gone = _open_trade(ledger, symbol="GONE", stop_order_id="s-gone",
                       stop_placed_at=(clock.t - timedelta(days=5)).astimezone(timezone.utc).isoformat())
    status = {"s-old": "new", "s-new": "held", "s-gone": "expired"}
    client.get_order_by_id.side_effect = lambda oid: SimpleNamespace(status=status.get(oid, "canceled"))
    client.get_orders.side_effect = lambda req: [SimpleNamespace(id="s-old", symbol="OLD", order_type="stop",
                                                                 status="new", legs=None)] if req.symbols == ["OLD"] else []
    assert sorted(engine.refresh_gtc_stops()) == sorted([old, gone])
    placed = entry_orders(client)
    assert {(o.symbol, o.qty, o.stop_price, o.time_in_force) for o in placed} == {
        ("OLD", 20, 98.5, TimeInForce.GTC), ("GONE", 20, 98.5, TimeInForce.GTC)}
    client.cancel_order_by_id.assert_called_once_with("s-old")
    assert ledger.get_trade(old)["stop_order_id"] != "s-old"


def test_earnings_job_sells_half_without_enough_cushion(engine, client, ledger):
    tid = _open_trade(ledger)
    engine.set_earnings({"ABC": {"date": SESSION + timedelta(days=1), "hour": "bmo"}}, True)
    acts = engine.earnings_job(SESSION, SESSION + timedelta(days=1), {"ABC": 6.0}, {"ABC": 102.0})
    assert acts == [(tid, "sell_half")]
    sell, stop = entry_orders(client)
    assert sell.qty == 10 and stop.qty == 10 and stop.type == OrderType.STOP
    assert ledger.get_trade(tid)["qty_open"] == 10


def test_earnings_job_exits_underwater_and_holds_with_cushion(engine, client, ledger):
    a = _open_trade(ledger, symbol="UND")
    b = _open_trade(ledger, symbol="CUS")
    engine.set_earnings({"UND": {"date": SESSION, "hour": "amc"}, "CUS": {"date": SESSION, "hour": "amc"}}, True)
    client.get_open_position.side_effect = None
    client.get_open_position.return_value = SimpleNamespace(qty="20")
    acts = engine.earnings_job(SESSION, SESSION + timedelta(days=1), {"UND": 6.0, "CUS": 6.0},
                               {"UND": 99.0, "CUS": 110.0})
    assert acts == [(a, "exit"), (b, "hold")]
    assert ledger.get_trade(a)["exit_reason"] == "earnings: no cushion"
