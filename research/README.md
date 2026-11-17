# Strategy research harness

A lab for testing trading ideas on years of real market data, with a strict pass bar.
It **never trades**: there is no order code in this folder, and a test fails the build if
any appears. It only reads historical bars from Alpaca's free market-data plan.

It stays quiet on purpose. Most ideas fail. You only get a report when an idea clears
every check below; otherwise the ledger just records "rejected, and why".

## The pass bar, in plain words

An idea becomes a **CANDIDATE** only if all of these are true. Failing any one = **REJECTED**.

| check | what it means | why it matters |
|---|---|---|
| walk-forward | Settings are picked on 2 years of data, then traded on the *next* 6 months it has never seen. Repeat, rolling forward. Only the unseen months count. | Picking the best settings and grading them on the same data always looks good. |
| expectancy | Average trade, after slippage, spread and fees, is above 0R. (1R = the amount risked on the trade.) | Costs are what kill most intraday ideas. |
| significance | The 95% bootstrap range for the average trade stays above 0, and t-stat >= 2. | Separates an edge from luck. |
| min trades | At least 300 out-of-sample trades. | 30 or 60 trades can't tell +0.1R from 0. |
| years | At least 3 out-of-sample years, most of them non-negative, and still profitable with the best year removed. | One great year (say 2020) can carry a bad idea. |
| market regimes | Non-negative in at least 3 of 4: SPY above / below its 200-day average, high / low volatility (SPY 20-day realized vol vs its 1-year median). | A real edge shouldn't only work in a bull market. |
| robustness | Every setting is nudged -20/-10/+10/+20%. The typical (median) nudged version must still make money. | If moving a stop 10% kills it, it was a lucky setting. |
| random benchmark | 200 runs of random entries with the *same* stops, targets, symbols, sides and trade count. The idea must beat the best 5% of those. | Shows the entry signal adds something, not just the exit rules. |

Also reported: max drawdown, profit factor, win rate, average R, Sharpe and time in market.

## Why 60 days of 5-minute data was misleading

- **Too few trades.** An opening-range breakout trades at most once a day: 60 days is
  about 60 trades per symbol. Intraday trades swing about 1R either way, so with 60 trades
  the average is only known to within roughly +/-0.25R. A real edge of +0.1R and a coin
  flip look the same.
- **One market mood.** Two months is one regime. Ten years includes 2018's chop, the 2020
  crash and rebound, the 2022 bear market and calm stretches.
- **Tuned on the test.** Trying a few settings and keeping the best one on the same 60
  days always flatters the result.
- **Costs.** A few basis points per side is a large share of a typical 5-minute move.

So "flat or negative on 60 days" is not proof an idea is bad, and a good-looking 60 days
would not have been proof it was good. The years of data here can actually tell.

## How to run it

```bash
python -m venv /workspace/research-venv
/workspace/research-venv/bin/pip install -r requirements.txt -r research/requirements.txt

export RESEARCH_ALPACA_KEY_ID=...        # separate research paper account, never the bots' keys
export RESEARCH_ALPACA_SECRET_KEY=...

python -m research.data_check                    # what the free data really has -> research/DATA_CHECK.md
python -m research.run_research --synthetic      # dry run on fake data (no network, no ledger)
python -m research.run_research                  # all ideas, default universe, 2016 -> now
python -m research.run_research --strategy orb --symbols SPY QQQ
python -m research.run_research --monthly-note   # "2026-12: tested 3, passed 0"
python -m pytest research -q
```

Output is one line per idea plus `tested N, passed K`. Each verdict is appended to
`research/results/ledger.jsonl` (never rewritten). A CANDIDATE also gets a one-page
markdown report with an equity-curve PNG in `research/results/reports/`.

Data is cached as Parquet under `/workspace/research-data` (outside the repo; change with
`RESEARCH_DATA_DIR` or `--data-dir`). The first full pull of 20 symbols x ~11 years of
1-minute bars takes a while (thousands of requests within the free plan's rate limit) and
roughly 1-2 GB of disk; later runs read the cache.

## What's in here

| file | role |
|---|---|
| `config.py` | settings, universe, the research-only key guard, the 15-minute embargo |
| `data.py` | download 1-min SIP bars (`adjustment='all'`), monthly Parquet cache, RTH filter (DST-aware, 13:00 half-days), 5/15/60-min + daily resampling |
| `data_check.py` | checks history depth, gaps, bars per day, half-days, SIP access, splits, delisted names; writes `DATA_CHECK.md` |
| `costs.py` | slippage + half-spread per side, SEC/TAF sell fees, short borrow |
| `backtest.py` | engine: signal on close, fill next open, stop / target / time / end-of-day exits, fixed-fractional sizing |
| `validation.py` | the pass bar above |
| `strategies/` | ORB, Harry Sharp-style bias + wick, 9 EMA / 20 SMA pullback. Rules and sources: `RULES.md` |
| `run_research.py` | the CLI |
| `ledger.py`, `report.py` | ledger and CANDIDATE reports |

## Costs used (conservative defaults)

- Liquid ETFs and large caps (the default universe): 3 bps slippage + 1 bp half-spread **per side**
  (about 0.08% round trip). Anything else: 10 + 5 bps per side.
- Commission 0. On every sale: SEC fee ~$27.80 per $1M sold and FINRA TAF $0.000166/share
  (max $8.30). Both rates change yearly; these are approximations.
- Shorts: borrow at 0.5%/yr (5% for small caps), charged for time held, minimum one hour.
- Stops fill at the stop, or at the open if the bar gaps through it. If a bar touches both
  the stop and the target, we assume the stop hit first.

## Known data gaps (free plan)

See `DATA_CHECK.md` for the measured numbers.

- **Survivorship.** Bars for dead names *are* available (TWTR, ATVI, SIVB, FRC... all return
  history), but there is no free list of what was in an index or passed a screen on a given
  date. The default universe is today's well-known names, which flatters results a bit.
  Fixed lists are used on purpose; stock-picking ideas would need a point-in-time list.
- **Ticker changes.** FB and META both return history (META's goes back to 2016). Don't
  include both in one universe.
- **Corporate actions.** `adjustment='all'` back-adjusts splits and dividends (checked on the
  AAPL 2020 split). Old prices are therefore not the exact prices traders saw. After a new
  split, delete that symbol's cache to refresh it.
- **Borrow.** No historical borrow rates or hard-to-borrow lists; a flat rate is assumed and
  every short is assumed borrowable.
- **Spreads.** Fixed bps rather than historical quotes.
- **VIX.** Not on this plan; the volatility regime uses SPY's own realized volatility.
- **Half-days.** SIP keeps printing thin trades after a 13:00 close; the RTH filter cuts them
  using the NYSE early-close rules (checked against the data).
- **No earnings calendar or news.** None of the current ideas need them.

## Rules for this folder

- No order or trading code, ever (`tests/test_no_trading_guard.py`).
- Keys only from `RESEARCH_ALPACA_KEY_ID` / `RESEARCH_ALPACA_SECRET_KEY`; never the bots' keys,
  never printed (`tests/test_credentials.py`).
- No request ever reaches into the last 15 minutes (free plan limit), enforced in `config.py`.
- No paid data. Gaps are documented above instead.
- Nothing here paper-trades a winner. A CANDIDATE goes to the user and Rook for review.
