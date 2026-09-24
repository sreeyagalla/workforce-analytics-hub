"""Create the committed sample: a seeded, stratified random sample of REAL rows.

For every (fiscal year, agency) cell, up to SAMPLE_PER_CELL raw rows are drawn
with a fixed seed. Rows are copied unchanged from the fetched files (no names
exist in them to begin with). Counts computed on the sample describe the sample,
not the real workforce; the app labels sample mode wherever it is used.
"""
from __future__ import annotations

import json

import pandas as pd

from . import settings

SAMPLE_PER_CELL = 250
SEED = 42


def make_sample(per_cell: int = SAMPLE_PER_CELL, seed: int = SEED) -> dict:
    full, sample = settings.get_paths(sample=False), settings.get_paths(sample=True)
    sample.raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"is_sample": True, "seed": seed, "per_cell": per_cell,
                "note": "Seeded stratified random sample of real rows from the fetched NYC payroll files.",
                "files": {}}
    for path in sorted(full.raw_dir.glob("payroll_fy*.csv")):
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        parts = [g.sample(n=min(per_cell, len(g)), random_state=seed)
                 for _, g in df.groupby("payroll_number", sort=True)]
        out = pd.concat(parts).sort_index()
        out.to_csv(sample.raw_dir / path.name, index=False)
        manifest["files"][path.name] = {"fiscal_year": int(out["fiscal_year"].iloc[0]),
                                        "source_row_count": len(out), "rows_in_full_file": len(df)}
    with open(sample.manifest, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest
