from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd

from research import config
from research import data as dl

UTC = timezone.utc


def minute_frame(day: str, start_utc: str, n: int, price=100.0):
    idx = pd.date_range(f"{day} {start_utc}", periods=n, freq="1min", tz="UTC")
    p = price + np.arange(n) * 0.01
    return pd.DataFrame({"open": p, "high": p + 0.05, "low": p - 0.05, "close": p + 0.01,
                         "volume": np.full(n, 100)}, index=idx)


def test_rth_handles_dst():
    # 2024-03-08 is EST (open 14:30 UTC); 2024-03-11 is EDT (open 13:30 UTC). Start each at 12:00 UTC.
    df = pd.concat([minute_frame("2024-03-08", "12:00", 600), minute_frame("2024-03-11", "12:00", 600)])
    r = dl.rth(df)
    for d in (date(2024, 3, 8), date(2024, 3, 11)):
        day = r[r.index.date == d]
        assert len(day) == 390
        assert day.index[0].strftime("%H:%M") == "09:30" and day.index[-1].strftime("%H:%M") == "15:59"
    assert r[r.index.date == date(2024, 3, 8)].index[0].tz_convert("UTC").hour == 14
    assert r[r.index.date == date(2024, 3, 11)].index[0].tz_convert("UTC").hour == 13


def test_rth_cuts_half_days_at_1pm():
    df = minute_frame("2024-11-29", "14:30", 390)  # SIP prints thin bars until 16:00
    r = dl.rth(df)
    assert len(r) == 210 and r.index[-1].strftime("%H:%M") == "12:59"


def test_early_close_calendar():
    ec = dl.early_closes(2018, 2026)
    assert {date(2018, 12, 24), date(2019, 7, 3), date(2024, 11, 29), date(2025, 12, 24)} <= ec
    assert date(2020, 7, 3) not in ec      # Friday: Independence Day observed, market closed
    assert date(2021, 12, 24) not in ec    # Friday: Christmas observed


def test_resample_5_15_60_and_daily():
    m1 = dl.rth(minute_frame("2024-06-03", "13:30", 390))
    for mins, n_bars in ((5, 78), (15, 26), (60, 7)):
        r = dl.resample(m1, mins)
        assert len(r) == n_bars
        assert r.index[0].strftime("%H:%M") == "09:30"
        first = m1.iloc[:mins]
        assert r.iloc[0]["open"] == first["open"].iloc[0]
        assert r.iloc[0]["close"] == first["close"].iloc[-1]
        assert r.iloc[0]["high"] == first["high"].max() and r.iloc[0]["low"] == first["low"].min()
        assert r.iloc[0]["volume"] == first["volume"].sum()
    assert dl.resample(m1, 60).index[-1].strftime("%H:%M") == "15:30"  # last hour bar is 30 min
    d = dl.daily(m1)
    assert len(d) == 1 and d.iloc[0]["open"] == m1["open"].iloc[0] and d.iloc[0]["close"] == m1["close"].iloc[-1]


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_stock_bars(self, req):
        self.calls.append(req)
        # alpaca-py stores request times as naive UTC
        start, end = (t if t.tzinfo else t.replace(tzinfo=UTC) for t in (req.start, req.end))
        days = pd.bdate_range(start.date(), (end - timedelta(seconds=1)).date())
        frames = [minute_frame(str(d.date()), "14:30", 390) for d in days]
        df = pd.concat(frames) if frames else minute_frame("2000-01-03", "14:30", 0)
        df = df[(df.index >= start) & (df.index < end)]
        df.index = pd.MultiIndex.from_product([[req.symbol_or_symbols], df.index], names=["symbol", "timestamp"])
        return SimpleNamespace(df=df)


def test_cache_requests_sip_adjusted_and_reuses_complete_months(tmp_path):
    fc = FakeClient()
    cache = dl.BarCache(tmp_path, client=fc)
    a, b = datetime(2023, 1, 1, tzinfo=UTC), datetime(2023, 3, 1, tzinfo=UTC)
    first = cache.minute_bars("SPY", a, b)
    assert len(fc.calls) == 2
    req = fc.calls[0]
    assert str(getattr(req.feed, "value", req.feed)) == "sip"
    assert str(getattr(req.adjustment, "value", req.adjustment)) == "all"
    assert (tmp_path / "minute" / "SPY").exists()
    again = cache.minute_bars("SPY", a, b)
    assert len(fc.calls) == 2  # served from cache
    pd.testing.assert_frame_equal(first, again, check_freq=False)


def test_requests_never_reach_into_last_15_minutes(tmp_path):
    fc = FakeClient()
    now = datetime.now(UTC)
    dl.fetch_bars(fc, "SPY", now - timedelta(days=3), now + timedelta(days=1))
    end = fc.calls[-1].end
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    assert end <= now - config.RECENT_EMBARGO
    # and the current, incomplete month is not written to the cache
    cache = dl.BarCache(tmp_path, client=fc)
    cache.minute_bars("QQQ", now - timedelta(days=2), None)
    assert not list((tmp_path / "minute").rglob(f"{now:%Y-%m}.*"))
