"""The HK Connection trading engine: intraday triggers, entries, trims, exits, and daily jobs.

Thread-safety: the bar stream and the scheduler both call in; every public method takes self.lock.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from . import rules, scanner
from .bars import ET, Bar, SessionState, in_regular_session, minutes_since_open, to_et
from .broker import OPEN_ORDER_STATUSES, Broker, PlaceStopFn, is_stop_through_market_error, stop_leg_id
from .config import Settings
from .ledger import Ledger, utcnow

log = logging.getLogger("hk_connection.engine")

INDEX_AND_INVERSE = set(rules.INVERSE_ETF_MAP) | set(rules.INVERSE_ETF_MAP.values())

BREACH_RETRY_S = 60         # periodic check: at most one exit attempt per trade per minute
BROKER_STOP_GRACE_S = 60    # in regular hours, give a live broker stop this long to trigger before we act
TRIGGERABLE_STOP_STATUSES = ("new", "accepted", "pending_new", "partially_filled")


def stop_breached(direction: str, price: Optional[float], stop: Optional[float]) -> bool:
    """Long: price at or below the stop. Short stock: price at or above the stop."""
    if not price or not stop:
        return False
    return price <= stop if direction == "long" else price >= stop


def _s(v: Any) -> str:
    return str(getattr(v, "value", v) or "").lower()


class Engine:
    def __init__(self, settings: Settings, broker: Broker, ledger: Ledger, data: Any = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.settings = settings
        self.broker = broker
        self.ledger = ledger
        self.data = data
        self.now = now
        self.lock = threading.RLock()
        self.sessions: dict[str, SessionState] = {}
        self.watch: dict[str, list[dict]] = {}       # signal symbol -> watchlist items
        self.earnings: dict[str, dict] = {}          # symbol -> {'date': date, 'hour': str}
        self.earnings_ok = False                     # False until a calendar fetch succeeded today
        self.session_date: Optional[str] = None
        self._breach_attempt: dict[int, datetime] = {}   # trade id -> last periodic exit attempt
        self._breach_seen: dict[int, datetime] = {}      # trade id -> first time seen through a live broker stop
        self._breach_deferred_logged: set[int] = set()

    # ================================================================ helpers
    def today_et(self) -> date:
        return to_et(self.now()).date()

    def capital_base(self) -> tuple[float, str]:
        """CAPITAL_BASE env override if set, otherwise the live account equity."""
        if self.settings.capital_base:
            return float(self.settings.capital_base), "CAPITAL_BASE"
        return self.broker.equity(), "account_equity"

    def session(self, symbol: str) -> SessionState:
        if symbol not in self.sessions:
            self.sessions[symbol] = SessionState(symbol)
        return self.sessions[symbol]

    def stream_symbols(self) -> list[str]:
        syms = set(self.watch)
        for items in self.watch.values():
            for it in items:
                syms.add(it["trade_symbol"])
        for t in self.ledger.open_trades():
            syms.add(t["symbol"])
        return sorted(syms)

    def load_watchlist(self, session_date: str, items: Optional[list[dict]] = None) -> None:
        with self.lock:
            items = items if items is not None else self.ledger.load_watchlist(session_date)
            self.watch = {}
            for it in items:
                self.watch.setdefault(it["symbol"], []).append(it)
            self.session_date = session_date
            for s in self.stream_symbols():
                self.session(s)

    def set_earnings(self, cal: dict[str, dict], ok: bool = True) -> None:
        with self.lock:
            self.earnings = cal
            self.earnings_ok = ok

    # ================================================================ intraday
    def on_bar(self, symbol: str, bar: Bar) -> None:
        with self.lock:
            if not in_regular_session(bar.start):
                return
            st = self.session(symbol)
            new_low, new_high, done = st.update(bar)
            for b in done:
                if b.minutes == 60:
                    k9, k21 = 2 / 10, 2 / 22
                    st.hourly_ema9 = b.close if st.hourly_ema9 is None else b.close * k9 + st.hourly_ema9 * (1 - k9)
                    st.hourly_ema21 = b.close if st.hourly_ema21 is None else b.close * k21 + st.hourly_ema21 * (1 - k21)
            self.manage_trims(symbol, bar)
            for it in self.watch.get(symbol, []):
                try:
                    self.evaluate_entry(it, st, bar, new_low, new_high)
                except Exception as e:  # never let one symbol kill the stream
                    log.exception("entry evaluation failed for %s", symbol)
                    self.ledger.event("error", None, where="evaluate_entry", symbol=symbol, error=str(e))

    def evaluate_entry(self, item: dict, st: SessionState, bar: Bar, new_low: bool, new_high: bool) -> Optional[int]:
        tf = rules.entry_timeframe(minutes_since_open(bar.end))
        if tf is None:
            return None
        levels = list(item.get("levels") or []) + [x for x in (st.hourly_ema9, st.hourly_ema21) if x]
        if item["bias"] == "long":
            if new_low:
                # A fresh low of day: arm only if it flushed into support; never buy the bar making the low.
                st.long_armed = rules.touches_support(st.lod, levels)
                return None
            if not st.long_armed:
                return None
            cur, prev = st.agg.current(tf), st.agg.previous(tf)
            if cur is None or prev is None or not cur.high > prev.high:
                return None
            st.long_armed = False  # "the first lower-timeframe breakout": one trigger per flush
            return self.try_enter(item, tf, "breakout of previous %d-min bar high" % tf)
        else:
            if new_high:
                st.short_armed = rules.touches_resistance(st.hod, levels)
                return None
            if not st.short_armed:
                return None
            cur, prev = st.agg.current(tf), st.agg.previous(tf)
            if cur is None or prev is None or not cur.low < prev.low:
                return None
            st.short_armed = False
            return self.try_enter(item, tf, "break of previous %d-min bar low" % tf)

    def _skip(self, item: dict, reason: str, **extra: Any) -> None:
        self.ledger.event("entry_skipped", None, symbol=item["symbol"], trade_symbol=item["trade_symbol"],
                          bias=item["bias"], reason=reason, **extra)
        return None

    def try_enter(self, item: dict, tf: int, trigger: str) -> Optional[int]:
        sig, trade_sym, bias = item["symbol"], item["trade_symbol"], item["bias"]
        inverse = trade_sym != sig
        direction = "long" if (bias == "long" or inverse) else "short"
        today = self.today_et()

        if bias == "short" and not self.settings.shorts_enabled:
            return self._skip(item, "shorts disabled")
        streak = self.ledger.load_streak()
        if rules.is_paused(streak, today):
            return self._skip(item, "losing-streak pause", paused_until=streak.paused_until)
        open_trades = self.ledger.open_trades()
        if any(t["symbol"] == trade_sym for t in open_trades):
            return self._skip(item, "already holding this symbol")
        if len(open_trades) >= rules.MAX_CONCURRENT_POSITIONS:
            return self._skip(item, "max concurrent positions")
        if any(t["bias"] != bias for t in open_trades):
            return self._skip(item, "one direction at a time")
        if trade_sym not in INDEX_AND_INVERSE:
            if not self.earnings_ok:
                return self._skip(item, "earnings calendar unavailable (rule fails closed)")
            e = self.earnings.get(sig)
            if e and rules.earnings_blocks_entry(today, e["date"]):
                return self._skip(item, "earnings within 5 days", earnings=str(e["date"]))

        ts = self.session(trade_sym)
        if ts.last <= 0:
            return self._skip(item, "no live price for trade symbol")
        entry = ts.last
        if direction == "long" and rules.too_extended_from_lod(entry, ts.lod):
            return self._skip(item, "more than 3% above low of day", entry=entry, lod=ts.lod)
        if direction == "short" and rules.too_extended_from_hod(entry, ts.hod):
            return self._skip(item, "more than 3% below high of day", entry=entry, hod=ts.hod)
        cur, prev = ts.agg.current(tf), ts.agg.previous(tf)
        if cur is None or prev is None:
            return self._skip(item, "not enough bars on trade symbol")
        adr = item.get("trade_adr_pct") if inverse else item.get("adr_pct")
        stop = rules.select_stop(direction, entry, ts.lod, ts.hod, cur.low, cur.high, prev.low, prev.high, ts.open, adr)
        if stop is None:
            return self._skip(item, "no stop fits the width cap", entry=entry, cap=rules.max_stop_pct(adr))
        if direction == "short" and not self.broker.is_shortable(trade_sym):
            return self._skip(item, "not shortable / easy to borrow")

        capital, source = self.capital_base()
        risk = rules.current_risk_pct(streak)
        bp = self.broker.buying_power()
        qty = rules.size_position(capital, risk, entry, stop.price, item.get("market_cap"), bp)
        if qty < 1:
            return self._skip(item, "size rounds to 0 shares", capital=capital, risk=risk, entry=entry, stop=stop.price)
        try:
            order = self.broker.submit_entry(trade_sym, qty, direction, stop.price)
        except Exception as e:
            return self._skip(item, "order rejected", error=str(e))
        return self.ledger.create_trade(
            symbol=trade_sym, signal_symbol=sig, direction=direction, bias=bias, setup="pullback_" + bias,
            timeframe=tf, entry_order_id=str(getattr(order, "id", "")), entry_price=entry, qty_initial=qty,
            qty_open=qty, stop_price=stop.price, stop_basis=stop.basis, stop_order_id=stop_leg_id(order),
            stop_placed_at=utcnow(), risk_pct=risk, capital_base=capital,
            notes=f"{trigger}; capital from {source}")

    def manage_trims(self, symbol: str, bar: Bar) -> None:
        """Sell 15% into strength at +3R and again at +5R (cancel stop -> sell -> re-place stop)."""
        t = self.ledger.open_trade_for(symbol)
        if not t or t["status"] != "open" or t["pending_exit"]:
            return
        n = int(t["trims_done"] or 0)
        if n >= len(rules.TRIM_R_LEVELS):
            return
        target = rules.trim_trigger_price(t["direction"], t["entry_price"], t["stop_price"], rules.TRIM_R_LEVELS[n])
        hit = bar.close >= target if t["direction"] == "long" else bar.close <= target
        if not hit:
            return
        q = rules.trim_qty(int(t["qty_initial"]), int(t["qty_open"]))
        if q == 0:
            self.ledger.update_trade(t["id"], trims_done=n + 1)
            self.ledger.event("trim_skipped", t["id"], reason="position too small to trim", r=rules.TRIM_R_LEVELS[n])
            return
        remaining = int(t["qty_open"]) - q
        try:
            sell, stop = self.broker.partial_exit(symbol, t["direction"], q, remaining, t["stop_price"],
                                                  place_stop=self.guarded_place_stop(t, "trim"))
        except Exception as e:
            self.ledger.event("error", t["id"], where="partial_exit", error=str(e))
            return
        sign = 1 if t["direction"] == "long" else -1
        pnl = sign * (bar.close - t["entry_price"]) * q
        self.ledger.update_trade(t["id"], qty_open=remaining, trims_done=n + 1,
                                 realized_pnl=(t["realized_pnl"] or 0) + pnl,
                                 stop_order_id=str(getattr(stop, "id", "")) if stop else None, stop_placed_at=utcnow())
        self.ledger.event("trim", t["id"], qty=q, remaining=remaining, price=bar.close, r=rules.TRIM_R_LEVELS[n],
                          sell_order=str(getattr(sell, "id", "")), new_stop=str(getattr(stop, "id", "")))

    # ================================================================ reconciliation
    def reconcile(self) -> None:
        """Sync the ledger with Alpaca: entry fills, stop-outs, exits, and the losing-streak count."""
        with self.lock:
            for t in self.ledger.open_trades():
                try:
                    self._reconcile_trade(t)
                except Exception as e:
                    self.ledger.event("error", t["id"], where="reconcile", error=str(e))

    def _fill_price(self, order_id: Optional[str]) -> Optional[float]:
        if not order_id:
            return None
        try:
            o = self.broker.get_order(order_id)
            if _s(o.status) == "filled" and o.filled_avg_price:
                return float(o.filled_avg_price)
        except Exception:
            return None
        return None

    def _reconcile_trade(self, t: dict) -> None:
        if t["status"] == "pending":
            o = self.broker.get_order(t["entry_order_id"])
            st = _s(o.status)
            if st == "filled":
                fq = int(float(o.filled_qty or t["qty_initial"]))
                self.ledger.update_trade(t["id"], status="open", entry_price=float(o.filled_avg_price),
                                         qty_initial=fq, qty_open=fq,
                                         stop_order_id=t["stop_order_id"] or stop_leg_id(o))
                self.ledger.event("entry_filled", t["id"], price=float(o.filled_avg_price), qty=fq)
                self._check_entry_stop_leg(t, fq, t["stop_order_id"] or stop_leg_id(o))
            elif st in ("canceled", "rejected", "expired"):
                self.ledger.update_trade(t["id"], status="rejected", exit_reason=st)
                self.ledger.event("entry_" + st, t["id"])
            return
        pos_qty = self.broker.position_qty(t["symbol"])
        if pos_qty > 0:
            if pos_qty != int(t["qty_open"]):
                self.ledger.update_trade(t["id"], qty_open=pos_qty)
            return
        # Flat: the stop filled, or our exit order filled.
        price = self._fill_price(t["exit_order_id"]) or self._fill_price(t["stop_order_id"])
        reason = t["exit_reason"] or ("stop" if self._fill_price(t["stop_order_id"]) else "flat")
        if price is None:
            price = self.session(t["symbol"]).last or t["stop_price"]
        sign = 1 if t["direction"] == "long" else -1
        total = (t["realized_pnl"] or 0) + sign * (price - t["entry_price"]) * int(t["qty_open"])
        self.ledger.update_trade(t["id"], status="closed", qty_open=0, exit_price=price, exit_time=utcnow(),
                                 realized_pnl=total, exit_reason=reason, pending_exit=None)
        streak = rules.record_trade_result(self.ledger.load_streak(), total, self.today_et())
        self.ledger.save_streak(streak)
        self.ledger.event("trade_closed", t["id"], price=price, pnl=total, reason=reason,
                          consecutive_losses=streak.consecutive_losses, paused_until=streak.paused_until)
        if str(reason).startswith("stop breach"):
            self.ledger.mark_stop_breach_filled(t["id"])

    # ================================================================ daily jobs
    def open_job(self) -> None:
        """At the open: execute exits queued by the end-of-day trail (close below the trail EMA)."""
        with self.lock:
            for t in self.ledger.open_trades():
                if t["status"] == "open" and t["pending_exit"]:
                    try:
                        # flatten = cancel stops, then sell what is held net of exit orders already working,
                        # so an exit that is already queued (e.g. a stop-breach exit) is never doubled.
                        o, qty, pending = self.broker.flatten(t["symbol"], t["direction"])
                        self.ledger.update_trade(t["id"], exit_order_id=str(getattr(o, "id", "")) if o else t["exit_order_id"],
                                                 exit_reason=t["pending_exit"], pending_exit=None)
                        self.ledger.event("exit_submitted", t["id"], reason=t["pending_exit"], qty=qty,
                                          already_pending_qty=pending)
                    except Exception as e:
                        self.ledger.event("error", t["id"], where="open_job", error=str(e))

    def eod_trail(self, daily_bars: dict[str, list[dict]]) -> list[int]:
        """After the close: flag positions whose daily close is through the trail EMA for exit at next open."""
        flagged = []
        with self.lock:
            n = self.settings.trail_ema
            for t in self.ledger.open_trades():
                if t["status"] != "open" or t["pending_exit"]:
                    continue
                bars = daily_bars.get(t["symbol"]) or []
                if len(bars) < n:
                    continue
                closes = [float(b["c"]) for b in bars]
                e = rules.ema(closes, n)
                if rules.trail_exit_signal(t["direction"], closes[-1], e):
                    reason = f"daily close {'below' if t['direction'] == 'long' else 'above'} {n} EMA"
                    self.ledger.update_trade(t["id"], pending_exit=reason)
                    self.ledger.event("trail_exit_flagged", t["id"], close=closes[-1], ema=e, ema_period=n)
                    flagged.append(t["id"])
        return flagged

    def refresh_gtc_stops(self) -> list[int]:
        """Alpaca cancels GTC orders after 90 days: re-place stops older than 80 days, and any stop that
        is no longer live (expired or cancelled outside the bot), at the same price for the open qty."""
        refreshed = []
        with self.lock:
            now = self.now()
            for t in self.ledger.open_trades():
                if t["status"] != "open" or t["pending_exit"] or int(t["qty_open"] or 0) <= 0:
                    continue
                if self._exit_pending(t, rth=False):  # a (stop-breach) exit is already working: no new stop
                    continue
                age_days = 0.0
                if t["stop_placed_at"]:
                    placed = datetime.fromisoformat(t["stop_placed_at"])
                    age_days = (now - placed).total_seconds() / 86400.0
                live = True
                if t["stop_order_id"]:
                    try:
                        live = _s(self.broker.get_order(t["stop_order_id"]).status) in ("new", "held", "accepted", "pending_new")
                    except Exception:
                        live = False
                else:
                    live = False
                if age_days >= rules.GTC_REFRESH_AFTER_DAYS or not live:
                    try:
                        o = self.broker.refresh_stop(t["symbol"], int(t["qty_open"]), t["direction"], t["stop_price"],
                                                     place_stop=self.guarded_place_stop(t, "gtc_refresh"))
                        if o is None:  # price already through the stop: flattened instead (see stop_breach_exits)
                            self.ledger.event("stop_refresh_replaced_by_exit", t["id"], age_days=round(age_days, 1))
                            continue
                        self.ledger.update_trade(t["id"], stop_order_id=str(o.id), stop_placed_at=utcnow())
                        self.ledger.event("stop_refreshed", t["id"], age_days=round(age_days, 1), was_live=live)
                        refreshed.append(t["id"])
                    except Exception as e:
                        self.ledger.event("error", t["id"], where="refresh_gtc_stops", error=str(e))
        return refreshed

    def earnings_job(self, today: date, next_trading_day: Optional[date], adr_by_symbol: dict[str, float],
                     last_price: dict[str, float]) -> list[tuple[int, str]]:
        """Before a release, without enough cushion: sell at least half (some cushion) or exit (none)."""
        actions = []
        with self.lock:
            for t in self.ledger.open_trades():
                if t["status"] != "open" or t["pending_exit"]:
                    continue
                e = self.earnings.get(t["signal_symbol"]) or self.earnings.get(t["symbol"])
                if not e or not rules.must_act_before_release(today, e["date"], e.get("hour"), next_trading_day):
                    continue
                last = last_price.get(t["symbol"]) or self.session(t["symbol"]).last
                if not last:
                    continue
                act = rules.earnings_hold_action(t["direction"], t["entry_price"], last, adr_by_symbol.get(t["symbol"]))
                try:
                    if act == "sell_half":
                        q = math.ceil(int(t["qty_open"]) / 2)
                        rem = int(t["qty_open"]) - q
                        if rem <= 0:
                            act = "exit"
                        else:
                            self.broker.partial_exit(t["symbol"], t["direction"], q, rem, t["stop_price"],
                                                     place_stop=self.guarded_place_stop(t, "earnings_sell_half"))
                            sign = 1 if t["direction"] == "long" else -1
                            self.ledger.update_trade(t["id"], qty_open=rem,
                                                     realized_pnl=(t["realized_pnl"] or 0) + sign * (last - t["entry_price"]) * q)
                    if act == "exit":
                        o = self.broker.full_exit(t["symbol"], t["direction"])
                        self.ledger.update_trade(t["id"], exit_order_id=str(getattr(o, "id", "")) if o else None,
                                                 exit_reason="earnings: no cushion")
                    self.ledger.event("earnings_" + act, t["id"], earnings=str(e["date"]), hour=e.get("hour"), last=last)
                    actions.append((t["id"], act))
                except Exception as ex:
                    self.ledger.event("error", t["id"], where="earnings_job", error=str(ex))
        return actions

    def update_equity_peak(self) -> None:
        with self.lock:
            s = rules.update_equity_peak(self.ledger.load_streak(), self.broker.equity())
            self.ledger.save_streak(s)

    # ================================================================ stop enforcement
    # HK never holds a position that is through its stop. Every stop placement goes through
    # guarded_place_stop, and enforce_stops runs at startup, at the open and every scheduler cycle.
    def is_rth(self) -> bool:
        et = to_et(self.now())
        return et.weekday() < 5 and in_regular_session(et)

    def latest_prices(self, symbols: Iterable[str]) -> dict[str, tuple[float, str]]:
        """symbol -> (price, source). SIP latest trade first (includes extended-hours prints), then the
        broker position's current_price, then the last streamed 1-minute close."""
        syms = sorted({s for s in symbols if s})
        out: dict[str, tuple[float, str]] = {}
        if not syms:
            return out
        if self.data is not None:
            try:
                res = self.data.latest_prices(syms)
                if isinstance(res, dict):
                    for s, p in res.items():
                        if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0:
                            out[s] = (float(p), "sip_latest_trade")
            except Exception as e:
                log.warning("latest price fetch failed: %s", e)
        missing = [s for s in syms if s not in out]
        if missing:
            pos: dict[str, float] = {}
            try:
                for p in self.broker.positions():
                    try:
                        pos[str(p.symbol).upper()] = float(p.current_price)
                    except Exception:
                        continue
            except Exception:
                pass
            for s in missing:
                if pos.get(s.upper(), 0) > 0:
                    out[s] = (pos[s.upper()], "broker_position")
                elif s in self.sessions and self.sessions[s].last > 0:
                    out[s] = (self.sessions[s].last, "stream_last_bar")
        return out

    def _exit_pending(self, t: dict, rth: bool) -> Optional[str]:
        """An exit already on its way for this trade (the broker-side check, net quantities, is in Broker.flatten)."""
        if t.get("pending_exit") and (not rth or to_et(self.now()).time() < time(9, 31)):
            return "queued for the open job: " + str(t["pending_exit"])
        if t.get("exit_order_id"):
            try:
                st = _s(self.broker.get_order(t["exit_order_id"]).status)
            except Exception:
                st = ""
            if st in OPEN_ORDER_STATUSES or st == "filled":
                return f"exit order {t['exit_order_id']} is {st}"
        return None

    def flatten_breach(self, t: dict, price: Optional[float], price_source: Optional[str], reason: str,
                       context: str, rth: Optional[bool] = None, error: Optional[str] = None) -> Optional[Any]:
        """Market order (DAY) for the full remaining position of a trade whose stop is breached.
        Outside regular hours Alpaca queues the DAY market order for the next open; if that submission
        is refused, the exit is queued in the ledger (pending_exit) for the 9:30 open job instead."""
        rth = self.is_rth() if rth is None else rth
        sym, d = t["symbol"], t["direction"]
        ev = dict(symbol=sym, direction=d, stop=t["stop_price"], trigger_price=price,
                  price_source=price_source, reason=reason, context=context)
        rec = dict(ev, trade_id=t["id"])
        log.warning("STOP BREACH %s %s: price %s through stop %s [%s/%s]%s", d, sym, price, t["stop_price"],
                    reason, context, f" broker error: {error}" if error else "")
        pend = self._exit_pending(t, rth)
        if pend:
            self.ledger.record_stop_breach(qty=0, status="already_pending", error=pend, **rec)
            self.ledger.event("stop_breach_exit", t["id"], status="already_pending", detail=pend, **ev)
            return None
        try:
            o, qty, pending = self.broker.flatten(sym, d)
        except Exception as e:
            if rth:
                self.ledger.record_stop_breach(qty=int(t["qty_open"] or 0), status="failed", error=str(e), **rec)
                self.ledger.event("error", t["id"], where="stop_breach_exit", error=str(e), **ev)
            else:
                self.ledger.update_trade(t["id"], pending_exit=f"stop breach ({reason})")
                self.ledger.record_stop_breach(qty=int(t["qty_open"] or 0), status="queued_for_open",
                                               error=str(e), **rec)
                self.ledger.event("stop_breach_exit", t["id"], status="queued_for_open", error=str(e), **ev)
            return None
        if o is None:
            status = "already_pending" if pending > 0 else "no_position"
            if pending > 0:
                self.ledger.record_stop_breach(qty=0, status=status, error=f"{pending} shares already in exit orders",
                                               **rec)
            self.ledger.event("stop_breach_exit", t["id"], status=status, pending_qty=pending, **ev)
            return None
        oid = str(getattr(o, "id", ""))
        self.ledger.update_trade(t["id"], exit_order_id=oid, exit_reason=f"stop breach ({reason})", pending_exit=None)
        self.ledger.record_stop_breach(qty=qty, order_id=oid, status="submitted", error=error, **rec)
        self.ledger.event("stop_breach_exit", t["id"], status="submitted", qty=qty, order_id=oid,
                          already_pending_qty=pending, rth=rth, broker_error=error, **ev)
        return o

    def guarded_place_stop(self, t: dict, context: str, rth: Optional[bool] = None) -> PlaceStopFn:
        """A place_stop replacement for the broker: checks the latest price first and, if the price is
        already through the stop (or Alpaca rejects the stop as through the market), market-exits the
        whole remaining position instead. Returns the stop order, or None when it exited instead."""
        def place(symbol: str, qty: int, direction: str, stop_price: float) -> Optional[Any]:
            px = self.latest_prices([symbol]).get(symbol)
            if px and stop_breached(direction, px[0], stop_price):
                self.flatten_breach(t, px[0], px[1], "pre_check", context, rth)
                return None
            try:
                return self.broker.place_stop(symbol, qty, direction, stop_price)
            except Exception as e:
                px2 = self.latest_prices([symbol]).get(symbol) or px
                if is_stop_through_market_error(e) or (px2 and stop_breached(direction, px2[0], stop_price)):
                    self.flatten_breach(t, px2[0] if px2 else None, px2[1] if px2 else None, "broker_reject",
                                        context, rth, error=str(e))
                    return None
                raise
        return place

    def _check_entry_stop_leg(self, t: dict, qty: int, leg_id: Optional[str]) -> None:
        """After an entry fills: if its OTO stop leg was rejected/cancelled, put the stop back (guarded)."""
        st = ""
        if leg_id:
            try:
                st = _s(self.broker.get_order(leg_id).status)
            except Exception:
                return
        if st not in ("rejected", "canceled", "cancelled", "expired"):
            return
        self.ledger.event("entry_stop_leg_" + st, t["id"], stop_order_id=leg_id)
        t = dict(t, status="open", qty_open=qty)
        o = self.guarded_place_stop(t, "entry")(t["symbol"], qty, t["direction"], t["stop_price"])
        if o is not None:
            self.ledger.update_trade(t["id"], stop_order_id=str(getattr(o, "id", "")), stop_placed_at=utcnow())

    def enforce_stops(self, context: str = "cycle", rth: Optional[bool] = None) -> list[int]:
        """For every open position: if the latest price is through its ledger stop and no exit is filled or
        working, market-exit it. A live, un-triggered broker stop that is also through the price is left to
        the broker outside regular hours (Alpaca triggers it at the open) and for BROKER_STOP_GRACE_S during
        regular hours; after that it is cancelled and replaced by a market order."""
        acted: list[int] = []
        with self.lock:
            rth = self.is_rth() if rth is None else rth
            trades = [t for t in self.ledger.open_trades()
                      if t["status"] == "open" and t["stop_price"] and int(t["qty_open"] or 0) > 0]
            if not trades:
                return acted
            prices = self.latest_prices(t["symbol"] for t in trades)
            now = self.now()
            for t in trades:
                tid, sym, d = t["id"], t["symbol"], t["direction"]
                try:
                    px = prices.get(sym)
                    if not px or not stop_breached(d, px[0], t["stop_price"]):
                        self._breach_seen.pop(tid, None)
                        self._breach_deferred_logged.discard(tid)
                        continue
                    last = self._breach_attempt.get(tid)
                    if last is not None and (now - last).total_seconds() < BREACH_RETRY_S:
                        continue
                    if self._exit_pending(t, rth):
                        continue
                    live = [o for o in self.broker.live_stops(sym)
                            if _s(getattr(o, "status", "")) in TRIGGERABLE_STOP_STATUSES
                            and stop_breached(d, px[0], _float(getattr(o, "stop_price", None)))]
                    if live:
                        if rth:  # the grace period only counts regular-hours time, when the stop can trigger
                            first = self._breach_seen.setdefault(tid, now)
                        else:
                            self._breach_seen.pop(tid, None)
                        if not rth or (now - first).total_seconds() < BROKER_STOP_GRACE_S:
                            if tid not in self._breach_deferred_logged:
                                self._breach_deferred_logged.add(tid)
                                self.ledger.event("stop_breach_deferred_to_broker_stop", tid, symbol=sym, price=px[0],
                                                  stop=t["stop_price"], rth=rth, context=context,
                                                  stop_order_ids=[str(o.id) for o in live])
                            continue
                    self._breach_attempt[tid] = now
                    self.flatten_breach(t, px[0], px[1], "periodic", context, rth)
                    acted.append(tid)
                except Exception as e:
                    self.ledger.event("error", tid, where="enforce_stops", error=str(e))
        return acted

    # ================================================================ scans
    def after_close_scan(self, next_session: date) -> list[dict]:
        """His Leader (1-month performance) and Potent (day's top % change) screens over the whole
        US market, then the EMA-stack lists, for the next session's watchlist."""
        d = self.data
        n = self.settings.watchlist_max
        uni = d.universe()
        light = d.light_scan(uni)
        self.ledger.set_state("liquid_symbols", {s: m["dollar_vol_1d"] for s, m in light.items()
                                                 if m["dollar_vol_1d"] > scanner.PREMARKET_MIN_DOLLAR_VOL})
        leaders = scanner.leader_screen(light)[: n * 4]
        laggards = scanner.leader_screen(light, ascending=True)[: n * 4]
        gainers = scanner.potent_screen(light)[: n * 4]
        losers = scanner.potent_screen(light, ascending=True)[: n * 4]
        cands = set(leaders + laggards + gainers + losers) | INDEX_AND_INVERSE
        full = {s: m for s, b in d.daily_bars(sorted(cands), 400).items() if (m := scanner.full_metrics(b))}
        mcaps = {}
        for s, m in full.items():
            if s in INDEX_AND_INVERSE:
                continue
            if scanner.long_qualifies(m) is None or scanner.short_qualifies(m) is None:
                mcaps[s] = d.market_cap(s)
        light_c = {s: light[s] for s in light if s in full}
        long_ranked = _dedupe(scanner.leader_screen(light_c, mcaps) + [s for s in gainers if s in full])
        short_ranked = _dedupe(scanner.leader_screen(light_c, mcaps, ascending=True) + [s for s in losers if s in full])
        long_ranked = [s for s in long_ranked if mcaps.get(s) is None or mcaps[s] > scanner.LEADER_MIN_MCAP]
        short_ranked = [s for s in short_ranked if mcaps.get(s) is None or mcaps[s] > scanner.LEADER_MIN_MCAP]
        wl = scanner.build_watchlist(full, long_ranked, short_ranked, n, self.settings.shorts_enabled, mcaps)
        self.ledger.save_watchlist(next_session.isoformat(), wl)
        self.ledger.event("scan_after_close", None, session=next_session.isoformat(), universe=len(uni),
                          candidates=len(cands), watchlist=len(wl))
        return wl

    def pre_open_scan(self, today: date) -> list[dict]:
        """Premarket screen (premarket % change, $vol > $50M), earnings calendar, and hourly EMAs."""
        d = self.data
        n = self.settings.watchlist_max
        wl = self.ledger.load_watchlist(today.isoformat())
        liquid = self.ledger.get_state("liquid_symbols", {}) or {}
        if liquid:
            pm = d.premarket_pct(list(liquid), today)
            metr = {s: {"dollar_vol_1d": v} for s, v in liquid.items()}
            up = scanner.premarket_screen(pm, metr)[:n]
            down = scanner.premarket_screen(pm, metr, ascending=True)[:n]
            have = {(i["symbol"], i["bias"]) for i in wl}
            new = sorted({s for s in up + down if (s, "long") not in have and (s, "short") not in have})
            if new:
                full = {s: m for s, b in d.daily_bars(new, 400).items() if (m := scanner.full_metrics(b))}
                mcaps = {s: d.market_cap(s) for s in full}
                extra = scanner.build_watchlist(full, [s for s in up if s in full], [s for s in down if s in full],
                                                n, self.settings.shorts_enabled, mcaps)
                wl += [i for i in extra if i["symbol"] not in rules.INVERSE_ETF_MAP]
        try:
            cal = d.earnings(today, today + timedelta(days=14))
            self.set_earnings(cal, True)
            for it in wl:
                e = cal.get(it["symbol"])
                it["next_earnings"] = e["date"].isoformat() if e else None
        except Exception as e:
            self.set_earnings({}, False)
            self.ledger.event("warning", None, where="earnings", error=str(e))
        self.ledger.save_watchlist(today.isoformat(), wl)
        self.load_watchlist(today.isoformat(), wl)
        try:
            hourly = d.hourly_bars(self.stream_symbols())
            for s, bars in hourly.items():
                closes = [float(b["c"]) for b in bars if 9 <= to_et(b["t"]).hour <= 15]
                if closes:
                    st = self.session(s)
                    st.hourly_ema9, st.hourly_ema21 = rules.ema(closes, 9), rules.ema(closes, 21)
        except Exception as e:
            self.ledger.event("warning", None, where="hourly_ema", error=str(e))
        self.ledger.event("scan_pre_open", None, session=today.isoformat(), watchlist=len(wl))
        return wl


def _float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def _dedupe(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out
