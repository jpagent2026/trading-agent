from fastapi import FastAPI, Request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from datetime import datetime, timedelta
import os
import pytz
import sqlite3
from pathlib import Path
import httpx
import json

app = FastAPI()

# === RISK SETTINGS ===
MAX_POSITIONS = 4
MAX_TRADES_PER_DAY = 3
DAILY_LOSS_LIMIT_PCT = 3.0
TEST_QTY = 3

# === PHASE 2 STEP 1: watchlist gate ===
DEFAULT_WATCHLIST = ("AAPL", "MSFT", "NVDA")

# === PHASE 2 STEP 2: signal log (log only, no new veto) ===
SIGNAL_LOG = []
SIGNAL_LOG_MAX = 200

# === PHASE 2 STEP 4: shadow regime (log only, never vetoes) ===
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
            (ticker,)
        ).fetchone()
        return dict(row) if row else None

def load_watchlist():
    raw = os.getenv("WATCHLIST", "")
    parts = [p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip()]
    return set(parts) if parts else set(DEFAULT_WATCHLIST)

WATCHLIST = load_watchlist()

def normalize_ticker(ticker):
    t = (ticker or "").strip().upper()
    if ":" in t:
        t = t.split(":")[-1]
    if "." in t:
        t = t.split(".")[0]
    if "-" in t:
        t = t.split("-")[0]
    return t

def on_watchlist(ticker):
    return ticker in WATCHLIST

ET = pytz.timezone("America/New_York")

def confirm_allows(data):
    if "confirm" not in data:
        return True, "no confirm field"
    value = data.get("confirm")
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("true", "1", "yes"):
            return True, "confirm true"
        if value in ("false", "0", "no"):
            return False, "confirm false"
        return False, "confirm invalid"
    if value is True:
        return True, "confirm true"
    if value is False:
        return False, "confirm false"
    return False, "confirm invalid"

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
            os.getenv("ALPACA_API_KEY"),
            os.getenv("ALPACA_SECRET_KEY"),
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
    persist_signal(row)
    return row

def get_trading_client():
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    return TradingClient(api_key, secret_key, paper=True)

def is_regular_market_hours():
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
        return qty if qty > 0 else 0
    except Exception:
        return 0

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

def count_trades_today(client):
    return len([o for o in get_today_orders(client) if order_status_str(o) == "filled"])

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
            "trades_today": count_trades_today(client),
            "watchlist": sorted(WATCHLIST),
            "regime": shadow_regime(),
            "industry": {name: industry_note(name) for name in sorted(WATCHLIST)},
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/memo")
async def memo(request: Request):
    data = await request.json()
    ticker = normalize_ticker(data.get("ticker", ""))
    if not ticker or not on_watchlist(ticker):
        return {"status": "ignored", "reason": "not_on_watchlist_or_missing"}
    signal_row = get_latest_signal_for(ticker)
    if not signal_row:
        return {"status": "no_signal", "ticker": ticker}
    memo_result = await generate_memo(ticker, signal_row)
    return {"status": "ok", "memo": memo_result}

@app.post("/approve")
async def approve(request: Request):
    data = await request.json()
    action = data.get("action", "").lower()
    ticker = normalize_ticker(data.get("ticker", ""))
    if action not in ["buy", "sell"]:
        log_signal(action, ticker, "blocked", "approve: action must be buy or sell")
        return {"status": "ignored", "message": "action must be buy or sell"}
    if not ticker:
        log_signal(action, ticker, "blocked", "approve: ticker required")
        return {"status": "error", "message": "ticker required"}
    # WATCHLIST CHECK REMOVED — any ticker posted by Research Desk is allowed
    # (other risk rules still apply)

    if not is_regular_market_hours():
        log_signal(action, ticker, "blocked", "approve: outside regular market hours")
        return {"status": "ignored", "message": "Outside regular market hours"}

    try:
        client = get_trading_client()
        can_trade, daily_change = check_daily_loss_limit(client)
        if not can_trade:
            reason = f"approve: daily loss limit {daily_change:.2f}%"
            log_signal(action, ticker, "blocked", reason)
            return {"status": "halted", "message": reason}

        trades_today = count_trades_today(client)
        if trades_today >= MAX_TRADES_PER_DAY:
            log_signal(action, ticker, "blocked", f"approve: max trades per day ({MAX_TRADES_PER_DAY})")
            return {"status": "ignored", "message": f"Max trades per day ({MAX_TRADES_PER_DAY}) reached"}

        if has_pending_order(client, ticker):
            log_signal(action, ticker, "blocked", "approve: pending order exists")
            return {"status": "ignored", "message": f"Pending order exists for {ticker}"}

        if action == "buy":
            current_qty = get_position_qty(client, ticker)
            if current_qty > 0:
                log_signal(action, ticker, "blocked", "approve: already long")
                return {"status": "ignored", "message": f"Already long {ticker}"}
            if sold_ticker_today(client, ticker):
                log_signal(action, ticker, "blocked", "approve: already sold today")
                return {"status": "ignored", "message": f"Already sold {ticker} today"}
            open_count = get_open_positions_count(client)
            if open_count >= MAX_POSITIONS:
                log_signal(action, ticker, "blocked", f"approve: max positions ({MAX_POSITIONS})")
                return {"status": "ignored", "message": f"Max positions ({MAX_POSITIONS}) reached"}
            qty = TEST_QTY
        else:
            current_qty = get_position_qty(client, ticker)
            if current_qty <= 0:
                log_signal(action, ticker, "blocked", "approve: no long position")
                return {"status": "ignored", "message": f"No long position in {ticker}"}
            if bought_ticker_today(client, ticker):
                log_signal(action, ticker, "blocked", "approve: bought today")
                return {"status": "ignored", "message": f"Bought {ticker} today"}
            qty = min(int(current_qty), TEST_QTY)

        side = OrderSide.BUY if action == "buy" else OrderSide.SELL
        order_data = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY
        )
        order = client.submit_order(order_data)
        log_signal(action, ticker, "allowed", f"approve paper {action} {qty} order {order.id}")
        return {
            "status": "success",
            "message": f"Approved paper trade placed: {action} {qty} {ticker}",
            "order_id": str(order.id),
            "qty": qty
        }
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
    data = await request.json()
    action = data.get("action", "").lower()
    ticker = normalize_ticker(data.get("ticker", ""))
    if not on_watchlist(ticker):
        log_signal(action, ticker, "blocked", "not_on_watchlist")
        print(f"WATCHLIST_REJECT symbol={ticker} action={action} src=tv")
        return {"status": "ignored", "reason": "not_on_watchlist", "ticker": ticker}
    # ... rest of webhook unchanged (omitted for brevity if it continues)
    # If your original file has more after this point, keep it as-is.
