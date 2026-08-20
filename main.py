from fastapi import FastAPI, Request
import os

app = FastAPI()

@app.get("/")
def home():
    return {"status": "Trading agent is running"}

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print("Received webhook:", data)
    return {"status": "received"}
