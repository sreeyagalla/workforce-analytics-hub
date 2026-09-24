"""Semantic layer: compute governed metrics from config/metrics.yml.

`compute()` is the only query path used by the dashboard, the report exports,
and the Q&A engine. Small-cell suppression is applied inside it, so no caller
can get an unsuppressed value for a small group.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd

from . import settings


class MetricError(ValueError):
    pass


@dataclass
class MetricQuery:
    metric: str
    group_by: list[str] = field(default_factory=list)
    filters: dict[str, list] = field(default_factory=dict)
    fiscal_years: list[int] | None = None


@dataclass
class MetricResult:
    query: MetricQuery
    metric: dict
    df: pd.DataFrame
    sql: str
    params: list
    min_cell_size: int

    @property
    def n_suppressed(self) -> int:
        return int(self.df["suppressed"].sum())


def filter_column(dim: str) -> str:
    """Filters on agency use the stable code, not the display name."""
    return "agency_code" if dim == "agency" else settings.dimensions()[dim]["column"]


def _where(filters: dict[str, list]) -> tuple[str, list]:
    dims = settings.dimensions()
    clauses, params = [], []
    for dim, values in filters.items():
        if dim not in dims:
            raise MetricError(f"Unknown filter dimension: {dim}")
        if not values:
            continue
        clauses.append(f"list_contains(?, {filter_column(dim)})")
        params.append([str(v) for v in values])
    return (" AND ".join(clauses) or "TRUE"), params


def build_sql(q: MetricQuery) -> tuple[str, list]:
    catalog, dims = settings.metric_catalog(), settings.dimensions()
    if q.metric not in catalog:
        raise MetricError(f"Unknown metric: {q.metric}")
    m = catalog[q.metric]
    for d in q.group_by:
        if d not in dims:
            raise MetricError(f"Unknown dimension: {d}")
        if d not in m["dimensions"]:
            raise MetricError(f"{m['label']} is not defined by {dims[d]['label'].lower()}.")
    cols = [dims[d]["column"] for d in q.group_by]
    sel = "".join(f", {c}" for c in cols)
    where, params = _where(q.filters)
    fy_clause = ""
    if q.fiscal_years:
        fy_clause = " AND list_contains(?, fiscal_year)"

    if m["type"] == "turnover":
        join = " AND ".join(["p.fiscal_year = c.fiscal_year - 1"] + [f"p.{c} IS NOT DISTINCT FROM c.{c}" for c in cols])
        csel = "".join(f", c.{c}" for c in cols)
        sql = f"""
            WITH g AS (
                SELECT fiscal_year{sel},
                       {m['numerator_sql']} AS separations,
                       {m['headcount_sql']} AS headcount
                FROM fct_payroll WHERE {where}
                GROUP BY ALL
            )
            SELECT c.fiscal_year{csel},
                   c.separations::DOUBLE AS numerator,
                   (p.headcount + c.headcount) / 2.0 AS denominator,
                   c.separations / nullif((p.headcount + c.headcount) / 2.0, 0) AS value,
                   (p.headcount + c.headcount) / 2.0 AS population
            FROM g c LEFT JOIN g p ON {join}
            WHERE TRUE{fy_clause.replace('fiscal_year', 'c.fiscal_year')}
            ORDER BY c.fiscal_year{csel}
        """
    else:
        num = m.get("numerator_sql", "NULL")
        den = m.get("denominator_sql", "NULL")
        if m["type"] == "count":
            value = num
        elif m["type"] == "ratio":
            value = f"({num}) / nullif(({den}), 0)"
        elif m["type"] == "aggregate":
            value = m["value_sql"]
        else:
            raise MetricError(f"Unsupported metric type {m['type']}")
        sql = f"""
            SELECT fiscal_year{sel},
                   ({num})::DOUBLE AS numerator,
                   ({den})::DOUBLE AS denominator,
                   ({value})::DOUBLE AS value,
                   ({m['population_sql']})::DOUBLE AS population
            FROM fct_payroll
            WHERE {where}{fy_clause}
            GROUP BY ALL
            ORDER BY fiscal_year{sel}
        """
    if q.fiscal_years:
        params = params + [[int(y) for y in q.fiscal_years]]
    return sql, params


def suppress(df: pd.DataFrame, min_cell_size: int) -> pd.DataFrame:
    """Blank out value/numerator/denominator where population < min_cell_size.

    Rows with no population (e.g. turnover with no prior year) are left as
    missing but are not flagged as suppressed.
    """
    df = df.copy()
    small = df["population"].notna() & (df["population"] < min_cell_size)
    df["suppressed"] = small
    df.loc[small, ["value", "numerator", "denominator"]] = np.nan
    return df


def compute(con: duckdb.DuckDBPyConnection, q: MetricQuery) -> MetricResult:
    sql, params = build_sql(q)
    df = con.execute(sql, params).df()
    k = int(settings.governance()["privacy"]["min_cell_size"])
    df = suppress(df, k)
    dims = settings.dimensions()
    df = df.rename(columns={dims[d]["column"]: d for d in q.group_by})
    return MetricResult(query=q, metric=settings.metric_catalog()[q.metric], df=df,
                        sql=sql, params=params, min_cell_size=k)


def available_fiscal_years(con: duckdb.DuckDBPyConnection) -> list[int]:
    return [r[0] for r in con.execute("SELECT DISTINCT fiscal_year FROM fct_payroll ORDER BY 1").fetchall()]


def dimension_values(con: duckdb.DuckDBPyConnection, dim: str) -> list[str]:
    col = settings.dimensions()[dim]["column"]
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT {col} FROM fct_payroll WHERE {col} IS NOT NULL ORDER BY 1").fetchall()]


# ---------------------------------------------------------------- formatting

def fmt(value, fmt_key: str) -> str:
    """Single formatter used everywhere text is produced, so tests can check it."""
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "n/a"
    if fmt_key == "pct":
        return f"{value * 100:.1f}%"
    if fmt_key == "count":
        return f"{value:,.0f}"
    if fmt_key == "usd":
        return f"${value:,.0f}"
    if fmt_key == "years":
        return f"{value:.1f} years"
    if fmt_key == "hours":
        return f"{value:,.1f} hours"
    return f"{value:,.2f}"


def fmt_delta(delta, fmt_key: str) -> str:
    if delta is None or (isinstance(delta, float) and np.isnan(delta)):
        return "n/a"
    sign = "+" if delta >= 0 else "-"
    if fmt_key == "pct":
        return f"{sign}{abs(delta) * 100:.1f} pts"
    return sign + fmt(abs(delta), fmt_key)
