"""Real-time 1-minute bars -> 5/15/60-minute bars, plus per-symbol session state.

Buckets are aligned to the 9:30 ET open (5-min: 9:30-9:35, ...; 60-min: 9:30-10:30, ...).
Only regular-session bars are aggregated; extended-hours bars are ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
TIMEFRAMES = (5, 15, 60)


@dataclass
class Bar:
    start: datetime          # bar start, tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    minutes: int = 1

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)


def to_et(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ZoneInfo("UTC"))
    return ts.astimezone(ET)


def minutes_since_open(ts: datetime) -> float:
    et = to_et(ts)
    open_dt = et.replace(hour=9, minute=30, second=0, microsecond=0)
    return (et - open_dt).total_seconds() / 60.0


def in_regular_session(bar_start: datetime) -> bool:
    t = to_et(bar_start).time()
    return SESSION_OPEN <= t < SESSION_CLOSE


class BarAggregator:
    """Holds completed and forming bars for 1, 5, 15 and 60 minutes for one symbol and one session."""

    def __init__(self) -> None:
        self.completed: dict[int, list[Bar]] = {1: [], 5: [], 15: [], 60: []}
        self.forming: dict[int, Optional[Bar]] = {5: None, 15: None, 60: None}

    def add(self, bar: Bar) -> list[Bar]:
        """Add a regular-session 1-minute bar; returns any higher-timeframe bars it completed."""
        bar = Bar(to_et(bar.start), bar.open, bar.high, bar.low, bar.close, bar.volume, 1)
        self.completed[1].append(bar)
        done: list[Bar] = []
        m = int(round(minutes_since_open(bar.start)))
        for tf in TIMEFRAMES:
            bucket = m // tf
            bucket_start = bar.start.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=bucket * tf)
            cur = self.forming[tf]
            if cur is not None and cur.start != bucket_start:
                # A gap in the stream: close the stale bucket.
                self.completed[tf].append(cur)
                done.append(cur)
                cur = None
            if cur is None:
                cur = Bar(bucket_start, bar.open, bar.high, bar.low, bar.close, bar.volume, tf)
            else:
                cur.high = max(cur.high, bar.high)
                cur.low = min(cur.low, bar.low)
                cur.close = bar.close
                cur.volume += bar.volume
            if (m + 1) % tf == 0:
                self.completed[tf].append(cur)
                done.append(cur)
                self.forming[tf] = None
            else:
                self.forming[tf] = cur
        return done

    def last_completed(self, tf: int, offset: int = 1) -> Optional[Bar]:
        lst = self.completed[tf]
        return lst[-offset] if len(lst) >= offset else None

    def current(self, tf: int) -> Optional[Bar]:
        """The bar containing the latest minute: the forming bar, or for 1-min the latest minute."""
        if tf == 1:
            return self.last_completed(1)
        f = self.forming[tf]
        if f is not None:
            return f
        # The latest minute just completed this bucket.
        return self.last_completed(tf)

    def previous(self, tf: int) -> Optional[Bar]:
        """The completed bar before the one containing the latest minute."""
        if tf == 1:
            return self.last_completed(1, 2)
        if self.forming[tf] is not None:
            return self.last_completed(tf, 1)
        return self.last_completed(tf, 2)


@dataclass
class SessionState:
    """Per-symbol intraday state for one session."""
    symbol: str
    session_date: Optional[str] = None
    open: float = 0.0
    lod: float = 0.0
    hod: float = 0.0
    last: float = 0.0
    agg: BarAggregator = field(default_factory=BarAggregator)
    long_armed: bool = False     # set when a new low of day prints in a support zone
    short_armed: bool = False    # set when a new high of day prints in a resistance zone
    hourly_ema9: Optional[float] = None
    hourly_ema21: Optional[float] = None

    def reset(self, session_date: str) -> None:
        self.session_date = session_date
        self.open = self.lod = self.hod = self.last = 0.0
        self.agg = BarAggregator()
        self.long_armed = self.short_armed = False

    def update(self, bar: Bar) -> tuple[bool, bool, list[Bar]]:
        """Returns (new_low_of_day, new_high_of_day, completed_higher_tf_bars)."""
        d = to_et(bar.start).date().isoformat()
        if self.session_date != d:
            h9, h21 = self.hourly_ema9, self.hourly_ema21
            self.reset(d)
            self.hourly_ema9, self.hourly_ema21 = h9, h21
        new_low = new_high = False
        if self.open == 0.0:
            self.open, self.lod, self.hod = bar.open, bar.low, bar.high
            new_low = new_high = True
        else:
            if bar.low < self.lod:
                self.lod, new_low = bar.low, True
            if bar.high > self.hod:
                self.hod, new_high = bar.high, True
        self.last = bar.close
        done = self.agg.add(bar)
        return new_low, new_high, done
