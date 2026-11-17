"""Trading cost model (conservative, approximate).

Per side, every fill is moved against us by slippage_bps + half_spread_bps.
Commission: 0 per share (Alpaca). Regulatory fees on SELLS only:
  * SEC Section 31 fee: sec_fee_per_million x sell notional / 1e6. The rate changes
    each fiscal year (it was $27.80 per $1M in 2024; it has been as low as $0). We keep
    $27.80 as a conservative default.
  * FINRA TAF: taf_per_share x shares sold, capped at taf_max per trade
    ($0.000166/share, max $8.30 in 2024).
Short borrow: borrow_annual_pct of entry notional, pro-rated by calendar time held
  (minimum 1 hour), /360. Easy-to-borrow names at Alpaca are often 0% intraday, so
  this is conservative.
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CostModel:
    slippage_bps: float = 3.0
    half_spread_bps: float = 1.0
    commission_per_share: float = 0.0
    sec_fee_per_million: float = 27.80
    taf_per_share: float = 0.000166
    taf_max: float = 8.30
    borrow_annual_pct: float = 0.5
    min_borrow_hours: float = 1.0

    @property
    def side_bps(self) -> float:
        return self.slippage_bps + self.half_spread_bps

    def fill(self, price: float, side: int, opening: bool) -> float:
        """Adverse fill. side=+1 long / -1 short; opening=True for the entry."""
        direction = side if opening else -side  # buying pays up, selling gets less
        return price * (1 + direction * self.side_bps / 1e4)

    def sell_fees(self, price: float, shares: float) -> float:
        sec = price * shares * self.sec_fee_per_million / 1e6
        taf = min(shares * self.taf_per_share, self.taf_max)
        return sec + taf

    def borrow(self, price: float, shares: float, hours: float) -> float:
        hours = max(hours, self.min_borrow_hours)
        return price * shares * self.borrow_annual_pct / 100 * hours / (24 * 360)

    def trade_fees(self, side: int, entry: float, exit_: float, shares: float, hours: float) -> float:
        """Dollar fees for a round trip (excluding the slippage already in the fills)."""
        fees = 2 * shares * self.commission_per_share
        if side > 0:
            fees += self.sell_fees(exit_, shares)
        else:
            fees += self.sell_fees(entry, shares) + self.borrow(entry, shares, hours)
        return fees


LIQUID = CostModel()                                         # ETFs / mega caps: 4 bps per side
SMALL_CAP = CostModel(slippage_bps=10.0, half_spread_bps=5.0, borrow_annual_pct=5.0)


def for_symbol(symbol: str, liquid: set[str] | None = None, base: CostModel | None = None) -> CostModel:
    from . import config
    liquid = set(config.DEFAULT_UNIVERSE) if liquid is None else liquid
    if base is not None:
        return base
    return LIQUID if symbol.upper() in liquid else SMALL_CAP


def scaled(model: CostModel, factor: float) -> CostModel:
    return replace(model, slippage_bps=model.slippage_bps * factor, half_spread_bps=model.half_spread_bps * factor)
