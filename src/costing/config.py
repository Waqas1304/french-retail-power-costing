"""Load config.yaml and build the folder paths used everywhere else."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Paths:
    root: Path
    raw: Path
    interim: Path
    processed: Path
    reports: Path
    figures: Path
    tables: Path

    def make(self) -> "Paths":
        for p in (self.raw, self.interim, self.processed, self.reports, self.figures, self.tables):
            p.mkdir(parents=True, exist_ok=True)
        for sub in ("prices", "enedis", "temperature"):
            (self.raw / sub).mkdir(parents=True, exist_ok=True)
        return self


def load_config(path: str | Path = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_paths(cfg: dict, base: str | Path = ".") -> Paths:
    base = Path(base)
    data = base / cfg.get("data_dir", "data")
    reports = base / cfg.get("reports_dir", "reports")
    return Paths(
        root=base,
        raw=data / "raw",
        interim=data / "interim",
        processed=data / "processed",
        reports=reports,
        figures=reports / "figures",
        tables=reports / "tables",
    ).make()
