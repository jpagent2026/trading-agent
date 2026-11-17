"""Check what the free Alpaca data plan actually gives us, and write a markdown report.

    python -m research.data_check                # SPY + QQQ sample, report to research/DATA_CHECK.md
    python -m research.data_check --full          # also pull + cache every 1-min bar (slow)

Needs RESEARCH_ALPACA_KEY_ID / RESEARCH_ALPACA_SECRET_KEY. Market data only.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import config
from . import data as dl
from .config import ET, UTC

DEFAULT_REPORT = Path(__file__).resolve().parent / "DATA_CHECK.md"

# 1-min sample windows spread over the history, chosen to include known half-days
# (day after Thanksgiving, July 3, Christmas Eve) and DST switch weeks.
SAMPLE_WINDOWS = [
    ("2016-01-04", "2016-01-09"),
    ("2016-03-14", "2016-03-19"),   # first week after the US DST switch
    ("2016-11-21", "2016-11-26"),   # half-day 2016-11-25
    ("2018-12-24", "2018-12-29"),   # half-day 2018-12-24
    ("2019-07-01", "2019-07-06"),   # half-day 2019-07-03
    ("2020-03-09", "2020-03-14"),   # Covid volatility, DST switch
    ("2022-06-13", "2022-06-18"),
    ("2024-11-25", "2024-11-30"),   # half-day 2024-11-29
    ("2025-11-03", "2025-11-08"),   # first week after DST ends
]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def check_sip(client) -> tuple[bool, str]:
    """A recent SIP request (ending >= 15 min ago) tells us whether SIP is permitted."""
    from alpaca.common.exceptions import APIError
    from alpaca.data.timeframe import TimeFrame

    end = config.safe_end()
    try:
        df = dl.fetch_bars(client, "SPY", end - timedelta(days=5), end, TimeFrame.Minute)
        return True, f"OK ({len(df)} SPY 1-min bars in the last 5 days ending {end.astimezone(ET):%Y-%m-%d %H:%M} ET)"
    except APIError as e:
        return False, f"refused: {str(e)[:200]}"


def earliest(client, symbol: str) -> dict:
    from alpaca.data.timeframe import TimeFrame
    d = dl.fetch_bars(client, symbol, _ts("2015-01-01"), _ts("2016-03-01"), TimeFrame.Day)
    m = dl.fetch_bars(client, symbol, _ts("2015-01-01"), _ts("2016-01-15"), TimeFrame.Minute)
    m = dl.rth(m)
    return {"daily": d.index.min().date() if len(d) else None,
            "minute": m.index.min().astimezone(ET) if len(m) else None}


def hourly_coverage(client, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """RTH hourly bars across the whole range: sessions + early closes, cheaply."""
    from alpaca.data.timeframe import TimeFrame
    h = dl.rth(dl.fetch_bars(client, symbol, start, end, TimeFrame.Hour))
    return dl.session_stats(h)


def minute_sample(client, symbol: str) -> pd.DataFrame:
    rows = []
    for a, b in SAMPLE_WINDOWS:
        m = dl.rth(dl.fetch_bars(client, symbol, _ts(a), _ts(b)))
        st = dl.session_stats(m)
        for d, r in st.iterrows():
            rows.append({"date": d.date(), "bars": int(r["bars"]), "first": r["first"], "last": r["last"]})
    return pd.DataFrame(rows)


def half_day_check(client, symbol: str, days) -> str:
    """On calendar half-days, compare raw SIP volume before vs after 13:00 ET."""
    from alpaca.data.timeframe import TimeFrame
    if not days:
        return "none"
    sample = days[:: max(1, len(days) // 6)][:6]
    parts = []
    for d in sample:
        a = datetime(d.year, d.month, d.day, tzinfo=UTC)
        m = dl.to_et(dl.fetch_bars(client, symbol, a, a + timedelta(days=1), TimeFrame.Minute))
        t = m.index.time
        before = m[(t >= dl.RTH_OPEN) & (t < dl.EARLY_CLOSE)]
        after = m[(t >= dl.EARLY_CLOSE) & (t < dl.RTH_CLOSE)]
        share = after["volume"].sum() / max(before["volume"].sum(), 1)
        parts.append(f"{d}: {len(before)} bars to 13:00, {len(after)} bars after 13:00 (volume {share:.1%} of the 09:30-13:00 volume, mostly the 13:00 closing-auction print)")
    return "checked " + "; ".join(parts) + ". The RTH filter drops the post-13:00 bars."


def adjustment_check(client) -> str:
    """AAPL 4:1 split on 2020-08-31: raw closes jump, 'all'-adjusted closes should not."""
    from alpaca.data.timeframe import TimeFrame
    a, b = _ts("2020-08-26"), _ts("2020-09-03")
    raw = dl.fetch_bars(client, "AAPL", a, b, TimeFrame.Day, adjustment="raw")["close"]
    adj = dl.fetch_bars(client, "AAPL", a, b, TimeFrame.Day, adjustment="all")["close"]
    if len(raw) < 3 or len(adj) < 3:
        return "could not fetch AAPL around the 2020 split"
    jr = raw.iloc[-1] / raw.iloc[0]
    ja = adj.iloc[-1] / adj.iloc[0]
    return (f"AAPL 2020-08-26 -> 2020-09-02 close ratio: raw {jr:.2f}, adjusted {ja:.2f} "
            f"({'split adjusted OK' if 0.8 < ja < 1.3 and jr < 0.5 else 'CHECK'})")


def delisted_probe(client) -> list[dict]:
    from alpaca.common.exceptions import APIError
    from alpaca.data.timeframe import TimeFrame
    out = []
    for sym, why in config.DELISTED_PROBE.items():
        try:
            d = dl.fetch_bars(client, sym, _ts("2016-01-01"), _ts("2024-12-31"), TimeFrame.Day)
            out.append({"symbol": sym, "event": why, "bars": len(d),
                        "first": d.index.min().date() if len(d) else None,
                        "last": d.index.max().date() if len(d) else None})
        except APIError as e:
            out.append({"symbol": sym, "event": why, "bars": 0, "first": None, "last": f"error: {str(e)[:80]}"})
    return out


def build_report(symbols, client, start: datetime, end: datetime, full: bool = False, cache_dir=None) -> str:
    now = datetime.now(ET)
    sip_ok, sip_msg = check_sip(client)
    L = [f"# Data check (free Alpaca plan)", "",
         f"Run {now:%Y-%m-%d %H:%M} ET. Feed `sip`, adjustment `all`, range {start:%Y-%m-%d} to "
         f"{end.astimezone(ET):%Y-%m-%d %H:%M} ET (end is always at least 15 minutes ago).", "",
         f"- SIP allowed: **{'yes' if sip_ok else 'no'}** - {sip_msg}",
         f"- Corporate actions: {adjustment_check(client)}", ""]
    cov = {}
    early_days = sorted(d for d in dl.early_closes(start.year, end.year) if start.date() <= d < end.date())
    for s in symbols:
        e = earliest(client, s)
        cov[s] = hourly_coverage(client, s, start, end)
        L += [f"## {s}", "",
              f"- Earliest daily bar: {e['daily']}; earliest RTH 1-min bar: {e['minute']}",
              f"- Sessions with RTH bars ({start:%Y-%m-%d} to now): {len(cov[s])}"]
        L.append(f"- Early closes (13:00 ET) by calendar rule in range: {len(early_days)}; "
                 + half_day_check(client, s, early_days))
        ms = minute_sample(client, s)
        if len(ms):
            full_days = ms[ms["bars"] >= 300]
            L += [f"- 1-min sample: {len(ms)} sessions in {len(SAMPLE_WINDOWS)} windows; bars/day "
                  f"min {ms['bars'].min()}, median {int(ms['bars'].median())}, max {ms['bars'].max()} "
                  f"(a full session is 390)",
                  f"- Sample sessions with < 390 bars: " + (", ".join(
                      f"{r.date} ({r.bars}, last {r.last})" for r in ms[ms['bars'] < 390].itertuples()) or "none"),
                  ""]
        if full:
            cache = dl.BarCache(cache_dir, client=client)
            m1 = cache.minute_bars(s, start, end)
            st = dl.session_stats(dl.rth(m1))
            L += [f"- Full 1-min pull cached: {len(m1):,} bars (all sessions), {len(st)} RTH sessions; "
                  f"sessions under 380 bars: {(st['bars'] < 380).sum()}", ""]
    L += ["Note: 2020-03-09 and 2020-03-12 (and 03-16, 03-18) had 15-minute market-wide circuit-breaker "
          "halts, so ~376 one-minute bars is correct for those days.", ""]
    # gaps: weekdays missing for one symbol but present for another are real gaps;
    # weekdays missing for every symbol are market holidays.
    all_days = set().union(*[set(c.index.date) for c in cov.values()]) if cov else set()
    holidays = dl.weekday_gaps(all_days, start.date(), (end - timedelta(days=1)).date())
    L += ["## Gaps", "", f"- Weekdays with no bars for any symbol (holidays): {len(holidays)} "
          f"(about 9-10 per year expected)"]
    for s, c in cov.items():
        missing = sorted(all_days - set(c.index.date))
        L.append(f"- {s}: sessions missing while another symbol traded: {len(missing)}"
                 + (" - " + ", ".join(map(str, missing[:20])) if missing else ""))
    L += ["", "## Survivorship (delisted names)", "",
          "Alpaca's asset list lives on the trading API, which research code never calls. Instead we ask the "
          "market-data API for bars of names that died after 2016:", "",
          "| symbol | event | daily bars 2016-2024 | first | last |", "|---|---|---|---|---|"]
    for r in delisted_probe(client):
        L.append(f"| {r['symbol']} | {r['event']} | {r['bars']} | {r['first']} | {r['last']} |")
    L += ["", "If dead names return bars, a point-in-time universe is possible, but we still need a list of which "
          "names existed/qualified on each date; the free plan has no such list (survivorship gap).", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"])
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default=str(DEFAULT_REPORT))
    ap.add_argument("--full", action="store_true", help="also download and cache every 1-min bar")
    ap.add_argument("--data-dir", default=None)
    a = ap.parse_args(argv)
    try:
        creds = config.get_credentials()
    except config.MissingCredentials as e:
        print(f"data_check: {e}", file=sys.stderr)
        return 2
    client = dl.make_client(creds)
    start = config.safe_start(_ts(a.start))
    end = config.safe_end(_ts(a.end) if a.end else None)
    report = build_report(a.symbols, client, start, end, a.full, a.data_dir)
    Path(a.out).write_text(report)
    print(f"data_check: wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
