"""Market-data access (all network I/O for scans lives here, so tests can mock it).

Data sources:
- Alpaca Market Data API, feed=SIP: daily bars (adjusted), hourly bars, 1-minute bars (backfill), snapshots
  (premarket % change), and the real-time 1-minute bar WebSocket (StockDataStream, in scheduler.py).
- Alpaca Trading API: the tradable US-equity asset list (scan universe) and the market calendar.
- Finnhub (FINNHUB_API_KEY): earnings calendar (/calendar/earnings) and market cap (/stock/profile2).
"""
from __future__ import annotations

import logging
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import httpx

log = logging.getLogger("hk_connection.data")

EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
BATCH = 200


def _chunks(seq: list[str], n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _bar_dict(b: Any) -> dict:
    return {"t": b.timestamp, "o": float(b.open), "h": float(b.high), "l": float(b.low),
            "c": float(b.close), "v": float(b.volume)}


class MarketData:
    def __init__(self, key_id: str, secret_key: str, trading_client: Any, finnhub_key: str = "",
                 http: Optional[httpx.Client] = None, sleep=_time.sleep):
        from alpaca.data.historical import StockHistoricalDataClient
        self.hist = StockHistoricalDataClient(key_id, secret_key)
        self.trading = trading_client
        self.finnhub_key = finnhub_key
        self.http = http or httpx.Client(timeout=20)
        self.sleep = sleep
        self._mcap_cache: dict[str, Optional[float]] = {}

    # ------------------------------------------------------------ universe / calendar
    def universe(self) -> list[str]:
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest
        assets = self.trading.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
        out = []
        for a in assets:
            ex = str(getattr(a.exchange, "value", a.exchange)).upper()
            if a.tradable and ex in EXCHANGES and "." not in a.symbol and "/" not in a.symbol:
                out.append(a.symbol)
        return out

    def trading_days(self, start: date, end: date) -> list[date]:
        from alpaca.trading.requests import GetCalendarRequest
        cal = self.trading.get_calendar(GetCalendarRequest(start=start, end=end))
        return [c.date if isinstance(c.date, date) else date.fromisoformat(str(c.date)) for c in cal]

    # ------------------------------------------------------------ bars
    def _bars(self, symbols: list[str], timeframe: Any, start: datetime, end: Optional[datetime] = None) -> dict[str, list[dict]]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        out: dict[str, list[dict]] = {}
        for chunk in _chunks(sorted(set(symbols)), BATCH):
            try:
                req = StockBarsRequest(symbol_or_symbols=chunk, timeframe=timeframe, start=start, end=end,
                                       adjustment=Adjustment.ALL, feed=DataFeed.SIP)
                res = self.hist.get_stock_bars(req)
                data = getattr(res, "data", {}) or {}
                for sym, bars in data.items():
                    out[sym] = [_bar_dict(b) for b in bars]
            except Exception as e:
                log.warning("bars batch failed (%d symbols): %s", len(chunk), e)
        return out

    def daily_bars(self, symbols: list[str], calendar_days: int) -> dict[str, list[dict]]:
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(timezone.utc) - timedelta(days=calendar_days)
        return self._bars(symbols, TimeFrame.Day, start)

    def light_scan(self, symbols: list[str], calendar_days: int = 45) -> dict[str, dict]:
        """Whole-market first pass, reduced batch by batch so memory stays small."""
        from alpaca.data.timeframe import TimeFrame
        from .scanner import light_metrics
        start = datetime.now(timezone.utc) - timedelta(days=calendar_days)
        out: dict[str, dict] = {}
        for chunk in _chunks(sorted(set(symbols)), BATCH):
            for sym, bars in self._bars(chunk, TimeFrame.Day, start).items():
                m = light_metrics(bars)
                if m:
                    out[sym] = m
        return out

    def hourly_bars(self, symbols: list[str], calendar_days: int = 14) -> dict[str, list[dict]]:
        from alpaca.data.timeframe import TimeFrame
        start = datetime.now(timezone.utc) - timedelta(days=calendar_days)
        return self._bars(symbols, TimeFrame.Hour, start)

    def minute_bars_today(self, symbols: list[str], session_open_utc: datetime) -> dict[str, list[dict]]:
        from alpaca.data.timeframe import TimeFrame
        return self._bars(symbols, TimeFrame.Minute, session_open_utc)

    def premarket_pct(self, symbols: list[str], today: date) -> dict[str, float]:
        """% change of the latest (premarket) trade vs the previous regular-session close."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockSnapshotRequest
        out: dict[str, float] = {}
        for chunk in _chunks(sorted(set(symbols)), BATCH):
            try:
                snaps = self.hist.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=chunk, feed=DataFeed.SIP))
            except Exception as e:
                log.warning("snapshot batch failed: %s", e)
                continue
            for sym, sn in (snaps or {}).items():
                try:
                    db, pdb = sn.daily_bar, sn.previous_daily_bar
                    prev_close = float(db.close) if db and db.timestamp.date() < today else float(pdb.close)
                    px = float(sn.latest_trade.price)
                    if prev_close > 0:
                        out[sym] = (px / prev_close - 1.0) * 100.0
                except Exception:
                    continue
        return out

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        """Latest SIP trade price per symbol (includes pre-market / after-hours prints). Used by the
        stop-breach guard; symbols that fail are simply missing from the result."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest
        out: dict[str, float] = {}
        for chunk in _chunks(sorted(set(symbols)), BATCH):
            try:
                res = self.hist.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=chunk, feed=DataFeed.SIP))
            except Exception as e:
                log.warning("latest trade batch failed: %s", e)
                continue
            for sym, tr in (res or {}).items():
                try:
                    px = float(tr.price)
                    if px > 0:
                        out[sym] = px
                except Exception:
                    continue
        return out

    # ------------------------------------------------------------ Finnhub
    def earnings(self, start: date, end: date) -> dict[str, dict]:
        """symbol -> {'date': date, 'hour': 'bmo'|'amc'|'dmh'|''} (earliest in the window)."""
        if not self.finnhub_key:
            raise RuntimeError("FINNHUB_API_KEY not set")
        r = self.http.get("https://finnhub.io/api/v1/calendar/earnings",
                          params={"from": start.isoformat(), "to": end.isoformat(), "token": self.finnhub_key})
        r.raise_for_status()
        out: dict[str, dict] = {}
        for row in (r.json() or {}).get("earningsCalendar", []) or []:
            sym = (row.get("symbol") or "").upper()
            try:
                d = date.fromisoformat(row.get("date"))
            except Exception:
                continue
            if sym and (sym not in out or d < out[sym]["date"]):
                out[sym] = {"date": d, "hour": (row.get("hour") or "").lower()}
        return out

    def market_cap(self, symbol: str) -> Optional[float]:
        if symbol in self._mcap_cache:
            return self._mcap_cache[symbol]
        cap = None
        if self.finnhub_key:
            try:
                r = self.http.get("https://finnhub.io/api/v1/stock/profile2",
                                  params={"symbol": symbol, "token": self.finnhub_key})
                if r.status_code == 429:
                    self.sleep(2.0)
                    r = self.http.get("https://finnhub.io/api/v1/stock/profile2",
                                      params={"symbol": symbol, "token": self.finnhub_key})
                v = (r.json() or {}).get("marketCapitalization")
                cap = float(v) * 1_000_000 if v else None
            except Exception:
                cap = None
            self.sleep(1.05)  # free tier: 60 calls/minute
        self._mcap_cache[symbol] = cap
        return cap
