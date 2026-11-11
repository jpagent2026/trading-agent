"""In-process, ET-aware scheduler and the real-time SIP 1-minute bar stream.

Daily timeline (America/New_York, trading days only, per Alpaca's market calendar):
  08:45  pre-open scan: premarket screen, earnings calendar, hourly EMAs, load the watchlist
  09:25  start the SIP 1-minute bar stream (backfills today's bars if started late)
  09:30:30 open job: execute exits queued by last night's EMA trail
  09:30-16:05 reconcile with Alpaca every minute (fills, stop-outs, losing streak)
  09:31-11:30 entries allowed (rules.ENTRY_WINDOW_MINUTES); trims monitored all session
  15:30  earnings job: sell half / exit positions without cushion before a release
  16:01  stop the stream
  16:10  end-of-day: EMA trail check, 90-day GTC stop refresh, equity peak
  17:15  after-close scan: Leader + Potent screens -> next session's watchlist
A job missed because the process started late runs once on catch-up the same day (scans and
EOD only; the open job only before 16:00).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Optional

from .bars import ET, Bar, to_et
from .engine import Engine

log = logging.getLogger("hk_connection.scheduler")

JOBS = [  # (name, start ET, latest ET for catch-up)
    ("pre_open_scan", time(8, 45), time(15, 30)),
    ("start_stream", time(9, 25), time(15, 59)),
    ("open_job", time(9, 30, 30), time(15, 55)),
    ("earnings_job", time(15, 30), time(15, 58)),
    ("stop_stream", time(16, 1), time(23, 59)),
    ("eod_job", time(16, 10), time(23, 59)),
    ("after_close_scan", time(17, 15), time(23, 59)),
]


class Scheduler:
    def __init__(self, engine: Engine, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 stream_factory: Optional[Callable[[], Any]] = None):
        self.engine = engine
        self.now = now
        self.stream_factory = stream_factory
        self.stream: Any = None
        self.stream_thread: Optional[threading.Thread] = None
        self.done: dict[str, str] = {}        # job name -> ET date it last ran
        self.last_runs: dict[str, str] = {}   # job name -> ISO timestamp
        self.last_reconcile: Optional[datetime] = None
        self._trading_day_cache: dict[str, bool] = {}
        self._stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ calendar
    def is_trading_day(self, d: date) -> bool:
        k = d.isoformat()
        if k not in self._trading_day_cache:
            try:
                self._trading_day_cache[k] = d in self.engine.data.trading_days(d, d)
            except Exception:
                self._trading_day_cache[k] = d.weekday() < 5
        return self._trading_day_cache[k]

    def next_trading_day(self, d: date) -> date:
        try:
            days = [x for x in self.engine.data.trading_days(d + timedelta(days=1), d + timedelta(days=10)) if x > d]
            if days:
                return days[0]
        except Exception:
            pass
        n = d + timedelta(days=1)
        while n.weekday() >= 5:
            n += timedelta(days=1)
        return n

    # ------------------------------------------------------------ loop
    def tick(self) -> list[str]:
        """Run whatever is due now. Returns the job names it ran (used by tests)."""
        ran = []
        et = to_et(self.now())
        today = et.date()
        if not self.is_trading_day(today):
            return ran
        for name, start, latest in JOBS:
            if self.done.get(name) == today.isoformat():
                continue
            if start <= et.time() <= latest:
                try:
                    getattr(self, name)(today)
                except Exception as e:
                    log.exception("job %s failed", name)
                    self.engine.ledger.event("error", None, where=name, error=str(e))
                self.done[name] = today.isoformat()
                self.last_runs[name] = et.isoformat()
                ran.append(name)
        if time(9, 30) <= et.time() <= time(16, 5):
            if self.last_reconcile is None or (self.now() - self.last_reconcile).total_seconds() >= 60:
                self.engine.reconcile()
                self.last_reconcile = self.now()
                ran.append("reconcile")
        return ran

    def run_forever(self, interval_s: float = 10.0) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._stop.wait(interval_s)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.run_forever, name="hk-scheduler", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self.stop_stream(None)

    # ------------------------------------------------------------ jobs
    def pre_open_scan(self, today: date) -> None:
        if not self.engine.ledger.load_watchlist(today.isoformat()):
            # No watchlist from last night (e.g. first boot): build it now from the latest daily bars.
            self.engine.after_close_scan(today)
        self.engine.pre_open_scan(today)

    def start_stream(self, today: date) -> None:
        syms = self.engine.stream_symbols()
        if not syms:
            self.engine.ledger.event("stream_skipped", None, reason="empty watchlist")
            return
        self._backfill(today, syms)
        if self.stream_factory is None:
            return
        self.stream = self.stream_factory()

        async def on_bar(b: Any) -> None:
            self.engine.on_bar(b.symbol, Bar(b.timestamp, float(b.open), float(b.high), float(b.low),
                                             float(b.close), float(b.volume)))

        self.stream.subscribe_bars(on_bar, *syms)
        self.stream_thread = threading.Thread(target=self.stream.run, name="hk-stream", daemon=True)
        self.stream_thread.start()
        self.engine.ledger.event("stream_started", None, symbols=len(syms))

    def _backfill(self, today: date, syms: list[str]) -> None:
        open_et = datetime.combine(today, time(9, 30), tzinfo=ET)
        if to_et(self.now()) <= open_et + timedelta(minutes=1):
            return
        try:
            bars = self.engine.data.minute_bars_today(syms, open_et.astimezone(timezone.utc))
        except Exception as e:
            self.engine.ledger.event("warning", None, where="backfill", error=str(e))
            return
        for s, lst in bars.items():
            for b in lst:
                st = self.engine.session(s)
                st.update(Bar(b["t"], b["o"], b["h"], b["l"], b["c"], b["v"]))  # state only, no orders

    def open_job(self, today: date) -> None:
        self.engine.open_job()

    def earnings_job(self, today: date) -> None:
        if not self.engine.earnings_ok:
            try:
                self.engine.set_earnings(self.engine.data.earnings(today, today + timedelta(days=14)), True)
            except Exception as e:
                self.engine.ledger.event("warning", None, where="earnings_job",
                                         error=f"earnings calendar unavailable, holdings not checked: {e}")
                return
        syms = sorted({t["symbol"] for t in self.engine.ledger.open_trades()})
        adr = {}
        if syms:
            from . import rules
            for s, b in self.engine.data.daily_bars(syms, 40).items():
                adr[s] = rules.adr_pct([x["h"] for x in b], [x["l"] for x in b]) or 0
        last = {}
        try:
            for p in self.engine.broker.positions():
                last[p.symbol] = float(p.current_price)
        except Exception:
            pass
        self.engine.earnings_job(today, self.next_trading_day(today), adr, last)

    def stop_stream(self, today: Optional[date]) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception:
                pass
            self.stream = None

    def eod_job(self, today: date) -> None:
        self.engine.reconcile()
        syms = sorted({t["symbol"] for t in self.engine.ledger.open_trades()})
        if syms:
            self.engine.eod_trail(self.engine.data.daily_bars(syms, 120))
        self.engine.refresh_gtc_stops()
        self.engine.update_equity_peak()
        self.engine.ledger.event("eod_done", None, date=today.isoformat())

    def after_close_scan(self, today: date) -> None:
        self.engine.after_close_scan(self.next_trading_day(today))


def make_stream_factory(key_id: str, secret_key: str) -> Callable[[], Any]:
    def factory() -> Any:
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream
        return StockDataStream(key_id, secret_key, feed=DataFeed.SIP)
    return factory
