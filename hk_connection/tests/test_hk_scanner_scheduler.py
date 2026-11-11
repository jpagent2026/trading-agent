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
