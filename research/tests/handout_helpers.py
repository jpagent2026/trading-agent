"""Hand-built synthetic sessions for the strategy-rule tests (5-minute RTH bars, ET index)."""
import numpy as np
import pandas as pd

from research.config import ET

N5 = 78  # 5-minute bars in a full session


def session_index(date, n=N5, tf=5):
    base = pd.Timestamp(date, tz=ET) + pd.Timedelta(minutes=570)
    return (base + pd.to_timedelta(np.arange(n) * tf, unit="min")).as_unit("ns")


def day_from_bars(date, bars, n=N5, tf=5, fill=None):
    """First len(bars) bars scripted as (open, high, low, close); the rest flat at `fill` (default last close)."""
    last = bars[-1][3] if fill is None else fill
    rows = list(bars) + [(last, last, last, last)] * (n - len(bars))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=session_index(date, n, tf)).assign(volume=1000)


def day_from_path(date, closes_by_block, n=N5, tf=5, start=None):
    """A session whose closes follow straight lines through the given (bar_number, close) knots."""
    knots = np.array(closes_by_block, float)
    xs = np.arange(n)
    closes = np.interp(xs, knots[:, 0], knots[:, 1])
    opens = np.r_[closes[0] if start is None else start, closes[:-1]]
    hi = np.maximum(opens, closes) + 0.01
    lo = np.minimum(opens, closes) - 0.01
    return pd.DataFrame({"open": opens, "high": hi, "low": lo, "close": closes, "volume": 1000},
                        index=session_index(date, n, tf))


def mirror(bars, pivot=200.0):
    """Flip a bar list around `pivot`: a long setup becomes the matching short setup."""
    return [(pivot - o, pivot - l, pivot - h, pivot - c) for o, h, l, c in bars]


def mirror_frame(df, pivot=200.0):
    out = df.copy()
    out["open"], out["close"] = pivot - df["open"], pivot - df["close"]
    out["high"], out["low"] = pivot - df["low"], pivot - df["high"]
    return out
