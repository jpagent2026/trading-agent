"""Strategy plugin base class.

A strategy is a small class with default params, a search grid, and a vectorised
``signals(bars, **params)`` that returns rows indexed by the SIGNAL bar's timestamp
with columns: side (+1 long / -1 short), stop (absolute price), target_r (R multiple
or NaN for none), max_hold (bars, 0 = none). Every value on row i must be computable
from bars[: i + 1] only. The engine fills at the next bar's open.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ..backtest import EngineConfig


@dataclass(frozen=True)
class ParamSpec:
    step: float | None = None   # rounding granularity when perturbed (None = continuous)
    minimum: float | None = None
    perturb: bool = True


class Strategy:
    name = "base"
    timeframe_minutes = 5
    source = ""                      # where the rules came from
    defaults: dict = {}
    grid: dict = {}
    specs: dict[str, ParamSpec] = {}

    def engine_config(self) -> EngineConfig:
        return EngineConfig(eod_flatten=True)

    def param_sets(self) -> list[dict]:
        keys = list(self.grid)
        out = []
        for combo in itertools.product(*(self.grid[k] for k in keys)):
            p = dict(self.defaults)
            p.update(dict(zip(keys, combo)))
            out.append(p)
        return out or [dict(self.defaults)]

    def signals(self, bars: pd.DataFrame, **params) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    def perturbable(self, params: dict) -> list[str]:
        out = []
        for k, v in params.items():
            spec = self.specs.get(k, ParamSpec())
            if spec.perturb and isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v) and v != 0:
                out.append(k)
        return out

    def perturb(self, params: dict, key: str, factor: float) -> dict | None:
        spec = self.specs.get(key, ParamSpec())
        v = params[key] * factor
        if spec.step:
            v = round(v / spec.step) * spec.step
            if isinstance(params[key], int):
                v = int(round(v))
        if spec.minimum is not None and v < spec.minimum:
            return None
        if v == params[key]:
            return None
        p = dict(params)
        p[key] = v
        return p


# ------------------------------------------------------------------ helpers (all causal)
def session_info(bars: pd.DataFrame) -> pd.DataFrame:
    idx = bars.index
    day = pd.Series(idx.date, index=idx)
    bar_no = day.groupby(day).cumcount()
    minutes = pd.Series((idx.hour * 60 + idx.minute) - (9 * 60 + 30), index=idx)
    return pd.DataFrame({"day": day, "bar_no": bar_no, "minute": minutes})


def prior_day(bars: pd.DataFrame) -> pd.DataFrame:
    """Previous completed session's OHLC, aligned onto each intraday bar (no lookahead)."""
    day = pd.Series(bars.index.date, index=bars.index)
    g = bars.groupby(day.values)
    d = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                      "low": g["low"].min(), "close": g["close"].last()})
    prev = d.shift(1)
    out = prev.reindex(day.values)
    out.index = bars.index
    return out.add_prefix("pd_")


def make_signals(index, side, stop, target_r, max_hold=0) -> pd.DataFrame:
    df = pd.DataFrame({"side": side, "stop": stop, "target_r": target_r, "max_hold": max_hold}, index=index)
    df = df[(df["side"] != 0) & df["stop"].notna()]
    df["side"] = df["side"].astype(int)
    df["max_hold"] = df["max_hold"].astype(int)
    return df
