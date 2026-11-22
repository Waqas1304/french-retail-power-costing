"""Step 1 - Download the three public datasets into data/raw/.

* Day-ahead electricity prices, France  -> Energy-Charts API (Fraunhofer ISE), no key
                                           (or ENTSO-E Transparency if ENTSOE_API_KEY is set)
* Half-hourly residential consumption    -> Enedis Open Data, dataset "conso-inf36"
* Daily temperature for 8 French cities  -> Open-Meteo historical weather API, no key

Every download is cached: if a file is already on disk it is not downloaded again,
so the script can be stopped and restarted safely.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

from .config import Paths

HEADERS = {"User-Agent": "french-retail-power-costing (portfolio project)"}


def _get(url: str, params: dict | None = None, retries: int = 5, timeout: int = 120) -> requests.Response:
    """GET with polite retries (handles HTTP 429 'too many requests')."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        except requests.RequestException as exc:
            wait = 10 * attempt
            print(f"    network error ({exc.__class__.__name__}), retry in {wait}s")
            time.sleep(wait)
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 60))
            print(f"    rate limited, waiting {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(10 * attempt)
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"Failed after {retries} attempts: {url}")


# ---------------------------------------------------------------- prices
def download_prices(cfg: dict, paths: Paths) -> None:
    src = cfg["sources"]["prices"]
    start_year = int(cfg["period"]["price_history_start"][:4])
    end_year = int(cfg["period"]["end"][:4])
    out_dir = paths.raw / "prices"

    if src["provider"] == "entsoe":
        _download_prices_entsoe(src, start_year, end_year, out_dir)
        return

    for year in range(start_year, end_year + 1):
        f = out_dir / f"energy_charts_{src['bidding_zone']}_{year}.json"
        if f.exists():
            print(f"  prices {year}: cached")
            continue
        print(f"  prices {year}: downloading from Energy-Charts ...")
        r = _get(
            "https://api.energy-charts.info/price",
            params={"bzn": src["bidding_zone"], "start": f"{year}-01-01", "end": f"{year}-12-31"},
        )
        data = r.json()
        if not data.get("unix_seconds"):
            raise RuntimeError(f"No price data returned for {year}: {str(data)[:300]}")
        f.write_text(json.dumps(data), encoding="utf-8")
        time.sleep(src.get("sleep_seconds", 35))


def _download_prices_entsoe(src: dict, start_year: int, end_year: int, out_dir: Path) -> None:
    key = os.environ.get("ENTSOE_API_KEY")
    if not key:
        raise RuntimeError("provider=entsoe needs the ENTSOE_API_KEY environment variable")
    from entsoe import EntsoePandasClient  # optional dependency

    client = EntsoePandasClient(api_key=key)
    for year in range(start_year, end_year + 1):
        f = out_dir / f"entsoe_{src['bidding_zone']}_{year}.csv"
        if f.exists():
            continue
        s = client.query_day_ahead_prices(
            src["bidding_zone"],
            start=pd.Timestamp(f"{year}-01-01", tz="Europe/Paris"),
            end=pd.Timestamp(f"{year + 1}-01-01", tz="Europe/Paris"),
        )
        s.rename("price").to_frame().to_csv(f)


# ---------------------------------------------------------------- Enedis
def download_enedis(cfg: dict, paths: Paths) -> None:
    """Page through the Enedis Data Fair API and save each page as JSON.

    Only the national 'all power ranges' rows (P0) of residential profiles are requested,
    which keeps the download to roughly 70 pages of 10,000 rows.
    """
    src = cfg["sources"]["enedis"]
    out_dir = paths.raw / "enedis"
    state_file = out_dir / "_next_url.txt"
    done_file = out_dir / "_complete.txt"
    if done_file.exists():
        print("  enedis: cached")
        return

    fields = "horodate,profil,plage_de_puissance_souscrite,nb_points_soutirage,total_energie_soutiree_wh"
    first_params = {
        "size": src["page_size"],
        "select": fields,
        "qs": "plage_de_puissance_souscrite:P0* AND profil:RES*",
        "sort": "horodate",
    }
    pages = sorted(out_dir.glob("page_*.json"))
    page_no = len(pages)
    url = state_file.read_text().strip() if state_file.exists() and page_no else None

    while True:
        if url is None and page_no == 0:
            try:
                r = _get(src["base_url"], params=first_params)
                if not r.json().get("results"):
                    raise RuntimeError("empty result")
            except Exception:  # noqa: BLE001 - fall back to the simpler filter that is known to work
                print("  enedis: profile filter not accepted, downloading all P0 rows (cleaning keeps RES only)")
                first_params["qs"] = "plage_de_puissance_souscrite:P0*"
                r = _get(src["base_url"], params=first_params)
        elif url:
            r = _get(url)
        else:
            break
        payload = r.json()
        rows = payload.get("results", [])
        if not rows:
            break
        (out_dir / f"page_{page_no:04d}.json").write_text(json.dumps(rows), encoding="utf-8")
        page_no += 1
        total = payload.get("total", "?")
        print(f"  enedis: page {page_no} saved ({page_no * src['page_size']:,} / {total:,} rows)"
              if isinstance(total, int) else f"  enedis: page {page_no} saved")
        url = payload.get("next")
        if not url:
            break
        state_file.write_text(url)
        time.sleep(1)
    done_file.write_text("ok")


# ---------------------------------------------------------------- temperature
def download_temperature(cfg: dict, paths: Paths) -> None:
    src = cfg["sources"]["temperature"]
    out_dir = paths.raw / "temperature"
    for city, c in src["cities"].items():
        f = out_dir / f"open_meteo_{city}.json"
        if f.exists():
            print(f"  temperature {city}: cached")
            continue
        print(f"  temperature {city}: downloading ...")
        r = _get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": c["lat"],
                "longitude": c["lon"],
                "start_date": src["start"],
                "end_date": cfg["period"]["end"],
                "daily": "temperature_2m_mean",
                "timezone": "Europe/Paris",
            },
        )
        f.write_text(r.text, encoding="utf-8")
        time.sleep(5)


def download_all(cfg: dict, paths: Paths) -> None:
    print("[1/5] Downloading public data")
    download_temperature(cfg, paths)
    download_enedis(cfg, paths)
    download_prices(cfg, paths)
