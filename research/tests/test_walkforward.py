import pandas as pd

from research import validation as val


def test_fold_boundaries():
    folds = val.make_folds("2016-01-01", "2020-03-15", val.WalkForward(24, 6, 6))
    assert folds[0].is_start == pd.Timestamp("2016-01-01")
    assert folds[0].is_end == folds[0].oos_start == pd.Timestamp("2018-01-01")
    assert folds[0].oos_end == pd.Timestamp("2018-07-01")
    for f in folds:
        assert f.is_end == f.oos_start                         # OOS starts right after IS
        assert f.is_start + pd.DateOffset(months=24) == f.is_end
        assert f.oos_end <= pd.Timestamp("2020-03-15")
    for a, b in zip(folds, folds[1:]):
        assert a.oos_end == b.oos_start                        # OOS windows tile, never overlap
    assert folds[-1].oos_end == pd.Timestamp("2020-03-15")      # last window clipped at data end
    assert len(folds) == 5


def test_window_membership_is_half_open():
    t = pd.DataFrame({"entry_time": pd.to_datetime(["2017-12-29 15:00", "2018-01-01 09:35", "2018-06-30 10:00",
                                                    "2018-07-01 10:00"]).tz_localize("America/New_York"),
                      "r_net": [1.0, 2.0, 3.0, 4.0]})
    f = val.make_folds("2016-01-01", "2019-01-01", val.WalkForward(24, 6, 6))[0]
    assert list(val.in_window(t, f.is_start, f.is_end)["r_net"]) == [1.0]
    assert list(val.in_window(t, f.oos_start, f.oos_end)["r_net"]) == [2.0, 3.0]


def test_selection_uses_only_in_sample(planted_data):
    from research.tests.conftest import PlantedEdge
    from research import costs
    res = val.evaluate(PlantedEdge(), planted_data, lambda s: costs.LIQUID, planted_data["SPY"],
                       val.PassBar(random_runs=20), val.WalkForward(12, 6, 6))
    oos_days = pd.to_datetime([t.date() for t in res.oos_trades["entry_time"]])
    assert oos_days.min() >= pd.Timestamp("2019-01-01")  # first 12 months are never counted
    for fp in res.fold_params:
        assert fp["is"][1] == fp["oos"][0]
