"""Step 5 - Backtest: was the risk premium big enough, year after year?

For every delivery year Y: calibrate on data before Y only, price the contract, then
compare with what really happened in Y. A year is 'covered' when the realised unit cost
stayed below the offered energy price (forward + expected shape + risk premium).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .risk_model import calibrate, forward_price, level_volatility, price_contract, realised_unit_cost


def run_backtest(hourly, daily, price_hist, dd, cfg):
    rows, results = [], {}
    available = set(hourly.groupby("year").size().loc[lambda s: s >= 24 * 300].index)
    for Y in cfg["backtest"]["years"]:
        if Y not in available:
            print(f"  backtest {Y}: skipped (no full year of data)")
            continue
        try:
            cal = calibrate(Y, hourly, daily, price_hist, dd, cfg)
        except ValueError as exc:
            print(f"  backtest {Y}: skipped ({exc})")
            continue
        F = forward_price(Y, price_hist, cfg)
        sigma, _ = level_volatility(Y, price_hist, cfg)
        cap = cfg["capacity"]["price_eur_per_mw"].get(Y)
        res = price_contract(Y, cal, F, sigma, cfg, cap)
        real = realised_unit_cost(hourly[hourly["year"] == Y], res)
        results[Y] = res
        rows.append({
            "year": Y,
            "forward_proxy": res.forward,
            "level_vol": res.level_vol,
            "expected_shape_premium": res.expected_shape_premium,
            "risk_premium": res.risk_premium,
            "offered_energy_price": res.offered_energy_price,
            "realised_unit_cost": real["realised_unit_cost"],
            "margin_vs_offer": res.offered_energy_price - real["realised_unit_cost"],
            "covered": bool(real["realised_unit_cost"] <= res.offered_energy_price),
            "realised_baseload": real["realised_baseload"],
            "realised_shape_premium": real["realised_shape_premium"],
            "dev_unhedged_price_move": real["dev_unhedged_price_move"],
            "dev_shape_surprise": real["dev_shape_surprise"],
            "hedge_cover_ratio": real["hedge_cover_ratio"],
            "percentile_of_outcome": float((res.sim_unit_costs < real["realised_unit_cost"]).mean() * 100),
            "load_model_r2": res.load_r2,
            "price_model_r2": res.price_r2,
            "notes": "; ".join(res.notes),
        })
        print(f"  backtest {Y}: F={F:6.1f}  premium={res.risk_premium:6.1f}  "
              f"offer={res.offered_energy_price:6.1f}  realised={real['realised_unit_cost']:6.1f} €/MWh")
    table = pd.DataFrame(rows).set_index("year") if rows else pd.DataFrame()
    return table, results


def run_scenarios(hourly, daily, price_hist, dd, cfg):
    sc = cfg["scenarios"]
    Y = int(sc["target_year"])
    cutoff = int(hourly["year"].max()) + 1
    cal = calibrate(cutoff, hourly, daily, price_hist, dd, cfg)
    last = price_hist["local_time"].max()
    recent = price_hist[price_hist["local_time"] > last - pd.Timedelta(days=365)]
    ref = float(recent["price_eur_mwh"].mean())
    cap = cfg["capacity"]["price_eur_per_mw"].get(Y)
    rows, results = [], {}
    for name, case in sc["cases"].items():
        F = ref * case["level_multiplier"]
        res = price_contract(Y, cal, F, case["level_vol"], cfg, cap)
        results[name] = res
        cap_cost = res.expected_capacity_cost if np.isfinite(res.expected_capacity_cost) else 0.0
        rows.append({
            "scenario": name,
            "forward_level": F,
            "level_vol": case["level_vol"],
            "expected_shape_premium": res.expected_shape_premium,
            "capacity_cost": res.expected_capacity_cost,
            "risk_premium": res.risk_premium,
            "total_sourcing_cost": F + res.expected_shape_premium + cap_cost + res.risk_premium,
        })
    table = pd.DataFrame(rows).set_index("scenario")
    table.attrs["reference_spot_last_12m"] = ref
    return table, results
