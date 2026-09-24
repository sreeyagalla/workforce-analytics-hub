"""Workforce planning: a separations outlook per agency, with an honest backtest.

Projection for fiscal year T:
    projected separations(T) = end-of-year headcount(T-1) x turnover rate estimate
Two rate estimates are compared, plus a naive count baseline:
    last_rate      turnover rate in T-1
    trailing_mean  mean turnover rate over up to the 3 years before T
    naive_count    separations(T) = separations(T-1)   (no rate model)
The backtest re-runs each method for every past year where the inputs and the
actual outcome exist, using only data from before that year.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import metrics
from .metrics import MetricQuery

METHODS = ["last_rate", "trailing_mean", "naive_count"]


def _panel(con) -> pd.DataFrame:
    t = metrics.compute(con, MetricQuery("turnover_rate", group_by=["agency"])).df
    h = metrics.compute(con, MetricQuery("headcount", group_by=["agency"])).df
    p = t[["fiscal_year", "agency", "value", "numerator"]].rename(columns={"value": "turnover", "numerator": "separations"})
    return p.merge(h[["fiscal_year", "agency", "value"]].rename(columns={"value": "headcount"}),
                   on=["fiscal_year", "agency"], how="outer")


def _predict(panel: pd.DataFrame, target_fy: int, lookback: int = 3) -> pd.DataFrame:
    rows = []
    for agency, g in panel.groupby("agency"):
        g = g.set_index("fiscal_year")
        prior = target_fy - 1
        if prior not in g.index:
            continue
        hc_prior = g.at[prior, "headcount"]
        past_rates = g.loc[[y for y in range(target_fy - lookback, target_fy) if y in g.index], "turnover"].dropna()
        rows.append({
            "fiscal_year": target_fy, "agency": agency, "headcount_prior": hc_prior,
            "last_rate": hc_prior * g.at[prior, "turnover"] if not np.isnan(g.at[prior, "turnover"]) else np.nan,
            "trailing_mean": hc_prior * past_rates.mean() if len(past_rates) else np.nan,
            "naive_count": g.at[prior, "separations"],
            "rate_years_used": len(past_rates),
            "actual": g.at[target_fy, "separations"] if target_fy in g.index else np.nan,
        })
    return pd.DataFrame(rows)


def backtest(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (per-agency predictions for every backtest year, error summary by year and method)."""
    panel = _panel(con)
    years = sorted(panel["fiscal_year"].unique())
    preds = pd.concat([_predict(panel, y) for y in years[1:]], ignore_index=True)
    preds = preds.dropna(subset=["actual"] + METHODS)
    summary = []
    for fy, g in preds.groupby("fiscal_year"):
        for m in METHODS:
            err = g[m] - g["actual"]
            summary.append({"fiscal_year": int(fy), "method": m, "agencies": len(g),
                            "mae": float(err.abs().mean()),
                            "mape": float((err.abs() / g["actual"]).mean()),
                            "total_predicted": float(g[m].sum()), "total_actual": float(g["actual"].sum())})
    return preds, pd.DataFrame(summary)


def select_method(summary: pd.DataFrame) -> tuple[str, float]:
    """The method with the lowest mean MAPE across all backtest years."""
    mean = summary.groupby("method")["mape"].mean()
    return str(mean.idxmin()), float(mean.min())


def outlook(con, lookback: int = 3) -> pd.DataFrame:
    """Projection for the year after the latest loaded fiscal year, using the method the backtest selects.
    All three methods are kept as columns so the choice is visible."""
    panel = _panel(con)
    _, summary = backtest(con)
    method, _ = select_method(summary)
    nxt = int(panel["fiscal_year"].max()) + 1
    p = _predict(panel, nxt, lookback)
    p["method"] = method
    p["projected_separations"] = p[method]
    return p[["fiscal_year", "agency", "headcount_prior", "projected_separations", "method", *METHODS,
              "rate_years_used"]].sort_values("projected_separations", ascending=False, ignore_index=True)
