from datetime import date

from hk_connection import rules


def test_sizing_uses_capital_and_risk_with_25pct_cap():
    # $2,000 capital, 0.3% risk = $6; entry 50, stop 49.25 (1.5%) -> 8 shares by risk; cap 25% = $500 -> 10 shares
    assert rules.size_position(2000, 0.003, 50.0, 49.25, market_cap=20e9) == 8
    # Ultra-tight stop: he does not size up; the 25% position cap binds
    assert rules.size_position(2000, 0.003, 50.0, 49.95, market_cap=20e9) == 10
    # Small cap / unknown cap -> 20% cap
    assert rules.size_position(2000, 0.003, 50.0, 49.95, market_cap=1e9) == 8
    assert rules.size_position(2000, 0.003, 50.0, 49.95) == 8
    # Buying power binds too
    assert rules.size_position(2000, 0.003, 50.0, 49.25, market_cap=20e9, buying_power=200) == 4
    assert rules.size_position(2000, 0.003, 50.0, 50.0) == 0


def test_stop_prefers_low_of_day_then_falls_back_and_respects_caps():
    # LOD 1.2% below -> LOD
    s = rules.select_stop("long", 100, 98.8, 101, 99.5, 100.2, 99.2, 100.0, 99.0, adr=6.0)
    assert (s.price, s.basis) == (98.8, "low_of_day")
    # LOD 4% below (too wide) -> breakout bar low
    s = rules.select_stop("long", 100, 96.0, 101, 98.6, 100.2, 99.2, 100.0, 99.0, adr=6.0)
    assert (s.price, s.basis) == (98.6, "breakout_bar_low")
    # Cap is min(2.5%, 0.5*ADR): ADR 3% -> 1.5% cap, bar lows too wide -> previous bar low? (99.2 = 0.8%)
    s = rules.select_stop("long", 100, 96.0, 101, 98.0, 100.2, 99.2, 100.0, 99.0, adr=3.0)
    assert s.basis == "previous_bar_low"
    # Nothing fits -> no trade
    assert rules.select_stop("long", 100, 90, 101, 90, 101, 90, 100, 90, adr=6.0) is None
    # Short mirror: high of day
    s = rules.select_stop("short", 100, 98, 101.5, 99, 100.6, 99.5, 100.4, 101, adr=6.0)
    assert (s.price, s.basis) == (101.5, "high_of_day")


def test_entry_timeframe_and_filters():
    assert rules.entry_timeframe(5) == 1 and rules.entry_timeframe(15) == 1
    assert rules.entry_timeframe(16) == 5 and rules.entry_timeframe(60) == 5
    assert rules.entry_timeframe(61) == 15 and rules.entry_timeframe(120) == 15
    assert rules.entry_timeframe(121) is None
    assert rules.too_extended_from_lod(103.5, 100) and not rules.too_extended_from_lod(102.9, 100)
    assert rules.touches_support(99.5, [100]) and rules.touches_support(100.9, [100])
    assert not rules.touches_support(101.5, [100]) and not rules.touches_support(96.5, [100])


def test_trim_and_trail():
    assert rules.trim_qty(20, 20) == 3      # 15% of 20
    assert rules.trim_qty(5, 5) == 1        # at least one share
    assert rules.trim_qty(5, 1) == 0        # never the whole remainder
    assert rules.trim_trigger_price("long", 100, 98.5, 3) == 104.5
    assert rules.trim_trigger_price("short", 100, 101.5, 5) == 92.5
    assert rules.trail_exit_signal("long", 99.0, 100.0) and not rules.trail_exit_signal("long", 101, 100)
    assert rules.trail_exit_signal("short", 101.0, 100.0)


def test_earnings_rule():
    today = date(2026, 11, 11)
    assert rules.earnings_blocks_entry(today, date(2026, 11, 16))       # 5 days out
    assert not rules.earnings_blocks_entry(today, date(2026, 11, 17))   # 6 days out
    assert not rules.earnings_blocks_entry(today, date(2026, 11, 10))   # already reported
    assert rules.earnings_hold_action("long", 100, 108, adr=6) == "hold"
    assert rules.earnings_hold_action("long", 100, 103, adr=6) == "sell_half"
    assert rules.earnings_hold_action("long", 100, 99, adr=6) == "exit"
    assert rules.earnings_hold_action("short", 100, 97, adr=6) == "sell_half"
    # amc: act on the release day; bmo: act the trading day before
    assert rules.must_act_before_release(today, today, "amc")
    assert rules.must_act_before_release(today, date(2026, 11, 12), "bmo", date(2026, 11, 12))
    assert not rules.must_act_before_release(today, date(2026, 11, 13), "bmo", date(2026, 11, 12))


def test_losing_streak_pause_reduced_risk_and_peak_reset():
    s = rules.StreakState(equity_peak=2100)
    today = date(2026, 11, 11)
    for _ in range(9):
        rules.record_trade_result(s, -5, today)
    assert s.consecutive_losses == 9 and not rules.is_paused(s, today)
    rules.record_trade_result(s, 0, today)          # breakeven: unchanged
    assert s.consecutive_losses == 9
    rules.record_trade_result(s, -5, today)         # 10th loss
    assert rules.is_paused(s, today) and s.reduced_risk
    assert s.paused_until == "2026-11-16"           # rest of Wed + Thu, Fri -> back Mon
    assert rules.is_paused(s, date(2026, 11, 13)) and not rules.is_paused(s, date(2026, 11, 16))
    assert rules.current_risk_pct(s) == rules.REDUCED_RISK_PCT
    rules.update_equity_peak(s, 2050)               # still in drawdown: never increase risk
    assert s.reduced_risk
    rules.update_equity_peak(s, 2150)               # new equity high: back to base risk
    assert not s.reduced_risk and rules.current_risk_pct(s) == rules.BASE_RISK_PCT


def test_indicators():
    assert rules.ema([1, 1, 1], 9) == 1
    assert rules.classify_ema_list(3, 2, 1) == "lead" and rules.classify_ema_list(1, 2, 3) == "lag"
    assert rules.classify_ema_list(2, 3, 1) == "mediocre"
    assert round(rules.adr_pct([105, 110], [100, 100]), 2) == 7.5
