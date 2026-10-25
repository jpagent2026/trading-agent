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
            parsed = json.loads(text)
            parsed["ticker"] = ticker
            parsed["based_on_signal"] = signal_row.get("time")
            parsed["note"] = parsed.get("note", "second opinion only — no order impact")
            return parsed
    except Exception as e:
        return {
            "ticker": ticker,
            "thesis": f"Error: {str(e)[:80]}",
            "risks": "—",
            "confidence": 0.0,
            "note": "second opinion only — no order impact",
            "based_on_signal": signal_row.get("time"),
        }

init_db()

def load_watchlist():
    raw = os.getenv("WATCHLIST", "")
    parts = [p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip()]
    return set(parts) if parts else set(DEFAULT_WATCHLIST)

WATCHLIST = load_watchlist()
ET = pytz.timezone("America/New_York")

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
        rows = list(bars.data.get("QQQ", []))
        closes = [float(b.close) for b in rows if getattr(b, "close", None) is not None]
        note = classify_regime(closes)
    except Exception as e:
        note = {
            "regime": "unknown",
            "risk_on": "unknown",
            "basis": "QQQ",
            "veto": False,
            "note": str(e)[:120],
        }
    note["as_of"] = now_et()
    note["veto"] = False
    REGIME_CACHE["at"] = datetime.now(ET)
    REGIME_CACHE["note"] = note
    print(
        f"REGIME time={note['as_of']} regime={note.get('regime')} "
        f"risk_on={note.get('risk_on')} basis=QQQ veto=false"
    )
    return note

def load_industry_feed():
    cached = INDUSTRY_CACHE.get("payload")
    cached_at = INDUSTRY_CACHE.get("at")
    if cached and cached_at and (datetime.now(ET) - cached_at).total_seconds() < INDUSTRY_TTL_SECONDS:
        return cached
    try:
        response = httpx.get(INDUSTRY_FEED, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        payload = cached or {"as_of": None, "industries": [], "tiers": {}, "note": str(e)[:120]}
    INDUSTRY_CACHE["at"] = datetime.now(ET)
    INDUSTRY_CACHE["payload"] = payload
    return payload

def industry_note(ticker):
    etf = STOCK_INDUSTRY.get((ticker or "").upper())
    if not etf:
        return {
            "stock": ticker,
            "etf": None,
            "industry": None,
            "tier": "neutral",
            "ret_3m": None,
            "would_skip": False,
            "would_skip_soft": False,
            "veto": False,
            "note": "no industry match",
        }
    payload = load_industry_feed()
    row = None
    for item in payload.get("industries", []):
        if str(item.get("ticker", "")).upper() == etf:
            row = item
            break
    tier = (row or {}).get("tier") or payload.get("tiers", {}).get(etf) or "unknown"
    ret_3m = (row or {}).get("ret_3m")
    would_skip = tier == "laggard"
    would_skip_soft = would_skip and isinstance(ret_3m, (int, float)) and ret_3m < 0
    note = {
        "stock": ticker,
        "etf": etf,
        "industry": (row or {}).get("industry"),
        "tier": tier,
        "rank": (row or {}).get("rank"),
        "ret_3m": ret_3m,
        "ret_6m": (row or {}).get("ret_6m"),
        "as_of": payload.get("as_of"),
        "would_skip": would_skip,
        "would_skip_soft": would_skip_soft,
        "veto": False,
    }
    print(
        f"INDUSTRY stock={ticker} etf={etf} tier={tier} "
        f"would_skip={str(would_skip).lower()} veto=false"
    )
    return note

def log_signal(action, ticker, result, reason):
    regime = shadow_regime()
    industry = industry_note(ticker)
    row = {
        "time": now_et(),
        "ticker": ticker or "",
        "action": action or "",
        "result": result,
        "reason": reason,
        "regime": regime.get("regime"),
        "risk_on": regime.get("risk_on"),
        "industry_etf": industry.get("etf"),
        "industry_tier": industry.get("tier"),
        "would_skip": industry.get("would_skip"),
    }
    SIGNAL_LOG.append(row)
    if len(SIGNAL_LOG) > SIGNAL_LOG_MAX:
        del SIGNAL_LOG[: len(SIGNAL_LOG) - SIGNAL_LOG_MAX]
    print(
        f"SIGNAL time={row['time']} ticker={row['ticker']} "
        f"action={row['action']} result={row['result']} reason={row['reason']} "
        f"regime={row['regime']} risk_on={row['risk_on']} "
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
    pending_statuses = {
        "new", "accepted", "pending_new", "accepted_for_bidding",
        "pending_replace", "pending_cancel", "partially_filled"
    }
    request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    for o in client.get_orders(request):
        symbol = str(getattr(o, "symbol", "")).upper()
        if symbol == ticker.upper() and order_status_str(o) in pending_statuses:
            return True
    return False

@app.get("/")
def home():
    try:
        client = get_trading_client()
        account = client.get_account()
        return {
            "status": "Trading agent is running",
            "mode": "paper",
            "equity": str(account.equity),
            "buying_power": str(account.buying_power),
            "open_positions": get_open_positions_count(client),
            "trades_today": count_trades_today(client),
            "watchlist": sorted(WATCHLIST),
            "signals_kept": len(SIGNAL_LOG),
            "regime": shadow_regime(),
            "industry": {
                name: industry_note(name) for name in sorted(WATCHLIST)
            },
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/signals")
def signals():
    return {"count": len(SIGNAL_LOG), "signals": SIGNAL_LOG[-50:]}

@app.get("/signals/persistent")
def signals_persistent(limit: int = 50):
    return {"count": limit, "signals": get_recent_signals(limit)}

@app.get("/summary/today")
def summary_today():
    return end_of_day_summary()

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
    if not on_watchlist(ticker):
        log_signal(action, ticker, "blocked", "approve: not_on_watchlist")
        return {"status": "ignored", "reason": "not_on_watchlist", "ticker": ticker}

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
    payload = load_industry_feed()
    return {
        "as_of": payload.get("as_of"),
        "veto": False,
        "map": {name: industry_note(name) for name in sorted(WATCHLIST)},
    }

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Received webhook:", data)
    action = data.get("action", "").lower()
    ticker = normalize_ticker(data.get("ticker", ""))
    if action not in ["buy", "sell"]:
        log_signal(action, ticker, "blocked", "action must be buy or sell")
        return {"status": "ignored", "message": "action must be buy or sell"}
    if not ticker:
        log_signal(action, ticker, "blocked", "ticker required")
        return {"status": "error", "message": "ticker required"}
    if not on_watchlist(ticker):
        log_signal(action, ticker, "blocked", "not_on_watchlist")
        print(f"WATCHLIST_REJECT symbol={ticker} action={action} src=tv")
        return {"status": "ignored", "reason": "not_on_watchlist", "ticker": ticker}
    allowed, confirm_reason = confirm_allows(data)
    if not allowed:
        log_signal(action, ticker, "blocked", confirm_reason)
        print(f"CONFIRM_REJECT symbol={ticker} action={action} reason={confirm_reason}")
        return {"status": "ignored", "reason": confirm_reason, "ticker": ticker}
    if not is_regular_market_hours():
        log_signal(action, ticker, "blocked", "outside regular market hours")
        print(f"Signal ignored - outside regular market hours: {action} {ticker}")
        return {"status": "ignored", "message": "Outside regular market hours"}
    try:
        client = get_trading_client()
        print("Trading client created successfully")
        can_trade, daily_change = check_daily_loss_limit(client)
        print(f"Daily change: {daily_change:.2f}%")
        if not can_trade:
            reason = f"daily loss limit {daily_change:.2f}%"
            log_signal(action, ticker, "blocked", reason)
            print(f"Daily loss limit reached ({daily_change:.2f}%). Trading halted.")
            return {"status": "halted", "message": f"Daily loss limit reached ({daily_change:.2f}%)"}
        trades_today = count_trades_today(client)
        print(f"Trades today: {trades_today}")
        if trades_today >= MAX_TRADES_PER_DAY:
            log_signal(action, ticker, "blocked", f"max trades per day ({MAX_TRADES_PER_DAY})")
            print(f"Max trades per day ({MAX_TRADES_PER_DAY}) reached. Ignoring signal.")
            return {"status": "ignored", "message": f"Max trades per day ({MAX_TRADES_PER_DAY}) reached"}
        if has_pending_order(client, ticker):
            log_signal(action, ticker, "blocked", "pending order exists")
            print(f"Pending order already exists for {ticker}. Ignoring {action}.")
            return {"status": "ignored", "message": f"Pending order exists for {ticker}"}
        if action == "buy":
            current_qty = get_position_qty(client, ticker)
            print(f"Current long position in {ticker}: {current_qty}")
            if current_qty > 0:
                log_signal(action, ticker, "blocked", "already long")
                print(f"Already long {ticker}. Ignoring add-on buy.")
                return {"status": "ignored", "message": f"Already long {ticker}"}
            if sold_ticker_today(client, ticker):
                log_signal(action, ticker, "blocked", "already sold today")
                print(f"Already sold {ticker} today. Ignoring same-day re-buy.")
                return {"status": "ignored", "message": f"Already sold {ticker} today"}
            open_count = get_open_positions_count(client)
            print(f"Open positions: {open_count}")
            if open_count >= MAX_POSITIONS:
                log_signal(action, ticker, "blocked", f"max positions ({MAX_POSITIONS})")
                print(f"Max positions ({MAX_POSITIONS}) reached. Ignoring buy.")
                return {"status": "ignored", "message": f"Max positions ({MAX_POSITIONS}) reached"}
            qty = TEST_QTY
            print(f"Using test quantity: {qty}")
        else:
            current_qty = get_position_qty(client, ticker)
            print(f"Current long position in {ticker}: {current_qty}")
            if current_qty <= 0:
                log_signal(action, ticker, "blocked", "no long position")
                print(f"No long position in {ticker}. Ignoring sell to avoid shorting.")
                return {"status": "ignored", "message": f"No long position in {ticker}"}
            if bought_ticker_today(client, ticker):
                log_signal(action, ticker, "blocked", "bought today")
                print(f"Bought {ticker} today. Ignoring same-day sell.")
                return {"status": "ignored", "message": f"Bought {ticker} today. Hold until next session."}
            try:
                requested_qty = int(float(data.get("qty", current_qty)))
            except Exception:
                requested_qty = int(current_qty)
            qty = min(requested_qty, int(current_qty))
            print(f"Selling {qty} shares of {ticker}")
        print(f"Submitting order: {action} {qty} {ticker}")
        side = OrderSide.BUY if action == "buy" else OrderSide.SELL
        order_data = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY
        )
        order = client.submit_order(order_data)
        print(f"PAPER TRADE PLACED: {action.upper()} {qty} {ticker} | Order ID: {order.id}")
        log_signal(action, ticker, "allowed", f"paper {action} {qty} order {order.id}")
        return {
            "status": "success",
            "message": f"Paper trade placed: {action} {qty} {ticker}",
            "order_id": str(order.id),
            "qty": qty
        }
    except Exception as e:
        print(f"ORDER FAILED - FULL ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        log_signal(action, ticker, "blocked", f"order failed {type(e).__name__}")
        return {"status": "error", "message": str(e)}
