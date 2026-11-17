"""End-to-end verdicts on synthetic data: a planted edge passes, a random walk fails."""
import json

import pytest

from research import costs, validation as val
from research.ledger import Ledger
from research.report import write_candidate_report
from research.tests.conftest import PlantedEdge

WF = val.WalkForward(12, 6, 6)


@pytest.fixture(scope="module")
def planted_result(planted_data):
    return val.evaluate(PlantedEdge(), planted_data, lambda s: costs.LIQUID, planted_data["SPY"], val.PassBar(), WF)


def test_planted_edge_is_candidate(planted_result):
    r = planted_result
    assert r.verdict == "CANDIDATE", r.failures
    assert r.metrics["trades"] >= 300
    assert r.metrics["avg_r"] > r.random["p95"]
    assert r.random["runs"] >= 200
    assert r.robustness["median_neighbour_avg_r"] > 0
    assert len(r.robustness["neighbours"]) >= 6
    for k in ("max_drawdown", "profit_factor", "win_rate", "avg_r", "sharpe", "exposure"):
        assert k in r.metrics
    assert 0 < r.metrics["exposure"] < 1


def test_random_walk_is_rejected(walk_data):
    r = val.evaluate(PlantedEdge(), walk_data, lambda s: costs.LIQUID, walk_data["SPY"], val.PassBar(), WF)
    assert r.verdict == "REJECTED"
    assert "expectancy" in r.failures or "significance" in r.failures
    assert "random_benchmark" in r.failures or "significance" in r.failures


def test_too_few_trades_is_rejected(planted_data):
    r = val.evaluate(PlantedEdge(), planted_data, lambda s: costs.LIQUID, planted_data["SPY"],
                     val.PassBar(min_trades=100_000, random_runs=20), WF)
    assert r.verdict == "REJECTED" and "min_trades" in r.failures


def test_one_lucky_year_is_rejected():
    table = {"year": {2019: {"trades": 100, "total_r": 50.0, "avg_r": 0.5},
                      2020: {"trades": 100, "total_r": -20.0, "avg_r": -0.2},
                      2021: {"trades": 100, "total_r": -25.0, "avg_r": -0.25}},
             "market": {}}
    j = val.judge_regimes(table, val.PassBar())
    assert not j["not_one_year"] and not j["regime_years"] and not j["regime_market"]


def test_ledger_is_append_only_and_report_written(planted_result, tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    led.append(planted_result.record())
    led.append({"idea": "x", "verdict": "REJECTED", "failures": ["expectancy"]})
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["verdict"] == "CANDIDATE"
    assert "tested 2, passed 1" in led.monthly_note()
    md = write_candidate_report(planted_result, tmp_path / "reports")
    assert md.exists() and list((tmp_path / "reports").glob("*.png"))
    assert "CANDIDATE: planted_edge" in md.read_text()
