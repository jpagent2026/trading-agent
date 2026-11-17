import numpy as np
import pandas as pd
import pytest

from research.backtest import EngineConfig
from research.strategies.base import ParamSpec, Strategy, make_signals, session_info
from research.synthetic import random_walk


class PlantedEdge(Strategy):
    """Test-only idea: go long at a fixed bar of the day. Paired with data that drifts up
    right after that bar it has a real edge; on a random walk it has none."""
    name = "planted_edge"
    defaults = {"entry_bar": 6, "stop_pct": 0.004, "hold": 8, "target_r": float("nan")}
    grid = {"stop_pct": [0.004, 0.006]}
    specs = {"entry_bar": ParamSpec(step=1, minimum=1), "hold": ParamSpec(step=1, minimum=1),
             "stop_pct": ParamSpec(minimum=0.001)}

    def engine_config(self):
        return EngineConfig(eod_flatten=True)

    def signals(self, bars, entry_bar=6, stop_pct=0.004, hold=8, target_r=float("nan"), **_):
        si = session_info(bars)
        side = np.where(si["bar_no"] == int(entry_bar), 1, 0)
        return make_signals(bars.index, side, bars["close"] * (1 - stop_pct), target_r, int(hold))


def planted_drift(bar_no):
    return np.where((bar_no >= 7) & (bar_no <= 14), 0.0008, 0.0)


SYMS = ["SPY", "AAA", "BBB", "CCC"]


@pytest.fixture(scope="session")
def planted_data():
    return {s: random_walk("2018-01-01", "2021-12-31", 5, seed=i, drift_fn=planted_drift) for i, s in enumerate(SYMS)}


@pytest.fixture(scope="session")
def walk_data():
    return {s: random_walk("2018-01-01", "2021-12-31", 5, seed=100 + i) for i, s in enumerate(SYMS)}


@pytest.fixture(scope="session")
def year_bars():
    return random_walk("2019-01-01", "2019-12-31", 5, seed=7, sigma=0.002)
