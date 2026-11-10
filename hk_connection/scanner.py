"""His screens and watchlists, as pure functions over daily bars (spec §2, §3).

Input bars: per symbol, a chronological list of dicts with keys t (date or datetime), o, h, l, c, v.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

from . import rules

# Leader screen (weekly, TradingView): mcap > $100M, 10-day avg volume > 500K, 1-week price x volume > $300M,
# sorted by 1-month % performance (spec §2, from his Oct 28, 2026 stream screenshot).
LEADER_MIN_MCAP = 100_000_000
LEADER_MIN_AVG_VOL_10D = 500_000
LEADER_MIN_DOLLAR_VOL_1W = 300_000_000
# Potent (daily) and Premarket screens: price x volume > $50M (spec §2).
POTENT_MIN_DOLLAR_VOL = 50_000_000
PREMARKET_MIN_DOLLAR_VOL = 50_000_000


def _d(t: Any) -> date:
    if isinstance(t, datetime):
        return t.date()
    if isinstance(t, date):
        return t
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")).date()


def light_metrics(bars: list[dict]) -> Optional[dict]:
    """Cheap metrics used for the whole-market first pass (needs ~25 daily bars)."""
    if len(bars) < 2:
        return None
    c = [float(b["c"]) for b in bars]
    v = [float(b["v"]) for b in bars]
    last, prev = c[-1], c[-2]
    m = {
        "close": last,
        "date": _d(bars[-1]["t"]).isoformat(),
        "pct_change_1d": (last / prev - 1.0) * 100.0 if prev else 0.0,
        "dollar_vol_1d": last * v[-1],
        "avg_vol_10d": sum(v[-10:]) / min(10, len(v)),
        "dollar_vol_1w": sum(ci * vi for ci, vi in zip(c[-5:], v[-5:])),
        "perf_1m": (last / c[-22] - 1.0) * 100.0 if len(c) >= 22 and c[-22] else None,
    }
    return m


def weekly_closes(bars: list[dict]) -> list[float]:
    out: list[float] = []
    key = None
    for b in bars:
        k = _d(b["t"]).isocalendar()[:2]
        if k != key:
            out.append(float(b["c"]))
            key = k
        else:
            out[-1] = float(b["c"])
    return out


def avwap_from_base_high(bars: list[dict], lookback: int = 20) -> Optional[float]:
    """AVWAP proxy (spec §8b): anchored at the highest high of the last `lookback` daily bars."""
    if not bars:
        return None
    window = bars[-lookback:]
    idx = max(range(len(window)), key=lambda i: float(window[i]["h"]))
    pv = vol = 0.0
    for b in window[idx:]:
        tp = (float(b["h"]) + float(b["l"]) + float(b["c"])) / 3.0
        pv += tp * float(b["v"])
        vol += float(b["v"])
    return pv / vol if vol else None


def full_metrics(bars: list[dict]) -> Optional[dict]:
    """Everything the watchlist needs (needs ~150+ daily bars for a settled 150 EMA)."""
    m = light_metrics(bars)
    if m is None or len(bars) < 60:
        return None
    c = [float(b["c"]) for b in bars]
    h = [float(b["h"]) for b in bars]
    lo = [float(b["l"]) for b in bars]
    e = {p: rules.ema_series(c, p) for p in (9, 21, 50, 150)}
    wk = weekly_closes(bars)
    m.update({
        "ema9": e[9][-1], "ema21": e[21][-1], "ema50": e[50][-1], "ema150": e[150][-1],
        "ema9_prev": e[9][-2], "ema21_prev": e[21][-2], "ema50_prev": e[50][-2],
        "weekly_ema9": rules.ema(wk, 9), "weekly_ema21": rules.ema(wk, 21),
        "adr_pct": rules.adr_pct(h, lo),
        "avwap": avwap_from_base_high(bars),
        "list": rules.classify_ema_list(e[9][-1], e[21][-1], e[50][-1]),
    })
    m["rising"] = m["ema9"] > m["ema9_prev"] and m["ema21"] > m["ema21_prev"] and m["ema50"] >= m["ema50_prev"]
    m["declining"] = m["ema9"] < m["ema9_prev"] and m["ema21"] < m["ema21_prev"] and m["ema50"] <= m["ema50_prev"]
    return m


def leader_screen(metrics: dict[str, dict], mcaps: dict[str, Optional[float]] | None = None,
                  ascending: bool = False) -> list[str]:
    mcaps = mcaps or {}
    out = []
    for s, m in metrics.items():
        if m.get("perf_1m") is None:
            continue
        cap = mcaps.get(s)
        if cap is not None and cap <= LEADER_MIN_MCAP:
            continue
        if m["avg_vol_10d"] <= LEADER_MIN_AVG_VOL_10D or m["dollar_vol_1w"] <= LEADER_MIN_DOLLAR_VOL_1W:
            continue
        out.append(s)
    return sorted(out, key=lambda s: metrics[s]["perf_1m"], reverse=not ascending)


def potent_screen(metrics: dict[str, dict], ascending: bool = False) -> list[str]:
    out = [s for s, m in metrics.items() if m["dollar_vol_1d"] > POTENT_MIN_DOLLAR_VOL]
    return sorted(out, key=lambda s: metrics[s]["pct_change_1d"], reverse=not ascending)


def premarket_screen(premarket_pct: dict[str, float], metrics: dict[str, dict], ascending: bool = False) -> list[str]:
    out = [s for s in premarket_pct if s in metrics and metrics[s]["dollar_vol_1d"] > PREMARKET_MIN_DOLLAR_VOL]
    return sorted(out, key=lambda s: premarket_pct[s], reverse=not ascending)


def levels_for(m: dict) -> list[float]:
    return [x for x in (m.get("ema9"), m.get("ema21"), m.get("ema50"), m.get("ema150"), m.get("avwap")) if x]


def long_qualifies(m: dict) -> Optional[str]:
    """Returns None if it qualifies for the pullback-long list, else the reason it doesn't."""
    if m.get("list") != "lead":
        return "not 9>21>50"
    if not m.get("rising"):
        return "EMAs not rising"
    if (m.get("adr_pct") or 0) < rules.MIN_ADR_PCT:
        return "ADR < 4%"
    if m.get("weekly_ema9") and m["close"] < m["weekly_ema9"]:
        return "below weekly 9 EMA"
    return None


def short_qualifies(m: dict, index_etf: bool = False) -> Optional[str]:
    if m.get("list") != "lag":
        return "not 9<21<50"
    if not m.get("declining"):
        return "EMAs not declining"
    if not index_etf and (m.get("adr_pct") or 0) < rules.MIN_ADR_PCT:
        return "ADR < 4%"
    if m.get("weekly_ema9") and m["close"] > m["weekly_ema9"]:
        return "above weekly 9 EMA"
    return None


def build_watchlist(full: dict[str, dict], long_ranked: Iterable[str], short_ranked: Iterable[str],
                    max_each: int, shorts_enabled: bool = True,
                    mcaps: dict[str, Optional[float]] | None = None) -> list[dict]:
    """Combine the screens' ranked candidates with his EMA-stack lists into the day's watchlist."""
    mcaps = mcaps or {}
    items: list[dict] = []
    seen = set()

    def item(sym: str, bias: str, m: dict, trade_symbol: Optional[str] = None) -> dict:
        return {"symbol": sym, "bias": bias, "trade_symbol": trade_symbol or sym,
                "levels": levels_for(m), "adr_pct": m.get("adr_pct"), "market_cap": mcaps.get(sym),
                "ema9": m.get("ema9"), "ema21": m.get("ema21"), "close": m.get("close"), "list": m.get("list")}

    n = 0
    for s in long_ranked:
        if n >= max_each:
            break
        if (s, "long") in seen or s in rules.INVERSE_ETF_MAP or s not in full:
            continue
        if long_qualifies(full[s]) is None:
            items.append(item(s, "long", full[s]))
            seen.add((s, "long"))
            n += 1
    if shorts_enabled:
        n = 0
        for s in short_ranked:
            if n >= max_each:
                break
            if (s, "short") in seen or s in rules.INVERSE_ETF_MAP or s not in full:
                continue
            if short_qualifies(full[s]) is None:
                items.append(item(s, "short", full[s]))
                seen.add((s, "short"))
                n += 1
        # Index pullback shorts are expressed by buying the leveraged inverse ETF (D13).
        for idx, inv in rules.INVERSE_ETF_MAP.items():
            if idx in full and short_qualifies(full[idx], index_etf=True) is None:
                it = item(idx, "short", full[idx], trade_symbol=inv)
                inv_m = full.get(inv) or {}
                it["trade_adr_pct"] = inv_m.get("adr_pct")
                items.append(it)
    return items
