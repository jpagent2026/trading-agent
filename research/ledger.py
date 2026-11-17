"""Append-only results ledger (JSON lines) and the monthly 'tested N, passed K' note."""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime
from pathlib import Path

from .config import ET, RESULTS_DIR


def _clean(o):
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if hasattr(o, "item"):
        return _clean(o.item())
    return o


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              cwd=Path(__file__).parent, timeout=5).stdout.strip() or None
    except Exception:  # pragma: no cover
        return None


class Ledger:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else RESULTS_DIR / "ledger.jsonl"

    def append(self, record: dict) -> dict:
        rec = {"ts": datetime.now(ET).isoformat(timespec="seconds"), "commit": git_commit(), **record}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:  # append only, never rewritten
            fh.write(json.dumps(_clean(rec), sort_keys=True) + "\n")
        return rec

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def monthly_note(self, month: str | None = None) -> str:
        month = month or datetime.now(ET).strftime("%Y-%m")
        recs = [r for r in self.records() if str(r.get("ts", "")).startswith(month)]
        ideas = {r["idea"] for r in recs}
        passed = sorted({r["idea"] for r in recs if r.get("verdict") == "CANDIDATE"})
        note = f"{month}: tested {len(ideas)}, passed {len(passed)}"
        return note + (f" ({', '.join(passed)})" if passed else "")
