"""Validation harness: the pass bar every idea must clear before it is called a CANDIDATE.

(a) Walk-forward: tune on a rolling in-sample (IS) window, trade the next out-of-sample
    (OOS) window with the chosen params. Only OOS trades count for everything below.
(b) After-cost expectancy > 0 OOS, and significant: bootstrap 95% CI of mean trade R
    excludes 0 AND t-stat >= 2.
(c) Regimes: OOS results by year and by market regime (SPY above/below its 200-day SMA,
    realized-vol high/low). Most years and most regimes must be non-negative, there must
    be >= 3 OOS years, and the total must stay positive without the best year.
(d) At least 300 OOS trades.
(e) Parameter robustness: every numeric param moved -20/-10/+10/+20%; the median
    neighbour must still have positive after-cost expectancy on the OOS periods.
(f) Random-entry benchmark: same symbols, sides, stop distances and exits, random entry
    bars in the same OOS periods, same trade count, >= 200 runs; the strategy's mean R
    must beat the 95th percentile.
(g) Reported: max drawdown, profit factor, win rate, avg R, Sharpe, exposure.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import backtest as bt
from . import data as dl


# ------------------------------------------------------------------ settings
@dataclass(frozen=True)
class PassBar:
    min_trades: int = 300
    min_is_trades: int = 30
    bootstrap_n: int = 2000
    min_t_stat: float = 2.0
    min_oos_years: int = 3
    year_nonneg_share: float = 2 / 3
    market_regimes_needed: int = 3          # of 4 (trend up/down, vol high/low)
    regime_min_trades: int = 20             # groups smaller than this are not judged
    perturb_factors: tuple = (0.8, 0.9, 1.1, 1.2)
    random_runs: int = 200
    random_percentile: float = 95.0
    seed: int = 12345


@dataclass(frozen=True)
class WalkForward:
    is_months: int = 24
    oos_months: int = 6
    step_months: int = 6


@dataclass(frozen=True)
class Fold:
    is_start: pd.Timestamp
    is_end: pd.Timestamp      # exclusive
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp     # exclusive


def make_folds(start, end, wf: WalkForward = WalkForward()) -> list[Fold]:
    """Rolling folds. IS = [is_start, is_end), OOS = [is_end, is_end + oos) (last one clipped at end)."""
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    folds = []
    is_start = start
    while True:
        is_end = is_start + pd.DateOffset(months=wf.is_months)
        oos_end = min(is_end + pd.DateOffset(months=wf.oos_months), end)
        if is_end >= end or oos_end <= is_end:
            break
        folds.append(Fold(is_start, is_end, is_end, oos_end))
        is_start = is_start + pd.DateOffset(months=wf.step_months)
    return folds


def _trade_dates(trades: pd.DataFrame) -> pd.Series:
    if len(trades) == 0:
        return pd.Series([], dtype="datetime64[ns]")
    return pd.to_datetime(pd.Series([t.date() for t in trades["entry_time"]], index=trades.index))


def in_window(trades, start, end) -> pd.DataFrame:
    d = _trade_dates(trades)
    return trades[(d >= start) & (d < end)]


def in_folds_oos(trades, folds) -> pd.DataFrame:
    d = _trade_dates(trades)
    m = pd.Series(False, index=trades.index)
    for f in folds:
        m |= (d >= f.oos_start) & (d < f.oos_end)
    return trades[m]


# ------------------------------------------------------------------ statistics
def t_stat(r: np.ndarray) -> float:
    r = np.asarray(r, float)
    if len(r) < 2 or r.std(ddof=1) == 0:
        return 0.0
    return float(r.mean() / (r.std(ddof=1) / math.sqrt(len(r))))


def bootstrap_ci(r: np.ndarray, n: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    r = np.asarray(r, float)
    if len(r) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = r[rng.integers(0, len(r), size=(n, len(r)))].mean(1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def summarize(trades: pd.DataFrame, risk_frac: float, total_bars: int | None = None,
              bootstrap_n: int = 2000, seed: int = 0) -> dict:
    r = trades["r_net"].to_numpy(float) if len(trades) else np.zeros(0)
    n = len(r)
    out = {"trades": n}
    if n == 0:
        out.update(avg_r=0.0, median_r=0.0, win_rate=0.0, profit_factor=0.0, total_r=0.0, t_stat=0.0,
                   ci_low=float("nan"), ci_high=float("nan"), max_drawdown=0.0, sharpe=0.0, exposure=0.0,
                   avg_r_gross=0.0)
        return out
    gains, losses = r[r > 0].sum(), -r[r < 0].sum()
    lo, hi = bootstrap_ci(r, bootstrap_n, seed=seed)
    eq = equity_curve(trades, risk_frac)
    dd = float((eq / eq.cummax() - 1).min()) if len(eq) else 0.0
    daily_ret = (trades.assign(d=[t.date() for t in trades["exit_time"]])
                 .groupby("d")["r_net"].sum() * risk_frac)
    days = pd.bdate_range(min(daily_ret.index), max(daily_ret.index))
    daily_ret = daily_ret.reindex(days.date, fill_value=0.0)
    sharpe = float(daily_ret.mean() / daily_ret.std() * math.sqrt(252)) if daily_ret.std() > 0 else 0.0
    out.update(
        avg_r=float(r.mean()), median_r=float(np.median(r)), win_rate=float((r > 0).mean()),
        profit_factor=float(gains / losses) if losses > 0 else float("inf"), total_r=float(r.sum()),
        t_stat=t_stat(r), ci_low=lo, ci_high=hi, max_drawdown=dd, sharpe=sharpe,
        exposure=float(trades["bars_held"].sum() / total_bars) if total_bars else float("nan"),
        avg_r_gross=float(np.nanmean(trades["r_gross"].to_numpy(float))),
    )
    return out


def equity_curve(trades: pd.DataFrame, risk_frac: float, start: float = 1.0) -> pd.Series:
    """Fixed-fractional compounding: each trade changes equity by risk_frac x R."""
    if len(trades) == 0:
        return pd.Series([start], dtype=float)
    t = trades.sort_values("exit_time", kind="stable")
    eq = start * np.cumprod(1 + risk_frac * t["r_net"].to_numpy(float))
    return pd.Series(eq, index=pd.DatetimeIndex(t["exit_time"]))


# ------------------------------------------------------------------ regimes
def regime_labels(benchmark_bars: pd.DataFrame) -> pd.DataFrame:
    """Per session date, labels known BEFORE that day's open (all from prior-day data)."""
    d = dl.daily(benchmark_bars)
    if len(d) == 0:
        return pd.DataFrame(columns=["trend", "vol"])
    c = d["close"]
    sma200 = c.rolling(200, min_periods=200).mean()
    rv = np.log(c).diff().rolling(20, min_periods=20).std() * math.sqrt(252)
    rv_med = rv.rolling(252, min_periods=60).median()
    trend = np.where(sma200.isna(), None, np.where(c > sma200, "spy_above_200d", "spy_below_200d"))
    vol = np.where(rv_med.isna() | rv.isna(), None, np.where(rv > rv_med, "vol_high", "vol_low"))
    lab = pd.DataFrame({"trend": trend, "vol": vol}, index=d.index).shift(1)  # use yesterday's state
    lab.index = pd.to_datetime(lab.index)
    return lab


def regime_table(trades: pd.DataFrame, labels: pd.DataFrame | None) -> dict:
    d = _trade_dates(trades)
    t = trades.assign(year=d.dt.year.values)
    by_year = t.groupby("year")["r_net"].agg(["count", "sum", "mean"])
    out = {"year": {int(k): {"trades": int(v["count"]), "total_r": float(v["sum"]), "avg_r": float(v["mean"])}
                    for k, v in by_year.iterrows()}}
    market = {}
    if labels is not None and len(labels):
        lab = labels.reindex(d.values)
        for col in ("trend", "vol"):
            vals = lab[col].values
            for key in ("spy_above_200d", "spy_below_200d") if col == "trend" else ("vol_high", "vol_low"):
                sel = t["r_net"].to_numpy(float)[vals == key]
                market[key] = {"trades": int(len(sel)), "total_r": float(sel.sum()) if len(sel) else 0.0,
                               "avg_r": float(sel.mean()) if len(sel) else 0.0}
    out["market"] = market
    return out


def judge_regimes(table: dict, bar: PassBar) -> dict[str, bool]:
    years = {y: v for y, v in table["year"].items() if v["trades"] >= bar.regime_min_trades}
    res = {}
    res["regime_years"] = (len(years) >= bar.min_oos_years and
                           sum(v["total_r"] >= 0 for v in years.values()) >= math.ceil(bar.year_nonneg_share * len(years)))
    total = sum(v["total_r"] for v in table["year"].values())
    best = max((v["total_r"] for v in table["year"].values()), default=0.0)
    res["not_one_year"] = (total - best) > 0
    mk = {k: v for k, v in table["market"].items() if v["trades"] >= bar.regime_min_trades}
    need = min(bar.market_regimes_needed, len(mk)) if mk else 1
    res["regime_market"] = bool(mk) and sum(v["total_r"] >= 0 for v in mk.values()) >= need
    return res


# ------------------------------------------------------------------ random-entry benchmark
def random_benchmark(trades: pd.DataFrame, data: dict, arrays: dict, folds: list[Fold], cost_for,
                     cfg: bt.EngineConfig, runs: int = 200, seed: int = 12345) -> np.ndarray:
    """Mean net R of `runs` random-entry replays with the strategy's own exits."""
    rng = np.random.default_rng(seed)
    if len(trades) == 0:
        return np.zeros(runs)
    plans = []
    for sym, g in trades.groupby("symbol"):
        arr = arrays[sym]
        dates = pd.to_datetime(arr.dates)
        m = np.zeros(arr.n, bool)
        for f in folds:
            m |= (dates >= f.oos_start) & (dates < f.oos_end)
        cand = np.nonzero(m)[0]
        cand = cand[cand >= 1]
        if cfg.eod_flatten:
            cand = cand[arr.session[cand] == arr.session[cand - 1]]  # a signal bar exists earlier that day
        if len(cand) == 0:
            continue
        plans.append((sym, arr, cand, g["side"].to_numpy(np.int64), g["stop_frac"].to_numpy(float),
                      g["target_r"].to_numpy(float), g["max_hold"].to_numpy(np.int64),
                      g["be_r"].to_numpy(float), g["trail_r"].to_numpy(float)))
    means = np.empty(runs)
    for k in range(runs):
        tot, cnt = 0.0, 0
        for sym, arr, cand, side, sf, tr, mh, be, trl in plans:
            j = rng.choice(cand, size=len(side), replace=True)
            res = bt.simulate_batch(arr, j, side, None, tr, mh, cost_for(sym), cfg, stop_from_fill=sf,
                                    be_r=be, trail_r=trl)
            r = res.loc[res["ok"], "r_net"].to_numpy(float)
            tot += r.sum()
            cnt += len(r)
        means[k] = tot / cnt if cnt else 0.0
    return means


# ------------------------------------------------------------------ orchestration
@dataclass
class IdeaResult:
    idea: str
    verdict: str
    failures: list
    checks: dict
    metrics: dict
    fold_params: list
    base_params: dict
    regimes: dict
    robustness: dict
    random: dict
    oos_trades: pd.DataFrame = field(repr=False, default=None)
    equity: pd.Series = field(repr=False, default=None)
    data_range: tuple = ()
    universe: list = field(default_factory=list)

    def record(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("oos_trades", "equity")}
        return d


def _key(p: dict) -> str:
    return repr(sorted(p.items()))


def evaluate(strategy, data: dict[str, pd.DataFrame], cost_for, benchmark_bars: pd.DataFrame | None = None,
             bar: PassBar = PassBar(), wf: WalkForward = WalkForward(), cfg: bt.EngineConfig | None = None,
             progress=None) -> IdeaResult:
    cfg = cfg or strategy.engine_config()
    data = {s: b for s, b in data.items() if len(b)}
    arrays = {s: bt.BarArrays(b) for s, b in data.items()}
    first = min(b.index[0] for b in data.values()).tz_localize(None).normalize()
    last = max(b.index[-1] for b in data.values()).tz_localize(None).normalize() + pd.Timedelta(days=1)
    folds = make_folds(first, last, wf)
    cache: dict[str, pd.DataFrame] = {}

    def run(p):
        k = _key(p)
        if k not in cache:
            cache[k] = bt.run_strategy(strategy, p, data, cost_for, cfg, arrays)
        return cache[k]

    grid = strategy.param_sets()
    for p in grid:
        run(p)
        if progress:
            progress(f"{strategy.name}: ran {p}")

    oos_parts, fold_params = [], []
    for f in folds:
        best, best_score = None, -np.inf
        for p in grid:
            is_t = in_window(cache[_key(p)], f.is_start, f.is_end)
            if len(is_t) < bar.min_is_trades:
                continue
            score = float(is_t["r_net"].sum())
            if score > best_score:
                best, best_score = p, score
        fold_params.append({"is": [str(f.is_start.date()), str(f.is_end.date())],
                            "oos": [str(f.oos_start.date()), str(f.oos_end.date())],
                            "params": best, "is_total_r": None if best is None else best_score})
        if best is not None:
            oos_parts.append(in_window(cache[_key(best)], f.oos_start, f.oos_end))
    oos = pd.concat(oos_parts, ignore_index=True) if oos_parts else bt._empty_trades()

    total_bars = 0
    for arr in arrays.values():
        dates = pd.to_datetime(arr.dates)
        for f in folds:
            total_bars += int(((dates >= f.oos_start) & (dates < f.oos_end)).sum())
    metrics = summarize(oos, cfg.risk_frac, total_bars, bar.bootstrap_n, bar.seed)

    labels = regime_labels(benchmark_bars) if benchmark_bars is not None and len(benchmark_bars) else None
    regimes = regime_table(oos, labels) if len(oos) else {"year": {}, "market": {}}

    chosen = [fp["params"] for fp in fold_params if fp["params"] is not None]
    base = Counter(_key(p) for p in chosen).most_common(1)
    base_params = next(p for p in chosen if _key(p) == base[0][0]) if base else dict(strategy.defaults)
    neighbours = []
    for key in strategy.perturbable(base_params):
        for fac in bar.perturb_factors:
            p = strategy.perturb(base_params, key, fac)
            if p is None:
                continue
            t = in_folds_oos(run(p), folds)
            neighbours.append({"param": key, "factor": fac, "value": p[key], "trades": len(t),
                               "avg_r": float(t["r_net"].mean()) if len(t) else 0.0})
    med = float(np.median([n["avg_r"] for n in neighbours])) if neighbours else float("nan")
    robustness = {"base_params": base_params, "neighbours": neighbours, "median_neighbour_avg_r": med}

    rnd = random_benchmark(oos, data, arrays, folds, cost_for, cfg, bar.random_runs, bar.seed)
    p95 = float(np.percentile(rnd, bar.random_percentile)) if len(rnd) else float("nan")
    pct_rank = float((rnd < metrics["avg_r"]).mean() * 100) if len(rnd) else 0.0
    random_info = {"runs": int(len(rnd)), "seed": bar.seed, "mean": float(rnd.mean()) if len(rnd) else 0.0,
                   "p95": p95, "strategy_percentile": pct_rank}

    checks = {
        "min_trades": metrics["trades"] >= bar.min_trades,
        "expectancy": metrics["avg_r"] > 0,
        "significance": bool(metrics["trades"] > 1 and metrics["ci_low"] > 0 and metrics["t_stat"] >= bar.min_t_stat),
        **(judge_regimes(regimes, bar) if len(oos) else {"regime_years": False, "not_one_year": False,
                                                         "regime_market": False}),
        "robustness": bool(neighbours) and med > 0,
        "random_benchmark": len(oos) > 0 and metrics["avg_r"] > p95,
    }
    failures = [k for k, v in checks.items() if not v]
    verdict = "CANDIDATE" if not failures else "REJECTED"
    return IdeaResult(
        idea=strategy.name, verdict=verdict, failures=failures, checks=checks, metrics=metrics,
        fold_params=fold_params, base_params=base_params, regimes=regimes, robustness=robustness,
        random=random_info, oos_trades=oos, equity=equity_curve(oos, cfg.risk_frac),
        data_range=(str(first.date()), str(last.date())), universe=sorted(data),
    )
