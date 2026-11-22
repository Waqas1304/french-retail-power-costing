"""Step 2 - Clean the raw files and build one hourly table + one daily table.

Rules, in plain words:
* All timestamps are converted to UTC first (no daylight-saving duplicates), then a
  local-time (Europe/Paris) copy is kept for calendar features (hour, month, holidays).
* Prices: duplicates removed, 15-minute prices (from Oct 2025) averaged to hourly,
  gaps of up to 3 hours filled by linear interpolation, longer gaps left empty and counted.
  Negative prices and price spikes are REAL market events and are kept.
* Consumption: only residential profiles and the 'all power ranges' total row are kept,
  profiles are summed, and the result is divided by the number of meters to get the
  load of an average household. Half-hours are summed to hours.
* Temperature: population-weighted average of 8 big cities -> one daily 'France' value.
"""
from __future__ import annotations

import json

import holidays
import numpy as np
import pandas as pd

from .config import Paths

TZ = "Europe/Paris"


# ---------------------------------------------------------------- prices
def clean_prices(cfg: dict, paths: Paths, quality: dict) -> pd.Series:
    files = sorted((paths.raw / "prices").glob("energy_charts_*.json"))
    parts = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        s = pd.Series(d["price"], index=pd.to_datetime(d["unix_seconds"], unit="s", utc=True), dtype="float64")
        parts.append(s)
    for f in sorted((paths.raw / "prices").glob("entsoe_*.csv")):
        df = pd.read_csv(f, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        parts.append(df["price"].astype("float64"))
    if not parts:
        raise FileNotFoundError("No raw price files found - run the download step first")

    raw = pd.concat(parts).sort_index()
    n_raw = len(raw)
    raw = raw[~raw.index.duplicated(keep="first")]
    hourly = raw.resample("h").mean()               # 15-min -> hourly average (no-op for hourly data)
    full_idx = pd.date_range(hourly.index.min(), hourly.index.max(), freq="h", tz="UTC")
    hourly = hourly.reindex(full_idx)
    missing_before = int(hourly.isna().sum())
    hourly = hourly.interpolate(limit=3, limit_area="inside")
    missing_after = int(hourly.isna().sum())

    quality["prices"] = {
        "raw_points": n_raw,
        "duplicates_removed": int(n_raw - len(raw)),
        "hours": int(len(hourly)),
        "missing_hours_before_fill": missing_before,
        "missing_hours_after_fill": missing_after,
        "negative_price_hours": int((hourly < 0).sum()),
        "hours_above_500": int((hourly > 500).sum()),
        "min": float(hourly.min()),
        "max": float(hourly.max()),
        "first": str(hourly.index.min()),
        "last": str(hourly.index.max()),
    }
    return hourly.rename("price_eur_mwh")


# ---------------------------------------------------------------- Enedis
def clean_enedis(cfg: dict, paths: Paths, quality: dict) -> pd.DataFrame:
    src = cfg["sources"]["enedis"]
    files = sorted((paths.raw / "enedis").glob("page_*.json"))
    if not files:
        raise FileNotFoundError("No raw Enedis pages found - run the download step first")
    rows = []
    for f in files:
        rows.extend(json.loads(f.read_text(encoding="utf-8")))
    df = pd.DataFrame(rows)
    n_raw = len(df)

    df = df[df["plage_de_puissance_souscrite"] == src["power_range"]]
    df = df[df["profil"].isin(src["profiles"])]
    df = df.drop_duplicates(subset=["horodate", "profil"])
    df["ts_utc"] = pd.to_datetime(df["horodate"], utc=True)
    df["total_energie_soutiree_wh"] = pd.to_numeric(df["total_energie_soutiree_wh"], errors="coerce")
    df["nb_points_soutirage"] = pd.to_numeric(df["nb_points_soutirage"], errors="coerce")
    df = df.dropna(subset=["total_energie_soutiree_wh", "nb_points_soutirage"])
    df = df[df["nb_points_soutirage"] > 0]

    # every half-hour should contain every residential profile; drop incomplete half-hours
    per_ts = df.groupby("ts_utc").agg(
        n_profiles=("profil", "nunique"),
        energy_wh=("total_energie_soutiree_wh", "sum"),
        meters=("nb_points_soutirage", "sum"),
    )
    expected = int(per_ts["n_profiles"].mode().iloc[0])
    incomplete = int((per_ts["n_profiles"] < expected).sum())
    per_ts = per_ts[per_ts["n_profiles"] >= expected]

    # load of the AVERAGE household, kWh per half-hour
    per_ts["kwh_per_customer"] = per_ts["energy_wh"] / per_ts["meters"] / 1000.0

    # half-hours -> hours: keep only hours where both half-hours exist
    hourly = per_ts["kwh_per_customer"].resample("h").agg(["sum", "count"])
    incomplete_hours = int(((hourly["count"] > 0) & (hourly["count"] < 2)).sum())
    hourly = hourly[hourly["count"] == 2]["sum"].rename("load_kwh")
    meters = per_ts["meters"].resample("h").mean().reindex(hourly.index).rename("meters")

    # flag (do not delete) extreme values: > 6 robust std from the median of the same hour-of-day
    local_hour = hourly.index.tz_convert(TZ).hour
    med = hourly.groupby(local_hour).transform("median")
    mad = (hourly - med).abs().groupby(local_hour).transform("median") * 1.4826
    flagged = int(((hourly - med).abs() > 6 * mad).sum())

    quality["enedis"] = {
        "raw_rows": n_raw,
        "rows_kept": int(len(df)),
        "profiles_per_half_hour_expected": expected,
        "incomplete_half_hours_dropped": incomplete,
        "incomplete_hours_dropped": incomplete_hours,
        "hours": int(len(hourly)),
        "extreme_hours_flagged_not_removed": flagged,
        "avg_meters": float(meters.mean()),
        "first": str(hourly.index.min()),
        "last": str(hourly.index.max()),
    }
    return pd.concat([hourly, meters], axis=1)


# ---------------------------------------------------------------- temperature
def clean_temperature(cfg: dict, paths: Paths, quality: dict) -> pd.Series:
    cities = cfg["sources"]["temperature"]["cities"]
    series, weights = [], []
    for city, c in cities.items():
        f = paths.raw / "temperature" / f"open_meteo_{city}.json"
        d = json.loads(f.read_text(encoding="utf-8"))["daily"]
        s = pd.Series(d["temperature_2m_mean"], index=pd.to_datetime(d["time"]), dtype="float64", name=city)
        series.append(s)
        weights.append(c["weight"])
    t = pd.concat(series, axis=1)
    w = pd.Series(weights, index=t.columns)
    # safety check: every city must cover the same period, otherwise the 'France' average drifts
    last_dates = {c: str(t[c].last_valid_index().date()) for c in t.columns}
    latest = t.apply(lambda s: s.last_valid_index()).max()
    for c in t.columns:
        lag = (latest - t[c].last_valid_index()).days
        if lag > 5:
            print(f"  WARNING: temperature for {c} stops {lag} days before the other cities "
                  f"({last_dates[c]}). Delete data/raw/temperature/open_meteo_{c}.json and run again.")
            t.loc[t.index > t[c].last_valid_index(), :] = np.nan   # never average over an incomplete set
    # weighted mean that ignores a missing city on a given day
    temp = (t * w).sum(axis=1, min_count=1) / t.notna().mul(w).sum(axis=1)
    missing = int(temp.isna().sum())
    temp = temp.interpolate(limit=2)
    quality["temperature"] = {
        "days": int(len(temp)),
        "missing_days_before_fill": missing,
        "first": str(temp.index.min().date()),
        "last": str(temp.index.max().date()),
        "mean_c": float(temp.mean()),
        "min_c": float(temp.min()),
        "max_c": float(temp.max()),
        "last_date_per_city": last_dates,
    }
    temp.index.name = "date"
    return temp.rename("temp_c")


# ---------------------------------------------------------------- calendar
def add_calendar(df: pd.DataFrame) -> pd.DataFrame:
    """Add local-time calendar columns to a UTC-indexed hourly frame."""
    local = df.index.tz_convert(TZ)
    years = range(local.year.min() - 1, local.year.max() + 2)
    fr_holidays = holidays.France(years=years)
    dates = local.normalize().tz_localize(None)
    is_hol = np.array([d.date() in fr_holidays for d in dates])
    wd = local.dayofweek
    daytype = np.where(is_hol | (wd == 6), 2, np.where(wd == 5, 1, 0))  # 0 weekday, 1 Saturday, 2 Sunday/holiday
    out = df.copy()
    out["local_time"] = local.tz_localize(None)
    out["date"] = dates
    out["year"] = local.year
    out["month"] = local.month
    out["hour"] = local.hour
    out["daytype"] = daytype
    return out


def degree_days(temp: pd.Series, cfg: dict) -> pd.DataFrame:
    m = cfg["model"]
    return pd.DataFrame({
        "temp_c": temp,
        "hdd": (m["hdd_base_c"] - temp).clip(lower=0),
        "cdd": (temp - m["cdd_base_c"]).clip(lower=0),
    })


# ---------------------------------------------------------------- build tables
def build_tables(cfg: dict, paths: Paths):
    """Return (hourly, daily, price_history, quality, degree_days)."""
    print("[2/5] Cleaning data")
    quality: dict = {}
    prices = clean_prices(cfg, paths, quality)
    load = clean_enedis(cfg, paths, quality)
    temp = clean_temperature(cfg, paths, quality)

    # full price history (used for forward-proxy errors)
    price_hist = add_calendar(prices.to_frame())

    # analysis window = where load AND price exist
    start = pd.Timestamp(cfg["period"]["start"], tz=TZ).tz_convert("UTC")
    end = (pd.Timestamp(cfg["period"]["end"], tz=TZ) + pd.Timedelta(days=1)).tz_convert("UTC")
    hourly = load.join(prices, how="inner")
    hourly = hourly[(hourly.index >= start) & (hourly.index < end)].dropna(subset=["load_kwh", "price_eur_mwh"])
    hourly = add_calendar(hourly)

    dd = degree_days(temp, cfg)
    hourly = hourly.join(dd, on="date")

    daily = hourly.groupby("date").agg(
        load_kwh=("load_kwh", "sum"),
        hours=("load_kwh", "size"),
        price_mean=("price_eur_mwh", "mean"),
        daytype=("daytype", "first"),
        year=("year", "first"),
        month=("month", "first"),
    ).join(dd)
    # keep only days with all their hours (23 or 25 on DST days are fine)
    daily = daily[daily["hours"] >= 23]

    quality["analysis_window"] = {
        "hours": int(len(hourly)),
        "first": str(hourly["local_time"].min()),
        "last": str(hourly["local_time"].max()),
        "avg_household_mwh_per_year": float(hourly.groupby("year")["load_kwh"].sum().median() / 1000),
    }

    hourly.to_csv(paths.processed / "hourly_master.csv", index_label="ts_utc")
    daily.to_csv(paths.processed / "daily_master.csv")
    dd.to_csv(paths.processed / "temperature_daily.csv")
    (paths.reports / "data_quality.json").write_text(json.dumps(quality, indent=2), encoding="utf-8")
    return hourly, daily, price_hist, quality, dd


def load_processed(cfg: dict, paths: Paths):
    """Reload the cleaned tables written by build_tables (skips re-reading the raw Enedis pages)."""
    print("[2/5] Loading cleaned tables from data/processed")
    hourly = pd.read_csv(paths.processed / "hourly_master.csv", index_col=0)
    hourly.index = pd.to_datetime(hourly.index, utc=True)
    hourly["date"] = pd.to_datetime(hourly["date"])
    hourly["local_time"] = pd.to_datetime(hourly["local_time"])
    daily = pd.read_csv(paths.processed / "daily_master.csv", index_col=0, parse_dates=True)
    dd = pd.read_csv(paths.processed / "temperature_daily.csv", index_col=0, parse_dates=True)
    quality: dict = {}
    qf = paths.reports / "data_quality.json"
    if qf.exists():
        quality = json.loads(qf.read_text(encoding="utf-8"))
    prices = clean_prices(cfg, paths, quality)
    price_hist = add_calendar(prices.to_frame())
    quality.setdefault("enedis", {"avg_meters": float(hourly["meters"].mean())})
    quality["analysis_window"] = {
        "hours": int(len(hourly)),
        "first": str(hourly["local_time"].min()),
        "last": str(hourly["local_time"].max()),
        "avg_household_mwh_per_year": float(hourly.groupby("year")["load_kwh"].sum().median() / 1000),
    }
    qf.write_text(json.dumps(quality, indent=2), encoding="utf-8")
    return hourly, daily, price_hist, quality, dd
