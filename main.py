from fastapi import FastAPI, Request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
import os

app = FastAPI()

# Connect to Alpaca Paper account
trading_client = TradingClient(
    os.getenv("PKP4UMGKFIY6SZSX2PNW6N5VYA"),
    os.getenv("J3Qdohdi7DsiGxim8FpRJG6iDeivRfpB5yA6idDAS4xj"),
    paper=True
)

@app.get("/")
def home():
    return {"status": "Trading agent is running", "mode": "paper"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Received webhook:", data)

    # For now we just acknowledge the message
    # Later we will add the logic to place trades
    return {"status": "received", "data": data}
