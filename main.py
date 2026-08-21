from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/")
def home():
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    return {
        "api_key_found": bool(api_key),
        "secret_key_found": bool(secret_key),
        "api_key_length": len(api_key) if api_key else 0,
        "secret_key_length": len(secret_key) if secret_key else 0
    }
