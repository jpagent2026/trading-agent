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

def get_trading_client():
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    return TradingClient(api_key, secret_key, paper=True)

def is_regular_market_hours():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

def get_today_start():
    et = pytz.timezone("America/New_York")
    return datetime.now(et).replace(hour=0, minute=0, second=0, microsecond=0)

def get_open_positions_count(client):
    positions = client.get_all_positions()
    return len(positions)

def get_position_qty(client, ticker):
    """Return current long quantity for a ticker. 0 if none."""
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
    orders = get_today_orders(client)
    filled = [o for o in orders if order_status_str(o) == "filled"]
    return len(filled)

def sold_ticker_today(client, ticker):
    """True if we already sold this ticker today."""
    for o in get_today_orders(client):
        symbol = str(getattr(o, "symbol", "")).upper()
        if symbol == ticker.upper() and order_side_str(o) == "sell" and order_status_str(o) in [
            "filled", "partially_filled", "new", "accepted", "pending_new"
        ]:
            return True
    return False

def has_pending_order(client, ticker):
    """True if this ticker already has an open/pending order."""
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
            "trades_today": count_trades_today(client)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Received webhook:", data)

    action = data.get("action", "").lower()
    ticker = data.get("ticker", "").upper()

    if action not in ["buy", "sell"]:
        print("Ignored: action must be buy or sell")
        return {"status": "ignored", "message": "action must be buy or sell"}

    if not ticker:
        print("Error: ticker required")
        return {"status": "error", "message": "ticker required"}

    if not is_regular_market_hours():
        print(f"Signal ignored - outside regular market hours: {action} {ticker}")
        return {"status": "ignored", "message": "Outside regular market hours"}

    try:
        client = get_trading_client()
        print("Trading client created successfully")

        can_trade, daily_change = check_daily_loss_limit(client)
        print(f"Daily change: {daily_change:.2f}%")
        if not can_trade:
            print(f"Daily loss limit reached ({daily_change:.2f}%). Trading halted.")
            return {"status": "halted", "message": f"Daily loss limit reached ({daily_change:.2f}%)"}

        trades_today = count_trades_today(client)
        print(f"Trades today: {trades_today}")
        if trades_today >= MAX_TRADES_PER_DAY:
            print(f"Max trades per day ({MAX_TRADES_PER_DAY}) reached. Ignoring signal.")
            return {"status": "ignored", "message": f"Max trades per day ({MAX_TRADES_PER_DAY}) reached"}

        if has_pending_order(client, ticker):
            print(f"Pending order already exists for {ticker}. Ignoring {action}.")
            return {"status": "ignored", "message": f"Pending order exists for {ticker}"}

        if action == "buy":
            current_qty = get_position_qty(client, ticker)
            print(f"Current long position in {ticker}: {current_qty}")
            if current_qty > 0:
                print(f"Already long {ticker}. Ignoring add-on buy.")
                return {"status": "ignored", "message": f"Already long {ticker}"}

            if sold_ticker_today(client, ticker):
                print(f"Already sold {ticker} today. Ignoring same-day re-buy.")
                return {"status": "ignored", "message": f"Already sold {ticker} today"}

            open_count = get_open_positions_count(client)
            print(f"Open positions: {open_count}")
            if open_count >= MAX_POSITIONS:
                print(f"Max positions ({MAX_POSITIONS}) reached. Ignoring buy.")
                return {"status": "ignored", "message": f"Max positions ({MAX_POSITIONS}) reached"}

            qty = TEST_QTY
            print(f"Using test quantity: {qty}")

        else:  # sell
            current_qty = get_position_qty(client, ticker)
            print(f"Current long position in {ticker}: {current_qty}")
            if current_qty <= 0:
                print(f"No long position in {ticker}. Ignoring sell to avoid shorting.")
                return {"status": "ignored", "message": f"No long position in {ticker}"}
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
        return {"status": "error", "message": str(e)}
