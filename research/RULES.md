# Strategy rules used, and where they came from

Before writing these, the box was searched for the user's earlier versions of these
strategies (the 60-day 5-min tests): `/workspace` including `turtle-backtest/`,
`bot-challenge/`, `agent200/` (incl. `agent200/backtest/`), `q.py`, `n.py`, `f.py`, and a
full-text search for ORB / opening range / Harry / Sharp / wick / 9 EMA / 20 SMA in `.py`,
`.md` and `.txt` files. **No implementation of ORB, the Harry Sharp-style idea or the
9 EMA / 20 SMA pullback was found** (the hits were the turtle/regime backtests, which use a
20-day SMA for other things, and HK Connection's Luk rules). So the rules below are
mechanised from the research brief. Please compare them with what you tested and tell us
where they differ; changing a rule is a one-file edit in `strategies/`.

All three run on 5-minute RTH bars (09:30-16:00 ET, 13:00 on half-days), signal on a bar's
close, enter at the next bar's open, are flat by the close, and trade longs and shorts.

## ORB - `strategies/orb.py`
- Opening range = high/low of the first 5, 15 or 30 minutes.
- First 5-min close above the range high = long; below the range low = short. One trade a day.
- Stop: the other side of the range, or the range midpoint.
- Target: 1.5R, 2R, 3R, or none (exit at the close).
- No new entries after 11:30 ET.
- Grid: 3 ranges x 2 stops x 4 targets = 24 settings.

## Harry Sharp-style bias + wick - `strategies/harry_sharp.py`  (INTERPRETATION, review please)
- Bias from yesterday's candle: `direction` (close above open = long bias) or `midpoint`
  (close above yesterday's (high+low)/2 = long bias). Short bias mirrors.
- Level: yesterday's low (longs) / high (shorts), or today's opening price.
- Long signal: a 5-min bar between 09:35 and 15:00 ET that dips below the level and closes
  back above it, with a lower wick of at least 40/50/60% of the bar's range, on a long-bias day.
- Stop just beyond the wick (wick low minus 10% of the bar's range). Target 1.5R, 2R or 3R.
  One trade a day, flat by the close.
- Grid: 2 bias modes x 2 levels x 3 wick sizes x 3 targets = 36 settings.

## 9 EMA / 20 SMA pullback - `strategies/ema_pullback.py`
- On 5-min closes: EMA(9) and SMA(20), carried across days.
- Uptrend: EMA above SMA and both higher than 2/3/5 bars ago.
- Pullback bar: low touches the EMA (or the SMA, per setting) while closing at/above the SMA.
- Signal: the next bar is still in an uptrend and closes above the pullback bar's high.
- Stop below the pullback low (lowest low of the two bars). Target 1.5R, 2R, 3R or none.
- Entries 09:45-15:30 ET; can re-enter later the same day once flat. Shorts mirror.
- Grid: 2 touch lines x 3 slope lookbacks x 4 targets = 24 settings. The EMA/SMA lengths
  themselves are not tuned, but they are moved +/-10-20% in the robustness test.
