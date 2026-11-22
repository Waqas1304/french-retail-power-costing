"""Tests: formulas on toy numbers + a full end-to-end run on synthetic raw files."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.make_fixtures import make  # noqa: E402


def test_unit_cost_identity_on_toy_numbers():
    """unit cost - F - expected shape == unhedged price move + shape surprise."""
    from costing.risk_model import PricingResult, realised_unit_cost
    L = np.array([1.0, 2.0, 3.0, 2.0])          # kWh per hour
    P = np.array([50.0, 80.0, 120.0, 70.0])     # €/MWh
    df = pd.DataFrame({"load_kwh": L, "price_eur_mwh": P})
    res = PricingResult(2099, 60.0, 0.2, 1.5, 0.008, 5.0, np.nan, np.array([0.0]), 0, 0, 0, 0, 0, 0, 0, [])
    r = realised_unit_cost(df, res)
    direct = (1.5 * 4 * 60.0 + ((L - 1.5) * P).sum()) / L.sum()
    assert r["realised_unit_cost"] == pytest.approx(direct)
    assert r["realised_unit_cost"] - 60.0 - 5.0 == pytest.approx(
        r["dev_unhedged_price_move"] + r["dev_shape_surprise"])


@pytest.fixture(scope="module")
def pipeline_run(tmp_path_factory):
    base = tmp_path_factory.mktemp("run")
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["period"]["end"] = "2025-12-31"
    cfg["model"]["n_simulations"] = 300
    cfg["backtest"]["years"] = [2021, 2022, 2023, 2024, 2025]
    make(base / "data" / "raw", end="2025-12-31")
    cfg_path = base / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    import run_all
    run_all.main(["--config", str(cfg_path), "--base", str(base), "--skip-download"])
    return base


def test_outputs_exist(pipeline_run):
    rep = pipeline_run / "reports"
    for f in ["results_summary.md", "data_quality.json", "tables/cost_stack_by_year.csv",
              "tables/backtest.csv", "tables/scenarios.csv", "figures/02_cost_stack.png",
              "figures/05_backtest.png", "figures/07_scenarios.png"]:
        assert (rep / f).exists(), f


def test_cleaning(pipeline_run):
    import json
    q = json.loads((pipeline_run / "reports" / "data_quality.json").read_text())
    assert q["enedis"]["profiles_per_half_hour_expected"] == 6
    assert q["prices"]["missing_hours_after_fill"] == 0          # 15-min data was averaged, no holes
    assert 2 < q["analysis_window"]["avg_household_mwh_per_year"] < 8


def test_shape_decomposition_adds_up(pipeline_run):
    s = pd.read_csv(pipeline_run / "reports" / "tables" / "cost_stack_by_year.csv", index_col=0)
    assert np.allclose(s["shape_seasonal"] + s["shape_intra_month"], s["shape_premium"])
    assert np.allclose(s["load_weighted_price"] - s["baseload_price"], s["shape_premium"])
    assert (s["shape_premium"] > 0).all()        # synthetic data: load and price peak together


def test_backtest_decomposition(pipeline_run):
    b = pd.read_csv(pipeline_run / "reports" / "tables" / "backtest.csv", index_col=0)
    lhs = b["realised_unit_cost"] - b["forward_proxy"] - b["expected_shape_premium"]
    assert np.allclose(lhs, b["dev_unhedged_price_move"] + b["dev_shape_surprise"])
    assert (b["risk_premium"] > 0).all()
