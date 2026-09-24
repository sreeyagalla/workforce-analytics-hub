"""External benchmark: BLS JOLTS separations for state & local government.

BLS publishes JOLTS rates monthly. To compare against a July-June fiscal year,
this module sums the 12 monthly rates from July (FY-1) through June (FY), which
approximates an annual separations rate (separations over the year divided by
average employment). Only fiscal years with all 12 months present are kept.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import requests

from . import settings


def fetch_monthly(series_ids: list[str], start_year: int, end_year: int, api: str) -> pd.DataFrame:
    payload = {"seriesid": series_ids, "startyear": str(start_year), "endyear": str(end_year)}
    r = requests.post(api, json=payload, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "REQUEST_SUCCEEDED":
        raise RuntimeError(f"BLS API error: {body.get('message')}")
    rows = []
    for s in body["Results"]["series"]:
        for d in s["data"]:
            if not d["period"].startswith("M") or d["period"] == "M13":
                continue
            rows.append({"series_id": s["seriesID"], "year": int(d["year"]),
                         "month": int(d["period"][1:]), "rate_pct": float(d["value"])})
    return pd.DataFrame(rows)


def to_fiscal_year(monthly: pd.DataFrame) -> pd.DataFrame:
    m = monthly.copy()
    m["fiscal_year"] = m["year"] + (m["month"] >= 7).astype(int)
    g = m.groupby(["series_id", "fiscal_year"]).agg(months=("rate_pct", "size"), rate_pct=("rate_pct", "sum"))
    g = g[g["months"] == 12].reset_index()
    g["rate"] = (g["rate_pct"] / 100).round(4)
    return g[["series_id", "fiscal_year", "rate"]]


def fetch_benchmark(out_csv: Path | None = None) -> pd.DataFrame:
    cfg = settings.sources()["benchmark"]
    out_csv = out_csv or settings.get_paths().benchmark_csv
    names = {v: k for k, v in cfg["series"].items()}
    monthly = fetch_monthly(list(cfg["series"].values()), cfg["start_year"], cfg["end_year"], cfg["api"])
    fy = to_fiscal_year(monthly)
    fy["measure"] = fy["series_id"].map(names)
    fy["industry"] = cfg["industry"]
    out = fy[["fiscal_year", "measure", "rate", "series_id", "industry"]].sort_values(["measure", "fiscal_year"])
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_csv, index=False)
    return out
