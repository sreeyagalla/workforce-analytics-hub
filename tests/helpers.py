"""Build tiny hand-written raw datasets for definition-level tests."""
from __future__ import annotations

import csv
import copy
import json
from pathlib import Path

from wfa import settings, warehouse

COLUMNS = settings.sources()["payroll"]["columns"]


def row(fy, code, name, start, boro, title, status, salary, basis, hours, gross, ot_h, ot_paid, other):
    return dict(zip(COLUMNS, [fy, code, name, start, boro, title, status, salary, basis, hours, gross, ot_h, ot_paid,
                              other]))


def write_raw(raw_dir: Path, rows: list[dict], manifest_counts: dict | None = None, extra_columns: dict | None = None):
    raw_dir.mkdir(parents=True, exist_ok=True)
    by_fy: dict[str, list[dict]] = {}
    for r in rows:
        by_fy.setdefault(str(r["fiscal_year"]), []).append(r)
    files = {}
    for fy, rs in by_fy.items():
        name = f"payroll_fy{fy}.csv"
        cols = COLUMNS + list(extra_columns or {})
        with open(raw_dir / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rs:
                w.writerow({**r, **(extra_columns or {})})
        files[name] = {"fiscal_year": int(fy), "source_row_count": (manifest_counts or {}).get(name, len(rs))}
    (raw_dir / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")


def build(tmp_path: Path, rows: list[dict], **kw):
    raw = tmp_path / "raw"
    write_raw(raw, rows, **kw)
    paths = settings.DataPaths(raw_dir=raw, warehouse=tmp_path / "w.duckdb", benchmark_csv=tmp_path / "none.csv",
                               reports_dir=tmp_path / "reports", is_sample=False)
    warehouse.build(paths)
    return warehouse.connect(paths.warehouse, read_only=True), paths


def governance_with(monkeypatch, **privacy):
    gov = copy.deepcopy(settings.load_yaml("governance.yml"))
    gov["privacy"].update(privacy)
    monkeypatch.setattr(settings, "governance", lambda: gov)
    return gov
