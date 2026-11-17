"""Hard rule: research/ must never be able to place orders. Fails on any trading import/call."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Patterns are assembled from pieces so this file does not match itself.
FORBIDDEN = [
    r"alpaca\." + "trading",
    r"\b" + "Trading" + r"Client\b",
    "submit" + "_order",
    "replace" + "_order",
    "cancel" + "_order",
    "close" + "_position",
    "close_all" + "_positions",
    "Order" + "Request",
    r"paper-" + r"api\.alpaca",
    r"//" + r"api\.alpaca\.markets",
    r"/v2/" + "orders",
]


def py_files():
    return sorted(p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def test_guard_scans_something():
    assert len(py_files()) > 10


def test_no_trading_strings():
    hits = []
    for p in py_files():
        text = p.read_text()
        for pat in FORBIDDEN:
            for m in re.finditer(pat, text):
                hits.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    assert not hits, "trading code found in research/: " + "; ".join(hits)


def test_only_market_data_alpaca_imports():
    allowed = ("alpaca.data", "alpaca.common")
    bad = []
    for p in py_files():
        tree = ast.parse(p.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                if n.split(".")[0] == "alpaca" and not n.startswith(allowed):
                    bad.append(f"{p.relative_to(ROOT)}: {n}")
    assert not bad, bad


def test_guard_catches_a_planted_violation(tmp_path):
    sample = "from alpaca." + "trading.client import Trading" + "Client\nc.submit" + "_order(x)\n"
    hits = [pat for pat in FORBIDDEN if re.search(pat, sample)]
    assert len(hits) >= 3
