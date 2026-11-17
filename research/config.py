"""Settings for the research harness.

Credentials come ONLY from RESEARCH_ALPACA_KEY_ID / RESEARCH_ALPACA_SECRET_KEY.
There is deliberately no fallback to any other key set: the live bots' keys
must never be used for research.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

KEY_ID_ENV = "RESEARCH_ALPACA_KEY_ID"
SECRET_ENV = "RESEARCH_ALPACA_SECRET_KEY"
DATA_DIR_ENV = "RESEARCH_DATA_DIR"

DEFAULT_DATA_DIR = Path("/workspace/research-data")
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Free (Basic) plan: SIP history from about 2016; the latest 15 minutes are off-limits.
EARLIEST_DATA = datetime(2016, 1, 1, tzinfo=UTC)
RECENT_EMBARGO = timedelta(minutes=15)
SAFETY_MARGIN = timedelta(minutes=1)
FEED = "sip"
ADJUSTMENT = "all"  # splits + dividends

# Default universe: liquid ETFs plus large caps (all "liquid" cost tier).
DEFAULT_ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLF", "XLK", "XLE", "XLV", "XLI", "XLY"]
DEFAULT_LARGE_CAPS = ["AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "JPM", "XOM", "UNH"]
DEFAULT_UNIVERSE = DEFAULT_ETFS + DEFAULT_LARGE_CAPS

# Symbols that were delisted or acquired after 2016. Used to probe whether free
# data still returns bars for dead names (survivorship check). Not traded by default.
DELISTED_PROBE = {
    "TWTR": "taken private Oct 2022",
    "ATVI": "acquired by Microsoft Oct 2023",
    "XLNX": "acquired by AMD Feb 2022",
    "CTXS": "taken private Sep 2022",
    "SIVB": "bank failure Mar 2023",
    "FRC": "bank failure May 2023",
    "CELG": "acquired by BMS Nov 2019",
    "FB": "renamed to META Jun 2022 (ticker change, not a delisting)",
    "META": "new ticker of FB: does its history start in 2016?",
}

BENCHMARK = "SPY"  # regime labels (trend + realized vol) come from this symbol


class MissingCredentials(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    key_id: str
    secret_key: str

    def __repr__(self) -> str:  # never show key values
        return "Credentials(key_id=***, secret_key=***)"

    __str__ = __repr__


def get_credentials(environ=None) -> Credentials:
    """Read research keys from the environment. No fallback to any other variables."""
    env = os.environ if environ is None else environ
    key = (env.get(KEY_ID_ENV) or "").strip()
    secret = (env.get(SECRET_ENV) or "").strip()
    missing = [n for n, v in ((KEY_ID_ENV, key), (SECRET_ENV, secret)) if not v]
    if missing:
        raise MissingCredentials(
            "Research data keys are not set: missing " + ", ".join(missing) + ". "
            "Create a separate Alpaca paper account key pair for research and export "
            f"{KEY_ID_ENV} and {SECRET_ENV}. Keys of the live bots are never used."
        )
    return Credentials(key, secret)


def data_dir(override: str | Path | None = None) -> Path:
    if override:
        return Path(override)
    return Path(os.environ.get(DATA_DIR_ENV) or DEFAULT_DATA_DIR)


def safe_end(end: datetime | None = None, now: datetime | None = None) -> datetime:
    """Clamp a requested end time so it is at least 15 minutes (+1 min margin) in the past."""
    now = now or datetime.now(UTC)
    latest = now - RECENT_EMBARGO - SAFETY_MARGIN
    if end is None:
        return latest
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return min(end, latest)


def safe_start(start: datetime) -> datetime:
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    return max(start, EARLIEST_DATA)


def assert_end_allowed(end: datetime, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if end > now - RECENT_EMBARGO:
        raise ValueError("end time must be at least 15 minutes in the past (free data plan)")
