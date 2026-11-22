"""Step 4 - Price a FIXED-PRICE contract before the year starts, and estimate its risk premium.

The supplier signs customers at a fixed €/MWh for year Y. At the end of year Y-1 it:
  1. buys a flat baseload block (H kW, every hour) at the forward price F,
  2. expects to buy/sell the rest (load minus block) on the spot market,
  3. adds a risk premium so the price still covers the cost in a bad year.

Everything the model knows is calibrated on data BEFORE year Y (no look-ahead).

Models (deliberately simple and explainable):
  * daily household load  = f(day type, heating & cooling degree-days, season) + residual
  * hourly load           = daily load x typical hourly share (by month, day type, hour)
  * daily price / yearly mean = g(day type, degree-days, season) + residual
      -> cold days are expensive days: this creates the price-volume correlation
  * hourly price          = price level x (daily ratio + typical hourly shape)
  * price level for year Y = F x lognormal shock (volatility from past forward-proxy errors)

Monte Carlo: weather from a random past year (1991..Y-1) + bootstrapped 7-day residual blocks
+ a random price-level shock -> thousands of possible years -> distribution of the unit cost.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .clean import TZ, add_calendar

SHAPE_LOOKBACK_YEARS = 3   # recent years define the hourly price shape (solar changed it a lot)
BLOCK_DAYS = 7


# ---------------------------------------------------------------- helpers
def _design(doy: np.ndarray, daytype: np.ndarray, hdd: np.ndarray, cdd: np.ndarray) -> np.ndarray:
    w = 2 * np.pi * doy / 365.25
    return np.column_stack([
        np.ones_like(hdd), daytype == 1, daytype == 2, hdd, cdd,
        np.sin(w), np.cos(w), np.sin(2 * w), np.cos(2 * w),
    ]).astype(float)


def _ols(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    r2 = 1 - resid.var() / y.var() if y.var() > 0 else float("nan")
    return beta, resid, float(r2)


def _block_bootstrap(resid: np.ndarray, n_sims: int, n_days: int, rng: np.random.Generator) -> np.ndarray:
    if len(resid) <= BLOCK_DAYS:
        return rng.choice(resid, size=(n_sims, n_days))
    n_blocks = int(np.ceil(n_days / BLOCK_DAYS))
    starts = rng.integers(0, len(resid) - BLOCK_DAYS, size=(n_sims, n_blocks))
    idx = (starts[:, :, None] + np.arange(BLOCK_DAYS)).reshape(n_sims, -1)[:, :n_days]
    return resid[idx]


def forward_price(year: int, price_hist: pd.DataFrame, cfg: dict) -> float:
    override = {int(k): v for k, v in (cfg["model"].get("forward_override") or {}).items()}
    if year in override:
        return float(override[year])
    ph = price_hist
    if cfg["model"]["forward_proxy"] == "q4_prev":
        sel = (ph["year"] == year - 1) & (ph["month"] >= 10)
    else:
        sel = ph["year"] == year - 1
    return float(ph.loc[sel, "price_eur_mwh"].mean())


def level_volatility(cutoff_year: int, price_hist: pd.DataFrame, cfg: dict) -> tuple[float, pd.DataFrame]:
    """Std of log(realised annual average / forward proxy) for years before cutoff."""
    rows = []
    years = sorted(price_hist["year"].unique())
    counts = price_hist.groupby("year").size()
    for y in years:
        if y >= cutoff_year or (y - 1) not in years or counts.get(y, 0) < 24 * 360:
            continue
        F = forward_price(int(y), price_hist, cfg)
        A = price_hist.loc[price_hist["year"] == y, "price_eur_mwh"].mean()
        if F > 0 and A > 0:
            rows.append({"year": int(y), "forward_proxy": F, "realised": A, "log_error": np.log(A / F)})
    errs = pd.DataFrame(rows)
    floor = cfg["model"]["min_level_vol"]
    sigma = float(errs["log_error"].std(ddof=1)) if len(errs) >= 3 else floor
    return max(sigma, floor), errs


# ---------------------------------------------------------------- calibration
@dataclass
class Calibration:
    cutoff_year: int
    load_beta: np.ndarray
    load_resid: np.ndarray
    load_r2: float
    price_beta: np.ndarray
    price_resid: np.ndarray
    price_r2: float
    load_share: pd.Series          # index (month, daytype, hour)
    price_shape: pd.Series         # index (month, daytype, hour)
    hdd_by_wy: np.ndarray          # (n_weather_years, 366)
    cdd_by_wy: np.ndarray
    weather_years: list
    n_load_days: int
    notes: list = field(default_factory=list)


def calibrate(cutoff_year: int, hourly: pd.DataFrame, daily: pd.DataFrame,
              price_hist: pd.DataFrame, dd: pd.DataFrame, cfg: dict) -> Calibration:
    notes = []
    # --- daily load model (years < cutoff)
    d = daily[daily["year"] < cutoff_year].dropna(subset=["hdd"])
    if len(d) < 180:
        raise ValueError(f"Not enough load history before {cutoff_year} ({len(d)} days)")
    if len(d) < 365:
        notes.append(f"thin calibration: only {len(d)} days of load history before {cutoff_year}")
    doy = pd.DatetimeIndex(d.index).dayofyear.to_numpy()
    Xl = _design(doy, d["daytype"].to_numpy(), d["hdd"].to_numpy(), d["cdd"].to_numpy())
    lb, lr, lr2 = _ols(Xl, d["load_kwh"].to_numpy())

    # --- daily price-ratio model on the longer price history (years < cutoff)
    ph = price_hist[price_hist["year"] < cutoff_year]
    counts = ph.groupby("year").size()
    full_years = counts[counts >= 24 * 360].index
    ph = ph[ph["year"].isin(full_years)]
    annual = ph.groupby("year")["price_eur_mwh"].mean()
    pdaily = ph.groupby("date").agg(price=("price_eur_mwh", "mean"), year=("year", "first"),
                                    daytype=("daytype", "first"))
    pdaily = pdaily.join(dd[["hdd", "cdd"]], how="inner")
    pdaily["ratio"] = pdaily["price"] / pdaily["year"].map(annual)
    pdoy = pd.DatetimeIndex(pdaily.index).dayofyear.to_numpy()
    Xp = _design(pdoy, pdaily["daytype"].to_numpy(), pdaily["hdd"].to_numpy(), pdaily["cdd"].to_numpy())
    pb, pr, pr2 = _ols(Xp, pdaily["ratio"].to_numpy())

    # --- typical hourly shapes
    h = hourly[hourly["year"] < cutoff_year]
    day_tot = h.groupby("date")["load_kwh"].transform("sum")
    load_share = (h["load_kwh"] / day_tot).groupby([h["month"], h["daytype"], h["hour"]]).mean()

    recent = ph[ph["year"] >= cutoff_year - SHAPE_LOOKBACK_YEARS]
    day_mean = recent.groupby("date")["price_eur_mwh"].transform("mean")
    yr_mean = recent["year"].map(annual)
    price_shape = ((recent["price_eur_mwh"] - day_mean) / yr_mean).groupby(
        [recent["month"], recent["daytype"], recent["hour"]]).mean()

    # --- weather years
    ddf = dd.copy()
    ddf["year"] = ddf.index.year
    ddf["doy"] = ddf.index.dayofyear
    wy = [y for y, g in ddf.groupby("year") if len(g) >= 365 and y < cutoff_year and g["hdd"].notna().all()]
    hdd = np.full((len(wy), 366), np.nan)
    cdd = np.full((len(wy), 366), np.nan)
    for i, y in enumerate(wy):
        g = ddf[ddf["year"] == y]
        hdd[i, g["doy"] - 1] = g["hdd"]
        cdd[i, g["doy"] - 1] = g["cdd"]
    hdd[:, 365] = np.where(np.isnan(hdd[:, 365]), hdd[:, 364], hdd[:, 365])   # day 366 in non-leap years
    cdd[:, 365] = np.where(np.isnan(cdd[:, 365]), cdd[:, 364], cdd[:, 365])

    return Calibration(cutoff_year, lb, lr, lr2, pb, pr, pr2, load_share, price_shape,
                       hdd, cdd, wy, len(d), notes)


# ---------------------------------------------------------------- simulation
def _target_grid(year: int) -> pd.DataFrame:
    local = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", tz=TZ, inclusive="left")
    grid = add_calendar(pd.DataFrame(index=local.tz_convert("UTC")))
    days = pd.Index(grid["date"].unique())
    grid["day_idx"] = days.get_indexer(grid["date"])
    return grid, days


def _lookup(series: pd.Series, grid: pd.DataFrame, fallback: float) -> np.ndarray:
    keys = pd.MultiIndex.from_arrays([grid["month"], grid["daytype"], grid["hour"]])
    vals = series.reindex(keys).to_numpy(dtype=float)
    if np.isnan(vals).any():   # e.g. a holiday-hour combination never seen: use month/hour mean
        alt = series.groupby(level=[0, 2]).mean()
        alt_vals = alt.reindex(pd.MultiIndex.from_arrays([grid["month"], grid["hour"]])).to_numpy(dtype=float)
        vals = np.where(np.isnan(vals), alt_vals, vals)
    return np.nan_to_num(vals, nan=fallback)


@dataclass
class PricingResult:
    year: int
    forward: float
    level_vol: float
    hedge_kw: float
    expected_mwh: float
    expected_shape_premium: float
    expected_capacity_cost: float
    sim_unit_costs: np.ndarray
    sim_mean: float
    sim_quantile: float
    sim_cvar: float
    risk_premium: float
    offered_energy_price: float
    load_r2: float
    price_r2: float
    notes: list


def price_contract(target_year: int, cal: Calibration, F: float, sigma: float, cfg: dict,
                   cap_price: float | None) -> PricingResult:
    m = cfg["model"]
    rng = np.random.default_rng(m["random_seed"] + target_year)
    n_sims = int(m["n_simulations"])
    grid, days = _target_grid(target_year)
    n_days, n_hours = len(days), len(grid)
    day_idx = grid["day_idx"].to_numpy()
    doy = pd.DatetimeIndex(days).dayofyear.to_numpy()
    day_type = grid.groupby("day_idx")["daytype"].first().to_numpy()

    share = _lookup(cal.load_share, grid, 1 / 24)
    share = share / np.bincount(day_idx, weights=share)[day_idx]     # each day's shares sum to 1
    kshape = _lookup(cal.price_shape, grid, 0.0)

    def run(hdd_days, cdd_days, lres, pres, level):
        """hdd_days etc: (n, n_days); level: (n,) -> hourly L (kWh), P (€/MWh)."""
        n = hdd_days.shape[0]
        E = np.empty((n, n_days)); R = np.empty((n, n_days))
        for i in range(n):
            X = _design(doy, day_type, hdd_days[i], cdd_days[i])
            E[i] = X @ cal.load_beta + lres[i]
            R[i] = X @ cal.price_beta + pres[i]
        E = np.clip(E, 0.1, None)
        L = E[:, day_idx] * share
        P = level[:, None] * (R[:, day_idx] + kshape)
        return L, P

    # --- expected (normal weather, no surprises, level = F)
    hdd_n = np.nanmean(cal.hdd_by_wy, axis=0)[doy - 1][None, :]
    cdd_n = np.nanmean(cal.cdd_by_wy, axis=0)[doy - 1][None, :]
    zeros = np.zeros((1, n_days))
    L0, P0 = run(hdd_n, cdd_n, zeros, zeros, np.array([1.0]))
    norm = P0.mean()                       # rescale so the expected baseload price is exactly F
    L0, P0 = L0[0], P0[0] * F / norm
    V0 = L0.sum()
    shape_exp = (L0 * P0).sum() / V0 - P0.mean()
    H = m["hedge_ratio"] * L0.mean()
    c = cfg["capacity"]
    peak = (grid["month"].isin(c["peak_months"]) & grid["hour"].isin(c["peak_hours"])
            & (grid["daytype"] == 0)).to_numpy()
    cap_exp = cap_price * (L0[peak].mean() / 1000) / (V0 / 1000) if cap_price else float("nan")

    # --- Monte Carlo
    unit = np.empty(n_sims)
    chunk = 250
    for s0 in range(0, n_sims, chunk):
        n = min(chunk, n_sims - s0)
        wy = rng.integers(0, len(cal.weather_years), size=n)
        hdd_s = cal.hdd_by_wy[wy][:, doy - 1]
        cdd_s = cal.cdd_by_wy[wy][:, doy - 1]
        lres = _block_bootstrap(cal.load_resid, n, n_days, rng)
        pres = _block_bootstrap(cal.price_resid, n, n_days, rng)
        z = rng.standard_normal(n)
        level = (F / norm) * np.exp(sigma * z - 0.5 * sigma ** 2)
        L, P = run(hdd_s, cdd_s, lres, pres, level)
        cost = H * n_hours * F + ((L - H) * P).sum(axis=1)      # (kWh x €/MWh) -> scaled below
        unit[s0:s0 + n] = cost / L.sum(axis=1)                  # €/MWh

    q = float(np.quantile(unit, m["risk_quantile"]))
    cvar = float(unit[unit >= q].mean())
    base = F + shape_exp
    return PricingResult(
        year=target_year, forward=F, level_vol=sigma, hedge_kw=H, expected_mwh=V0 / 1000,
        expected_shape_premium=shape_exp, expected_capacity_cost=cap_exp,
        sim_unit_costs=unit, sim_mean=float(unit.mean()), sim_quantile=q, sim_cvar=cvar,
        risk_premium=q - base, offered_energy_price=q,
        load_r2=cal.load_r2, price_r2=cal.price_r2, notes=list(cal.notes),
    )


def realised_unit_cost(hourly_year: pd.DataFrame, res: PricingResult) -> dict:
    """What the hedged contract actually cost in the delivery year, and why."""
    L = hourly_year["load_kwh"].to_numpy()
    P = hourly_year["price_eur_mwh"].to_numpy()
    n, V = len(L), L.sum()
    H, F = res.hedge_kw, res.forward
    unit = (H * n * F + ((L - H) * P).sum()) / V
    B = P.mean()
    S = (L * P).sum() / V - B
    cover = H * n / V
    return {
        "realised_baseload": B,
        "realised_shape_premium": S,
        "realised_unit_cost": unit,
        "hedge_cover_ratio": cover,
        "dev_unhedged_price_move": (B - F) * (1 - cover),
        "dev_shape_surprise": S - res.expected_shape_premium,
        "realised_mwh": V / 1000,
    }
