# HK Connection (paper)

A second, fully separate Alpaca **paper** trading bot that runs a codified snapshot (Oct–Nov 2026) of Martin Luk's
pullback method: EMA-stack watchlists from his Leader / Potent / Premarket screens, intraday "first breakout of the
previous bar" entries after a flush into support (1-min, then 5-min, then 15-min bars), low-of-day stop-market stops,
0.3% risk per trade, 15% trims at +3R and +5R, and an exit on the first daily close below the 9 EMA. It also runs his
mirror-image shorts (inverse ETFs for index shorts), his earnings rule, and his losing-streak rule.
How every rule maps to the spec is in [DECISIONS.md](DECISIONS.md).

It shares nothing with Agent 200 (`main.py`): it has its own entrypoint, Alpaca keys, ledger and requirements.
`paper=True` is hard-coded.

## Layout

| File | What it does |
|---|---|
| `rules.py` | All of his rules as pure functions and constants (sizing, stops, triggers, trims, trail, earnings, losing streak) |
| `bars.py` | 1-min → 5/15/60-min aggregation aligned to 9:30 ET; per-symbol low/high of day |
| `scanner.py` | Leader / Potent / Premarket screens, Lead/Mediocre/Lag lists, watchlist |
| `data.py` | Alpaca SIP market data, asset list, calendar; Finnhub earnings and market cap |
| `broker.py` | OTO + GTC stop entries; cancel → sell → re-place stop partials; exits; GTC refresh |
| `engine.py` | Intraday logic, reconciliation with Alpaca, daily jobs |
| `scheduler.py` | ET-aware in-process scheduler and the real-time SIP 1-minute bar stream |
| `ledger.py` | SQLite ledger: trades, partials, exits, stops, P&L, losing-streak state, watchlists, events |
| `app.py` | FastAPI: read-only `GET /status`, `/positions`, `/trades`, `/watchlist`, `/events`, `/health` |

## Daily timeline (ET, trading days only)

08:45 pre-open scan (premarket screen, earnings calendar, hourly EMAs) · 09:25 start SIP 1-min stream ·
09:30:30 exits queued by last night's trail · 09:31–11:30 entries · all session: trims, reconcile every minute ·
15:30 earnings check · 16:01 stop stream · 16:10 EMA trail, 90-day GTC refresh, equity peak ·
17:15 after-close scan for the next session.

## Configuration

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `HK_ALPACA_PAPER_KEY_ID` | yes | – | Key for the HK Connection paper account (never Agent 200's; the app refuses to start if it matches `ALPACA_API_KEY`/`ALPACA_PAPER_KEY_ID`) |
| `HK_ALPACA_PAPER_SECRET_KEY` | yes | – | Secret for that account |
| `FINNHUB_API_KEY` | strongly recommended | – | Earnings calendar + market cap. Without it, new stock entries are blocked by the earnings rule (ETFs still trade) |
| `CAPITAL_BASE` | no | unset = live account equity | Optional sizing override in dollars. Unset or empty → sizing uses the account's current `equity` from Alpaca |
| `HK_DATA_DIR` | no | `/data` | Where the ledger lives (mount a Railway volume here) |
| `HK_LEDGER_PATH` | no | `$HK_DATA_DIR/hk_connection.db` | SQLite file |
| `HK_ENGINE_ENABLED` | no | `true` | `false` = monitoring API only, no scheduler, stream or orders |
| `HK_SHORTS_ENABLED` | no | `true` | User decision: shorts on |
| `HK_TRAIL_EMA` | no | `9` | `9` or `21` |
| `HK_WATCHLIST_MAX` | no | `25` | Longs and shorts per day, each |

Market data needs a real-time **SIP** subscription on the login that owns the paper account (spec §8b.2). Paper-only
logins get IEX only.

## Run locally

```
pip install -r hk_connection/requirements.txt
uvicorn hk_connection.app:app --host 0.0.0.0 --port 8000
pytest hk_connection/tests   # Alpaca fully mocked, no network
```

## Deploy (new Railway service, not done by this PR)

1. In the same Railway project, add a **new service** from the `jpagent2026/trading-agent` repo, branch `main`.
2. Service settings → Config-as-code → set the path to `hk_connection/railway.json`. Or set it manually:
   start command `uvicorn hk_connection.app:app --host 0.0.0.0 --port $PORT`, build command
   `pip install -r hk_connection/requirements.txt`.
3. Add a **volume** mounted at `/data` (1 GB is plenty), so the ledger survives redeploys.
4. Variables: `HK_ALPACA_PAPER_KEY_ID`, `HK_ALPACA_PAPER_SECRET_KEY`, `FINNHUB_API_KEY`; optionally `CAPITAL_BASE`.
   Don't copy Agent 200's variables into this service.
5. Keep one replica: the scheduler and stream must not run twice.
6. Check `GET /status` (engine_running true, account shows the $2,000 paper account) and `GET /watchlist` after 17:15 ET.

Agent 200's service is untouched: it keeps its own start command and env vars.
