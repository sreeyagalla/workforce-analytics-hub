"""Paths and config loading.

Two data modes:
  * full   - the complete fetched dataset in data/raw -> data/warehouse.duckdb
  * sample - a committed, seeded sample of real rows in data/sample
             -> data/sample_warehouse.duckdb (used by tests and quick demos)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


@lru_cache(maxsize=None)
def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def sources() -> dict:
    return load_yaml("sources.yml")


def governance() -> dict:
    return load_yaml("governance.yml")


def metric_catalog() -> dict:
    return load_yaml("metrics.yml")["metrics"]


def dimensions() -> dict:
    return load_yaml("metrics.yml")["dimensions"]


@dataclass(frozen=True)
class DataPaths:
    raw_dir: Path
    warehouse: Path
    benchmark_csv: Path
    reports_dir: Path
    is_sample: bool

    @property
    def manifest(self) -> Path:
        return self.raw_dir / "manifest.json"


def data_dir() -> Path:
    return Path(os.environ.get("WFA_DATA_DIR", PROJECT_ROOT / "data"))


def get_paths(sample: bool | None = None) -> DataPaths:
    """Resolve data paths. `sample=None` reads the WFA_SAMPLE env var."""
    if sample is None:
        sample = os.environ.get("WFA_SAMPLE", "0") == "1"
    d = data_dir()
    return DataPaths(
        raw_dir=d / ("sample" if sample else "raw"),
        warehouse=Path(os.environ.get(
            "WFA_WAREHOUSE", d / ("sample_warehouse.duckdb" if sample else "warehouse.duckdb"))),
        benchmark_csv=d / "benchmarks" / "bls_jolts_state_local_excl_education.csv",
        reports_dir=PROJECT_ROOT / "reports",
        is_sample=sample,
    )
