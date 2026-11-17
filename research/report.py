"""One-page markdown report + equity-curve PNG for each CANDIDATE."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import ET, RESULTS_DIR


def _fmt(v, pct=False):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v * 100:.1f}%" if pct else f"{v:.3f}"
    return str(v)


def equity_png(res, path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 3.5))
    eq = res.equity
    ax.plot(eq.index, eq.values, lw=1.2)
    ax.set_title(f"{res.idea}: out-of-sample equity (fixed-fractional, after costs)")
    ax.set_ylabel("equity (start = 1.0)")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def write_candidate_report(res, out_dir: str | Path | None = None) -> Path:
    out_dir = Path(out_dir) if out_dir else RESULTS_DIR / "reports"
    stamp = datetime.now(ET).strftime("%Y%m%d-%H%M")
    png = equity_png(res, out_dir / f"{res.idea}-{stamp}.png")
    m = res.metrics
    lines = [
        f"# CANDIDATE: {res.idea}",
        "",
        f"Generated {datetime.now(ET):%Y-%m-%d %H:%M} ET. Data {res.data_range[0]} to {res.data_range[1]}, "
        f"{len(res.universe)} symbols. Every number below is out-of-sample and after costs.",
        "",
        f"![equity]({png.name})",
        "",
        "| metric | value |", "|---|---|",
        f"| OOS trades | {m['trades']} |",
        f"| avg R (net) | {_fmt(m['avg_r'])} (gross {_fmt(m['avg_r_gross'])}) |",
        f"| 95% bootstrap CI of avg R | {_fmt(m['ci_low'])} to {_fmt(m['ci_high'])} |",
        f"| t-stat | {_fmt(m['t_stat'])} |",
        f"| win rate | {_fmt(m['win_rate'], True)} |",
        f"| profit factor | {_fmt(m['profit_factor'])} |",
        f"| max drawdown | {_fmt(m['max_drawdown'], True)} |",
        f"| Sharpe (daily, annualised) | {_fmt(m['sharpe'])} |",
        f"| exposure | {_fmt(m['exposure'], True)} |",
        f"| random-entry p95 avg R | {_fmt(res.random['p95'])} (strategy at {res.random['strategy_percentile']:.1f} pct) |",
        f"| median neighbour avg R | {_fmt(res.robustness['median_neighbour_avg_r'])} |",
        "",
        "## By year", "", "| year | trades | total R | avg R |", "|---|---|---|---|",
    ]
    for y, v in sorted(res.regimes["year"].items()):
        lines.append(f"| {y} | {v['trades']} | {v['total_r']:.1f} | {v['avg_r']:.3f} |")
    lines += ["", "## By market regime", "", "| regime | trades | total R | avg R |", "|---|---|---|---|"]
    for k, v in res.regimes["market"].items():
        lines.append(f"| {k} | {v['trades']} | {v['total_r']:.1f} | {v['avg_r']:.3f} |")
    lines += ["", "## Params chosen per walk-forward fold", ""]
    for fp in res.fold_params:
        lines.append(f"- IS {fp['is'][0]}..{fp['is'][1]} -> OOS {fp['oos'][0]}..{fp['oos'][1]}: `{fp['params']}`")
    lines += ["", "Not a recommendation to trade. Next step: review by the user and Rook before any paper test."]
    path = out_dir / f"{res.idea}-{stamp}.md"
    path.write_text("\n".join(lines) + "\n")
    return path
