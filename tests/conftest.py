from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from wfa import settings, warehouse  # noqa: E402

import reference  # noqa: E402

SAMPLE_RAW = ROOT / "data" / "sample"
FULL_RAW = ROOT / "data" / "raw"
FULL_WAREHOUSE = ROOT / "data" / "warehouse.duckdb"
BENCHMARK = ROOT / "data" / "benchmarks" / "bls_jolts_state_local_excl_education.csv"


@pytest.fixture(scope="session")
def sample_paths(tmp_path_factory) -> settings.DataPaths:
    tmp = tmp_path_factory.mktemp("sample")
    paths = settings.DataPaths(raw_dir=SAMPLE_RAW, warehouse=tmp / "sample.duckdb", benchmark_csv=BENCHMARK,
                               reports_dir=tmp / "reports", is_sample=True)
    warehouse.build(paths)
    return paths


@pytest.fixture(scope="session")
def sample_con(sample_paths):
    con = warehouse.connect(sample_paths.warehouse, read_only=True)
    yield con
    con.close()


@pytest.fixture(scope="session")
def sample_ref():
    return reference.load(SAMPLE_RAW)


@pytest.fixture(scope="session")
def full_con():
    if not FULL_WAREHOUSE.exists() or not list(FULL_RAW.glob("payroll_fy*.csv")):
        pytest.skip("full warehouse not built (run `wfa fetch` and `wfa build`)")
    con = warehouse.connect(FULL_WAREHOUSE, read_only=True)
    yield con
    con.close()


@pytest.fixture(scope="session")
def full_ref(full_con):
    return reference.load(FULL_RAW)
