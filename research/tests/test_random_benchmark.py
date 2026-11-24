import numpy as np

from research import backtest as bt
from research import costs, validation as val
from research.tests.conftest import PlantedEdge


def _setup(data):
    s = PlantedEdge()
    cfg = s.engine_config()
    arrays = {k: bt.BarArrays(v) for k, v in data.items()}
    trades = bt.run_strategy(s, s.defaults, data, lambda x: costs.LIQUID, cfg, arrays)
    folds = val.make_folds("2018-01-01", "2022-01-01", val.WalkForward(12, 6, 6))
    oos = val.in_folds_oos(trades, folds)
    return oos, arrays, folds, cfg


def test_reproducible_with_seed(walk_data):
    oos, arrays, folds, cfg = _setup(walk_data)
    a = val.random_benchmark(oos, walk_data, arrays, folds, lambda x: costs.LIQUID, cfg, runs=30, seed=7)
    b = val.random_benchmark(oos, walk_data, arrays, folds, lambda x: costs.LIQUID, cfg, runs=30, seed=7)
    c = val.random_benchmark(oos, walk_data, arrays, folds, lambda x: costs.LIQUID, cfg, runs=30, seed=8)
    np.testing.assert_array_equal(a, b)
    assert not np.array_equal(a, c)
    assert len(a) == 30


def test_random_entries_have_no_edge_on_random_walk(walk_data):
    oos, arrays, folds, cfg = _setup(walk_data)
    r = val.random_benchmark(oos, walk_data, arrays, folds, lambda x: costs.LIQUID, cfg, runs=50, seed=1)
    assert r.mean() < 0.05  # costs make random entries lose a little on average
