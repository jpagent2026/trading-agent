from fastapi import FastAPI, Request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from datetime import datetime
import os
import pytz

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


# === PHASE 2 STEP 3: optional confirm ===
# Missing confirm = EMA9 path, still trades.
# "confirm": false blocks. "confirm": true passes into the same risk rules.

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


def log_signal(action, ticker, result, reason):
    """One line per webhook. result is allowed or blocked. Does not veto."""
    row = {
        "time": now_et(),
        "ticker": ticker or "",
        "action": action or "",
        "result": result,
        "reason": reason,
    }
    SIGNAL_LOG.append(row)
    if len(SIGNAL_LOG) > SIGNAL_LOG_MAX:
        del SIGNAL_LOG[: len(SIGNAL_LOG) - SIGNAL_LOG_MAX]
    print(
        f"SIGNAL time={row['time']} ticker={row['ticker']} "
        f"action={row['action']} result={row['result']} reason={row['reason']}"
    )
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
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/signals")
def signals():
    return {"count": len(SIGNAL_LOG), "signals": SIGNAL_LOG[-50:]}

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

        else:  # sell
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
