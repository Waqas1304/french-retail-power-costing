"""Create SYNTHETIC raw files in the exact formats returned by the three APIs.

Used only by the tests, to prove the pipeline runs end to end without internet.
The numbers are made up and must never be presented as results.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

PROFILES = {"RES1 (+ RES1WE)": 0.55, "RES11 (+ RES11WE)": 0.05, "RES2 (+ RES5)": 0.33,
            "RES2WE": 0.02, "RES3": 0.02, "RES4": 0.03}
CITIES = ["Paris", "Lyon", "Marseille", "Toulouse", "Lille", "Bordeaux", "Nantes", "Strasbourg"]
LEVELS = {2016: 37, 2017: 45, 2018: 50, 2019: 39, 2020: 32, 2021: 109, 2022: 276,
          2023: 97, 2024: 58, 2025: 62}   # illustrative annual levels only


def _unix(idx) -> list:
    return ((idx - pd.Timestamp('1970-01-01', tz='UTC')) // pd.Timedelta('1s')).tolist()


def _temps(start="1991-01-01", end="2025-12-31", seed=1) -> pd.Series:
    rng = np.random.default_rng(seed)
    d = pd.date_range(start, end, freq="D")
    doy = d.dayofyear.to_numpy()
    base = 12.5 - 7.5 * np.cos(2 * np.pi * (doy - 15) / 365.25)
    noise = np.zeros(len(d))
    for i in range(1, len(d)):
        noise[i] = 0.8 * noise[i - 1] + rng.normal(0, 1.6)
    return pd.Series(base + noise, index=d)


def make(raw: Path, load_start="2020-04-01", end="2025-12-31", page_size=20000, seed=7):
    rng = np.random.default_rng(seed)
    (raw / "prices").mkdir(parents=True, exist_ok=True)
    (raw / "enedis").mkdir(parents=True, exist_ok=True)
    (raw / "temperature").mkdir(parents=True, exist_ok=True)
    end_year = int(end[:4])

    temp = _temps(end=end)
    for i, c in enumerate(CITIES):
        t = temp + (i - 3) * 0.4
        t.iloc[5 + i] = np.nan  # a few missing days to exercise the cleaning
        payload = {"latitude": 0, "longitude": 0, "daily_units": {"temperature_2m_mean": "°C"},
                   "daily": {"time": [x.strftime("%Y-%m-%d") for x in t.index],
                             "temperature_2m_mean": [None if np.isnan(v) else round(float(v), 1) for v in t]}}
        (raw / "temperature" / f"open_meteo_{c}.json").write_text(json.dumps(payload))

    # prices 2016..end, local-time driven, 15-min from Oct 2025
    for year in range(2016, end_year + 1):
        idx = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", tz="Europe/Paris", inclusive="left")
        loc_date = idx.normalize().tz_localize(None)
        tday = temp.reindex(loc_date).to_numpy()
        hdd = np.clip(15 - tday, 0, None)
        hour = idx.hour.to_numpy()
        shape = 0.12 * np.sin((hour - 6) / 24 * 2 * np.pi) + 0.15 * ((hour >= 18) & (hour <= 20)) - 0.1 * ((hour >= 12) & (hour <= 15))
        lvl = LEVELS.get(year, 60)
        p = lvl * (0.85 + 0.03 * hdd + shape) + rng.normal(0, 0.12 * lvl, len(idx))
        utc = idx.tz_convert("UTC")
        secs = _unix(utc)
        prices = np.round(p, 2).tolist()
        if year == 2025:   # switch to 15-min resolution in October, like the real market
            s = pd.Series(prices, index=utc)
            pre = s[s.index < pd.Timestamp("2025-09-30T22:00Z")]
            post = s[s.index >= pd.Timestamp("2025-09-30T22:00Z")].resample("15min").ffill()
            s = pd.concat([pre, post])
            secs, prices = _unix(s.index), s.round(2).tolist()
        payload = {"license_info": "synthetic test data", "unix_seconds": secs, "price": prices,
                   "unit": "EUR / MWh", "deprecated": False}
        (raw / "prices" / f"energy_charts_FR_{year}.json").write_text(json.dumps(payload))

    # Enedis half-hourly rows
    idx = pd.date_range(load_start, pd.Timestamp(end) + pd.Timedelta(days=1), freq="30min",
                        tz="Europe/Paris", inclusive="left")
    loc_date = idx.normalize().tz_localize(None)
    tday = temp.reindex(loc_date).ffill().to_numpy()
    hdd = np.clip(15 - tday, 0, None)
    hr = (idx.hour + idx.minute / 60).to_numpy()
    intraday = 1 + 0.25 * np.exp(-((hr - 19.5) ** 2) / 4) + 0.12 * np.exp(-((hr - 8) ** 2) / 3) - 0.25 * ((hr > 1) & (hr < 6))
    wh_per_meter = (150 + 14 * hdd) * intraday * (1 + rng.normal(0, 0.03, len(idx)))
    meters_total = 34_000_000
    rows = []
    stamps = [t.isoformat() for t in idx]
    for name, share in PROFILES.items():
        meters = int(meters_total * share)
        factor = 1.6 if name in ("RES11 (+ RES11WE)", "RES4") else 1.0
        e = wh_per_meter * factor * meters
        for s, v in zip(stamps, e):
            rows.append({"horodate": s, "profil": name, "plage_de_puissance_souscrite": "P0: Total <= 36 kVA",
                         "nb_points_soutirage": meters, "total_energie_soutiree_wh": float(round(v))})
    # noise the cleaner must remove: a non-total power range row and a duplicate
    rows.append({**rows[0], "plage_de_puissance_souscrite": "P2: ]3-6] kVA"})
    rows.append(dict(rows[1]))
    rows.sort(key=lambda r: r["horodate"])
    for i in range(0, len(rows), page_size):
        (raw / "enedis" / f"page_{i // page_size:04d}.json").write_text(json.dumps(rows[i:i + page_size]))
    (raw / "enedis" / "_complete.txt").write_text("ok")
