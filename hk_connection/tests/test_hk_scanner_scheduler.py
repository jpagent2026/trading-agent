from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

from hk_connection import scanner
from hk_connection.scheduler import Scheduler
from hk_connection.tests.conftest import SESSION, Clock, et


def bars_from(closes, vol=5e6, rng=0.06):
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return [{"t": start + timedelta(days=i), "o": c, "h": c * (1 + rng / 2), "l": c * (1 - rng / 2), "c": c, "v": vol}
            for i, c in enumerate(closes)]


def test_metrics_lists_and_screens():
    up = scanner.full_metrics(bars_from([50 * 1.01 ** i for i in range(200)]))
    down = scanner.full_metrics(bars_from([200 * 0.99 ** i for i in range(200)]))
    assert up["list"] == "lead" and up["rising"] and up["adr_pct"] > 4
    assert down["list"] == "lag" and down["declining"]
    assert scanner.long_qualifies(up) is None and scanner.short_qualifies(down) is None
    assert scanner.long_qualifies(down) == "not 9>21>50"
    light = {"UP": up, "DOWN": down, "THIN": dict(up, avg_vol_10d=1000)}
    assert scanner.leader_screen(light) == ["UP", "DOWN"]
    assert scanner.leader_screen(light, ascending=True) == ["DOWN", "UP"]
    assert scanner.leader_screen(light, {"UP": 50e6}) == ["DOWN"]   # market cap <= $100M filtered
    wl = scanner.build_watchlist({"UP": up, "DOWN": down, "QQQ": down, "SQQQ": up}, ["UP", "DOWN"], ["DOWN", "UP"], 5)
    got = {(i["symbol"], i["bias"], i["trade_symbol"]) for i in wl}
    assert got == {("UP", "long", "UP"), ("DOWN", "short", "DOWN"), ("QQQ", "short", "SQQQ")}
    assert scanner.build_watchlist({"UP": up, "DOWN": down}, ["UP"], ["DOWN"], 5, shorts_enabled=False) == [
        i for i in wl if i["symbol"] == "UP"]


def test_scheduler_runs_jobs_on_et_timeline():
    eng = MagicMock()
    eng.data.trading_days.side_effect = lambda a, b: [d for d in (SESSION, SESSION + timedelta(days=1)) if a <= d <= b]
    clock = Clock(et(8, 50))
    sch = Scheduler(eng, now=clock)
    for name in ("pre_open_scan", "start_stream", "open_job", "earnings_job", "stop_stream", "eod_job", "after_close_scan"):
        setattr(sch, name, MagicMock(name=name))
    assert sch.tick() == ["pre_open_scan"]
    assert sch.tick() == []                          # once per day
    clock.t = et(9, 26); assert sch.tick() == ["start_stream"]
    clock.t = et(9, 31); assert sch.tick() == ["open_job", "reconcile"]
    clock.t = et(15, 31); assert sch.tick() == ["earnings_job", "reconcile"]
    clock.t = et(16, 12); assert sch.tick() == ["stop_stream", "eod_job"]
    clock.t = et(17, 20); assert sch.tick() == ["after_close_scan"]
    clock.t = et(10, 0, SESSION + timedelta(days=3))  # Saturday
    assert sch.tick() == []
    assert sch.next_trading_day(SESSION) == SESSION + timedelta(days=1)


THANKSGIVING = date(2026, 11, 26)  # Thursday, market closed; the live redeploy day


def test_startup_fetches_earnings_calendar_on_a_holiday(settings, broker, ledger):
    """A holiday/weekend redeploy used to leave earnings_calendar_ok false until the next
    trading day's 08:45 pre-open scan. Startup now fetches the calendar any day."""
    from hk_connection.engine import Engine

    clock = Clock(et(10, 13, THANKSGIVING))
    engine = Engine(settings, broker, ledger, data=MagicMock(name="MarketData"), now=clock)
    assert engine.earnings_ok is False
    cal = {"ABC": {"date": date(2026, 12, 1), "hour": "amc"}}
    engine.data.earnings.return_value = cal
    engine.data.trading_days.return_value = []
    sch = Scheduler(engine, now=clock)
    assert sch.tick() == []
    assert engine.earnings_ok is True
    assert engine.earnings == cal
    engine.data.earnings.assert_called_once_with(THANKSGIVING, THANKSGIVING + timedelta(days=14))
    sch.tick()
    engine.data.earnings.assert_called_once()  # not fetched again


def test_startup_earnings_fetch_failure_leaves_flag_false(settings, broker, ledger):
    from hk_connection.engine import Engine

    clock = Clock(et(10, 13, THANKSGIVING))
    engine = Engine(settings, broker, ledger, data=MagicMock(name="MarketData"), now=clock)
    engine.data.earnings.side_effect = RuntimeError("finnhub 403")
    engine.data.trading_days.return_value = []
    sch = Scheduler(engine, now=clock)
    assert sch.tick() == []
    assert engine.earnings_ok is False
    assert engine.earnings == {}
    evs = [e for e in ledger.events(20) if e["kind"] == "warning"]
    assert evs and evs[0]["detail"]["where"] == "earnings_startup"
    assert "finnhub 403" in evs[0]["detail"]["error"]


def test_refresh_earnings_job_retry_keeps_holdings_warning(settings, broker, ledger):
    from hk_connection.engine import Engine

    engine = Engine(settings, broker, ledger, data=MagicMock(name="MarketData"))
    engine.data.earnings.side_effect = RuntimeError("finnhub down")
    assert engine.refresh_earnings(SESSION, where="earnings_job") is False
    evs = [e for e in ledger.events(20) if e["kind"] == "warning"]
    assert evs[0]["detail"]["where"] == "earnings_job"
    assert evs[0]["detail"]["error"] == "earnings calendar unavailable, holdings not checked: finnhub down"
