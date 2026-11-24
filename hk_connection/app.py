"""HK Connection FastAPI app. Read-only monitoring endpoints; the engine runs in background threads.

Start command:  uvicorn hk_connection.app:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI

from . import rules
from .config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("hk_connection.app")


class Runtime:
    def __init__(self) -> None:
        self.settings: Optional[Settings] = None
        self.ledger: Any = None
        self.broker: Any = None
        self.engine: Any = None
        self.scheduler: Any = None
        self.error: Optional[str] = None


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


def create_app(runtime: Optional[Runtime] = None, start_engine: bool = True) -> FastAPI:
    rt = runtime or Runtime()

    def _startup() -> None:
        if rt.ledger is not None or not start_engine:
            return
        try:
            from .broker import Broker, make_trading_client
            from .data import MarketData
            from .engine import Engine
            from .ledger import Ledger
            from .scheduler import Scheduler, make_stream_factory
            rt.settings = Settings.from_env()
            rt.ledger = Ledger(rt.settings.ledger_path)
            for w in rt.settings.warnings:
                log.warning(w)
            if not (rt.settings.key_id and rt.settings.secret_key):
                return
            client = make_trading_client(rt.settings.key_id, rt.settings.secret_key)
            rt.broker = Broker(client)
            data = MarketData(rt.settings.key_id, rt.settings.secret_key, client, rt.settings.finnhub_api_key)
            rt.engine = Engine(rt.settings, rt.broker, rt.ledger, data)
            if rt.settings.engine_enabled:
                rt.scheduler = Scheduler(rt.engine, stream_factory=make_stream_factory(rt.settings.key_id, rt.settings.secret_key))
                rt.scheduler.start()
        except Exception as e:  # keep the monitoring API up so the error is visible
            log.exception("startup failed")
            rt.error = str(e)

    def _shutdown() -> None:
        if rt.scheduler is not None:
            rt.scheduler.shutdown()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _startup()
        yield
        _shutdown()

    app = FastAPI(title="HK Connection (paper)", lifespan=lifespan)
    app.state.rt = rt

    @app.get("/")
    def root() -> dict:
        return {"service": "hk_connection", "mode": "paper", "endpoints": ["/health", "/status", "/positions",
                                                                         "/trades", "/watchlist", "/events"]}

    @app.get("/health")
    def health() -> dict:
        return {"ok": rt.error is None}

    @app.get("/status")
    def status() -> dict:
        s = rt.settings
        out: dict[str, Any] = {"service": "hk_connection", "mode": "paper", "error": rt.error,
                               "engine_running": bool(rt.scheduler and rt.scheduler.thread and rt.scheduler.thread.is_alive()),
                               "warnings": s.warnings if s else []}
        if s:
            out["settings"] = {"capital_base_env": s.capital_base, "shorts_enabled": s.shorts_enabled,
                               "trail_ema": s.trail_ema, "watchlist_max": s.watchlist_max,
                               "ledger_path": str(s.ledger_path)}
        out["rules"] = {"base_risk_pct": rules.BASE_RISK_PCT, "reduced_risk_pct": rules.REDUCED_RISK_PCT,
                        "max_stop_pct": rules.MAX_STOP_PCT, "position_cap_pct": rules.POSITION_CAP_PCT,
                        "trim": {"fraction": rules.TRIM_FRACTION, "r_levels": list(rules.TRIM_R_LEVELS)},
                        "entry_window_minutes": rules.ENTRY_WINDOW_MINUTES,
                        "max_concurrent_positions": rules.MAX_CONCURRENT_POSITIONS}
        if rt.broker is not None:
            try:
                a = rt.broker.account()
                out["account"] = {"equity": _f(a.equity), "cash": _f(a.cash), "buying_power": _f(a.buying_power),
                                  "multiplier": _f(getattr(a, "multiplier", None)), "status": str(getattr(a, "status", ""))}
                if rt.engine is not None:
                    cap, src = rt.engine.capital_base()
                    out["capital_base"] = {"value": cap, "source": src}
            except Exception as e:
                out["account_error"] = str(e)
        if rt.ledger is not None:
            streak = rt.ledger.load_streak()
            out["losing_streak"] = streak.__dict__
            out["current_risk_pct"] = rules.current_risk_pct(streak)
            out["open_trades"] = len(rt.ledger.open_trades())
            out["realized_pnl"] = rt.ledger.realized_pnl_total()
            out["record"] = rt.ledger.closed_count()
            d = rt.ledger.latest_watchlist_date()
            out["watchlist"] = {"session": d, "size": len(rt.ledger.load_watchlist(d)) if d else 0}
            out["recent_events"] = rt.ledger.events(10)
            out["stop_breach_exits"] = [
                {k: r.get(k) for k in ("symbol", "qty", "stop", "trigger_price", "reason", "order_id", "time", "status",
                                       "trade_id", "direction", "context", "price_source", "error")}
                for r in rt.ledger.stop_breach_exits(50)]
        if rt.scheduler is not None:
            out["jobs_last_run"] = rt.scheduler.last_runs
            out["last_stop_check"] = rt.scheduler.last_stop_check
        if rt.engine is not None:
            out["earnings_calendar_ok"] = rt.engine.earnings_ok
        return out

    @app.get("/positions")
    def positions() -> dict:
        trades = {t["symbol"]: t for t in (rt.ledger.open_trades() if rt.ledger else [])}
        rows = []
        broker_error = None
        if rt.broker is not None:
            try:
                for p in rt.broker.positions():
                    t = trades.pop(p.symbol, None)
                    rows.append({"symbol": p.symbol, "qty": _f(p.qty), "side": str(getattr(p.side, "value", p.side)),
                                 "avg_entry_price": _f(p.avg_entry_price), "current_price": _f(p.current_price),
                                 "market_value": _f(p.market_value), "unrealized_pl": _f(p.unrealized_pl),
                                 "unrealized_plpc": _f(p.unrealized_plpc), "ledger": t})
            except Exception as e:
                broker_error = str(e)
        for sym, t in trades.items():  # in the ledger but not at the broker (pending entries)
            rows.append({"symbol": sym, "qty": 0, "ledger": t})
        return {"mode": "paper", "positions": rows, "broker_error": broker_error}

    @app.get("/trades")
    def trades(limit: int = 50) -> dict:
        return {"trades": rt.ledger.recent_trades(min(limit, 500)) if rt.ledger else []}

    @app.get("/watchlist")
    def watchlist(session: Optional[str] = None) -> dict:
        if rt.ledger is None:
            return {"session": None, "items": []}
        d = session or rt.ledger.latest_watchlist_date()
        return {"session": d, "items": rt.ledger.load_watchlist(d) if d else []}

    @app.get("/events")
    def events(limit: int = 100) -> dict:
        return {"events": rt.ledger.events(min(limit, 1000)) if rt.ledger else []}

    return app


app = create_app()
