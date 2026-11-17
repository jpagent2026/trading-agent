# Strategy rules

These rules now follow **the user's strategy handout dated 2026-11-17** (written from the
YouTube-transcript tests, `Strategy Research Handoff for Bishop`). They replace the first-pass
interpretation, which was written before the handout existed. The handout's own code files
(`orb_backtester.py`, `harry_sharp_*.py`, `ma_trend_backtester.py`) were not available, so the
rules are built from the handout's written descriptions. Every judgement call below is a
mechanical choice, because a script cannot make a discretionary one. If a choice differs from
how the user reads the method, changing it is a one-file edit in `strategies/`.

## Common to all
- **Data / session.** Regular trading hours only (09:30-16:00 ET, 13:00 on half-days). A strategy
  decides on a bar's **close** and trades on the **next bar's open**, with one exception: the
  Harry Sharp buy-stop / sell-stop fills *inside* the bar that trades through it (a gap fills at
  the open). Everything is flat by the close (no overnight). Longs and shorts.
- **Costs.** Same cost model for all: 3 bps slippage + 1 bp half-spread per side, SEC/TAF fees on
  sales, 0.5%/yr borrow on shorts. If a bar touches both stop and target the stop wins.
- **The pass bar is unchanged** (walk-forward, >= 300 OOS trades, bootstrap CI above 0, years and
  market regimes, parameter nudges, random-entry benchmark; see `README.md`).
- **Universe: futures stand-ins.** The handout tested futures (NQ, ES, YM, GC), which Alpaca's free
  stock data does not include. The default universe therefore holds the ETF stand-ins
  **QQQ (for NQ), SPY (ES), DIA (YM) and GLD (GC)**, plus the handout's stocks **NVDA, AAPL and
  TSLA**, plus other liquid ETFs and large caps (21 symbols in all; see `config.py`). ETFs only
  trade the cash session and have a small tracking gap to futures, so results are close
  analogues, not identical to the futures tests.
- **Grids.** Each idea's parameter grid is kept under 100 settings, because the walk-forward picks
  the best setting per fold and the robustness test then re-runs nudged copies of the winner.
  Unused knobs are not nudged (for example the EMA length when the bias mode is not 'ema').
- **Not mechanisable, and so not in any test:** stock/catalyst selection, "zone quality" and
  "clean path of least resistance", reading structure by eye, and session context. The handout itself
  says these are probably doing the heavy lifting in the original videos. If a strategy fails here,
  that is evidence about the *mechanical* part only.

## 1. ORB - `strategies/orb.py`
Rules (handout, Strategy 1)
- Opening range = high/low of the first **5 or 15** minutes (**30** kept as an extra variant).
- **Entries only in the first 2 hours**: an entry bar must start before 11:30 ET (the last entry
  bar is 11:25-11:30). One trade a day.
- **Breakout** entry: the first bar that **closes** beyond the range is the confirmation candle
  (close above the range high = long, below the range low = short). Enter at the next bar's open.
- **Retest** entry: a bar first closes beyond the range (the breakout). Later a bar trades back to
  the range edge (long: its low is at or under the range high) and **closes beyond it again**: that
  bar is the confirmation candle. Enter at the next bar's open. A retest bar that closes back inside
  the range is not a confirmation.
- **Stop:** the other side of the opening range, or beyond the confirmation candle (its low for
  longs, its high for shorts).
- **Target:** 1R, 2R (extra) or hold to the end of the day (the handout's "hold toward high/low
  of day"; the exit is the close).
- Trades whose stop is closer than 0.05% of price are skipped (costs would swamp 1R); not tuned.

Grid: 3 ranges (5/15/30) x 2 entry modes x 2 stops x 3 targets (1R / 2R / EOD) = **36 settings**.

Judgement calls: "confirmation candle" = a bar *closing* beyond the range (the handout's wording);
"retest" needs an earlier breakout close first; "hold to HOD/LOD" is implemented as hold to the close.

Handout's prior result: thin and regime-sensitive on ~60 days of NQ/ES-style data; "not durable".

## 2. Harry Sharp-style - `strategies/harry_sharp.py`
Rules (handout, Strategy 2)
- **Bias, from completed higher-timeframe candles of the previous session:**
  - `strict`: the previous **daily** candle and the previous **4H** candle agree (a candle is "up"
    when close > open). Both up = long bias, both down = short bias, disagreement = no trade.
  - `daily`: previous daily candle only.
  - `ema`: previous daily close and previous 4H close are both above (below) their EMA(20).
- **4H bars from 5-min data:** two RTH blocks per session, **09:30-13:30 and 13:30-16:00** (a
  half-day has one block). At the open, the "previous 4H candle" is yesterday's 13:30-16:00 block.
  Because that block ends at the daily close, its *close* equals the daily close, so the strict
  bias really compares the **daily direction with the afternoon leg's direction**, not two copies
  of the same number. (A 24-hour futures 4H grid with 18:00 anchoring would differ.)
- **Market-structure filter** (optional): the last N (=3) completed daily candles make higher
  highs *and* higher lows in a row for longs (lower highs and lows for shorts). "Structure" is
  deliberately a daily-candle rule: an intraday swing-point rule needs a pivot definition the
  handout does not give.
- **Range:** the high/low of the previous 6 five-minute bars of today (at least 3 bars, so the
  first possible wick bar is 09:45).
- **Liquidity wick (spring):** a bar that trades *below* the range low and closes back *above* it,
  with a lower wick >= `wick_ratio` of the bar's full range (shorts mirror with the range high and
  the upper wick). The handout's sweep: **0.10, 0.20, 0.30, 0.40**.
- **Structure break + close:** within 4 bars after the wick bar, a bar **closes above the range
  high** (shorts: below the range low) - the break bar.
- **Entry:** a **buy-stop at the break bar's high** (sell-stop at its low), live for 3 bars. It
  fills in the first bar that trades through it (a gap fills at the open).
- **Invalidation:** cancelled if a bar closes back inside the range (at or below the broken range
  high) before the order fills, or if the 3 bars pass.
- **Stop:** beyond the wick bar's extreme (its low for longs, its high for shorts).
- **Targets / management:** `fixed_1r` = take profit at 1R; `be_1r` = once +1R is reached the stop
  moves to breakeven (from the next bar) and the target is 2R; `trail_1r` = once +1R is reached the
  stop trails 1R behind the best price (previous bars), no target; `session_end` = no target, hold
  to the close.
- **Session:** only the first 2 hours of the NY session (the fill must come before 11:30 ET). One
  trade a day, flat by the close.

Grid: 3 bias modes x 2 structure settings (off / 3 days) x 4 wick ratios x 4 exits = **96 settings**.

Judgement calls: the 4H blocks above; "range" = last 6 five-minute bars; "structure" = daily
higher highs/lows; "structure break" = a close above the range high; stop under the wick, not under
the break bar; "move to BE at 1R" modelled as BE + a 2R target because the engine is all-in /
all-out and cannot take a half off at 1R. "Clean zone / path of least resistance" is not mechanised.

Handout's prior results: the headline **75-79% win rate on NQ (12-14 trades)** used strict bias +
simple breakout + a 0.15-0.20 wick + first 2h + 1R. It was **fragile**: the first half of the sample
was ~62.5%, the second half 100%, and the 2-year 1-hour adaptation was ~50% with average R near or
below 0 on NQ/ES/YM. That is exactly the "tiny, lucky sample" pattern the pass bar is built to
reject; this harness needs hundreds of out-of-sample trades across years and regimes. Note that
this setup is rare, so reaching 300 OOS trades may itself be hard (the idea can fail `min_trades`).

## 3. 9 EMA / 20 SMA / 200 SMA pullback - `strategies/ema_pullback.py`
Rules (handout, Strategy 3), on **5-minute bars**
- **Trend:** EMA(9) above SMA(20) and both rising over 3 bars (shorts mirror).
- **Entry zone = between the 9 EMA (top) and the 20 SMA (bottom).** A pullback bar trades down into
  the zone (`touch='ema'`: low <= EMA, the shallow edge; `touch='sma'`: low <= SMA, the deeper edge)
  and still **closes at or above the SMA**. The next bar is the signal bar: still in the trend and
  **closing above the pullback bar's high**; enter at the next open.
- **Slope filter on the 20 SMA:** percent change per bar (averaged over 3 bars) must lie inside a
  band: at least `slope_min` (skips a flat 20 SMA) and at most `slope_max` = 0.10 % per 5-min bar
  (skips an over-steep, exhausted one). The "45 degrees" in the video has no fixed numeric
  meaning; the band is a proxy. Percent is not volatility-adjusted, so a fast stock like TSLA
  sits near the top of the band more often than SPY.
- **Extension filter:** skip when the signal bar's close is more than **2 x ATR(14)** from the 20 SMA
  (an ATR rule; a percent-from-SMA version is not implemented).
- **Skip the first pullback after a steep, accelerating move** (`skip_first`): a trend "leg" starts
  when the trend condition turns on and ends when it turns off. The leg's *first* pullback is skipped if,
  just before it began, price had moved more than **4 x ATR over 12 bars** in the trade direction and
  the last 6 bars moved more than the 6 before. Later pullbacks of the same leg are allowed.
- **200 SMA (higher-timeframe level):** the **200-day SMA of completed sessions' closes** (an
  hourly 200 is built in as `ma200_frame='hourly'` but is not in the grid; weekly is not built).
  `ma200_mode='trend'` takes longs only above it and shorts only below it. Exit `ma200` targets the
  200 SMA itself and requires it to be at least 1R ahead - that means longs *below* the 200, so the
  trend filter is ignored for that exit.
- **Stop:** below the pullback low (the lowest low of the pullback bar and the signal bar).
- **Targets:** 1R, 2R, the 200 SMA, or hold to the close.
- Entries 09:45-15:30 ET; several trades a day are allowed once flat.

Grid: 2 touches x 2 ma200 modes x 4 exits x 2 slope minimums (0.005 / 0.015 % per bar) x 2
skip-first settings = 64, minus 8 duplicates (200-SMA exit ignores the trend filter) = **56 settings**.

**Timeframe-rotation variant** - `ema9_sma20_rotation` (runs on **1-minute** data; simplified):
**2-minute bars before 10:00 ET, 5-minute bars 10:00-12:00, 15-minute bars after 12:00.** Each
segment uses its own bars' EMA/SMA and the same rules and grid; a signal is acted on at the next
1-minute open after the aggregated bar closes. The slope band scales with bar size. Not
implemented: the handout's multi-timeframe *confluence* (e.g. the 20 on 2-min aligning with the 9 on
5-min) and the 1-minute option for 09:30-10:00.

Not mechanised: catalyst / stock selection, zone quality, and "more touches = more reliable".

Handout's prior result (60-day 5-min, 1R, QQQ SPY NVDA AAPL TSLA): QQQ -0.19R (PF 0.65), SPY +0.06R
(PF 1.14), NVDA -0.02R, AAPL -0.07R, TSLA -0.44R (PF 0.39); variants stayed negative on QQQ; only
SPY was mildly positive. With only 38-47 trades per symbol, most of those averages cannot be told apart
from zero; this harness gives each idea years of data instead.
