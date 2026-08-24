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

        # Daily loss limit
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

            print(f"Calculating shares for {ticker}...")
            qty = calculate_shares(client, ticker)
            print(f"Calculated qty: {qty}")
            if qty <= 0:
                print("Could not calculate valid share quantity")
                return {"status": "error", "message": "Could not calculate valid share quantity"}
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
