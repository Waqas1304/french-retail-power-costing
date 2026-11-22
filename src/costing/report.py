"""Write reports/results_summary.md from the result tables (numbers are never typed by hand)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def md_table(df: pd.DataFrame, fmt: str = "{:.1f}") -> str:
    cols = [df.index.name or ""] + list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for idx, row in df.iterrows():
        cells = [str(idx)]
        for v in row:
            if isinstance(v, (bool, np.bool_)):
                cells.append("yes" if v else "no")
            elif isinstance(v, (int, float, np.floating, np.integer)):
                cells.append("n/a" if pd.isna(v) else fmt.format(v))
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_summary(path, quality, stack, bt, sc, cfg):
    q = quality
    lines = ["# Results summary", "",
             "_Generated automatically by `run_all.py`. All values in €/MWh unless stated._", ""]

    lines += ["## Data used", "",
              f"- Analysis window: {q['analysis_window']['first']} → {q['analysis_window']['last']} "
              f"({q['analysis_window']['hours']:,} hours with both load and price)",
              f"- Average household consumption: {q['analysis_window']['avg_household_mwh_per_year']:.2f} MWh/year",
              f"- Average number of residential meters in the data: {q['enedis']['avg_meters']:,.0f}",
              f"- Price hours: {q['prices']['hours']:,}; negative-price hours: {q['prices']['negative_price_hours']:,}; "
              f"hours above 500 €/MWh: {q['prices']['hours_above_500']:,}",
              ""]

    if len(stack):
        s = stack
        hi = s["shape_premium"].idxmax()
        pos = s[s["shape_premium"] > 0]
        neg = s[s["shape_premium"] <= 0]
        lines += ["## 1. What did it cost to supply a household? (ex-post)", "",
                  md_table(s[["baseload_price", "load_weighted_price", "shape_premium", "shape_seasonal",
                              "shape_intra_month", "shape_premium_pct", "capacity_cost",
                              "price_load_correlation"]].round(2), "{:.2f}"), "",
                  f"- A household MWh cost more than a flat baseload MWh in **{len(pos)} of {len(s)}** years. "
                  f"The largest shape premium was in **{hi}**: **{s.loc[hi, 'shape_premium']:.1f} €/MWh** "
                  f"({s.loc[hi, 'shape_premium_pct']:.1f}% of baseload)."]
        if len(pos):
            share = (pos["shape_seasonal"] / pos["shape_premium"]).median() * 100
            lines.append(f"- In those years, the seasonal effect (more MWh in expensive winter months) explains "
                         f"about **{share:.0f}%** of the premium (median); the rest comes from daily peaks.")
        for y, r in neg.iterrows():
            lines.append(f"- **{y}** is the exception: shape premium **{r['shape_premium']:.1f} €/MWh**. The "
                         f"seasonal part is {r['shape_seasonal']:.1f} €/MWh, i.e. the months when households "
                         f"consume little were the most expensive ones.")
        lines.append("")

    if len(bt):
        cov = int(bt["covered"].sum())
        lines += ["## 2. Backtest of the fixed-price contract", "",
                  md_table(bt[["forward_proxy", "expected_shape_premium", "risk_premium", "offered_energy_price",
                               "realised_unit_cost", "margin_vs_offer", "covered", "percentile_of_outcome",
                               "dev_unhedged_price_move", "dev_shape_surprise"]]), "",
                  f"- The price set before the year covered the real cost in **{cov} of {len(bt)}** years "
                  f"(target: {cfg['model']['risk_quantile'] * 100:.0f}% of outcomes).",
                  "- `dev_unhedged_price_move` = cost of buying the unhedged share at a spot price different from "
                  "the forward; `dev_shape_surprise` = realised shape premium minus expected.", ""]
        notes = bt["notes"][bt["notes"].astype(bool)]
        for y, n in notes.items():
            lines.append(f"- Note {y}: {n}")
        lines.append("")

    if sc is not None and len(sc):
        lines += [f"## 3. {cfg['scenarios']['target_year']} what-if scenarios", "",
                  f"Reference = average spot price over the last 12 months of data: "
                  f"**{sc.attrs.get('reference_spot_last_12m', float('nan')):.1f} €/MWh**. "
                  "Scenarios are illustrative, not forecasts.", "",
                  md_table(sc), ""]

    lines += ["## Charts", "",
              "![](figures/01_daily_profiles.png)", "![](figures/02_cost_stack.png)",
              "![](figures/03_load_vs_temperature.png)", "![](figures/04_monthly_price_volume.png)",
              "![](figures/05_backtest.png)", "![](figures/06_simulation_last_year.png)",
              "![](figures/07_scenarios.png)", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
