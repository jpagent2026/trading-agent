"""Shared fixtures. Alpaca is fully mocked; nothing here touches the network."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from hk_connection.bars import Bar
from hk_connection.broker import Broker
from hk_connection.config import Settings
from hk_connection.engine import Engine
from hk_connection.ledger import Ledger

ET = ZoneInfo("America/New_York")
SESSION = date(2026, 11, 11)  # a Wednesday


def et(h: int, m: int, d: date = SESSION) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ET)


def mbar(h: int, m: int, o: float, hi: float, lo: float, c: float, v: float = 1000) -> Bar:
    return Bar(et(h, m), o, hi, lo, c, v)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def make_client(equity: float = 2000.0, buying_power: float = 8000.0) -> MagicMock:
    c = MagicMock(name="TradingClient")
    c.get_account.return_value = SimpleNamespace(equity=str(equity), cash=str(equity), buying_power=str(buying_power),
                                                 multiplier="4", status="ACTIVE")
    c.get_all_positions.return_value = []
    c.get_orders.return_value = []
    c.get_open_position.side_effect = Exception("position does not exist")
    c.get_asset.return_value = SimpleNamespace(shortable=True, easy_to_borrow=True)
    counter = {"n": 0}

    def submit(req):
        counter["n"] += 1
        legs = None
        if getattr(req, "stop_loss", None) is not None:
            legs = [SimpleNamespace(id=f"stop-leg-{counter['n']}", order_type="stop", status="held")]
        return SimpleNamespace(id=f"order-{counter['n']}", legs=legs, status="accepted")

    c.submit_order.side_effect = submit
    c.get_order_by_id.return_value = SimpleNamespace(status="canceled", filled_avg_price=None, filled_qty=None, legs=None)
    return c


@pytest.fixture
def settings(tmp_path):
    return Settings(key_id="hk-key", secret_key="hk-secret", capital_base=None, data_dir=tmp_path,
                    ledger_path=tmp_path / "hk.db", engine_enabled=False, shorts_enabled=True, trail_ema=9,
                    watchlist_max=25, finnhub_api_key="x")


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "hk.db")


@pytest.fixture
def client():
    return make_client()


@pytest.fixture
def broker(client):
    return Broker(client, sleep=lambda s: None, cancel_wait_s=0.5)


@pytest.fixture
def clock():
    return Clock(et(9, 40))


@pytest.fixture
def engine(settings, broker, ledger, clock):
    e = Engine(settings, broker, ledger, data=MagicMock(name="MarketData"), now=clock)
    e.set_earnings({}, True)
    return e
