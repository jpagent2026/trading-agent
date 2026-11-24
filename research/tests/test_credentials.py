"""Key-env guard: only RESEARCH_* keys, never the live bots' keys, never printed."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research import config, data_check, run_research

ROOT = Path(__file__).resolve().parents[1]
LIVE = {"ALPACA_" + "PAPER_KEY_ID": "live-a", "ALPACA_" + "PAPER_SECRET_KEY": "live-b",
        "HK_" + "ALPACA_PAPER_KEY_ID": "live-c", "HK_" + "ALPACA_PAPER_SECRET_KEY": "live-d",
        "ALPACA_API_KEY": "live-e", "APCA_API_KEY_ID": "live-f", "APCA_API_SECRET_KEY": "live-g"}


def test_missing_research_keys_never_fall_back(monkeypatch):
    for k in (config.KEY_ID_ENV, config.SECRET_ENV):
        monkeypatch.delenv(k, raising=False)
    for k, v in LIVE.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(config.MissingCredentials) as e:
        config.get_credentials()
    assert config.KEY_ID_ENV in str(e.value) and config.SECRET_ENV in str(e.value)
    assert not any(v in str(e.value) for v in LIVE.values())


def test_reads_only_research_vars():
    creds = config.get_credentials({config.KEY_ID_ENV: "rk", config.SECRET_ENV: "rs", **LIVE})
    assert (creds.key_id, creds.secret_key) == ("rk", "rs")
    with pytest.raises(config.MissingCredentials):
        config.get_credentials({config.KEY_ID_ENV: "rk", **LIVE})


def test_key_values_are_never_shown():
    creds = config.Credentials("SECRETKEYID123", "SECRETVALUE456")
    for s in (repr(creds), str(creds), f"{creds}"):
        assert "SECRET" not in s


def test_source_never_mentions_live_key_vars():
    needles = ["ALPACA_" + "PAPER", "HK_" + "ALPACA", "APCA_" + "API"]
    hits = []
    for p in ROOT.rglob("*.py"):
        if "tests" in p.parts:
            continue
        hits += [f"{p.name}: {n}" for n in needles if n in p.read_text()]
    assert not hits, hits


def test_data_check_missing_keys_is_graceful(monkeypatch, capsys, tmp_path):
    for k in (config.KEY_ID_ENV, config.SECRET_ENV):
        monkeypatch.delenv(k, raising=False)
    for k, v in LIVE.items():
        monkeypatch.setenv(k, v)
    out = tmp_path / "r.md"
    assert data_check.main(["--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert config.KEY_ID_ENV in err and not out.exists()


def test_run_research_missing_keys_is_graceful(monkeypatch, capsys, tmp_path):
    for k in (config.KEY_ID_ENV, config.SECRET_ENV):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path))
    rc = run_research.main(["--strategy", "orb", "--symbols", "SPY", "--start", "2024-01-01",
                            "--end", "2024-03-01", "--no-ledger"])
    assert rc == 2
    assert config.KEY_ID_ENV in capsys.readouterr().err


def test_end_time_embargo():
    now = datetime(2025, 6, 2, 15, 0, tzinfo=timezone.utc)
    assert config.safe_end(now, now=now) <= now - timedelta(minutes=15)
    assert config.safe_end(None, now=now) <= now - timedelta(minutes=15)
    early = now - timedelta(days=1)
    assert config.safe_end(early, now=now) == early
    with pytest.raises(ValueError):
        config.assert_end_allowed(now - timedelta(minutes=5), now=now)
    config.assert_end_allowed(now - timedelta(minutes=16), now=now)
    assert config.safe_start(datetime(2010, 1, 1, tzinfo=timezone.utc)) == config.EARLIEST_DATA
