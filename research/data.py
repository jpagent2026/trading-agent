"""Historical bar data: download 1-min SIP bars from Alpaca (free Basic plan),
cache them by symbol/month, filter to regular trading hours and resample.

Only alpaca.data (StockHistoricalDataClient) is used. Nothing here can trade.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from . import config
from .config import ET, UTC

log = logging.getLogger(__name__)

OHLCV = ["open", "high", "low", "close", "volume"]
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


# ---------------------------------------------------------------- client
def make_client(creds: config.Credentials | None = None):
    """Market-data client only. Raises MissingCredentials when keys are absent."""
    from alpaca.data.historical import StockHistoricalDataClient

    creds = creds or config.get_credentials()
    return StockHistoricalDataClient(api_key=creds.key_id, secret_key=creds.secret_key)


def _bars_request(symbol: str, start: datetime, end: datetime, timeframe=None, adjustment: str = "all"):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start = config.safe_start(start)
    end = config.safe_end(end)
    config.assert_end_allowed(end)  # hard guard: never ask for the last 15 minutes
    return StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe or TimeFrame.Minute,
        start=start,
        end=end,
        adjustment=Adjustment(adjustment),
        feed=DataFeed.SIP,
    )


def fetch_bars(client, symbol: str, start: datetime, end: datetime, timeframe=None,
               adjustment: str = config.ADJUSTMENT) -> pd.DataFrame:
    """Download bars (UTC index, bar-start timestamps). Empty frame if none."""
    start = config.safe_start(start)
    end = config.safe_end(end)
    if end <= start:
        return _empty()
    resp = client.get_stock_bars(_bars_request(symbol, start, end, timeframe, adjustment))
    df = getattr(resp, "df", None)
    if df is None or len(df) == 0:
        return _empty()
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol") if "symbol" in df.index.names else df.droplevel(0)
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "timestamp"
    keep = [c for c in OHLCV + ["trade_count", "vwap"] if c in df.columns]
    return df[keep].sort_index()


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz=UTC, name="timestamp"))


# ---------------------------------------------------------------- cache
def _month_starts(start: datetime, end: datetime) -> list[datetime]:
    cur = datetime(start.year, start.month, 1, tzinfo=UTC)
    out = []
    while cur < end:
        out.append(cur)
        cur = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1, tzinfo=UTC)
    return out


def _next_month(m: datetime) -> datetime:
    return datetime(m.year + (m.month == 12), m.month % 12 + 1, 1, tzinfo=UTC)


class BarCache:
    """Per symbol, per month cache of raw 1-min bars (Parquet if pyarrow is present, else CSV).

    Only complete months are written, so a cached file never misses bars. Because bars
    use adjustment='all', a new split/dividend changes history: delete the cache to refresh.
    """

    def __init__(self, root: str | Path | None = None, client=None):
        self.root = config.data_dir(root) / "minute"
        self._client = client
        try:
            import pyarrow  # noqa: F401
            self.fmt = "parquet"
        except ImportError:  # pragma: no cover
            self.fmt = "csv"

    @property
    def client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    def _path(self, symbol: str, month: datetime) -> Path:
        return self.root / symbol.upper() / f"{month:%Y-%m}.{self.fmt}"

    def _read(self, p: Path) -> pd.DataFrame:
        if self.fmt == "parquet":
            df = pd.read_parquet(p)
        else:
            df = pd.read_csv(p, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "timestamp"
        return df

    def _write(self, df: pd.DataFrame, p: Path) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        if self.fmt == "parquet":
            df.to_parquet(tmp)
        else:
            df.to_csv(tmp)
        tmp.replace(p)

    def minute_bars(self, symbol: str, start: datetime, end: datetime | None = None) -> pd.DataFrame:
        start = config.safe_start(start)
        end = config.safe_end(end)
        frames = []
        for m in _month_starts(start, end):
            m_end = _next_month(m)
            p = self._path(symbol, m)
            if p.exists():
                frames.append(self._read(p))
                continue
            df = fetch_bars(self.client, symbol, m, min(m_end, end))
            complete = m_end <= config.safe_end() - timedelta(days=1)
            if complete:
                self._write(df, p)
            frames.append(df)
        if not frames:
            return _empty()
        out = pd.concat([f for f in frames if len(f)]) if any(len(f) for f in frames) else _empty()
        out = out[~out.index.duplicated(keep="last")].sort_index()
        return out[(out.index >= start) & (out.index < end)]

    def write_meta(self, info: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_meta.json").write_text(json.dumps(info, indent=1, default=str))


# ---------------------------------------------------------------- transforms
def to_et(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    idx = out.index if out.index.tz is not None else out.index.tz_localize(UTC)
    out.index = idx.tz_convert(ET)
    return out


def early_closes(start_year: int = 2016, end_year: int | None = None) -> set[date]:
    """NYSE 13:00 ET early closes by rule: July 3 (Mon-Thu), the day after Thanksgiving,
    Dec 24 (Mon-Thu). Verified against SIP volume in DATA_CHECK.md. One-off events
    (e.g. circuit-breaker halts) are not early closes and stay in the data.
    """
    end_year = end_year or datetime.now().year + 1
    out = set()
    for y in range(start_year, end_year + 1):
        j3 = date(y, 7, 3)
        if j3.weekday() <= 3:
            out.add(j3)
        nov1 = date(y, 11, 1)
        thanksgiving = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
        out.add(thanksgiving + timedelta(days=1))
        c24 = date(y, 12, 24)
        if c24.weekday() <= 3:
            out.add(c24)
    return out


EARLY_CLOSE = time(13, 0)


def rth(df: pd.DataFrame) -> pd.DataFrame:
    """Keep regular-hours bars, 09:30 <= bar start < 16:00 America/New_York
    (< 13:00 on early-close days).

    Conversion goes through tz-aware timestamps, so DST shifts (13:30 vs 14:30 UTC open)
    are handled. SIP still prints thin after-hours trades between 13:00 and 16:00 on
    half-days; those are dropped here using the early-close calendar.
    """
    out = to_et(df)
    if len(out) == 0:
        return out
    t = out.index.time
    mask = (t >= RTH_OPEN) & (t < RTH_CLOSE)
    d = out.index.date
    years = sorted({x.year for x in (d[0], d[-1])})
    half = early_closes(years[0], years[-1])
    if half:
        is_half = pd.Index(d).isin(list(half))
        mask &= ~(is_half & (t >= EARLY_CLOSE))
    return out[mask]


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Resample RTH 1-min bars (ET index) into N-minute bars anchored at 09:30.

    Labels are bar start times. Bars are built per session, so no bar spans two days.
    """
    if len(df) == 0:
        return df.copy()
    idx = df.index
    day = idx.normalize()
    mins = (idx - day).total_seconds() // 60 - (9 * 60 + 30)
    bucket = (mins // minutes) * minutes
    label = day + pd.to_timedelta(9 * 60 + 30 + bucket, unit="min")
    g = df.groupby(label)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    })
    out.index.name = "timestamp"
    return out


def daily(df: pd.DataFrame) -> pd.DataFrame:
    """Daily RTH bars from intraday RTH bars (ET index). Index = session date (naive Timestamp)."""
    if len(df) == 0:
        return pd.DataFrame(columns=OHLCV)
    key = pd.DatetimeIndex(df.index.date)
    g = df.groupby(key)
    out = pd.DataFrame({
        "open": g["open"].first(),
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "volume": g["volume"].sum(),
    })
    out.index.name = "date"
    return out


def load_bars(symbol: str, start: datetime, end: datetime | None, minutes: int = 5,
              cache: BarCache | None = None) -> pd.DataFrame:
    """Cached 1-min SIP bars -> RTH -> N-minute bars (ET index)."""
    cache = cache or BarCache()
    m1 = rth(cache.minute_bars(symbol, start, end))
    return m1 if minutes == 1 else resample(m1, minutes)


def session_stats(m1_rth: pd.DataFrame) -> pd.DataFrame:
    """Per session: bar count, first/last bar time (ET)."""
    if len(m1_rth) == 0:
        return pd.DataFrame(columns=["bars", "first", "last"])
    key = pd.DatetimeIndex(m1_rth.index.date)
    t = pd.Series(m1_rth.index.time, index=m1_rth.index)
    g = t.groupby(key)
    return pd.DataFrame({"bars": g.size(), "first": g.min(), "last": g.max()})


def weekday_gaps(sessions: Iterable[date], start: date, end: date) -> list[date]:
    have = set(sessions)
    days = pd.bdate_range(start, end).date
    return [d for d in days if d not in have]
