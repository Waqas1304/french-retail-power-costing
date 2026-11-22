"""Step 3 - What did it ACTUALLY cost to supply an average French household? (ex-post)

For each calendar year:
  baseload price B   = simple average of hourly spot prices (what a flat 1 MW block costs)
  load-weighted W    = sum(load_h * price_h) / sum(load_h) (what the household's MWh cost)
  shape premium S    = W - B  (> 0 because households consume more when power is expensive)

S is split into two parts that always add up exactly:
  seasonal part      = sum_m V_m * B_m / V  - B     (more MWh in winter, when months are expensive)
  intra-month part   = sum_m V_m * (W_m - B_m) / V  (evening peaks / cold days inside each month)

Capacity cost (simplified) = capacity price (€/MW) x household obligation (MW) / annual MWh,
with the obligation approximated by the average load on winter weekday peak hours.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def capacity_obligation_kw(df: pd.DataFrame, cfg: dict) -> float:
    c = cfg["capacity"]
    m = df["month"].isin(c["peak_months"]) & df["hour"].isin(c["peak_hours"]) & (df["daytype"] == 0)
    return float(df.loc[m, "load_kwh"].mean()) if m.any() else float("nan")


def annual_cost_stack(hourly: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    out = []
    cap_prices = cfg["capacity"]["price_eur_per_mw"]
    for year, g in hourly.groupby("year"):
        if len(g) < 24 * 300:          # skip incomplete years (e.g. 2020 starts in April)
            continue
        L, P = g["load_kwh"].to_numpy() / 1000.0, g["price_eur_mwh"].to_numpy()  # MWh per hour
        V = L.sum()
        B = P.mean()
        W = (L * P).sum() / V
        months = g.assign(L=L, LP=L * P).groupby("month").agg(V=("L", "sum"), LP=("LP", "sum"),
                                                             B=("price_eur_mwh", "mean"))
        months["W"] = months["LP"] / months["V"]
        seasonal = (months["V"] * months["B"]).sum() / V - B
        intra = (months["V"] * (months["W"] - months["B"])).sum() / V
        cap_price = cap_prices.get(int(year))
        obligation_mw = capacity_obligation_kw(g, cfg) / 1000.0
        cap_cost = cap_price * obligation_mw / V if cap_price else np.nan
        out.append({
            "year": int(year),
            "annual_mwh_per_household": V,
            "baseload_price": B,
            "load_weighted_price": W,
            "shape_premium": W - B,
            "shape_seasonal": seasonal,
            "shape_intra_month": intra,
            "shape_premium_pct": (W - B) / B * 100,
            "capacity_obligation_kw": obligation_mw * 1000,
            "capacity_cost": cap_cost,
            "energy_plus_capacity": W + (cap_cost if np.isfinite(cap_cost) else 0.0),
            "price_load_correlation": float(np.corrcoef(L, P)[0, 1]),
        })
    return pd.DataFrame(out).set_index("year")


def monthly_view(hourly: pd.DataFrame) -> pd.DataFrame:
    g = hourly.assign(LP=hourly["load_kwh"] * hourly["price_eur_mwh"])
    m = g.groupby(["year", "month"]).agg(load_kwh=("load_kwh", "sum"), LP=("LP", "sum"),
                                         baseload_price=("price_eur_mwh", "mean"),
                                         temp_c=("temp_c", "mean"))
    m["load_weighted_price"] = m["LP"] / m["load_kwh"]
    return m.drop(columns="LP")


def average_daily_profiles(hourly: pd.DataFrame) -> pd.DataFrame:
    """Average load and price by local hour, winter (Dec-Feb) vs summer (Jun-Aug), weekdays."""
    season = np.select([hourly["month"].isin([12, 1, 2]), hourly["month"].isin([6, 7, 8])],
                       ["winter", "summer"], default="other")
    g = hourly.assign(season=season)
    g = g[(g["season"] != "other") & (g["daytype"] == 0)]
    return g.groupby(["season", "hour"]).agg(load_kwh=("load_kwh", "mean"),
                                             price=("price_eur_mwh", "mean")).reset_index()
