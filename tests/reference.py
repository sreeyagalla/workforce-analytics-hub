"""Independent reference implementation of every governed metric, in pandas.

This deliberately does NOT use wfa.warehouse, wfa.metrics, or the SQL in
config/metrics.yml. It re-derives every flag from the raw CSV columns and
re-implements each definition by hand, so the tests compare two separate
implementations of the same written definitions on the same raw data.
Only the governed names/thresholds (agency display names, borough map, tenure
bands, min cell size) are read from config, because those are policy, not logic.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = yaml.safe_load((ROOT / "config" / "sources.yml").read_text(encoding="utf-8"))
GOV = yaml.safe_load((ROOT / "config" / "governance.yml").read_text(encoding="utf-8"))
K = GOV["privacy"]["min_cell_size"]
DIM_COL = {"agency": "agency", "location": "location", "title": "title", "tenure_band": "tenure_band",
           "pay_basis": "pay_basis"}
NULL = "<NULL>"


def load(raw_dir: Path) -> pd.DataFrame:
    raw = pd.concat([pd.read_csv(p, dtype=str, keep_default_na=False) for p in sorted(raw_dir.glob("payroll_fy*.csv"))],
                    ignore_index=True)
    num = lambda c: pd.to_numeric(raw[c].replace("", np.nan), errors="coerce")  # noqa: E731
    d = pd.DataFrame()
    d["fiscal_year"] = raw["fiscal_year"].astype(int)
    d["agency_code"] = raw["payroll_number"]
    d["agency"] = raw["payroll_number"].map({k: v["name"] for k, v in SRC["agencies"].items()})
    boro = raw["work_location_borough"].str.strip().str.upper()
    loc = GOV["locations"]
    d["location"] = np.where(boro == "", loc["unknown_label"], boro.map(loc["borough_map"]).fillna(loc["outside_label"]))
    d["title"] = raw["title_description"].str.strip().str.upper().replace("", np.nan)
    d["status"] = raw["leave_status_as_of_june_30"].str.strip().str.upper()
    d["pay_basis"] = raw["pay_basis"].str.strip()
    hours = num("regular_hours").fillna(0)
    start = pd.to_datetime(raw["agency_start_date"].str[:10], errors="coerce", format="%Y-%m-%d")
    fy_end = pd.to_datetime(dict(year=d["fiscal_year"], month=6, day=30))
    fy_start = pd.to_datetime(dict(year=d["fiscal_year"] - 1, month=7, day=1))
    valid = start.notna() & (start >= pd.Timestamp(GOV["data_quality"]["min_valid_start_date"])) & (start <= fy_end)
    d["tenure_years"] = np.where(valid, (fy_end - start).dt.days / 365.25, np.nan)
    band = pd.Series(np.nan, index=d.index, dtype=object)
    for label, lo, hi in GOV["tenure_bands"]:
        band[(d["tenure_years"] >= lo) & (d["tenure_years"] < hi)] = label
    d["tenure_band"] = band
    d["base_salary"] = num("base_salary")
    d["ot_hours"] = num("ot_hours").fillna(0)
    d["ot_paid"] = num("total_ot_paid").fillna(0)
    d["total_pay"] = num("regular_gross_paid").fillna(0) + d["ot_paid"] + num("total_other_pay").fillna(0)
    d["worked"] = hours > 0
    d["employed"] = d["status"].isin(["ACTIVE", "ON LEAVE", "ON SEPARATION LEAVE"])
    d["separation"] = (d["status"] == "CEASED") & (hours > 0)
    d["new_hire"] = valid & (start >= fy_start)
    return d


def _cell(g: pd.DataFrame, metric: str) -> dict:
    """numerator / denominator / value / population for one group of records."""
    if metric == "headcount":
        n = g["employed"].sum()
        return dict(numerator=n, denominator=np.nan, value=n, population=n)
    if metric == "separations":
        n = g["separation"].sum()
        return dict(numerator=n, denominator=np.nan, value=n, population=len(g))
    if metric == "new_hires":
        n = g["new_hire"].sum()
        return dict(numerator=n, denominator=np.nan, value=n, population=len(g))
    if metric == "new_hire_attrition_rate":
        num, den = (g["new_hire"] & g["separation"]).sum(), g["new_hire"].sum()
        return dict(numerator=num, denominator=den, value=num / den if den else np.nan, population=den)
    if metric == "avg_tenure_years":
        t = g.loc[g["employed"], "tenure_years"].dropna()
        return dict(numerator=np.nan, denominator=np.nan, value=t.mean() if len(t) else np.nan, population=len(t))
    if metric == "median_base_salary":
        s = g.loc[g["employed"] & (g["pay_basis"] == "per Annum"), "base_salary"].dropna()
        return dict(numerator=np.nan, denominator=np.nan, value=s.median() if len(s) else np.nan, population=len(s))
    if metric == "ot_hours_per_worker":
        w = g[g["worked"]]
        num = round(w["ot_hours"].sum(), 2)  # sums are exact to the cent, as in the warehouse
        return dict(numerator=num, denominator=len(w), value=num / len(w) if len(w) else np.nan, population=len(w))
    if metric == "ot_share_of_pay":
        w = g[g["worked"]]
        num, den = round(w["ot_paid"].sum(), 2), round(w["total_pay"].sum(), 2)
        return dict(numerator=num, denominator=den, value=num / den if den else np.nan, population=len(w))
    raise KeyError(metric)


def compute(d: pd.DataFrame, metric: str, group_by: str | None = None, filters: dict | None = None,
            fiscal_years: list[int] | None = None) -> pd.DataFrame:
    """Same output columns as wfa.metrics.compute(): fiscal_year, <group>, numerator, denominator, value,
    population, suppressed."""
    f = d
    for dim, vals in (filters or {}).items():
        col = "agency_code" if dim == "agency" else DIM_COL[dim]
        f = f[f[col].isin([str(v) for v in vals])]
    keys = ["fiscal_year"] + ([DIM_COL[group_by]] if group_by else [])
    if group_by:  # SQL groups NULLs together (IS NOT DISTINCT FROM); mirror that with a sentinel
        f = f.assign(**{DIM_COL[group_by]: f[DIM_COL[group_by]].astype(object).where(f[DIM_COL[group_by]].notna(), NULL)})
    rows = []
    if metric == "turnover_rate":
        hc, sep = {}, {}
        for key, g in f.groupby(keys):
            key = key if isinstance(key, tuple) else (key,)
            hc[key], sep[key] = g["employed"].sum(), g["separation"].sum()
        for key, s in sep.items():
            prior = (key[0] - 1,) + key[1:]
            avg = (hc[prior] + hc[key]) / 2 if prior in hc else np.nan
            rows.append(dict(zip(keys, key), numerator=s, denominator=avg,
                             value=s / avg if avg and not np.isnan(avg) else np.nan, population=avg))
    else:
        for key, g in f.groupby(keys):
            key = key if isinstance(key, tuple) else (key,)
            rows.append(dict(zip(keys, key), **_cell(g, metric)))
    out = pd.DataFrame(rows)
    if fiscal_years:
        out = out[out["fiscal_year"].isin(fiscal_years)]
    if group_by:
        out = out.rename(columns={DIM_COL[group_by]: group_by})
    out["suppressed"] = out["population"].notna() & (out["population"] < K)
    out.loc[out["suppressed"], ["value", "numerator", "denominator"]] = np.nan
    return out.reset_index(drop=True)
