"""Scorecards: one row per unit (agency or location), governed metrics as columns,
year-over-year change, and a RAG status against the all-in-scope value.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import metrics, settings
from .metrics import MetricQuery

ALL_LABEL = "All in scope"


def rag_status(value, baseline, polarity: str, amber: float, red: float) -> str:
    if polarity not in ("higher_is_worse", "higher_is_better"):
        return ""
    if value is None or baseline is None or np.isnan(value) or np.isnan(baseline) or baseline == 0:
        return ""
    if polarity == "higher_is_better":
        if value == 0:
            return "red"
        ratio = baseline / value
    else:
        ratio = value / baseline
    if ratio >= red:
        return "red"
    if ratio >= amber:
        return "amber"
    return "green"


@dataclass
class Scorecard:
    fiscal_year: int
    dimension: str
    metric_keys: list[str]
    table: pd.DataFrame          # one row per unit + an ALL_LABEL row
    baseline: dict[str, float]   # all-in-scope value per metric
    n_suppressed: int


def build(con, fiscal_year: int, dimension: str = "agency", filters: dict | None = None) -> Scorecard:
    gov = settings.governance()["scorecard"]
    catalog = settings.metric_catalog()
    keys = [k for k in gov["metrics"] if dimension in catalog[k]["dimensions"]]
    years = [fiscal_year - 1, fiscal_year]
    filters = filters or {}
    table, baseline, n_supp = None, {}, 0
    for key in keys:
        m = catalog[key]
        by = metrics.compute(con, MetricQuery(key, group_by=[dimension], filters=filters, fiscal_years=years)).df
        tot = metrics.compute(con, MetricQuery(key, filters=filters, fiscal_years=years)).df
        tot[dimension] = ALL_LABEL
        both = pd.concat([by, tot], ignore_index=True)
        cur = both[both.fiscal_year == fiscal_year].set_index(dimension)
        prev = both[both.fiscal_year == fiscal_year - 1].set_index(dimension)
        n_supp += int(cur["suppressed"].sum())
        base = float(cur.loc[ALL_LABEL, "value"]) if ALL_LABEL in cur.index else np.nan
        baseline[key] = base
        col = pd.DataFrame(index=cur.index)
        col[key] = cur["value"]
        if m["format"] == "pct":
            col[f"{key}__delta"] = cur["value"] - prev["value"].reindex(cur.index)
        else:
            col[f"{key}__delta"] = cur["value"] / prev["value"].reindex(cur.index) - 1
        col[f"{key}__rag"] = [rag_status(v, base, m["polarity"], gov["amber_ratio"], gov["red_ratio"])
                              if u != ALL_LABEL else "" for u, v in zip(cur.index, cur["value"])]
        table = col if table is None else table.join(col, how="outer")
    table = table.reset_index().rename(columns={"index": dimension})
    sort_key = "headcount" if "headcount" in table else keys[0]
    body = table[table[dimension] != ALL_LABEL].sort_values(sort_key, ascending=False, na_position="last")
    table = pd.concat([body, table[table[dimension] == ALL_LABEL]], ignore_index=True)
    return Scorecard(fiscal_year, dimension, keys, table, baseline, n_supp)


def watch_list(card: Scorecard) -> list[dict]:
    """Red cells, most severe first: the scorecard's 'where to look' list."""
    catalog = settings.metric_catalog()
    items = []
    for _, row in card.table.iterrows():
        if row[card.dimension] == ALL_LABEL:
            continue
        for key in card.metric_keys:
            if row.get(f"{key}__rag") == "red":
                base = card.baseline[key]
                items.append({"unit": row[card.dimension], "metric": key, "label": catalog[key]["label"],
                              "value": row[key], "baseline": base, "ratio": row[key] / base if base else np.nan})
    return sorted(items, key=lambda x: -x["ratio"])
