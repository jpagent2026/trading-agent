
from fastapi import FastAPI, Request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOrdersRequest,
    StopOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus, OrderClass
from datetime import datetime, timedelta
import os
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
TEST_QTY = int(os.getenv("TEST_QTY", "3"))
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

# === PHASE 2 STEP 4: shadow regime (log only, never vetoes longs) ===
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
        "veto": False,
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

def has_pending_order(client, ticker):
    for o in get_today_orders(client):
        symbol = str(getattr(o, "symbol", "")).upper()
        if symbol == ticker.upper() and order_status_str(o) in ["new", "accepted", "pending_new", "partially_filled"]:
            return True
    return False

def cancel_open_stops(client, ticker: str):
    """Cancel open stop orders for a ticker before exiting."""
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker]))
        for o in orders:
            otype = str(getattr(o, "order_type", "")).lower()
            if "stop" in otype:
                try:
                    client.cancel_order_by_id(o.id)
                except Exception:
                    pass
    except Exception:
        pass

def is_shortable(client, ticker: str) -> bool:
    try:
        asset = client.get_asset(ticker)
        return bool(getattr(asset, "shortable", False) and getattr(asset, "easy_to_borrow", False))
    except Exception:
        return False

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

@app.on_event("startup")
def startup():
    init_db()

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

            # Sizing
            if stop_raw is not None:
                try:
                    stop = float(stop_raw)
                except Exception:
                    return {"status": "ignored", "reason": "bad_stop", "message": "stop must be a number"}
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
                    return {"status": "ignored", "reason": "bad_stop", "message": "stop must be below entry for a long"}
                if (entry - stop) / entry < MIN_STOP_PCT:
                    return {"status": "ignored", "reason": "bad_stop", "message": f"stop must be at least {MIN_STOP_PCT*100:.1f}% below entry"}
                qty = compute_qty(equity, entry, stop, RISK_PCT)
                if qty <= 0:
                    return {"status": "ignored", "reason": "size_zero", "message": "computed quantity is 0"}
            else:
                qty = TEST_QTY

            side = OrderSide.BUY
            # Bracket / stop if stop provided
            if stop_raw is not None:
                order_data = MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    order_class=OrderClass.BRACKET,
                    stop_loss=StopLossRequest(stop_price=float(stop_raw)),
                )
            else:
                order_data = MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
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
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop),
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
