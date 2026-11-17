"""Synthetic RTH bars for tests and dry runs (no network)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import ET


def rth_index(start: str, end: str, minutes: int = 5, half_days=()) -> pd.DatetimeIndex:
    days = pd.bdate_range(start, end)
    stamps = []
    for d in days:
        close_min = 210 if d.date() in set(half_days) else 390
        base = pd.Timestamp(d.date(), tz=ET) + pd.Timedelta(minutes=570)
        stamps.append(base + pd.to_timedelta(np.arange(0, close_min, minutes), unit="min"))
    return stamps[0].append(stamps[1:]).as_unit("ns")


def random_walk(start: str, end: str, minutes: int = 5, sigma: float = 0.001, price: float = 100.0,
                seed: int = 0, drift_fn=None) -> pd.DataFrame:
    """GBM-ish bars. drift_fn(bar_no_in_session: ndarray) -> per-bar mean log return."""
    idx = rth_index(start, end, minutes)
    rng = np.random.default_rng(seed)
    n = len(idx)
    day = pd.Series(idx.date)
    bar_no = day.groupby(day).cumcount().to_numpy()
    mu = drift_fn(bar_no) if drift_fn else np.zeros(n)
    ret = mu + sigma * rng.standard_normal(n)
    close = price * np.exp(np.cumsum(ret))
    open_ = np.r_[price, close[:-1]]
    span = np.abs(rng.standard_normal(n)) * sigma * close * 0.5
    high = np.maximum(open_, close) + span
    low = np.minimum(open_, close) - np.abs(rng.standard_normal(n)) * sigma * close * 0.5
    vol = rng.integers(1_000, 10_000, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)
