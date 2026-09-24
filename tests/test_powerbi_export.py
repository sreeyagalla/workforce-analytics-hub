"""Power BI export: the exported star schema + measure logic must reproduce the governed metric layer.

`evaluate()` below applies each measure's specification (wfa.powerbi.MEASURES, the same spec the
DAX is generated from) to the exported CSV files, for one fiscal year and a set of dimension filters.
Every single-dimension breakdown, for every metric and year, is compared with wfa.metrics.compute().
This checks the exported components and the measure logic. The DAX text itself is executed in
Power BI's engine by scripts/verify_powerbi.py (not part of pytest: it needs Power BI Desktop).
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
import pytest

from wfa import metrics, powerbi, settings
from wfa.metrics import MetricQuery

K = settings.governance()["privacy"]["min_cell_size"]


@pytest.fixture(scope="module")
def export(sample_con, tmp_path_factory):
    out = tmp_path_factory.mktemp("pbi")
    manifest = powerbi.export(sample_con, out, is_sample=True)
    tables = {name: pd.read_csv(out / "data" / f"{name}.csv", dtype={"agency_code": str}, keep_default_na=True)
              for name in manifest["tables"]}
    return out, manifest, tables


def evaluate(tables, key, fy, filters):
    """Python mirror of the generated DAX for one filter context. filters: {dim: value}."""
    spec = powerbi.MEASURES[key]
    if any(d in filters for d in powerbi.undefined_dimensions(key)):
        return np.nan
    fact = tables[powerbi.FACT]
    mask = pd.Series(True, index=fact.index)
    for dim, value in filters.items():
        mask &= fact[powerbi.DIMS[dim][2]] == value
    cur = fact[mask & (fact["fiscal_year"] == fy)]
    s = lambda col: cur[col].sum() if len(cur) else np.nan  # noqa: E731  (SUM over no rows is BLANK)
    if spec["kind"] == "count":
        pop = s(spec["pop"])
        return np.nan if np.isnan(pop) or pop < K else s(spec["num"])
    if spec["kind"] == "ratio":
        pop, den = s(spec["pop"]), s(spec["den"])
        return np.nan if np.isnan(pop) or pop < K or not den else s(spec["num"]) / den
    if spec["kind"] == "turnover":
        prior = fact[mask & (fact["fiscal_year"] == fy - 1)]
        if prior.empty:
            return np.nan
        avg = (prior["headcount"].sum() + (cur["headcount"].sum() if len(cur) else 0)) / 2
        return np.nan if avg < K else (cur["separations"].sum() if len(cur) else np.nan) / avg
    if spec["kind"] == "lookup":
        if "location" in filters or "tenure_band" in filters:
            return np.nan
        if "agency" in filters:
            t = tables["agg_median_salary"]
            row = t[(t["fiscal_year"] == fy) & (t["agency_code"] == filters["agency"])]
        else:
            t = tables["agg_median_salary_total"]
            row = t[t["fiscal_year"] == fy]
        return row["median_base_salary"].iloc[0] if len(row) else np.nan
    raise ValueError(key)


def governed(con, key, dim, fy):
    """{group value: governed value} for one breakdown and year, using export keys."""
    df = metrics.compute(con, MetricQuery(key, group_by=[dim] if dim else [], fiscal_years=[fy])).df
    if not dim:
        return {None: df["value"].iloc[0] if len(df) else np.nan}
    if dim == "agency":
        codes = dict(con.execute("SELECT agency_name, agency_code FROM dim_agency").fetchall())
        return {codes[g]: v for g, v in zip(df["agency"], df["value"])}
    if dim == "tenure_band":
        return {(powerbi.UNKNOWN_BAND if pd.isna(g) else g): v for g, v in zip(df[dim], df["value"])}
    return dict(zip(df[dim], df["value"]))


CASES = [(k, d) for k in powerbi.MEASURES for d in [None, "agency", "location", "pay_basis", "tenure_band"]]


@pytest.mark.parametrize("key,dim", CASES, ids=[f"{k}-{d or 'all'}" for k, d in CASES])
def test_measure_logic_matches_governed_layer(sample_con, export, key, dim):
    _, _, tables = export
    catalog = settings.metric_catalog()
    for fy in metrics.available_fiscal_years(sample_con):
        if dim and dim not in catalog[key]["dimensions"]:
            with pytest.raises(metrics.MetricError):
                governed(sample_con, key, dim, fy)
            values = tables[powerbi.DIMS[dim][0]][powerbi.DIMS[dim][1]]
            assert all(np.isnan(evaluate(tables, key, fy, {dim: v})) for v in values)
            continue
        expected = governed(sample_con, key, dim, fy)
        if dim and key in powerbi.PRECOMPUTED_DIMS and dim not in powerbi.PRECOMPUTED_DIMS[key]:
            # non-additive metric not exported at this breakdown: Power BI must show BLANK, not a wrong number
            assert all(np.isnan(evaluate(tables, key, fy, {dim: g})) for g in expected)
            continue
        for group, value in expected.items():
            got = evaluate(tables, key, fy, {dim: group} if dim else {})
            np.testing.assert_allclose(got, value, rtol=1e-9, equal_nan=True, err_msg=f"{key} {dim}={group} FY{fy}")


def test_fact_totals_reconcile_to_warehouse(sample_con, export):
    _, _, tables = export
    fact = tables[powerbi.FACT]
    direct = sample_con.execute("SELECT count(*), count(*) FILTER (WHERE is_separation) FROM fct_payroll").fetchone()
    assert (fact["records"].sum(), fact["separations"].sum()) == direct


def test_export_contains_no_pii_or_record_level_detail(export):
    _, _, tables = export
    pii = set(settings.governance()["privacy"]["pii_columns"])
    for name, df in tables.items():
        assert not pii & set(df.columns), name
    assert "title" not in tables[powerbi.FACT].columns
    grain = ["fiscal_year", "agency_code", "location", "pay_basis", "tenure_band"]
    assert not tables[powerbi.FACT].duplicated(grain).any()


def test_model_files_are_consistent(export):
    out, manifest, tables = export
    sm = out / f"{powerbi.PROJECT}.SemanticModel" / "definition"
    fact_cols = set(tables[powerbi.FACT].columns)
    tmdl = (sm / "tables" / f"{powerbi.MEASURE_TABLE}.tmdl").read_text(encoding="utf-8")
    fact_tmdl = (sm / "tables" / f"{powerbi.FACT}.tmdl").read_text(encoding="utf-8")
    assert "measure " not in fact_tmdl  # measures live in the Metrics table
    # names are case-insensitive: a measure may not share a name with a column in its own table
    metrics_columns = {c.strip("'").lower() for c in re.findall(r"^\tcolumn (\S+)", tmdl, flags=re.M)}
    for key in powerbi.MEASURES:
        assert powerbi.measure_name(key).lower() not in metrics_columns
    for col in re.findall(r"fact_workforce\[(\w+)\]", tmdl):
        assert col in fact_cols, col
    for key in powerbi.MEASURES:
        assert f"measure {powerbi._q(powerbi.measure_name(key))} =" in tmdl
    for name in manifest["tables"]:
        text = (sm / "tables" / f"{name}.tmdl").read_text(encoding="utf-8")
        declared = re.findall(r"^\tcolumn (\S+)", text, flags=re.M)
        assert [c.strip("'") for c in declared] == list(tables[name].columns), name
        assert "partition" in text and f'\\{name}.csv"' in text
    rels = (sm / "relationships.tmdl").read_text(encoding="utf-8")
    for frm, to in re.findall(r"fromColumn: (\w+)\.(\w+)\n\ttoColumn: \w+\.\w+", rels):
        assert to in tables[frm].columns
    for path in out.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))  # every JSON file parses
    assert "discourageImplicitMeasures" in (sm / "model.tmdl").read_text(encoding="utf-8")


def test_measures_file_lists_every_governed_metric(export):
    out, _, _ = export
    text = (out / "measures.dax").read_text(encoding="utf-8")
    for key in settings.metric_catalog():
        assert f"[{powerbi.measure_name(key)}] =" in text


def test_committed_dax_listing_matches_generator():
    """docs/powerbi/measures.dax is what reviewers read on GitHub; it must be the generator's current output."""
    from pathlib import Path
    committed = (Path(__file__).resolve().parents[1] / "docs" / "powerbi" / "measures.dax").read_text(encoding="utf-8")
    assert committed == powerbi.measures_dax_file(), "run `wfa export-powerbi` logic: regenerate docs/powerbi/measures.dax"
