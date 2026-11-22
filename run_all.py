"""Run the whole project end to end.

    python run_all.py                  # download (cached) + clean + analyse + report
    python run_all.py --skip-download  # reuse data/raw/ as it is
    python run_all.py --from-processed # reuse the cleaned CSVs in data/processed/
    python run_all.py --config other.yaml --base some/folder
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from costing import plots  # noqa: E402
from costing.backtest import run_backtest, run_scenarios  # noqa: E402
from costing.clean import build_tables, load_processed  # noqa: E402
from costing.config import build_paths, load_config  # noqa: E402
from costing.cost_stack import annual_cost_stack, average_daily_profiles, monthly_view  # noqa: E402
from costing.download import download_all  # noqa: E402
from costing.report import write_summary  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--base", default=".", help="folder that holds data/ and reports/")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--from-processed", action="store_true",
                    help="reuse data/processed/*.csv instead of re-cleaning the raw files")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    paths = build_paths(cfg, args.base)

    if not (args.skip_download or args.from_processed):
        download_all(cfg, paths)

    if args.from_processed:
        hourly, daily, price_hist, quality, dd = load_processed(cfg, paths)
    else:
        hourly, daily, price_hist, quality, dd = build_tables(cfg, paths)

    print("[3/5] Ex-post cost stack")
    stack = annual_cost_stack(hourly, cfg)
    monthly = monthly_view(hourly)
    profiles = average_daily_profiles(hourly)
    stack.to_csv(paths.tables / "cost_stack_by_year.csv")
    monthly.to_csv(paths.tables / "monthly_view.csv")
    profiles.to_csv(paths.tables / "daily_profiles.csv", index=False)

    print("[4/5] Fixed-price contract: backtest + scenarios")
    bt, bt_results = run_backtest(hourly, daily, price_hist, dd, cfg)
    if len(bt):
        bt.to_csv(paths.tables / "backtest.csv")
    sc, _ = run_scenarios(hourly, daily, price_hist, dd, cfg)
    sc.to_csv(paths.tables / "scenarios.csv")

    print("[5/5] Charts and summary")
    f = paths.figures
    plots.daily_profiles(profiles, f / "01_daily_profiles.png")
    plots.cost_stack(stack, f / "02_cost_stack.png")
    plots.load_vs_temperature(daily, f / "03_load_vs_temperature.png")
    plots.monthly_price_volume(monthly, f / "04_monthly_price_volume.png")
    if len(bt):
        plots.backtest_chart(bt, f / "05_backtest.png")
        last = bt.index.max()
        plots.simulation_hist(bt_results[last], bt.loc[last, "realised_unit_cost"], f / "06_simulation_last_year.png")
    plots.scenarios_chart(sc, cfg["scenarios"]["target_year"], f / "07_scenarios.png")
    write_summary(paths.reports / "results_summary.md", quality, stack, bt, sc, cfg)
    print(f"Done. Open {paths.reports / 'results_summary.md'}")


if __name__ == "__main__":
    main()
