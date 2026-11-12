"""Stop-breach enforcement: HK never keeps holding a position that is already through its stop.
Alpaca (trading and market data) is fully mocked."""
from datetime import timedelta, timezone
from types import SimpleNamespace

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from fastapi.testclient import TestClient

from hk_connection.app import Runtime, create_app
from hk_connection.broker import is_stop_through_market_error
from hk_connection.ledger import utcnow
from hk_connection.scheduler import JOBS, Scheduler
from hk_connection.tests.conftest import SESSION, et, mbar

REJECT = Exception('{"code":42210000,"message":"stop price must not be greater than base price / 100"}')


def orders(client):
    return [c.args[0] for c in client.submit_order.call_args_list]


def stops(client):
    return [o for o in orders(client) if o.type == OrderType.STOP]


def markets(client):
    return [o for o in orders(client) if o.type == OrderType.MARKET]


def open_orka(ledger, **kw):
    f = dict(symbol="ORKA", signal_symbol="ORKA", direction="long", bias="long", setup="pullback_long", status="open",
             entry_price=112.0, qty_initial=20, qty_open=20, stop_price=107.80, stop_order_id="s-old",
             stop_placed_at=utcnow())
    f.update(kw)
    return ledger.create_trade(**f)


def hold(client, qty=20):
    client.get_open_position.side_effect = None
    client.get_open_position.return_value = SimpleNamespace(qty=str(qty))


def price(engine, *seq):
    """Latest SIP trade for ORKA; successive calls return successive prices (last one repeats)."""
    seq = list(seq)

    def f(symbols):
        p = seq.pop(0) if len(seq) > 1 else seq[0]
        return {s: p for s in symbols} if p is not None else {}
    engine.data.latest_prices.side_effect = f


def stop_rejecting_client(client, error=REJECT):
    base = client.submit_order.side_effect

    def submit(req):
        if req.type == OrderType.STOP:
            raise error
        return base(req)
    client.submit_order.side_effect = submit


def working_exits(client):
    """Make submitted market orders show up as working (accepted, unfilled) at the mocked broker, the way
    Alpaca queues a DAY market order outside regular hours."""
    base = client.submit_order.side_effect
    work: dict = {}

    def submit(req):
        o = base(req)
        if req.type == OrderType.MARKET:
            work[o.id] = SimpleNamespace(id=o.id, symbol=req.symbol, order_type="market", side=req.side.value,
                                         status="accepted", qty=str(req.qty), filled_qty="0", legs=None,
                                         filled_avg_price=None)
        return o
    client.submit_order.side_effect = submit
    client.get_orders.side_effect = lambda req: [o for o in work.values() if o.symbol in (req.symbols or [o.symbol])]
    client.get_order_by_id.side_effect = lambda oid: work.get(oid, SimpleNamespace(status="canceled",
                                                                                filled_avg_price=None, legs=None))
    return work


def breach_rows(ledger):
    return ledger.stop_breach_exits()


# ---------------------------------------------------------------- stop placement paths
def test_stop_rejected_by_broker_becomes_market_exit(engine, client, ledger):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 108.10, 107.70)               # above the stop at the pre-check, through it by the reject
    stop_rejecting_client(client)
    assert engine.refresh_gtc_stops() == []     # stop not live -> refresh -> rejected -> exit instead
    m = markets(client)
    assert len(m) == 1 and m[0].symbol == "ORKA" and m[0].qty == 20 and m[0].side == OrderSide.SELL
    assert m[0].time_in_force == TimeInForce.DAY
    t = ledger.get_trade(tid)
    assert t["exit_reason"] == "stop breach (broker_reject)" and t["exit_order_id"] and t["pending_exit"] is None
    row = breach_rows(ledger)[0]
    assert (row["symbol"], row["qty"], row["stop"], row["trigger_price"], row["reason"], row["status"]) == \
        ("ORKA", 20, 107.80, 107.70, "broker_reject", "submitted")
    assert row["context"] == "gtc_refresh" and row["order_id"] == t["exit_order_id"] and "base price" in row["error"]


def test_any_stop_rejection_with_price_through_stop_exits(engine, client, ledger):
    open_orka(ledger)
    hold(client)
    price(engine, None, 106.0)                  # no price at the pre-check; through the stop after the error
    stop_rejecting_client(client, Exception("some other APIError"))
    engine.refresh_gtc_stops()
    assert [o.qty for o in markets(client)] == [20]
    assert breach_rows(ledger)[0]["reason"] == "broker_reject"


def test_unrelated_stop_rejection_with_price_above_stop_is_not_an_exit(engine, client, ledger):
    open_orka(ledger)
    hold(client)
    price(engine, 110.0)
    stop_rejecting_client(client, Exception("insufficient qty available for order"))
    assert engine.refresh_gtc_stops() == []
    assert markets(client) == [] and breach_rows(ledger) == []
    assert any(e["kind"] == "error" for e in ledger.events())


def test_price_already_below_stop_on_refresh_exits_without_submitting_a_stop(engine, client, ledger):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 105.43)
    assert engine.refresh_gtc_stops() == []
    assert stops(client) == []
    m = markets(client)
    assert len(m) == 1 and m[0].qty == 20 and m[0].side == OrderSide.SELL and m[0].time_in_force == TimeInForce.DAY
    row = breach_rows(ledger)[0]
    assert row["reason"] == "pre_check" and row["trigger_price"] == 105.43 and row["status"] == "submitted"
    assert ledger.get_trade(tid)["exit_reason"] == "stop breach (pre_check)"


def test_normal_refresh_price_above_stop_places_stop_as_before(engine, client, ledger):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 112.50)
    assert engine.refresh_gtc_stops() == [tid]
    (s,) = stops(client)
    assert (s.symbol, s.qty, s.stop_price, s.side, s.time_in_force) == ("ORKA", 20, 107.8, OrderSide.SELL, TimeInForce.GTC)
    assert markets(client) == [] and breach_rows(ledger) == []
    t = ledger.get_trade(tid)
    assert t["exit_order_id"] is None and t["stop_order_id"] != "s-old"


def test_trim_replace_rejected_exits_only_the_remaining_shares(engine, client, ledger):
    """cancel stop -> sell 3 -> re-place stop for 17 is rejected -> market-sell the 17 still held, net of the
    3 already in the working trim order (no over-selling into a short)."""
    tid = ledger.create_trade(symbol="ABC", signal_symbol="ABC", direction="long", bias="long", setup="pullback_long",
                              status="open", entry_price=100.0, qty_initial=20, qty_open=20, stop_price=98.5,
                              stop_order_id="stop-0", stop_placed_at=utcnow())
    hold(client)
    price(engine, 104.6, 98.4)
    stop_rejecting_client(client)
    working = []
    base = client.submit_order.side_effect

    def submit(req):
        o = base(req)
        if req.type == OrderType.MARKET:
            working.append(SimpleNamespace(id=o.id, symbol=req.symbol, order_type="market", side="sell",
                                           status="new", qty=str(req.qty), filled_qty="0", legs=None))
        return o
    client.submit_order.side_effect = submit
    client.get_orders.side_effect = lambda req: list(working)
    engine.on_bar("ABC", mbar(9, 40, 104.0, 104.7, 103.9, 104.6))   # +3R trim
    assert [o.qty for o in markets(client)] == [3, 17]
    t = ledger.get_trade(tid)
    assert t["trims_done"] == 1 and t["exit_reason"] == "stop breach (broker_reject)"
    assert breach_rows(ledger)[0]["context"] == "trim" and breach_rows(ledger)[0]["qty"] == 17


def test_entry_fill_with_rejected_oto_leg_and_price_through_stop_exits(engine, client, ledger):
    tid = open_orka(ledger, status="pending", entry_order_id="e1", stop_order_id="leg1")
    hold(client)
    price(engine, 107.0)
    o = {"e1": SimpleNamespace(status="filled", filled_avg_price="112.0", filled_qty="20", legs=None),
         "leg1": SimpleNamespace(status="rejected", filled_avg_price=None, filled_qty=None, legs=None)}
    client.get_order_by_id.side_effect = lambda oid: o.get(oid, SimpleNamespace(status="new"))
    engine.reconcile()
    assert stops(client) == [] and [m.qty for m in markets(client)] == [20]
    row = breach_rows(ledger)[0]
    assert row["context"] == "entry" and row["reason"] == "pre_check"
    assert ledger.get_trade(tid)["status"] == "open"


def test_broker_error_classifier():
    assert is_stop_through_market_error(REJECT)
    assert is_stop_through_market_error(Exception("stop price must be less than current price"))
    assert not is_stop_through_market_error(Exception("insufficient buying power"))


# ---------------------------------------------------------------- periodic check
def test_periodic_check_flattens_breached_long_and_does_not_double_submit(engine, client, ledger, clock):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 105.43)
    clock.t = et(10, 0)
    assert engine.enforce_stops("cycle", rth=True) == [tid]
    assert [(m.symbol, m.qty, m.side) for m in markets(client)] == [("ORKA", 20, OrderSide.SELL)]
    exit_id = ledger.get_trade(tid)["exit_order_id"]
    # Next cycles: exit order is working at the broker -> nothing new is sent
    client.get_order_by_id.side_effect = lambda oid: SimpleNamespace(status="new" if oid == exit_id else "canceled")
    assert engine.enforce_stops("cycle", rth=True) == []
    clock.t = et(10, 5)
    assert engine.enforce_stops("cycle", rth=True) == []
    assert len(markets(client)) == 1
    assert [r["reason"] for r in breach_rows(ledger)] == ["periodic"]


def test_periodic_check_skips_when_broker_already_has_a_working_exit(engine, client, ledger, clock):
    open_orka(ledger)
    hold(client)
    price(engine, 105.0)
    client.get_orders.return_value = [SimpleNamespace(id="manual-sell", symbol="ORKA", order_type="market", side="sell",
                                                      status="accepted", qty="20", filled_qty="0", legs=None)]
    engine.enforce_stops("cycle", rth=True)
    assert markets(client) == []
    assert breach_rows(ledger)[0]["status"] == "already_pending"


def test_periodic_check_leaves_position_above_stop_alone(engine, client, ledger):
    open_orka(ledger)
    hold(client)
    price(engine, 107.81)
    assert engine.enforce_stops("cycle", rth=True) == []
    assert client.submit_order.call_count == 0 and breach_rows(ledger) == []


def test_live_broker_stop_gets_a_grace_period_then_is_replaced_by_market_exit(engine, client, ledger, clock):
    tid = open_orka(ledger, stop_order_id="s-live")
    hold(client)
    price(engine, 107.0)
    live = SimpleNamespace(id="s-live", symbol="ORKA", order_type="stop", side="sell", status="new", stop_price="107.80",
                           qty="20", filled_qty="0", legs=None)
    client.get_orders.return_value = [live]
    clock.t = et(20, 0)                                          # after hours: broker triggers it at the open
    assert engine.enforce_stops("startup", rth=False) == []
    clock.t = et(9, 30, SESSION + timedelta(days=1))
    assert engine.enforce_stops("open", rth=True) == []          # give the live stop a minute
    clock.t = et(9, 32, SESSION + timedelta(days=1))
    assert engine.enforce_stops("cycle", rth=True) == [tid]      # still not triggered -> cancel it and sell
    client.cancel_order_by_id.assert_called_with("s-live")
    assert [m.qty for m in markets(client)] == [20]


def test_short_stock_mirror_rule_buys_to_cover(engine, client, ledger):
    tid = ledger.create_trade(symbol="XYZ", signal_symbol="XYZ", direction="short", bias="short", setup="pullback_short",
                              status="open", entry_price=50.0, qty_initial=10, qty_open=10, stop_price=51.0,
                              stop_order_id="s", stop_placed_at=utcnow())
    hold(client, -10)
    engine.data.latest_prices.side_effect = lambda syms: {"XYZ": 51.25}
    assert engine.enforce_stops("cycle", rth=True) == [tid]
    (m,) = markets(client)
    assert m.side == OrderSide.BUY and m.qty == 10


# ---------------------------------------------------------------- startup / open
def _scheduler(engine, clock, trading_day=True):
    engine.data.trading_days.side_effect = lambda a, b: [d for d in (SESSION, SESSION + timedelta(days=1))
                                                         if a <= d <= b] if trading_day else []
    sch = Scheduler(engine, now=clock)
    return sch


def test_startup_check_catches_existing_breach_after_hours_and_queues_market_order(engine, client, ledger, clock):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 96.15)
    working_exits(client)
    clock.t = et(20, 30)                                         # deployed in the evening
    sch = _scheduler(engine, clock)
    sch.tick()                                                   # startup check, then the catch-up EOD job
    (m,) = markets(client)
    assert (m.symbol, m.qty, m.side, m.time_in_force) == ("ORKA", 20, OrderSide.SELL, TimeInForce.DAY)
    row = breach_rows(ledger)[0]
    assert (row["reason"], row["context"], row["status"], row["trigger_price"]) == ("periodic", "startup", "submitted", 96.15)
    assert ledger.get_trade(tid)["exit_order_id"] == row["order_id"]
    assert sch.startup_stop_check_done and sch.last_stop_check


def test_startup_exit_refused_outside_hours_is_queued_for_the_open_job(engine, client, ledger, clock):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 96.15)
    base = client.submit_order.side_effect
    calls = {"n": 0}

    def submit(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("market is closed")
        return base(req)
    client.submit_order.side_effect = submit
    clock.t = et(20, 30)
    _scheduler(engine, clock).tick()
    t = ledger.get_trade(tid)
    assert t["pending_exit"] == "stop breach (periodic)" and breach_rows(ledger)[0]["status"] == "queued_for_open"
    engine.open_job()                                            # 9:30:30 next session
    assert calls["n"] == 2 and ledger.get_trade(tid)["pending_exit"] is None
    assert markets(client)[-1].qty == 20 and markets(client)[-1].side == OrderSide.SELL


def test_first_cycle_after_the_open_catches_breach(engine, client, ledger, clock):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 108.5)
    working_exits(client)
    clock.t = et(8, 0)
    sch = _scheduler(engine, clock)
    for name, _, _ in JOBS:
        sch.done[name] = SESSION.isoformat()                     # isolate from the scan/stream jobs
    sch.tick()
    assert markets(client) == []                                 # premarket: above the stop
    price(engine, 105.43)
    clock.t = et(9, 30) + timedelta(seconds=10)
    sch.tick()
    assert [m.qty for m in markets(client)] == [20]
    clock.t = et(9, 35)
    sch.tick()                                                   # later cycles: exit is working, nothing new
    assert [m.qty for m in markets(client)] == [20]
    row = breach_rows(ledger)[0]
    assert (row["context"], row["reason"], row["symbol"]) == ("open", "periodic", "ORKA")
    assert ledger.get_trade(tid)["exit_reason"] == "stop breach (periodic)"


def test_open_job_does_not_double_submit_when_exit_already_working(engine, client, ledger):
    tid = open_orka(ledger, pending_exit="daily close below 9 EMA")
    hold(client)
    client.get_orders.return_value = [SimpleNamespace(id="x1", symbol="ORKA", order_type="market", side="sell",
                                                      status="accepted", qty="20", filled_qty="0", legs=None)]
    engine.open_job()
    assert markets(client) == [] and ledger.get_trade(tid)["pending_exit"] is None


def test_closed_breach_trade_marks_row_filled(engine, client, ledger):
    tid = open_orka(ledger)
    hold(client)
    price(engine, 105.43)
    engine.enforce_stops("cycle", rth=True)
    client.get_open_position.side_effect = Exception("position does not exist")
    client.get_order_by_id.side_effect = lambda oid: SimpleNamespace(status="filled", filled_avg_price="105.40",
                                                                     filled_qty="20", legs=None)
    engine.reconcile()
    t = ledger.get_trade(tid)
    assert t["status"] == "closed" and t["exit_price"] == 105.40 and t["exit_reason"] == "stop breach (periodic)"
    assert breach_rows(ledger)[0]["status"] == "filled"


# ---------------------------------------------------------------- monitoring
def test_status_shows_stop_breach_exits(settings, ledger, broker, engine, client):
    open_orka(ledger)
    hold(client)
    price(engine, 105.43)
    engine.enforce_stops("open", rth=True)
    rt = Runtime()
    rt.settings, rt.ledger, rt.broker, rt.engine = settings, ledger, broker, engine
    r = TestClient(create_app(rt, start_engine=False)).get("/status").json()
    (x,) = r["stop_breach_exits"]
    for k in ("symbol", "qty", "stop", "trigger_price", "reason", "order_id", "time", "status"):
        assert k in x
    assert (x["symbol"], x["qty"], x["stop"], x["trigger_price"], x["reason"], x["status"]) == \
        ("ORKA", 20, 107.8, 105.43, "periodic", "submitted")
    assert x["order_id"] and x["time"]


def test_market_data_latest_prices_uses_sip_latest_trade():
    from unittest.mock import MagicMock
    from alpaca.data.enums import DataFeed
    from hk_connection.data import MarketData
    md = MarketData.__new__(MarketData)
    md.hist = MagicMock()
    md.hist.get_stock_latest_trade.return_value = {"ORKA": SimpleNamespace(price=96.15), "BAD": SimpleNamespace(price=0)}
    assert md.latest_prices(["ORKA", "BAD"]) == {"ORKA": 96.15}
    req = md.hist.get_stock_latest_trade.call_args.args[0]
    assert req.feed == DataFeed.SIP
