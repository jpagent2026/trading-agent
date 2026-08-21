from fastapi import FastAPI, Request
from alpaca.trading.client import TradingClient
import os

app = FastAPI()

def get_trading_client():
    api_key = os.getenv("PKP4UMGKFIY6SZSX2PNW6N5VYA")
    secret_key = os.getenv("J3Qdohdi7DsiGxim8FpRJG6iDeivRfpB5yA6idDAS4xj")
    return TradingClient(api_key, secret_key, paper=True)

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
    return {"status": "received", "data": data}
