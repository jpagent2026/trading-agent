# HK Connection: spec mapping and decisions

Source spec: `LUK_METHOD_SPEC.md` (compiled Tue Nov 10, 2026; 249 lines), plus the TraderLion talk transcript
[S1] (`VKNEJA5r8zw.txt` / `tl_para.txt`, timestamps below refer to it). S-numbers are the spec's sources:
[S1] TraderLion podcast transcript, [S2] his 2025 guest post, [S3] his stream Q&A collected on martinluk.com
(a fan site, edited answers linked to his streams), [S10]/[S11] secondary write-ups.

Snapshot: his rules as stated Oct–Nov 2026. He says himself that some of his method is judgment ("I actually
don't have a very strict sell rule" [S1 ~1:06]) and that it is still evolving, so every place he uses judgment
is replaced by the closest fixed rule from his own words. Each one is listed below as D1–D20. The constants
are in `rules.py`, tagged with the same D-numbers.

## What is copied as stated (no judgment involved)

| His rule | Where in the code | Source |
|---|---|---|
| Watchlists: Lead 9>21>50 EMA, Mediocre mixed, Lag 9<21<50 | `rules.classify_ema_list`, `scanner.full_metrics` | spec §2 [S2, S3] |
| Leader screen: mcap > $100M, 10-day avg vol > 500K, 1-week price×volume > $300M, sorted by 1-month % | `scanner.leader_screen` | spec §2 [S3 screeners, Oct 28, 2026] |
| Potent screen: previous day's top % gainers, price×volume > $50M | `scanner.potent_screen` | spec §2 [S2, S3] |
| Premarket screen: sorted by premarket % change, price×volume > $50M | `scanner.premarket_screen` | spec §2 [S3] |
| Weekly chart check: not long below the weekly 9 EMA, not short above it | `scanner.long_qualifies/short_qualifies` | spec §3 [S1 ~92:32–97:55; S3 14 Jul 2026] |
| Trigger: first lower-timeframe breakout of the previous bar's high after a flush into support; 1-min bars for the first ~15 min, 5-min to ~60 min, 15-min later | `rules.entry_timeframe`, `Engine.evaluate_entry` | spec §3a [S1 ~117:00–118:15; S3 24 Jun 2026] |
| "I don't usually buy the dip on the way down": never buys the bar that makes the low | `Engine.evaluate_entry` | spec §3a [S3] |
| Market orders (~90% of the time) | `broker.build_entry_order` | spec §3a [S3 14 Jul, 6 Aug 2026] |
| Skip if more than 3% above the low of day | `rules.too_extended_from_lod` | [S1 ~24:33] "if it's up for more than 3% from the low of day then I'm just avoiding it" |
| Pullback short = mirror image: bounce into declining EMAs, break of the previous bar's low, stop at the high of day | `Engine.evaluate_entry`, `rules.select_stop` | spec §3e [S1 ~126:35–127:08; S3 28 Oct 2026] |
| Re-entries are independent trades, no maximum count | re-arms on every new low of day in support | spec §3f [S3 21 Sep 2026; S11] |
| Hard stop placed with the entry, stop-**market**, never stop-limit | OTO entry with a stop-market leg, GTC | spec §4 [S2; S3 14 Jul 2026] |
| Stop never wider than half the ADR | `rules.max_stop_pct` | spec §4 [S2] |
| "I rarely manage my stops… rarely raise them to breakeven" | stops are never moved (only re-placed at the same price for the remaining shares) | spec §4 [S3 10 Jun 2026] |
| Size = risk / (entry − stop), anchored to a position-size cap; "does not size up for an ultra-tight stop" | `rules.size_position` | spec §4 [S1 ~16:41–18:05, ~33:36–34:43] |
| Margin: no margin cap; the per-trade risk limit is the control | only Alpaca buying power limits gross exposure | spec §4 [S1 ~40:57–43:17] |
| First trim ~15–20% into strength; trail the 9 EMA; exit on the first daily close below the 9 EMA | `manage_trims`, `eod_trail`, `open_job` | spec §5 [S1 ~1:06, ~149:07, ~172:26] |
| Earnings: no new entry within 5 days; holding without enough cushion → sell at least half or exit | `rules.earnings_blocks_entry`, `Engine.earnings_job` | spec §6 [S3 26 Oct 2026] |
| Losing streak: after ~10 straight losses stop 1–2 days, restart smaller; never increase risk in a drawdown | `rules.record_trade_result`, `update_equity_peak` | spec §6 [S3 26 May, 28 Oct 2026; S2] |
| Multiple entries a day (no one-buy-a-day limit) | no daily entry cap | spec §8d [S1 ~44:41]; user decision |

## Decisions where he uses judgment (the closest fixed rule from his own words)

**D1. Risk per trade = 0.3% of capital.** Spec §4: "Base about 0.3% of equity now (0.5% in 2025, 0.5–1% in
2024). Less when trades aren't working; more with high conviction or a big cushion." The raises (conviction,
cushion, "1–2% when chasing in a hot market") are judgment and are not implemented, so risk never goes above 0.3%.

**D2. Capital = live account equity, with an optional CAPITAL_BASE override.** He sizes off equity (spec §4
"Shares = (equity × risk%) / (entry − stop)"). By default the bot reads `equity` from Alpaca's account endpoint at
each entry. If the `CAPITAL_BASE` env var is set (and not empty), sizing uses that number instead. Gross exposure
is still limited by the account's real `buying_power`, so both limits always apply.

**D3. Position cap = 25% of capital; 20% for small caps (< $5B) or unknown market cap.** [S1 ~16:41–18:05]:
"Most positions are around 25 to 30%… at least around maybe 25%… small or micro caps… 20% or even less."
25% is the "at least" anchor. Market cap comes from Finnhub; when it's unknown (no key, an ETF, a lookup error)
the 20% small-cap cap applies. The ">35% for slow names" exception is judgment and is not implemented.

**D4. Stop: low of day first; if wider than the cap, the breakout bar's low, then the previous bar's low, then
the day's open; cap = min(2.5%, 0.5 × ADR). No stop fits → no trade.** [S1 ~118:20–119:50]: "set the stop either
at the low of day… maybe 60 to 70% at the low of day. But sometimes the stop may be too wide… Then I may set it to
the low of the breakout bar or the low of the previous bar." [S1 ~179:00] "sometimes I'm just setting a stop at
the opening price". Width: "Rarely… wider than 3% or even 2.5%" [S1 ~18:20] and [S1 ~132:13] "I didn't put it at
the low of day because the stop is too wide… more than 2%". The 2.5% end of his range is used. Long stops are
rounded down to the cent and short stops up, so rounding never tightens the stop.

**D5. ADR = 20-day average of (high/low − 1).** The spec says his ADR lookback is UNKNOWN (§8b.6); 20 days is the
common Qullamaggie-style setting he learned from ("80% of my trading comes from him" [S2]).

**D6. Minimum ADR 4% for stocks.** Spec §2: historically "ADR > 5% (not a hard rule)", and in 2026 "ADR above 4% or
5%" counts as a normal fast mover. 4% is the lower end. Index ETFs used for inverse-ETF shorts (D13) are exempt
because he trades them as slow names.

**D7. "Flush into support" = the day's low within +1% above to −3% below one of his levels.** The levels are the
daily 9/21/50/150 EMAs (as of the prior close), an AVWAP proxy, and the hourly 9/21 EMA [S1 ~116:44]. The AVWAP
anchor is judgment (spec §8c). The proxy anchors at the highest high of the last 20 daily bars (spec §8b.6
suggests "the highest high of the last N-day base"). The −3% floor mirrors his 3%-from-low-of-day rule.
Confluence quality and clean-vs-choppy are judgment and are not scored.

**D8. Entries only in the first 120 minutes (9:31–11:30 ET).** Spec §3a: "most entries come in the first hour,
maybe two. Midday entries are occasional. He does not trade the close." Trims, stops and exits run all session.

**D9. Trims: sell 15% of the original position at +3R, and another 15% at +5R.** [S1 ~1:06]: "I would sell
partial into strength when it's up 3R or 5R." [S1 ~149:07]: "every time I think this stock is extended I sell 15%."
[S1 ~172:17]: "I sell some into strength here and then I sell more into strength here and then I close my whole
position at the first close below the 9 EMA." A trim fires when a 1-minute bar closes at or beyond the R level.
Each trim is a market order, done as cancel stop → sell → re-place the stop for the remaining shares (Alpaca won't
let you sell shares a stop is holding, and OTO legs can't be replaced; spec §8b.4). The trim is at least 1 share
and never the last share. His discretionary choices are not implemented: skipping trims in a strong market,
selling everything into strength, and "sell three of five longs".

**D10. Trail: exit on the first daily close below the 9 EMA (above it for shorts), at the next open.** Spec §5:
"Ride the trend with the 9 EMA (or the 21 EMA) and exit on the first daily close below the 9 EMA."
`HK_TRAIL_EMA=21` switches to the 21 EMA. The spec gives no rule for when he uses the 21, so 9 is the default. The
check runs at 16:10 ET; the exit (cancel stop → market order) goes in at 9:30:30 ET the next session (spec §8b.4:
"after the close, if close < 9 EMA, exit the next session"). The "half stop under the 9 EMA candle" variant is not
used; the plain close rule is the one he states as a rule.

**D11. Earnings.** No new entry when the next earnings date is 0–5 calendar days away (Finnhub calendar). Holding
into a release: on the last session before it (the release day for after-close reports, the day before for
pre-market or unknown times), at 15:30 ET: open profit ≥ 1× ADR% → hold; some profit but less → sell half
(rounded up; "sell at least half"); no profit → exit. The spec says the cushion "depends on ADR and past earnings
moves (e.g. 5–8% for COIN)". 1× ADR is the closest fixed proxy; past earnings moves are not modelled. If the
calendar can't be fetched, new **stock** entries are blocked (the rule fails closed) while ETFs still trade. The
"95% of the time" exceptions are not implemented.

**D12. Losing streak: 10 straight losing trades → no entries for the rest of that day plus the next 2 trading
days, then risk drops to 0.2% until equity makes a new high.** Spec §6: "After ~10 straight losses: stop for 1–2
days, then restart smaller with only high-confidence entries. Never increase risk in a drawdown." 2 days is the
safer end; 0.2% is the low end of "0.25–0.2%" (spec §1, §7). Risk only returns to 0.3% at a new equity high
(account equity at the 16:10 ET check), so it is never raised during a drawdown. A winner resets the count; a
breakeven trade leaves it unchanged. "High-confidence only" is judgment and is not implemented.

**D13. Shorts and inverse ETFs.** Stocks on the Lag list are shorted directly (Alpaca must report them shortable and
easy to borrow). Spec §3e lists the instruments as "the stock, leveraged inverse ETFs (e.g. SQQQ), or UVXY". The
fixed rule here: a pullback-short signal on an index ETF (QQQ, SPY, IWM) is expressed by **buying** its leveraged
inverse ETF (SQQQ, SPXU, TZA), with the stop at the inverse ETF's own low of day. UVXY ("for fast flushes") is
judgment and is not traded.

**D14. One direction at a time, at most 11 positions.** "I'm not good at it" (trading both sides) [S3]; flipping in
chop caused the Dec 2025 drawdown [S1 ~50:44]. So no new long while any short-bias trade is open, and vice versa.
11 is his stated peak count of open positions [S1 ~43:49–44:25].

**D15. GTC 90-day expiry.** Alpaca auto-cancels GTC orders after 90 days [S18]. At 16:10 ET each day, any stop older
than 80 days, or no longer live (expired or cancelled outside the bot), is cancelled and re-placed at the same
price for the open quantity.

**D16. Universe and selection.** "The screeners… don't tell me which stocks to trade… I'm the one who decides" [S3].
His final pick is judgment. Fixed rule: the day's watchlist is up to 25 longs (Leader screen by 1-month %, then
Potent, then Premarket) that pass his Lead-list + rising EMAs + ADR + weekly checks, and up to 25 shorts (the
mirror: worst 1-month %, biggest losers, biggest premarket losers) that pass the Lag-list checks. Every name on it
can trigger. Theme detection and relative-strength judgment are not modelled. The Potent screen's EMA, volatility
and change-from-open thresholds are unknown (spec §2), so only its known parts are used.

**D17. Liquidity.** He "prefers" ≥ $500M/day or ≥ $10B cap but "still trades liquid high-ADR names below that"
(spec §2). Only the screens' own floors (Leader $300M/week, Potent/Premarket $50M) are enforced.

**D18. Setups not automated.** ORH/breakout entries (he "shifted away from" them [S3 24 Jul 2026]), EPs (criteria
"not stated by Luk", spec §3c), and parabolic long/short (rare, extreme conditions) are not automated. Pullback
long/short were "over half" of 2025 trades and the main setup in 2025–26 (spec §3a).

**D19. No market-regime switch.** Spec §6: "No single indicator… DISCRETIONARY synthesis", and "A mechanical 'QQQ vs
21 EMA' switch… is not attributable to Luk." The individual-stock EMA lists and the weekly check are the filter.

**D20. Trigger timing.** He buys "at the breakout rather than seconds later". The bot gets 1-minute bars, so it acts
at the close of the first 1-minute bar that takes out the previous bar's high (up to ~1 minute late). The planned
entry used for sizing is that bar's close.

## Known limitations

- Paper fills don't simulate slippage, queue position or impact (spec §8b.7). With tight 1–2% stops, paper results
  will look better than live trading would.
- Partial-sell P&L in the ledger uses the triggering bar's close, not the actual fill price.
- Hourly EMAs come from Alpaca's clock-aligned hourly bars (9:00, 10:00, …); the intraday 60-minute bars are
  aligned to 9:30. This is a small approximation.
- Market cap filters need `FINNHUB_API_KEY`. Without it, market cap is treated as unknown (passes the $100M screen
  and gets the 20% cap).
