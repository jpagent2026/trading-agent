
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
    StopOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from datetime import datetime, timedelta, time as dtime
import os
import threading
import time as time_module
import pytz
import sqlite3
from pathlib import Path
import httpx
import json
import math

app = FastAPI()

# === RISK SETTINGS (env-overridable) ===
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "8"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "3"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))
RISK_PCT = float(os.getenv("RISK_PCT", "0.01"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.25"))
MIN_STOP_PCT = float(os.getenv("MIN_STOP_PCT", "0.015"))
SHORTS_ENABLED = os.getenv("SHORTS_ENABLED", "false").lower() in ("true", "1", "yes")
SHORT_RISK_PCT = float(os.getenv("SHORT_RISK_PCT", "0.005"))

# === PHASE 2 STEP 1: watchlist gate (display only for /status) ===
DEFAULT_WATCHLIST = ("AAPL", "MSFT", "NVDA")

# === PHASE 2 STEP 2: signal log ===
SIGNAL_LOG = []
SIGNAL_LOG_MAX = 200

# === PHASE 2 STEP 4: market regime filter ===
# A RISK-OFF reading (risk_on == "no") sets veto=True, and /approve then refuses NEW LONG BUYS.
# Sells, covers, and broker stop/target exits are never blocked. If the reading cannot be
# computed (regime "unknown"), veto stays False and buys are allowed (fail-open), with a log line.
REGIME_CACHE = {"at": None, "note": None}
REGIME_TTL_SECONDS = 1800

# === PHASE 2 STEP 5: industry map (log only, never vetoes) ===
INDUSTRY_FEED = "https://heartbeat-backtest-production.up.railway.app/industry_ranking.json"
STOCK_INDUSTRY = {
    "NVDA": "SMH",
    "MSFT": "IGV",
}
INDUSTRY_CACHE = {"at": None, "payload": None}
INDUSTRY_TTL_SECONDS = 6 * 3600

# === Persistent decision log ===
DB_PATH = Path("/data/agent200.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT,
                ticker TEXT,
                action TEXT,
                result TEXT,
                reason TEXT,
                regime TEXT,
                risk_on TEXT,
                industry_etf TEXT,
                industry_tier TEXT,
                would_skip INTEGER,
                raw_json TEXT
            )
        """)
        conn.commit()

def persist_signal(row: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO signals (
                time, ticker, action, result, reason,
                regime, risk_on, industry_etf, industry_tier, would_skip, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row.get("time"),
            row.get("ticker"),
            row.get("action"),
            row.get("result"),
            row.get("reason"),
            row.get("regime"),
            row.get("risk_on"),
            row.get("industry_etf"),
            row.get("industry_tier"),
            1 if row.get("would_skip") else 0,
            str(row),
        ))
        conn.commit()

def get_recent_signals(limit: int = 50):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

def get_latest_signal_for(ticker: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM signals WHERE ticker = ? ORDER BY id DESC LIMIT 1",
            (ticker.upper(),)
        ).fetchone()
        return dict(row) if row else None

def end_of_day_summary():
    today = datetime.now(ET).strftime("%Y-%m-%d")
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM signals WHERE time LIKE ? ORDER BY id",
            (f"{today}%",)
        ).fetchall()
    signals = [dict(r) for r in rows]
    allowed = [s for s in signals if s["result"] == "allowed"]
    blocked = [s for s in signals if s["result"] == "blocked"]
    return {
        "date": today,
        "total_signals": len(signals),
        "allowed": len(allowed),
        "blocked": len(blocked),
        "signals": signals[-30:],
    }

async def generate_memo(ticker: str, signal_row: dict) -> dict:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "ticker": ticker,
            "thesis": "No ANTHROPIC_API_KEY set",
            "risks": "—",
            "confidence": 0.0,
            "note": "second opinion only — no order impact",
            "based_on_signal": signal_row.get("time"),
        }
    prompt = (
        f"Second-opinion memo only. Do not recommend size or orders.\n"
        f"Ticker: {ticker}\n"
        f"Last signal: {signal_row.get('action')} {signal_row.get('result')} — {signal_row.get('reason')}\n"
        f"Regime: {signal_row.get('regime')} risk_on={signal_row.get('risk_on')}\n"
        f"Industry: {signal_row.get('industry_etf')} tier={signal_row.get('industry_tier')}\n"
        f"Return only valid JSON with keys: thesis, risks, confidence (0.0-1.0), note.\n"
        f"Example: {{\"thesis\": \"one sentence\", \"risks\": \"one sentence\", \"confidence\": 0.6, \"note\": \"second opinion only\"}}"
    )
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["content"][0]["text"]
            start = text.find("{")
            end = text.rfind("}") + 1
            parsed = json.loads(text[start:end]) if start >= 0 else {}
            return {
                "ticker": ticker,
                "thesis": parsed.get("thesis", ""),
                "risks": parsed.get("risks", ""),
                "confidence": float(parsed.get("confidence", 0.0)),
                "note": parsed.get("note", "second opinion only"),
                "based_on_signal": signal_row.get("time"),
            }
    except Exception as e:
        return {
            "ticker": ticker,
            "thesis": "memo failed",
            "risks": str(e),
            "confidence": 0.0,
            "note": "second opinion only — no order impact",
            "based_on_signal": signal_row.get("time"),
        }

def load_watchlist():
    raw = os.getenv("WATCHLIST", "")
    parts = [p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip()]
    return set(parts) if parts else set(DEFAULT_WATCHLIST)

WATCHLIST = load_watchlist()

def normalize_ticker(ticker):
    """Keep class shares intact. BRK.B stays BRK.B; BF-B becomes BF.B."""
    t = (ticker or "").strip().upper()
    if ":" in t:
        t = t.split(":")[-1]
    # Convert common class-share dash form to dot form
    if "-" in t and len(t.split("-")[-1]) == 1:
        t = t.replace("-", ".")
    return t

def on_watchlist(ticker):
    return ticker in WATCHLIST

ET = pytz.timezone("America/New_York")

def now_et():
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

def classify_regime(closes):
    if len(closes) < 21:
        return {"regime": "unknown", "risk_on": "unknown", "basis": "QQQ", "note": "not enough bars"}
    last = closes[-1]
    sma20 = sum(closes[-20:]) / 20
    prior_sma20 = sum(closes[-21:-1]) / 20
    week_ago = closes[-6]
    five_day = (last - week_ago) / week_ago * 100 if week_ago else 0
    window = closes[-10:]
    span = (max(window) - min(window)) / last * 100 if last else 0
    if span >= 6:
        regime = "volatile"
    elif last > sma20 and sma20 >= prior_sma20 and five_day > 0:
        regime = "trend_up"
    elif last < sma20 and sma20 <= prior_sma20 and five_day < 0:
        regime = "trend_down"
    else:
        regime = "chop"
    risk_on = "yes" if regime == "trend_up" else "no"
    return {
        "regime": regime,
        "risk_on": risk_on,
        "basis": "QQQ",
        "last": round(last, 2),
        "sma20": round(sma20, 2),
        "five_day_pct": round(five_day, 2),
        "ten_day_span_pct": round(span, 2),
        # Veto = the filter's own RISK-OFF reading (anything other than trend_up). No new thresholds.
        "veto": risk_on == "no",
    }

def shadow_regime():
    cached = REGIME_CACHE.get("note")
    cached_at = REGIME_CACHE.get("at")
    if cached and cached_at and (datetime.now(ET) - cached_at).total_seconds() < REGIME_TTL_SECONDS:
        return cached
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        data_client = StockHistoricalDataClient(
            get_alpaca_key(),
            get_alpaca_secret(),
        )
        end = datetime.now(ET)
        start = end - timedelta(days=50)
        request = StockBarsRequest(
            symbol_or_symbols="QQQ",
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = data_client.get_stock_bars(request)
        closes = [float(b.close) for b in bars["QQQ"]]
        note = classify_regime(closes)
        REGIME_CACHE["note"] = note
        REGIME_CACHE["at"] = datetime.now(ET)
        return note
    except Exception as e:
        return {"regime": "unknown", "risk_on": "unknown", "note": str(e), "veto": False}

def qqq_above_sma50() -> bool:
    """True if QQQ is above its 50-day SMA. Used only to block shorts."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        data_client = StockHistoricalDataClient(get_alpaca_key(), get_alpaca_secret())
        end = datetime.now(ET)
        start = end - timedelta(days=80)
        request = StockBarsRequest(
            symbol_or_symbols="QQQ",
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
        )
        bars = data_client.get_stock_bars(request)
        closes = [float(b.close) for b in bars["QQQ"]]
        if len(closes) < 50:
            return False
        sma50 = sum(closes[-50:]) / 50
        return closes[-1] > sma50
    except Exception:
        return False

def industry_note(ticker):
    etf = STOCK_INDUSTRY.get(ticker, "neutral")
    return {"etf": etf, "tier": "neutral", "would_skip": False}

def log_signal(action, ticker, result, reason, extra=None):
    regime = shadow_regime()
    ind = industry_note(ticker)
    row = {
        "time": now_et(),
        "ticker": ticker,
        "action": action,
        "result": result,
        "reason": reason,
        "regime": regime.get("regime"),
        "risk_on": regime.get("risk_on"),
        "industry_etf": ind.get("etf"),
        "industry_tier": ind.get("tier"),
        "would_skip": ind.get("would_skip", False),
    }
    if extra:
        row.update(extra)
    SIGNAL_LOG.append(row)
    if len(SIGNAL_LOG) > SIGNAL_LOG_MAX:
        SIGNAL_LOG.pop(0)
    print(
        f"SIGNAL time={row['time']} ticker={ticker} action={action} result={result} "
        f"reason={reason} regime={row['regime']} risk_on={row['risk_on']} "
        f"industry={row['industry_etf']} tier={row['industry_tier']} "
        f"would_skip={str(row['would_skip']).lower()}"
    )
    try:
        persist_signal(row)
    except Exception as e:
        print(f"LOG_ERROR persist_signal failed: {e}")
        row["log_error"] = True
    return row

def get_alpaca_key():
    return os.getenv("ALPACA_API_KEY") or os.getenv("ALPACA_PAPER_KEY_ID")

def get_alpaca_secret():
    return os.getenv("ALPACA_SECRET_KEY") or os.getenv("ALPACA_PAPER_SECRET_KEY")

def get_trading_client():
    return TradingClient(get_alpaca_key(), get_alpaca_secret(), paper=True)

def is_market_open(client=None):
    """Holiday-aware. Uses Alpaca clock."""
    try:
        c = client or get_trading_client()
        clock = c.get_clock()
        return bool(clock.is_open)
    except Exception:
        # Fallback to simple hours if clock fails
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now <= market_close

def get_today_start():
    return datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)

def get_open_positions_count(client):
    return len(client.get_all_positions())

def get_position_qty(client, ticker):
    try:
        position = client.get_open_position(ticker)
        qty = float(position.qty)
        return qty
    except Exception:
        return 0.0

def check_daily_loss_limit(client):
    account = client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    if last_equity <= 0:
        return True, 0.0
    change_pct = ((equity - last_equity) / last_equity) * 100
    if change_pct <= -DAILY_LOSS_LIMIT_PCT:
        return False, change_pct
    return True, change_pct

def get_today_orders(client):
    request = GetOrdersRequest(
        status=QueryOrderStatus.ALL,
        after=get_today_start()
    )
    return client.get_orders(request)

def order_status_str(order):
    return str(getattr(order.status, "value", order.status)).lower()

def order_side_str(order):
    return str(getattr(order.side, "value", order.side)).lower()

def count_entry_trades_today(client):
    """Count only buys and shorts (entries). Sells and covers do not count."""
    count = 0
    for o in get_today_orders(client):
        if order_status_str(o) == "filled" and order_side_str(o) == "buy":
            # Heuristic: buy that opened or added is an entry. We count all filled buys
            # as entries for simplicity; covers of shorts are also buys but rare.
            # For strictness we count filled buys and filled sells that opened shorts.
            count += 1
        elif order_status_str(o) == "filled" and order_side_str(o) == "sell":
            # A filled sell could be an exit or a short entry. We treat short entries
            # as counted via the action we log; for order counting we only count buys
            # as entries here. Shorts are counted separately when submitted.
            pass
    return count

def ticker_side_today(client, ticker, side):
    for o in get_today_orders(client):
        symbol = str(getattr(o, "symbol", "")).upper()
        if (
            symbol == ticker.upper()
            and order_side_str(o) == side
            and order_status_str(o) in [
                "filled", "partially_filled", "new", "accepted", "pending_new"
            ]
        ):
            return True
    return False

def sold_ticker_today(client, ticker):
    return ticker_side_today(client, ticker, "sell")

def bought_ticker_today(client, ticker):
    return ticker_side_today(client, ticker, "buy")

def order_type_str(order):
    return str(getattr(getattr(order, "order_type", ""), "value", getattr(order, "order_type", ""))).lower()

def is_stop_order(order):
    return "stop" in order_type_str(order)

# Statuses in which a resting stop (e.g. the stop leg of an OTO entry) is still live.
LIVE_STOP_STATUSES = ("new", "held", "accepted", "pending_new", "partially_filled")

def has_pending_order(client, ticker):
    for o in get_today_orders(client):
        symbol = str(getattr(o, "symbol", "")).upper()
        if symbol != ticker.upper():
            continue
        status = order_status_str(o)
        # A resting protective stop leg is not a "pending order"; exits cancel it first.
        if is_stop_order(o) and status in ("new", "held"):
            continue
        if status in ["new", "accepted", "pending_new", "partially_filled"]:
            return True
    return False

def _flatten_orders(orders):
    """Yield each order and, recursively, any nested legs."""
    for o in orders or []:
        yield o
        legs = getattr(o, "legs", None) or []
        if legs:
            yield from _flatten_orders(legs)

def cancel_open_stops(client, ticker: str):
    """Cancel live stop orders for a ticker (including held OTO/bracket stop legs) before exiting."""
    queries = [
        # Currently open orders (any age, e.g. GTC stops from earlier days), legs listed flat.
        GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker]),
        # Same, rolled up under parents, so held child legs are visible via .legs.
        GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker], nested=True),
        # All of today's orders, filtered locally, in case held legs are omitted from the open query.
        GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[ticker], after=get_today_start(), nested=True),
    ]
    seen = set()
    for req in queries:
        try:
            orders = client.get_orders(req)
        except Exception:
            continue
        for o in _flatten_orders(orders):
            oid = str(getattr(o, "id", ""))
            if not oid or oid in seen:
                continue
            symbol = str(getattr(o, "symbol", ticker) or ticker).upper()
            if symbol != ticker.upper():
                continue
            if not is_stop_order(o) or order_status_str(o) not in LIVE_STOP_STATUSES:
                continue
            seen.add(oid)
            try:
                client.cancel_order_by_id(o.id)
            except Exception:
                pass

def is_shortable(client, ticker: str) -> bool:
    try:
        asset = client.get_asset(ticker)
        return bool(getattr(asset, "shortable", False) and getattr(asset, "easy_to_borrow", False))
    except Exception:
        return False

def parse_stop_value(stop_raw):
    """Return (stop, None) for a usable positive number, else (None, (reason, message))."""
    if stop_raw is None or (isinstance(stop_raw, str) and not stop_raw.strip()):
        return None, ("stop_required", "a buy must include a stop price; buys without a stop are rejected")
    if isinstance(stop_raw, bool):
        return None, ("bad_stop", "stop must be a number")
    try:
        stop = float(stop_raw)
    except Exception:
        return None, ("bad_stop", "stop must be a number")
    if not math.isfinite(stop):
        return None, ("bad_stop", "stop must be a finite number")
    if stop <= 0:
        return None, ("bad_stop", "stop must be greater than 0")
    return stop, None

def bad_stop_rejection(action: str, ticker: str, stop_raw, reason: str, message: str, entry=None):
    """422 response for a buy whose stop is missing or invalid. Logged like every other block."""
    log_signal(action, ticker, "blocked", f"approve: {reason} ({message})")
    content = {
        "status": "rejected",
        "reason": reason,
        "action": action,
        "ticker": ticker,
        "stop_received": stop_raw if isinstance(stop_raw, (int, float, str, type(None))) else str(stop_raw),
        "message": f"Buy rejected: {message}.",
    }
    if entry is not None:
        content["entry_price"] = entry
    return JSONResponse(status_code=422, content=content)

def compute_qty(equity: float, entry: float, stop: float, risk_pct: float) -> int:
    """Risk-based quantity. Returns 0 if invalid."""
    if entry <= 0 or stop <= 0:
        return 0
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return 0
    dollar_risk = equity * risk_pct
    raw = math.floor(dollar_risk / risk_per_share)
    max_shares = math.floor((equity * MAX_POSITION_PCT) / entry)
    qty = max(0, min(raw, max_shares))
    return int(qty)

def regime_veto_refusal(action: str, ticker: str, reading: dict):
    """409 response for a buy refused by the regime filter. Logged like every other block."""
    regime_name = reading.get("regime")
    message = (
        f"Buy refused: the market regime filter reads RISK-OFF "
        f"(QQQ regime '{regime_name}', risk_on='{reading.get('risk_on')}'), "
        f"so new long buys are blocked. Sells, covers, and stop/target exits are not affected."
    )
    log_signal(action, ticker, "blocked", f"approve: regime_veto regime={regime_name} risk_on={reading.get('risk_on')}")
    return JSONResponse(
        status_code=409,
        content={
            "status": "refused",
            "reason": "regime_veto",
            "action": action,
            "ticker": ticker,
            "veto": True,
            "regime": dict(reading),
            "message": message,
        },
    )

# === GTC STOP RENEWAL (Alpaca's 90-day aged-order policy) ===
# Alpaca cancels every GTC order 90 days after it was created (at 4:15 pm ET on the order's
# `expires_at` date). Replacing an order does not help: the replacement inherits the old
# expires_at. So once a day, before the open, each live protective stop that is older than
# STOP_RENEW_AFTER_DAYS (or expires within STOP_RENEW_WITHIN_DAYS) is cancelled and immediately
# re-placed as a new GTC stop at the SAME stop price, same side, same quantity.
#
# Why cancel-then-place (not place-then-cancel): Alpaca will not accept a second sell order for
# shares that are already reserved by the old stop, so the new stop can only go in after the old
# one is confirmed cancelled. The gap is a second or two, and the job runs pre-market, when
# Alpaca stop orders cannot trigger anyway (they only trigger in regular hours), so the
# position is never exposed during a session in which the old stop could have fired.
#
# Fallbacks (modelled on HK Connection's guarded refresh):
#  - Price already through the stop before we start: leave the old stop alone (it is still live
#    and will fire at the open) and record it. Nothing is cancelled.
#  - The old stop filled while we were cancelling: nothing to re-place; recorded.
#  - The new stop is rejected because the price is through it: submit a DAY market exit for the
#    same quantity (exactly what the stop would have done; queued for the open if pre-market).
#  - Any other failure: the stop is saved as a pending repair (in SQLite, so it survives a
#    restart) and retried every minute until it is placed.
# Stop prices are never changed. No risk rule is read or changed here.
STOP_RENEW_AFTER_DAYS = 80
STOP_RENEW_WITHIN_DAYS = 10
STOP_RENEWAL_START_ET = dtime(9, 0)     # first try: pre-market
STOP_RENEWAL_LATEST_ET = dtime(15, 45)  # catch-up window if the app was down at 9:00
STOP_RENEWAL_LOOP_SECONDS = 60
STOP_CANCEL_WAIT_SECONDS = 5.0
STOP_RENEWAL_STATE = {"thread_started": False, "last_run_date": None, "last_run_at": None,
                      "last_result": None, "last_error": None}
STOP_RENEWAL_LOCK = threading.Lock()
RENEWABLE_STOP_STATUSES = ("new", "accepted", "partially_filled")  # live, active stops (not held legs)


def init_stop_renewal_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stop_renewals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT, symbol TEXT, side TEXT, qty INTEGER, stop_price REAL,
                old_order_id TEXT, new_order_id TEXT, result TEXT, detail TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stop_renewal_pending (
                symbol TEXT PRIMARY KEY, side TEXT, qty INTEGER, stop_price REAL,
                old_order_id TEXT, since TEXT, attempts INTEGER, last_error TEXT
            )
        """)
        conn.commit()


def record_stop_renewal(symbol, side, qty, stop_price, old_id, new_id, result, detail=""):
    row = {"time": datetime.now(ET).isoformat(), "symbol": symbol, "side": side, "qty": qty,
           "stop_price": stop_price, "old_order_id": old_id, "new_order_id": new_id,
           "result": result, "detail": detail}
    print(f"STOP_RENEWAL {result} {symbol} {side} qty={qty} stop={stop_price} old={old_id} new={new_id} {detail}")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO stop_renewals (time, symbol, side, qty, stop_price, old_order_id, new_order_id, result, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (row["time"], symbol, side, qty, stop_price, old_id, new_id, result, detail))
            conn.commit()
    except Exception as e:
        print(f"LOG_ERROR record_stop_renewal failed: {e}")
    return row


def recent_stop_renewals(limit=20):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM stop_renewals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def pending_stop_repairs():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute("SELECT * FROM stop_renewal_pending ORDER BY since").fetchall()]
    except Exception:
        return []


def save_pending_repair(symbol, side, qty, stop_price, old_id, error):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO stop_renewal_pending (symbol, side, qty, stop_price, old_order_id, since, attempts, last_error)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(symbol) DO UPDATE SET attempts = attempts + 1, last_error = excluded.last_error
        """, (symbol, side, qty, stop_price, old_id, datetime.now(ET).isoformat(), error))
        conn.commit()


def clear_pending_repair(symbol):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM stop_renewal_pending WHERE symbol = ?", (symbol,))
        conn.commit()


def _as_dt(value):
    """Order timestamps arrive as datetimes from alpaca-py; accept ISO strings too. Always tz-aware."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = pytz.utc.localize(value)
    return value


def stop_renewal_due(order, now):
    """(due, info). Due when Alpaca will cancel it within STOP_RENEW_WITHIN_DAYS (expires_at),
    or, if expires_at is missing, when it was created at least STOP_RENEW_AFTER_DAYS ago."""
    expires = _as_dt(getattr(order, "expires_at", None))
    created = _as_dt(getattr(order, "created_at", None) or getattr(order, "submitted_at", None))
    info = {}
    if created is not None:
        info["age_days"] = round((now - created).total_seconds() / 86400.0, 1)
    if expires is not None:
        info["expires_at"] = expires.isoformat()
        info["days_to_expiry"] = round((expires - now).total_seconds() / 86400.0, 1)
        return info["days_to_expiry"] <= STOP_RENEW_WITHIN_DAYS, info
    if created is not None:
        return info["age_days"] >= STOP_RENEW_AFTER_DAYS, info
    return False, {"note": "no created_at/expires_at on order; cannot age it"}


def list_renewable_stops(client):
    """Live, active stop orders on the account (including OTO stop legs once the entry has filled)."""
    out, seen = [], set()
    for req in (GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500),
                GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500, nested=True)):
        try:
            orders = client.get_orders(req)
        except Exception:
            continue
        for o in _flatten_orders(orders):
            oid = str(getattr(o, "id", ""))
            if not oid or oid in seen:
                continue
            if not is_stop_order(o) or order_status_str(o) not in RENEWABLE_STOP_STATUSES:
                continue
            seen.add(oid)
            out.append(o)
    return out


def latest_trade_price(ticker):
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        data_client = StockHistoricalDataClient(get_alpaca_key(), get_alpaca_secret())
        trade = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
        return float(trade[ticker].price)
    except Exception:
        return None


def stop_is_through(side, price, stop_price):
    """True when the market is already at/through the stop (a sell stop at/above price, a buy stop at/below)."""
    if price is None or stop_price is None:
        return False
    return price <= stop_price if side == "sell" else price >= stop_price


def is_stop_through_market_error(err):
    text = str(err).lower()
    return "stop price" in text and any(k in text for k in (
        "must be less than", "must be greater than", "must not be greater", "must not be less",
        "base price", "current price", "market price"))


def wait_for_cancel(client, order_id, timeout=None, sleep=time_module.sleep):
    """Poll until the order is no longer live. Returns its final status string."""
    timeout = STOP_CANCEL_WAIT_SECONDS if timeout is None else timeout
    waited = 0.0
    status = "unknown"
    while True:
        try:
            status = order_status_str(client.get_order_by_id(order_id))
        except Exception:
            status = "unknown"
        if status in ("canceled", "cancelled", "filled", "expired", "replaced", "rejected"):
            return status
        if waited >= timeout:
            return status
        sleep(0.25)
        waited += 0.25


def build_renewal_stop(symbol, side, qty, stop_price):
    return StopOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
        time_in_force=TimeInForce.GTC, stop_price=round(float(stop_price), 2),
    )


def place_renewal_stop(client, symbol, side, qty, stop_price, old_id, context="renewal"):
    """Place the replacement GTC stop, with the fallbacks described above. Returns a result row."""
    last_err = None
    for _ in range(2):
        try:
            new = client.submit_order(build_renewal_stop(symbol, side, qty, stop_price))
            clear_pending_repair(symbol)
            return record_stop_renewal(symbol, side, qty, stop_price, old_id, str(getattr(new, "id", "")),
                                       "renewed" if context == "renewal" else "repaired")
        except Exception as e:
            last_err = e
            px = latest_trade_price(symbol)
            if is_stop_through_market_error(e) or stop_is_through(side, px, stop_price):
                try:
                    exit_order = client.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=qty, side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
                        time_in_force=TimeInForce.DAY))
                    clear_pending_repair(symbol)
                    return record_stop_renewal(symbol, side, qty, stop_price, old_id, str(getattr(exit_order, "id", "")),
                                               "stop_through_market_exit",
                                               f"new stop rejected ({e}); last={px}; market exit submitted")
                except Exception as e2:
                    last_err = e2
                    break
    save_pending_repair(symbol, side, qty, stop_price, old_id, str(last_err))
    return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "place_failed_pending_retry", str(last_err))


def renew_stop(client, order, now, sleep=time_module.sleep):
    symbol = str(getattr(order, "symbol", "")).upper()
    side = order_side_str(order)
    old_id = str(getattr(order, "id", ""))
    try:
        stop_price = float(getattr(order, "stop_price", None))
        qty = int(round(float(getattr(order, "qty", 0) or 0) - float(getattr(order, "filled_qty", 0) or 0)))
    except Exception:
        return record_stop_renewal(symbol, side, None, None, old_id, None, "skipped_bad_order", "missing qty/stop_price")
    held = get_position_qty(client, symbol)
    if (side == "sell" and held <= 0) or (side == "buy" and held >= 0):
        return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "skipped_no_position")
    qty = min(qty, int(abs(held)))
    if qty <= 0:
        return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "skipped_zero_qty")
    px = latest_trade_price(symbol)
    if stop_is_through(side, px, stop_price):
        # Old stop is still live and will trigger at the open: do not cancel it.
        return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "skipped_price_through_stop",
                                   f"last={px}; existing stop left in place")
    try:
        client.cancel_order_by_id(old_id)
    except Exception as e:
        st = wait_for_cancel(client, old_id, timeout=0, sleep=sleep)
        if st == "filled":
            return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "old_stop_filled")
        if st not in ("canceled", "cancelled", "expired"):
            return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "cancel_failed_left_in_place", str(e))
    st = wait_for_cancel(client, old_id, sleep=sleep)
    if st == "filled":
        return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "old_stop_filled")
    held = get_position_qty(client, symbol)  # re-check: a fill may have happened while cancelling
    if (side == "sell" and held <= 0) or (side == "buy" and held >= 0):
        return record_stop_renewal(symbol, side, qty, stop_price, old_id, None, "position_closed_during_renewal")
    qty = min(qty, int(abs(held)))
    return place_renewal_stop(client, symbol, side, qty, stop_price, old_id)


def run_stop_renewal(client=None, now=None, sleep=time_module.sleep):
    """Renew every due stop once. Safe to call repeatedly (fresh stops are simply not due)."""
    with STOP_RENEWAL_LOCK:
        client = client or get_trading_client()
        now = now or datetime.now(ET)
        results, checked = [], 0
        for o in list_renewable_stops(client):
            checked += 1
            due, info = stop_renewal_due(o, now)
            if not due:
                continue
            try:
                row = renew_stop(client, o, now, sleep=sleep)
            except Exception as e:
                row = record_stop_renewal(str(getattr(o, "symbol", "")), order_side_str(o), None,
                                          getattr(o, "stop_price", None), str(getattr(o, "id", "")), None,
                                          "error", str(e))
            row.update(info)
            results.append(row)
        summary = {"at": now.isoformat(), "stops_checked": checked, "renewed_or_handled": len(results),
                   "results": results}
        STOP_RENEWAL_STATE["last_result"] = summary
        STOP_RENEWAL_STATE["last_run_at"] = now.isoformat()
        return summary


def retry_pending_repairs(client=None, sleep=time_module.sleep):
    pending = pending_stop_repairs()
    if not pending:
        return []
    out = []
    with STOP_RENEWAL_LOCK:
        client = client or get_trading_client()
        for p in pending:
            held = get_position_qty(client, p["symbol"])
            if (p["side"] == "sell" and held <= 0) or (p["side"] == "buy" and held >= 0):
                clear_pending_repair(p["symbol"])
                out.append(record_stop_renewal(p["symbol"], p["side"], p["qty"], p["stop_price"],
                                               p["old_order_id"], None, "repair_dropped_no_position"))
                continue
            qty = min(int(p["qty"]), int(abs(held)))
            out.append(place_renewal_stop(client, p["symbol"], p["side"], qty, p["stop_price"],
                                          p["old_order_id"], context="repair"))
    return out


def stop_renewal_tick(now=None, client=None):
    """One scheduler step: retry any pending repairs, then run the daily renewal if it is due."""
    now = now or datetime.now(ET)
    ran = []
    try:
        if pending_stop_repairs():
            retry_pending_repairs(client)
            ran.append("repairs")
        today = now.date().isoformat()
        if (now.weekday() < 5 and STOP_RENEWAL_STATE["last_run_date"] != today
                and STOP_RENEWAL_START_ET <= now.time() <= STOP_RENEWAL_LATEST_ET):
            STOP_RENEWAL_STATE["last_run_date"] = today
            run_stop_renewal(client, now)
            ran.append("renewal")
        STOP_RENEWAL_STATE["last_error"] = None
    except Exception as e:
        STOP_RENEWAL_STATE["last_error"] = f"{type(e).__name__}: {e}"
        print(f"STOP_RENEWAL tick failed: {e}")
    return ran


def _stop_renewal_loop():
    while True:
        stop_renewal_tick()
        time_module.sleep(STOP_RENEWAL_LOOP_SECONDS)


def start_stop_renewal_thread():
    if STOP_RENEWAL_STATE["thread_started"]:
        return None
    t = threading.Thread(target=_stop_renewal_loop, name="stop-renewal", daemon=True)
    t.start()
    STOP_RENEWAL_STATE["thread_started"] = True
    return t


@app.on_event("startup")
def startup():
    init_db()
    init_stop_renewal_db()
    start_stop_renewal_thread()

@app.get("/")
def root():
    return {"status": "ok", "agent": "200", "mode": "paper"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/status")
def status():
    try:
        client = get_trading_client()
        account = client.get_account()
        positions = client.get_all_positions()
        return {
            "equity": float(account.equity),
            "buying_power": float(account.buying_power),
            "open_positions": len(positions),
            "trades_today": count_entry_trades_today(client),
            "watchlist": sorted(WATCHLIST),  # display only
            "regime": shadow_regime(),
            "industry": {name: industry_note(name) for name in sorted(WATCHLIST)},
            "shorts_enabled": SHORTS_ENABLED,
            "max_positions": MAX_POSITIONS,
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/signals")
def signals():
    return {"signals": SIGNAL_LOG[-50:]}

@app.get("/signals/persistent")
def signals_persistent(limit: int = 50):
    return {"signals": get_recent_signals(limit)}

@app.get("/summary/today")
def summary_today():
    return end_of_day_summary()

@app.get("/positions")
def positions():
    try:
        client = get_trading_client()
        pos_list = client.get_all_positions()
        out = []
        for p in pos_list:
            stop_price = None
            try:
                open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[p.symbol]))
                for o in open_orders:
                    if "stop" in str(getattr(o, "order_type", "")).lower():
                        stop_price = float(getattr(o, "stop_price", 0) or 0) or None
                        break
            except Exception:
                pass
            out.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": "long" if float(p.qty) > 0 else "short",
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price) if p.current_price else None,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else None,
                "stop_price": stop_price,
            })
        return {"positions": out}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/memo")
async def memo(request: Request):
    data = await request.json()
    ticker = normalize_ticker(data.get("ticker", ""))
    if not ticker:
        return {"status": "ignored", "reason": "ticker_required"}
    signal_row = get_latest_signal_for(ticker)
    if not signal_row:
        return {"status": "no_signal", "ticker": ticker}
    memo_result = await generate_memo(ticker, signal_row)
    return {"status": "ok", "memo": memo_result}

@app.post("/approve")
async def approve(request: Request):
    data = await request.json()
    action = (data.get("action") or "").lower().strip()
    ticker = normalize_ticker(data.get("ticker", ""))
    stop_raw = data.get("stop")

    if action not in ("buy", "sell", "short", "cover"):
        log_signal(action, ticker, "blocked", "approve: action must be buy, sell, short, or cover")
        return {"status": "ignored", "reason": "bad_action", "message": "action must be buy, sell, short, or cover"}
    if not ticker:
        log_signal(action, ticker, "blocked", "approve: ticker required")
        return {"status": "error", "reason": "ticker_required", "message": "ticker required"}

    client = get_trading_client()

    if not is_market_open(client):
        log_signal(action, ticker, "blocked", "approve: market_closed")
        return {"status": "ignored", "reason": "market_closed", "message": "Market is closed"}

    try:
        can_trade, daily_change = check_daily_loss_limit(client)
        if not can_trade:
            reason = f"approve: daily loss limit {daily_change:.2f}%"
            log_signal(action, ticker, "blocked", reason)
            return {"status": "halted", "reason": "daily_loss_limit", "message": reason}

        # Entries (buy/short) count toward daily limit; exits do not
        if action in ("buy", "short"):
            entries_today = count_entry_trades_today(client)
            if entries_today >= MAX_TRADES_PER_DAY:
                log_signal(action, ticker, "blocked", f"approve: max trades per day ({MAX_TRADES_PER_DAY})")
                return {"status": "ignored", "reason": "max_trades", "message": f"Max trades per day ({MAX_TRADES_PER_DAY}) reached"}

        if has_pending_order(client, ticker):
            log_signal(action, ticker, "blocked", "approve: pending order exists")
            return {"status": "ignored", "reason": "pending_order", "message": f"Pending order exists for {ticker}"}

        account = client.get_account()
        equity = float(account.equity)

        # ---------- BUY ----------
        if action == "buy":
            # Regime veto: a RISK-OFF reading refuses new long buys (exits are handled below, unaffected).
            regime_reading = shadow_regime()
            if regime_reading.get("veto") is True:
                return regime_veto_refusal(action, ticker, regime_reading)
            if regime_reading.get("risk_on") not in ("yes", "no"):
                print(
                    f"REGIME_UNAVAILABLE ticker={ticker} action=buy "
                    f"regime={regime_reading.get('regime')} note={regime_reading.get('note')} "
                    f"-> veto not applied (fail-open), buy continues to the normal checks"
                )
            current_qty = get_position_qty(client, ticker)
            if current_qty > 0:
                log_signal(action, ticker, "blocked", "approve: already long")
                return {"status": "ignored", "reason": "already_long", "message": f"Already long {ticker}"}
            if sold_ticker_today(client, ticker):
                log_signal(action, ticker, "blocked", "approve: already sold today")
                return {"status": "ignored", "reason": "sold_today", "message": f"Already sold {ticker} today"}
            open_count = get_open_positions_count(client)
            if open_count >= MAX_POSITIONS:
                log_signal(action, ticker, "blocked", f"approve: max positions ({MAX_POSITIONS})")
                return {"status": "ignored", "reason": "max_positions", "message": f"Max positions ({MAX_POSITIONS}) reached"}

            # Sizing: every buy must carry a valid protective stop. There is no unprotected
            # fixed-size fallback any more; a missing or invalid stop is rejected with a 422.
            stop, stop_error = parse_stop_value(stop_raw)
            if stop_error:
                return bad_stop_rejection(action, ticker, stop_raw, *stop_error)
            # Need a reference entry; use last trade price or reject if missing
            # For simplicity we require the caller to have a reasonable stop relative to current price.
            # We fetch a quote if possible; fall back to rejecting if we cannot size.
            entry = None
            try:
                # Use latest trade via data client if available; otherwise require stop only and use account last price proxy
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockLatestTradeRequest
                data_client = StockHistoricalDataClient(get_alpaca_key(), get_alpaca_secret())
                trade = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
                entry = float(trade[ticker].price)
            except Exception:
                entry = None
            if entry is None:
                return {"status": "ignored", "reason": "no_price", "message": "Could not determine entry price for sizing"}
            if stop >= entry:
                return bad_stop_rejection(
                    action, ticker, stop_raw, "bad_stop",
                    f"stop {stop} must be below the current price {entry} for a long", entry=entry,
                )
            if (entry - stop) / entry < MIN_STOP_PCT:
                return bad_stop_rejection(
                    action, ticker, stop_raw, "bad_stop",
                    f"stop must be at least {MIN_STOP_PCT*100:.1f}% below entry", entry=entry,
                )
            qty = compute_qty(equity, entry, stop, RISK_PCT)
            if qty <= 0:
                return {"status": "ignored", "reason": "size_zero", "message": "computed quantity is 0"}

            side = OrderSide.BUY
            # OTO entry with a GTC protective stop (always present for buys now)
            order_data = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC,
                order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=round(stop, 2)),
            )

        # ---------- SHORT ----------
        elif action == "short":
            if not SHORTS_ENABLED:
                return {"status": "ignored", "reason": "shorts_disabled", "message": "SHORTS_ENABLED is false"}
            if qqq_above_sma50():
                log_signal(action, ticker, "blocked", "regime_blocks_short")
                return {"status": "ignored", "reason": "regime_blocks_short", "message": "QQQ above 50-day SMA; shorts blocked"}
            if not is_shortable(client, ticker):
                return {"status": "ignored", "reason": "not_shortable", "message": f"{ticker} is not shortable / easy to borrow"}
            current_qty = get_position_qty(client, ticker)
            if current_qty < 0:
                return {"status": "ignored", "reason": "already_short", "message": f"Already short {ticker}"}
            open_count = get_open_positions_count(client)
            if open_count >= MAX_POSITIONS:
                return {"status": "ignored", "reason": "max_positions", "message": f"Max positions ({MAX_POSITIONS}) reached"}

            if stop_raw is None:
                return {"status": "ignored", "reason": "bad_stop", "message": "short requires a stop above entry"}
            try:
                stop = float(stop_raw)
            except Exception:
                return {"status": "ignored", "reason": "bad_stop", "message": "stop must be a number"}

            entry = None
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockLatestTradeRequest
                data_client = StockHistoricalDataClient(get_alpaca_key(), get_alpaca_secret())
                trade = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
                entry = float(trade[ticker].price)
            except Exception:
                entry = None
            if entry is None:
                return {"status": "ignored", "reason": "no_price", "message": "Could not determine entry price for sizing"}
            if stop <= entry:
                return {"status": "ignored", "reason": "bad_stop", "message": "stop must be above entry for a short"}
            if (stop - entry) / entry < MIN_STOP_PCT:
                return {"status": "ignored", "reason": "bad_stop", "message": f"stop must be at least {MIN_STOP_PCT*100:.1f}% above entry"}

            qty = compute_qty(equity, entry, stop, SHORT_RISK_PCT)
            if qty <= 0:
                return {"status": "ignored", "reason": "size_zero", "message": "computed quantity is 0"}

            side = OrderSide.SELL
            order_data = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.GTC,
                order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=round(stop, 2)),
            )

        # ---------- SELL (close long) ----------
        elif action == "sell":
            current_qty = get_position_qty(client, ticker)
            if current_qty <= 0:
                return {"status": "ignored", "reason": "no_position", "message": f"No long position in {ticker}"}
            cancel_open_stops(client, ticker)
            qty = int(abs(current_qty))
            side = OrderSide.SELL
            order_data = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )

        # ---------- COVER (close short) ----------
        elif action == "cover":
            current_qty = get_position_qty(client, ticker)
            if current_qty >= 0:
                return {"status": "ignored", "reason": "no_position", "message": f"No short position in {ticker}"}
            cancel_open_stops(client, ticker)
            qty = int(abs(current_qty))
            side = OrderSide.BUY
            order_data = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )

        else:
            return {"status": "ignored", "reason": "bad_action"}

        # Submit
        order = client.submit_order(order_data)
        log_row = log_signal(action, ticker, "allowed", f"approve paper {action} {qty} order {order.id}")
        resp = {
            "status": "success",
            "message": f"Approved paper trade placed: {action} {qty} {ticker}",
            "order_id": str(order.id),
            "qty": qty,
        }
        if log_row.get("log_error"):
            resp["log_error"] = True
        return resp

    except Exception as e:
        log_signal(action, ticker, "blocked", f"approve order failed {type(e).__name__}")
        return {"status": "error", "message": str(e)}

@app.get("/regime")
def regime():
    return shadow_regime()

@app.get("/industry")
def industry():
    return {
        "feed": INDUSTRY_FEED,
        "map": {name: industry_note(name) for name in sorted(WATCHLIST)},
    }

@app.get("/stops/renewal")
def stops_renewal():
    """Read-only: what the daily GTC stop renewal did last, and any stop still waiting to be re-placed."""
    return {
        "renew_after_days": STOP_RENEW_AFTER_DAYS,
        "renew_within_days_of_expiry": STOP_RENEW_WITHIN_DAYS,
        "schedule_et": f"weekdays from {STOP_RENEWAL_START_ET.strftime('%H:%M')} (catch-up until {STOP_RENEWAL_LATEST_ET.strftime('%H:%M')})",
        "thread_started": STOP_RENEWAL_STATE["thread_started"],
        "last_run_at": STOP_RENEWAL_STATE["last_run_at"],
        "last_result": STOP_RENEWAL_STATE["last_result"],
        "last_error": STOP_RENEWAL_STATE["last_error"],
        "pending_repairs": pending_stop_repairs(),
        "recent": recent_stop_renewals(20),
    }

@app.post("/webhook")
async def webhook(request: Request):
    """Legacy TradingView path. Watchlist still applies here."""
    data = await request.json()
    action = (data.get("action") or "").lower().strip()
    ticker = normalize_ticker(data.get("ticker", ""))
    if not on_watchlist(ticker):
        log_signal(action, ticker, "blocked", "not_on_watchlist")
        return {"status": "ignored", "reason": "not_on_watchlist", "ticker": ticker}
    # Forward to approve logic by constructing a minimal request body
    # (re-use the same client path for consistency)
    # For simplicity, re-call the core checks by building a fake request is hard;
    # we implement a thin wrapper that posts internally via the same rules.
    # Direct call of the approve handler logic is duplicated lightly here for compatibility.
    return await approve(request)
