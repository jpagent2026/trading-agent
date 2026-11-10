"""Environment settings for HK Connection. Paper trading only: paper=True is hard-coded in broker.py."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _opt_float(name: str) -> Optional[float]:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return None
    f = float(v)
    if f <= 0:
        raise ValueError(f"{name} must be > 0 when set")
    return f


@dataclass
class Settings:
    key_id: str = ""
    secret_key: str = ""
    # Optional sizing override. Unset/empty = size from the live Alpaca account equity.
    capital_base: Optional[float] = None
    data_dir: Path = Path("/data")
    ledger_path: Path = Path("/data/hk_connection.db")
    engine_enabled: bool = True
    shorts_enabled: bool = True
    trail_ema: int = 9
    watchlist_max: int = 25
    finnhub_api_key: str = ""
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("HK_DATA_DIR", "/data"))
        s = cls(
            key_id=os.getenv("HK_ALPACA_PAPER_KEY_ID", "").strip(),
            secret_key=os.getenv("HK_ALPACA_PAPER_SECRET_KEY", "").strip(),
            capital_base=_opt_float("CAPITAL_BASE"),
            data_dir=data_dir,
            ledger_path=Path(os.getenv("HK_LEDGER_PATH", str(data_dir / "hk_connection.db"))),
            engine_enabled=_bool("HK_ENGINE_ENABLED", True),
            shorts_enabled=_bool("HK_SHORTS_ENABLED", True),
            trail_ema=int(os.getenv("HK_TRAIL_EMA", "9") or 9),
            watchlist_max=int(os.getenv("HK_WATCHLIST_MAX", "25") or 25),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
        )
        if s.trail_ema not in (9, 21):
            raise ValueError("HK_TRAIL_EMA must be 9 or 21")
        # Never run on Agent 200's keys.
        agent200 = {os.getenv(n, "").strip() for n in ("ALPACA_API_KEY", "ALPACA_PAPER_KEY_ID")} - {""}
        if s.key_id and s.key_id in agent200:
            raise RuntimeError("HK_ALPACA_PAPER_KEY_ID matches Agent 200's key; HK Connection needs its own paper account")
        if not s.key_id or not s.secret_key:
            s.warnings.append("HK_ALPACA_PAPER_KEY_ID / HK_ALPACA_PAPER_SECRET_KEY not set: engine will not start")
        if not s.finnhub_api_key:
            s.warnings.append("FINNHUB_API_KEY not set: earnings dates unknown, so new stock entries are blocked "
                              "(earnings rule fails closed); ETFs still trade. Market caps unknown: 20% position cap.")
        return s
