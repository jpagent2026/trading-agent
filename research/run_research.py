"""Run strategy ideas through the pass bar and record verdicts.

    python -m research.run_research                       # all ideas, default universe, 2016 -> now
    python -m research.run_research --strategy orb --symbols SPY QQQ --start 2019-01-01
    python -m research.run_research --synthetic           # dry run on fake data, no network
    python -m research.run_research --monthly-note        # print 'YYYY-MM: tested N, passed K'

Prints one summary line per idea plus 'tested N, passed K'. Every verdict is appended to
the ledger; each CANDIDATE also gets a one-page markdown report with an equity curve.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import config, costs, strategies
from . import data as dl
from . import validation as val
from .ledger import Ledger
from .report import write_candidate_report


def summary_line(res: val.IdeaResult) -> str:
    m = res.metrics
    why = f" failing: {', '.join(res.failures)}" if res.failures else ""
    return (f"{res.idea}: {res.verdict}{why} | OOS trades {m['trades']}, avg R {m['avg_r']:+.3f}, "
            f"t {m['t_stat']:.2f}, PF {m['profit_factor']:.2f}, maxDD {m['max_drawdown']:.1%}, "
            f"random p95 {res.random['p95']:+.3f}")


def load_universe(symbols, start, end, minutes, data_dir=None) -> dict:
    cache = dl.BarCache(data_dir)
    return {s: dl.load_bars(s, start, end, minutes, cache) for s in symbols}


def synthetic_universe(symbols, minutes=5) -> dict:
    from .synthetic import random_walk
    return {s: random_walk("2018-01-01", "2021-12-31", minutes, seed=i) for i, s in enumerate(symbols)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default="all", help="all or one of: " + ", ".join(strategies.REGISTRY))
    ap.add_argument("--symbols", nargs="+", default=config.DEFAULT_UNIVERSE)
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--ledger", default=None)
    ap.add_argument("--reports-dir", default=None)
    ap.add_argument("--min-trades", type=int, default=val.PassBar.min_trades)
    ap.add_argument("--random-runs", type=int, default=val.PassBar.random_runs)
    ap.add_argument("--is-months", type=int, default=val.WalkForward.is_months)
    ap.add_argument("--oos-months", type=int, default=val.WalkForward.oos_months)
    ap.add_argument("--synthetic", action="store_true", help="fake random-walk data; nothing is fetched")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--monthly-note", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(message)s")
    if a.synthetic:
        a.no_ledger = True  # fake data never goes into the real ledger
    ledger = Ledger(a.ledger)
    if a.monthly_note:
        print(ledger.monthly_note())
        return 0

    ideas = strategies.all_strategies() if a.strategy == "all" else [strategies.get(a.strategy)]
    bar = val.PassBar(min_trades=a.min_trades, random_runs=a.random_runs)
    wf = val.WalkForward(is_months=a.is_months, oos_months=a.oos_months, step_months=a.oos_months)
    liquid = set(config.DEFAULT_UNIVERSE)
    cost_for = lambda s: costs.for_symbol(s, liquid)  # noqa: E731

    symbols = [s.upper() for s in a.symbols]
    loaded: dict[int, dict] = {}
    passed = 0
    for strat in ideas:
        tf = strat.timeframe_minutes
        if tf not in loaded:
            if a.synthetic:
                loaded[tf] = synthetic_universe(sorted(set(symbols) | {config.BENCHMARK}), tf)
            else:
                try:
                    start = datetime.fromisoformat(a.start).replace(tzinfo=config.UTC)
                    end = datetime.fromisoformat(a.end).replace(tzinfo=config.UTC) if a.end else None
                    loaded[tf] = load_universe(sorted(set(symbols) | {config.BENCHMARK}), start, end, tf, a.data_dir)
                except config.MissingCredentials as e:
                    print(f"run_research: {e}", file=sys.stderr)
                    return 2
        universe = loaded[tf]
        data = {s: universe[s] for s in symbols if s in universe}
        res = val.evaluate(strat, data, cost_for, universe.get(config.BENCHMARK), bar, wf,
                           progress=(logging.info if a.verbose else None))
        print(summary_line(res))
        if res.verdict == "CANDIDATE":
            passed += 1
            if not a.no_ledger:
                path = write_candidate_report(res, a.reports_dir)
                print(f"  report: {path}")
        if not a.no_ledger:
            rec = res.record()
            rec["synthetic"] = a.synthetic
            ledger.append(rec)
    print(f"tested {len(ideas)}, passed {passed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
