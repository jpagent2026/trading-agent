from fastapi import FastAPI, Request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from datetime import datetime
import os
import pytz

app = FastAPI()

def get_trading_client():
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    return TradingClient(api_key, secret_key, paper=True)

def is_regular_market_hours():
    """Only allow trades during regular US market hours (9:30 AM - 4:00 PM ET)"""
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    
    # Weekday check (Monday=0, Sunday=6)
    if now.weekday() >= 5:
        return False
    
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    
    return market_open <= now <= market_close

@app.get("/")
def home():
    try:
        client = get_trading_client()
        account = client.get_account()
        return {
            "status": "Trading agent is running",
            "mode": "paper",
            "equity": str(account.equity),
            "buying_power": str(account.buying_power)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Received webhook:", data)

    action = data.get("action", "").lower()
    ticker = data.get("ticker", "").upper()
    
    # Force whole shares only (integer)
    try:
        qty = int(float(data.get("qty", 0)))
    except (ValueError, TypeError):
        return {"status": "error", "message": "qty must be a number"}

    if action not in ["buy", "sell"]:
        return {"status": "ignored", "message": "action must be buy or sell"}

    if not ticker or qty <= 0:
        return {"status": "error", "message": "ticker and positive qty required"}

    # Safety rule: regular market hours only
    if not is_regular_market_hours():
        print(f"Signal ignored - outside regular market hours: {action} {qty} {ticker}")
        return {
            "status": "ignored",
            "message": "Outside regular market hours. Trade not placed."
        }

    # Place the paper trade
    try:
        client = get_trading_client()
        side = OrderSide.BUY if action == "buy" else OrderSide.SELL

        order_data = MarketOrderRequest(
            symbol=ticker,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY
        )

        order = client.submit_order(order_data)
        print(f"PAPER TRADE PLACED: {action.upper()} {qty} shares of {ticker}")

        return {
            "status": "success",
            "message": f"Paper trade placed: {action} {qty} {ticker}",
            "order_id": str(order.id)
        }

    except Exception as e:
        print(f"Order failed: {e}")
        return {"status": "error", "message": str(e)}
