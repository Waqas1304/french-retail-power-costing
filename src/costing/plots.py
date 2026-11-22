"""Charts (static PNGs for the README and the write-up)."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, INK2, GRID, SURFACE = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 10,
    "axes.titlesize": 12, "axes.titleweight": "bold", "axes.titlecolor": INK, "lines.linewidth": 2,
    "legend.frameon": False,
})


def _save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def daily_profiles(prof: pd.DataFrame, path):
    """Two panels (no dual axis): household load and spot price by hour, winter vs summer."""
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for season, color in (("winter", BLUE), ("summer", ORANGE)):
        p = prof[prof["season"] == season]
        axes[0].plot(p["hour"], p["load_kwh"] * 1000, color=color, label=season.capitalize())
        axes[1].plot(p["hour"], p["price"], color=color, label=season.capitalize())
    axes[0].set(title="Average household load (weekdays)", xlabel="Hour of day (local)", ylabel="Wh per hour")
    axes[1].set(title="Average spot price (weekdays)", xlabel="Hour of day (local)", ylabel="€/MWh")
    for ax in axes:
        ax.set_xticks(range(0, 24, 3))
        ax.legend()
    _save(fig, path)


def cost_stack(stack: pd.DataFrame, path):
    """Baseload bar, then shape parts stacked up (positive) or down from the top of baseload (negative)."""
    fig, ax = plt.subplots(figsize=(9, 4.8))
    years = stack.index.astype(str)
    base = stack["baseload_price"].to_numpy()
    parts = [(stack["shape_seasonal"].to_numpy(), ORANGE, "Shape: seasonal (winter)"),
             (stack["shape_intra_month"].to_numpy(), AQUA, "Shape: within month (peaks)"),
             (stack["capacity_cost"].fillna(0).to_numpy(), YELLOW, "Capacity (where price known)")]
    neg_total = sum(np.clip(v, None, 0) for v, _, _ in parts)
    ax.bar(years, base + neg_total, color=BLUE, label="Baseload price", edgecolor=SURFACE, linewidth=2)
    up = base.copy()
    down = base + neg_total
    for vals, color, lab in parts:
        pos, neg = np.clip(vals, 0, None), np.clip(vals, None, 0)
        ax.bar(years, pos, bottom=up, color=color, label=lab, edgecolor=SURFACE, linewidth=2)
        ax.bar(years, -neg, bottom=down, color=color, edgecolor=INK2, linewidth=0.8, hatch="///")
        up = up + pos
        down = down - neg
    total = stack["load_weighted_price"] + stack["capacity_cost"].fillna(0)
    for x, top, w in zip(years, np.maximum(up, base), total):
        ax.text(x, top + 4, f"{w:.0f}", ha="center", va="bottom", color=INK, fontsize=9)
    ax.set_ylim(0, up.max() * 1.15)
    ax.set(title="What one MWh for a household cost, by year", ylabel="€/MWh")
    ax.text(0.01, 0.97, "Hatched = negative part (2022: summer was pricier than winter)",
            transform=ax.transAxes, fontsize=8, color=INK2, va="top")
    ax.legend(loc="upper right", fontsize=8)
    _save(fig, path)


def load_vs_temperature(daily: pd.DataFrame, path):
    fig, ax = plt.subplots(figsize=(7, 4.5))
    wd = daily[daily["daytype"] == 0]
    ax.scatter(wd["temp_c"], wd["load_kwh"], s=8, color=BLUE, alpha=0.45, edgecolors="none")
    ax.set(title="Daily household consumption vs temperature (weekdays)",
           xlabel="Population-weighted daily temperature (°C)", ylabel="kWh per household per day")
    _save(fig, path)


def monthly_price_volume(monthly: pd.DataFrame, path):
    fig, ax = plt.subplots(figsize=(7, 4.5))
    m = monthly.reset_index()
    # divide by each year's average so 2022 does not dwarf the other years
    m["rel_price"] = m["baseload_price"] / m.groupby("year")["baseload_price"].transform("mean")
    ax.scatter(m["load_kwh"], m["rel_price"], s=30, color=BLUE, alpha=0.75, edgecolors=SURFACE, linewidths=0.8)
    ax.axhline(1, color=INK2, linewidth=1, linestyle="--")
    ax.set(title="Months with more consumption tend to be pricier",
           xlabel="Household consumption in the month (kWh)",
           ylabel="Month price ÷ that year's average")
    _save(fig, path)


def backtest_chart(bt: pd.DataFrame, path):
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = np.arange(len(bt))
    w = 0.38
    ax.bar(x - w / 2, bt["offered_energy_price"], w, color=BLUE, label="Price set before the year (with risk premium)",
           edgecolor=SURFACE, linewidth=2)
    ax.bar(x + w / 2, bt["realised_unit_cost"], w, color=ORANGE, label="Cost that actually happened",
           edgecolor=SURFACE, linewidth=2)
    for i, (o, r, c) in enumerate(zip(bt["offered_energy_price"], bt["realised_unit_cost"], bt["covered"])):
        ax.text(i, max(o, r) + 4, "covered" if c else "NOT covered", ha="center", color=INK, fontsize=9)
    ax.set_xticks(x, bt.index.astype(str))
    ax.set_ylim(0, max(bt["offered_energy_price"].max(), bt["realised_unit_cost"].max()) * 1.18)
    ax.set(title="Backtest: was the price high enough?", ylabel="€/MWh")
    ax.legend(fontsize=8, loc="upper right")
    _save(fig, path)


def simulation_hist(res, realised: float | None, path):
    fig, ax = plt.subplots(figsize=(8, 4.2))
    ax.hist(res.sim_unit_costs, bins=60, color=BLUE, edgecolor=SURFACE, linewidth=0.6)
    ax.axvline(res.forward + res.expected_shape_premium, color=INK2, linestyle="--", linewidth=1.5)
    ax.axvline(res.sim_quantile, color=ORANGE, linewidth=2)
    ymax = ax.get_ylim()[1]
    ax.text(res.forward + res.expected_shape_premium, ymax * 0.95, " expected (F + shape)", color=INK2, fontsize=8)
    ax.set_xlim(right=np.quantile(res.sim_unit_costs, 0.999) * 1.03)
    ax.text(res.sim_quantile, ymax * 0.80, " price set (P95)", color=INK, fontsize=8)
    if realised is not None:
        ax.axvline(realised, color=AQUA, linewidth=2)
        ax.text(realised, ymax * 0.68, " what happened", color=INK, fontsize=8)
    ax.set(title=f"{res.year}: {len(res.sim_unit_costs):,} simulated years of supply cost",
           xlabel="Unit sourcing cost (€/MWh)", ylabel="Number of simulations")
    _save(fig, path)


def scenarios_chart(sc: pd.DataFrame, year: int, path):
    fig, ax = plt.subplots(figsize=(8, 4.5))
    order = [k for k in ("low", "central", "high") if k in sc.index] + [k for k in sc.index if k not in ("low", "central", "high")]
    sc = sc.loc[order]
    names = [s.capitalize() for s in sc.index]
    parts = [("forward_level", BLUE, "Forward (baseload)"), ("expected_shape_premium", ORANGE, "Shape premium"),
             ("capacity_cost", YELLOW, "Capacity"), ("risk_premium", AQUA, "Risk premium (P95)")]
    bottom = np.zeros(len(sc))
    for col, color, lab in parts:
        v = sc[col].fillna(0).clip(lower=0).to_numpy()
        ax.bar(names, v, bottom=bottom, color=color, label=lab, edgecolor=SURFACE, linewidth=2)
        bottom += v
    for n, b in zip(names, bottom):
        ax.text(n, b + 3, f"{b:.0f}", ha="center", color=INK, fontsize=9)
    ax.set(title=f"{year} sourcing cost under three what-if scenarios", ylabel="€/MWh")
    ax.legend(fontsize=8, loc="upper left")
    _save(fig, path)
