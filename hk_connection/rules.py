"""Martin Luk's rules as fixed, testable functions.

Every constant here is a codified snapshot of his stated 2025-2026 rules (Oct-Nov 2026).
Where he uses judgment, the closest fixed rule from his own words is used; each such
choice is listed with its source in hk_connection/DECISIONS.md (the D-numbers below).
Nothing in this module talks to the network.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

# ---------------------------------------------------------------- risk and sizing
BASE_RISK_PCT = 0.003          # ~0.3% of equity per trade since Dec 2025 (spec §4)          D1
REDUCED_RISK_PCT = 0.002       # "0.25-0.2% after losses": restart smaller (spec §1, §6)     D12
POSITION_CAP_PCT = 0.25        # "most positions around 25 to 30%... at least around 25%"     D3
SMALL_CAP_POSITION_CAP_PCT = 0.20  # "small or micro caps... 20% or even less"                D3
SMALL_CAP_MAX_MCAP = 5_000_000_000  # his small-cap watchlist is "< $5B" (spec §2)
MAX_CONCURRENT_POSITIONS = 11  # his stated peak count (spec §4)                              D14

# ---------------------------------------------------------------- stops
MAX_STOP_PCT = 0.025           # "rarely... wider than 3% or even 2.5%" (spec §1, §4)        D4
MAX_STOP_ADR_FRACTION = 0.5    # "never more than half the ADR" (spec §1, §4)
ADR_LOOKBACK = 20              # lookback UNKNOWN in spec; Qullamaggie-style 20-day           D5

# ---------------------------------------------------------------- entries
MAX_ABOVE_LOD_PCT = 0.03       # "if it's up for more than 3% from the low of day... avoiding it"
SUPPORT_TOUCH_ABOVE = 0.01     # flush "into" support: session low within +1% of the level    D7
SUPPORT_TOUCH_BELOW = 0.03     # ...and not more than 3% through it                           D7
MIN_ADR_PCT = 4.0              # "ADR above 4% or 5%" counts as a normal fast mover            D6
ENTRY_WINDOW_MINUTES = 120     # "most entries come in the first hour, maybe two"             D8
ONE_MIN_PHASE_END = 15         # 1-minute bars during the first ~15 minutes
FIVE_MIN_PHASE_END = 60        # 5-minute bars from ~15 to 60 minutes, 15-minute after

# ---------------------------------------------------------------- exits
TRIM_FRACTION = 0.15           # "every time I think this stock is extended I sell 15%"       D9
TRIM_R_LEVELS = (3.0, 5.0)     # "sell partial into strength when it's up 3R or 5R"           D9
DEFAULT_TRAIL_EMA = 9          # "close my whole position at the first close below the 9 EMA" D10

# ---------------------------------------------------------------- earnings
EARNINGS_NO_ENTRY_DAYS = 5     # "95% of the time no new entry within 5 days of earnings"     D11

# ---------------------------------------------------------------- losing streak
LOSING_STREAK_LENGTH = 10      # "after ~10 straight losses"                                  D12
LOSING_STREAK_PAUSE_DAYS = 2   # "stop for 1-2 days"                                          D12

# ---------------------------------------------------------------- GTC
GTC_REFRESH_AFTER_DAYS = 80    # Alpaca auto-cancels GTC orders after 90 days; refresh early  D15

# Index ETFs whose short signals are expressed by BUYING a leveraged inverse ETF       D13
INVERSE_ETF_MAP = {"QQQ": "SQQQ", "SPY": "SPXU", "IWM": "TZA"}


# ================================================================ indicators
def ema_series(values: Sequence[float], period: int) -> list[float]:
    """Standard EMA (alpha = 2/(n+1)), seeded with the first value."""
    out: list[float] = []
    if not values:
        return out
    k = 2.0 / (period + 1.0)
    e = float(values[0])
    for v in values:
        e = float(v) * k + e * (1.0 - k)
        out.append(e)
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    s = ema_series(values, period)
    return s[-1] if s else None


def adr_pct(highs: Sequence[float], lows: Sequence[float], lookback: int = ADR_LOOKBACK) -> Optional[float]:
    """Average daily range in percent: mean(high/low - 1) * 100 over the lookback."""
    n = min(len(highs), len(lows), lookback)
    if n == 0:
        return None
    hs, ls = list(highs)[-n:], list(lows)[-n:]
    vals = [(h / l - 1.0) * 100.0 for h, l in zip(hs, ls) if l and l > 0]
    return sum(vals) / len(vals) if vals else None


def classify_ema_list(e9: float, e21: float, e50: float) -> str:
    """His watchlists: Lead 9>21>50, Lag 9<21<50, otherwise Mediocre (spec §2)."""
    if e9 > e21 > e50:
        return "lead"
    if e9 < e21 < e50:
        return "lag"
    return "mediocre"


# ================================================================ entries
def entry_timeframe(minutes_since_open: float) -> Optional[int]:
    """Which bar size his trigger uses at this point of the session, or None if outside the window."""
    if minutes_since_open < 0 or minutes_since_open > ENTRY_WINDOW_MINUTES:
        return None
    if minutes_since_open <= ONE_MIN_PHASE_END:
        return 1
    if minutes_since_open <= FIVE_MIN_PHASE_END:
        return 5
    return 15


def touches_support(session_low: float, levels: Iterable[float]) -> bool:
    """Long: the day's low flushed into (or slightly through) a support level."""
    for lvl in levels:
        if lvl and lvl > 0 and lvl * (1 - SUPPORT_TOUCH_BELOW) <= session_low <= lvl * (1 + SUPPORT_TOUCH_ABOVE):
            return True
    return False


def touches_resistance(session_high: float, levels: Iterable[float]) -> bool:
    """Short mirror: the day's high bounced into (or slightly through) a resistance level."""
    for lvl in levels:
        if lvl and lvl > 0 and lvl * (1 - SUPPORT_TOUCH_ABOVE) <= session_high <= lvl * (1 + SUPPORT_TOUCH_BELOW):
            return True
    return False


def too_extended_from_lod(entry: float, lod: float) -> bool:
    return lod > 0 and entry > lod * (1 + MAX_ABOVE_LOD_PCT)


def too_extended_from_hod(entry: float, hod: float) -> bool:
    return hod > 0 and entry < hod * (1 - MAX_ABOVE_LOD_PCT)


def max_stop_pct(adr: Optional[float]) -> float:
    """Stop width cap: <= 0.5 x ADR and <= 2.5%."""
    cap = MAX_STOP_PCT
    if adr is not None and adr > 0:
        cap = min(cap, MAX_STOP_ADR_FRACTION * adr / 100.0)
    return cap


def _floor_cent(x: float) -> float:
    return math.floor(x * 100 + 1e-9) / 100.0


def _ceil_cent(x: float) -> float:
    return math.ceil(x * 100 - 1e-9) / 100.0


@dataclass
class StopChoice:
    price: float
    basis: str
    pct: float


def select_stop(direction: str, entry: float, lod: float, hod: float, breakout_bar_low: float,
                breakout_bar_high: float, prev_bar_low: float, prev_bar_high: float,
                session_open: float, adr: Optional[float]) -> Optional[StopChoice]:
    """His stop order of preference (D4): low of day (60-70% of the time); if that is too wide,
    the breakout bar's low, then the previous bar's low, then the day's open. Shorts mirror with highs.
    Returns None when no candidate fits under the width cap (then the trade is skipped)."""
    cap = max_stop_pct(adr)
    if direction == "long":
        cands = [("low_of_day", lod), ("breakout_bar_low", breakout_bar_low),
                 ("previous_bar_low", prev_bar_low), ("open", session_open)]
        for basis, raw in cands:
            if not raw or raw <= 0:
                continue
            px = _floor_cent(raw)
            if px >= entry:
                continue
            pct = (entry - px) / entry
            if 0 < pct <= cap:
                return StopChoice(px, basis, pct)
    else:
        cands = [("high_of_day", hod), ("breakout_bar_high", breakout_bar_high),
                 ("previous_bar_high", prev_bar_high), ("open", session_open)]
        for basis, raw in cands:
            if not raw or raw <= 0:
                continue
            px = _ceil_cent(raw)
            if px <= entry:
                continue
            pct = (px - entry) / entry
            if 0 < pct <= cap:
                return StopChoice(px, basis, pct)
    return None


def position_cap_pct(market_cap: Optional[float]) -> float:
    if market_cap is None or market_cap < SMALL_CAP_MAX_MCAP:
        return SMALL_CAP_POSITION_CAP_PCT
    return POSITION_CAP_PCT


def size_position(capital: float, risk_pct: float, entry: float, stop: float,
                  market_cap: Optional[float] = None, buying_power: Optional[float] = None) -> int:
    """Shares = min(capital*risk / (entry-stop), capital*cap / entry, buying_power / entry).
    He does not size up for an ultra-tight stop: the position cap binds (spec §4). Whole shares only
    (GTC stops and shorts can't be fractional at Alpaca)."""
    if capital <= 0 or entry <= 0 or stop <= 0:
        return 0
    rps = abs(entry - stop)
    if rps <= 0:
        return 0
    by_risk = math.floor(capital * risk_pct / rps)
    by_cap = math.floor(capital * position_cap_pct(market_cap) / entry)
    qty = min(by_risk, by_cap)
    if buying_power is not None:
        qty = min(qty, math.floor(max(buying_power, 0.0) / entry))
    return max(0, int(qty))


# ================================================================ exits
def trim_qty(initial_qty: int, open_qty: int) -> int:
    """15% of the original position, at least 1 share, never the whole remainder."""
    q = max(1, int(math.floor(initial_qty * TRIM_FRACTION)))
    if q >= open_qty:
        return 0
    return q


def trim_trigger_price(direction: str, entry: float, stop: float, r_multiple: float) -> float:
    r = abs(entry - stop)
    return entry + r_multiple * r if direction == "long" else entry - r_multiple * r


def trail_exit_signal(direction: str, daily_close: float, trail_ema_value: float) -> bool:
    """First daily close below the trail EMA exits a long; first close above exits a short (mirror)."""
    if direction == "long":
        return daily_close < trail_ema_value
    return daily_close > trail_ema_value


# ================================================================ earnings
def earnings_blocks_entry(today: date, next_earnings: Optional[date]) -> bool:
    """No new entry when earnings fall within the next 5 calendar days (inclusive of today)."""
    if next_earnings is None:
        return False
    delta = (next_earnings - today).days
    return 0 <= delta <= EARNINGS_NO_ENTRY_DAYS


def earnings_hold_action(direction: str, entry: float, last: float, adr: Optional[float]) -> str:
    """Holding into a release: 'hold' with enough cushion, 'sell_half' with some cushion,
    'exit' with none. Cushion needed = 1 x ADR% of open profit (D11; his COIN example 5-8%)."""
    gain_pct = ((last - entry) / entry * 100.0) if direction == "long" else ((entry - last) / entry * 100.0)
    need = adr if adr and adr > 0 else MIN_ADR_PCT
    if gain_pct >= need:
        return "hold"
    if gain_pct > 0:
        return "sell_half"
    return "exit"


def must_act_before_release(today: date, earnings_day: date, hour: Optional[str],
                            next_trading_day: Optional[date] = None) -> bool:
    """True on the last regular session before the release: the release day itself if it's after
    the close ('amc'), otherwise the trading day before it (bmo / during / unknown)."""
    if hour == "amc":
        return today == earnings_day
    if next_trading_day is not None:
        return today < earnings_day <= next_trading_day
    return today < earnings_day <= today + timedelta(days=1)


# ================================================================ losing streak
@dataclass
class StreakState:
    consecutive_losses: int = 0
    paused_until: Optional[str] = None   # ISO date; no new entries before this date
    reduced_risk: bool = False
    equity_peak: float = 0.0


def add_trading_days(start: date, n: int) -> date:
    d = start
    added = 0
    while added < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def record_trade_result(state: StreakState, pnl: float, today: date) -> StreakState:
    """Update the streak after a closed trade. 10 straight losses -> pause 2 trading days,
    then restart at reduced risk (D12). A winner resets the count; breakeven leaves it unchanged."""
    if pnl < 0:
        state.consecutive_losses += 1
        if state.consecutive_losses >= LOSING_STREAK_LENGTH:
            # Pause the rest of today plus the next LOSING_STREAK_PAUSE_DAYS trading days.
            state.paused_until = add_trading_days(today, LOSING_STREAK_PAUSE_DAYS + 1).isoformat()
            state.reduced_risk = True
            state.consecutive_losses = 0
    elif pnl > 0:
        state.consecutive_losses = 0
    return state


def update_equity_peak(state: StreakState, equity: float) -> StreakState:
    """Never increase risk in a drawdown: the reduced risk only lifts at a new equity high."""
    if equity > state.equity_peak:
        if state.equity_peak > 0 and state.reduced_risk:
            state.reduced_risk = False
        state.equity_peak = equity
    return state


def is_paused(state: StreakState, today: date) -> bool:
    return bool(state.paused_until) and today < date.fromisoformat(state.paused_until)


def current_risk_pct(state: StreakState) -> float:
    return REDUCED_RISK_PCT if state.reduced_risk else BASE_RISK_PCT
