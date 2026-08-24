from fastapi import FastAPI, Request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from datetime import datetime
import os
import pytz

app = FastAPI()

MAX_POSITIONS = 4
DAILY_LOSS_LIMIT_PCT = 3.0
TEST_QTY = 3   # Temporary fixed quantity for testing

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

def get_open_positions_count(client):
    positions = client.get_all_positions()
    return len(positions)

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
            "open_positions": get_open_positions_count(client)
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

        if action == "buy":
            open_count = get_open_positions_count(client)
            print(f"Open positions: {open_count}")
            if open_count >= MAX_POSITIONS:
                print(f"Max positions ({MAX_POSITIONS}) reached. Ignoring buy.")
                return {"status": "ignored", "message": f"Max positions ({MAX_POSITIONS}) reached"}
            qty = TEST_QTY
            print(f"Using test quantity: {qty}")
        else:
            try:
                qty = int(float(data.get("qty", 0)))
            except:
                print("qty required for sells")
                return {"status": "error", "message": "qty required for sells"}

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
