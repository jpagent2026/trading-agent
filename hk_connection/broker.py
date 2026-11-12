"""Order construction and execution on the HK Connection Alpaca PAPER account.

- Entries: market order, OTO with a GTC stop-market stop-loss leg (same structure as Agent 200's fix).
- Partial sells: cancel the stop -> sell the portion -> re-place a GTC stop for the remaining shares.
- Full exits: cancel the stop -> market order for the whole position.
- GTC refresh: Alpaca cancels GTC orders after 90 days, so stops are re-placed before that.
- Stop-breach flatten: when a stop can't be placed because price is already through it, cancel any stop and
  market-sell (or buy to cover) what is held, net of exit orders already working (no double submits).
"""
from __future__ import annotations

import logging
import time as _time
from typing import Any, Callable, Iterable, Optional, Tuple

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopLossRequest, StopOrderRequest

log = logging.getLogger("hk_connection.broker")

LIVE_STOP_STATUSES = ("new", "held", "accepted", "pending_new", "partially_filled")
OPEN_ORDER_STATUSES = ("new", "held", "accepted", "pending_new", "partially_filled", "accepted_for_bidding",
                       "pending_replace", "calculated")

_THROUGH_MARKET_HINTS = ("base price", "current price", "market price", "last price", "must be less", "must be below",
                         "must be greater", "must be above", "must not be greater", "must not be less",
                         "less than", "greater than", "below", "above")

PlaceStopFn = Callable[[str, int, str, float], Any]


def is_stop_through_market_error(err: BaseException) -> bool:
    """True when Alpaca refused a stop because its price is already through the market, e.g.
    'stop price must not be greater than base price' (sell stop above the last price)."""
    msg = str(err).lower()
    return "stop" in msg and any(h in msg for h in _THROUGH_MARKET_HINTS)


def make_trading_client(key_id: str, secret_key: str) -> TradingClient:
    return TradingClient(key_id, secret_key, paper=True)  # PAPER ONLY, by design


def _s(v: Any) -> str:
    return str(getattr(v, "value", v) or "").lower()


def entry_side(direction: str) -> OrderSide:
    return OrderSide.BUY if direction == "long" else OrderSide.SELL


def exit_side(direction: str) -> OrderSide:
    return OrderSide.SELL if direction == "long" else OrderSide.BUY


def build_entry_order(symbol: str, qty: int, direction: str, stop_price: float) -> MarketOrderRequest:
    return MarketOrderRequest(
        symbol=symbol,
        qty=int(qty),
        side=entry_side(direction),
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OTO,
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
    )


def build_stop_order(symbol: str, qty: int, direction: str, stop_price: float) -> StopOrderRequest:
    return StopOrderRequest(symbol=symbol, qty=int(qty), side=exit_side(direction),
                            time_in_force=TimeInForce.GTC, stop_price=round(stop_price, 2))


def build_exit_order(symbol: str, qty: int, direction: str) -> MarketOrderRequest:
    return MarketOrderRequest(symbol=symbol, qty=int(qty), side=exit_side(direction), time_in_force=TimeInForce.DAY)


def stop_leg_id(order: Any) -> Optional[str]:
    for leg in getattr(order, "legs", None) or []:
        if "stop" in _s(getattr(leg, "order_type", getattr(leg, "type", ""))):
            return str(leg.id)
    return None


def _flatten(orders: Iterable[Any]):
    for o in orders or []:
        yield o
        yield from _flatten(getattr(o, "legs", None) or [])


class Broker:
    def __init__(self, client: TradingClient, sleep: Callable[[float], None] = _time.sleep,
                 cancel_wait_s: float = 5.0):
        self.client = client
        self.sleep = sleep
        self.cancel_wait_s = cancel_wait_s

    # ------------------------------------------------------------ account
    def account(self) -> Any:
        return self.client.get_account()

    def equity(self) -> float:
        return float(self.account().equity)

    def buying_power(self) -> float:
        return float(self.account().buying_power)

    def positions(self) -> list[Any]:
        return list(self.client.get_all_positions() or [])

    def position_qty(self, symbol: str) -> int:
        try:
            return abs(int(float(self.client.get_open_position(symbol).qty)))
        except Exception:
            return 0

    def is_shortable(self, symbol: str) -> bool:
        try:
            a = self.client.get_asset(symbol)
            return bool(getattr(a, "shortable", False) and getattr(a, "easy_to_borrow", False))
        except Exception:
            return False

    # ------------------------------------------------------------ entries
    def submit_entry(self, symbol: str, qty: int, direction: str, stop_price: float) -> Any:
        return self.client.submit_order(build_entry_order(symbol, qty, direction, stop_price))

    # ------------------------------------------------------------ stops
    def live_stops(self, symbol: str) -> list[Any]:
        found, seen = [], set()
        for req in (GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=True),
                    GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            try:
                orders = self.client.get_orders(req)
            except Exception:
                continue
            for o in _flatten(orders):
                oid = str(getattr(o, "id", ""))
                if not oid or oid in seen:
                    continue
                if str(getattr(o, "symbol", symbol) or symbol).upper() != symbol.upper():
                    continue
                if "stop" not in _s(getattr(o, "order_type", getattr(o, "type", ""))):
                    continue
                if _s(getattr(o, "status", "")) not in LIVE_STOP_STATUSES:
                    continue
                seen.add(oid)
                found.append(o)
        return found

    def cancel_stops(self, symbol: str) -> list[str]:
        """Cancel every live stop for the symbol and wait (briefly) until Alpaca reports them gone."""
        ids = []
        for o in self.live_stops(symbol):
            try:
                self.client.cancel_order_by_id(o.id)
                ids.append(str(o.id))
            except Exception as e:  # already filled/cancelled
                log.warning("cancel %s failed: %s", o.id, e)
        deadline = self.cancel_wait_s
        pending = list(ids)
        while pending and deadline > 0:
            still = []
            for oid in pending:
                try:
                    st = _s(self.client.get_order_by_id(oid).status)
                except Exception:
                    st = "canceled"
                if st in LIVE_STOP_STATUSES or st == "pending_cancel":
                    still.append(oid)
            pending = still
            if pending:
                self.sleep(0.25)
                deadline -= 0.25
        return ids

    def place_stop(self, symbol: str, qty: int, direction: str, stop_price: float) -> Any:
        return self.client.submit_order(build_stop_order(symbol, qty, direction, stop_price))

    # ------------------------------------------------------------ exits
    def partial_exit(self, symbol: str, direction: str, sell_qty: int, remaining_qty: int,
                     stop_price: float, place_stop: Optional[PlaceStopFn] = None) -> tuple[Any, Any]:
        """cancel stop -> sell/cover sell_qty -> re-place stop for remaining_qty. If the sell is rejected,
        the stop is put back for the full quantity so the position is never left unprotected.
        `place_stop` lets the engine route the re-placement through its stop-breach guard."""
        ps = place_stop or self.place_stop
        self.cancel_stops(symbol)
        try:
            sell = self.client.submit_order(build_exit_order(symbol, sell_qty, direction))
        except Exception:
            ps(symbol, sell_qty + remaining_qty, direction, stop_price)
            raise
        stop = ps(symbol, remaining_qty, direction, stop_price) if remaining_qty > 0 else None
        return sell, stop

    def full_exit(self, symbol: str, direction: str, qty: Optional[int] = None) -> Optional[Any]:
        self.cancel_stops(symbol)
        q = qty if qty is not None else self.position_qty(symbol)
        if q <= 0:
            return None
        return self.client.submit_order(build_exit_order(symbol, q, direction))

    def refresh_stop(self, symbol: str, qty: int, direction: str, stop_price: float,
                     place_stop: Optional[PlaceStopFn] = None) -> Any:
        """Re-place the protective stop (same price, current qty) to restart Alpaca's 90-day GTC clock."""
        self.cancel_stops(symbol)
        return (place_stop or self.place_stop)(symbol, qty, direction, stop_price)

    # ------------------------------------------------------------ stop-breach flatten
    def open_exit_orders(self, symbol: str, direction: str) -> list[Any]:
        """Working (unfilled) non-stop orders on the exit side for this symbol: market/limit sells for a
        long, buys for a short. Used so a breach exit is never submitted twice."""
        side = _s(exit_side(direction))
        out, seen = [], set()
        orders = self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=True))
        for o in _flatten(orders):
            oid = str(getattr(o, "id", ""))
            if not oid or oid in seen:
                continue
            seen.add(oid)
            if str(getattr(o, "symbol", symbol) or symbol).upper() != symbol.upper():
                continue
            if "stop" in _s(getattr(o, "order_type", getattr(o, "type", ""))):
                continue
            if _s(getattr(o, "side", "")) != side or _s(getattr(o, "status", "")) not in OPEN_ORDER_STATUSES:
                continue
            out.append(o)
        return out

    @staticmethod
    def _unfilled(o: Any) -> int:
        try:
            q = float(getattr(o, "qty", 0) or 0)
            f = float(getattr(o, "filled_qty", 0) or 0)
            return max(int(round(q - f)), 0)
        except Exception:
            return 0

    def flatten(self, symbol: str, direction: str) -> Tuple[Optional[Any], int, int]:
        """Cancel live stops, then market DAY order for (position qty - qty already working in exit orders).
        Returns (order or None, qty submitted, qty already pending). Outside regular hours Alpaca accepts the
        DAY market order and queues it for the next open. Working orders are read BEFORE the position so a
        fill in between can only make us under-sell (fixed next cycle), never over-sell into a reverse position."""
        self.cancel_stops(symbol)
        pending = sum(self._unfilled(o) for o in self.open_exit_orders(symbol, direction))
        need = self.position_qty(symbol) - pending
        if need <= 0:
            return None, 0, pending
        return self.client.submit_order(build_exit_order(symbol, need, direction)), need, pending

    def get_order(self, order_id: str) -> Any:
        return self.client.get_order_by_id(order_id)
