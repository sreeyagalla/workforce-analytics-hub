"""Power BI-ready export: a star schema (CSV) plus a Power BI Project (PBIP) whose
semantic model (TMDL) carries the governed metric catalog as DAX measures.

Why an aggregated fact with additive components:
  Ratios such as turnover must be recomputed at whatever level a visual slices
  them, so the fact stores counts and sums (never ratios) at the grain
  fiscal_year x agency x location x pay_basis x tenure_band. The component
  definitions are the SQL fragments from config/metrics.yml, so the export and
  the governed metric layer cannot drift apart.

Governance carried into the model:
  * every measure returns BLANK for groups below the minimum cell size;
  * every measure is defined for exactly one fiscal year (BLANK otherwise);
  * breakdowns the catalog does not define (e.g. turnover by tenure band) return BLANK;
  * implicit measures are discouraged, so report authors use the governed measures;
  * component columns are hidden;
  * median salary is not additive, so it is exported pre-computed (and already
    suppressed) by the metric layer at agency x year and all-agency x year.

The fact keeps small cells so that rollups are exact; like the warehouse, the
export folder is analyst-tier data and is not committed to git.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import metrics, settings
from .metrics import MetricQuery

PROJECT = "WorkforceAnalytics"
FACT = "fact_workforce"
MEASURE_TABLE = "Metrics"  # measures live apart from data tables: model names are case-insensitive
DIMS = {  # model dimension -> (dimension table, key column, fact column)
    "fiscal_year": ("dim_fiscal_year", "fiscal_year", "fiscal_year"),
    "agency": ("dim_agency", "agency_code", "agency_code"),
    "location": ("dim_location", "location", "location"),
    "pay_basis": ("dim_pay_basis", "pay_basis", "pay_basis"),
    "tenure_band": ("dim_tenure_band", "tenure_band", "tenure_band"),
}
UNKNOWN_BAND = "Unknown"

# Fact component columns, defined by reference to the catalog's own SQL so there is one definition.
COMPONENTS = {
    "records": ("separations", "population_sql"),
    "headcount": ("headcount", "numerator_sql"),
    "separations": ("separations", "numerator_sql"),
    "new_hires": ("new_hires", "numerator_sql"),
    "new_hire_separations": ("new_hire_attrition_rate", "numerator_sql"),
    "workers": ("ot_hours_per_worker", "denominator_sql"),
    "ot_hours": ("ot_hours_per_worker", "numerator_sql"),
    "ot_pay": ("ot_share_of_pay", "numerator_sql"),
    "total_pay": ("ot_share_of_pay", "denominator_sql"),
    "tenure_count": ("avg_tenure_years", "population_sql"),
}
EXTRA_COMPONENTS = {"tenure_years_sum": "sum(tenure_years) FILTER (WHERE is_employed_eoy)"}
DECIMAL_COMPONENTS = {"ot_hours", "ot_pay", "total_pay"}
DOUBLE_COMPONENTS = {"tenure_years_sum"}

# How each governed metric is computed from the fact components.
MEASURES = {
    "headcount": {"kind": "count", "num": "headcount", "pop": "headcount"},
    "separations": {"kind": "count", "num": "separations", "pop": "records"},
    "turnover_rate": {"kind": "turnover"},
    "new_hires": {"kind": "count", "num": "new_hires", "pop": "records"},
    "new_hire_attrition_rate": {"kind": "ratio", "num": "new_hire_separations", "den": "new_hires", "pop": "new_hires"},
    "avg_tenure_years": {"kind": "ratio", "num": "tenure_years_sum", "den": "tenure_count", "pop": "tenure_count"},
    "median_base_salary": {"kind": "lookup"},
    "ot_hours_per_worker": {"kind": "ratio", "num": "ot_hours", "den": "workers", "pop": "workers"},
    "ot_share_of_pay": {"kind": "ratio", "num": "ot_pay", "den": "total_pay", "pop": "workers"},
}
# Non-additive metrics are exported pre-computed only for these breakdowns; the measure is BLANK otherwise.
PRECOMPUTED_DIMS = {"median_base_salary": {"agency"}}
FORMAT = {"pct": "0.0%", "count": "#,##0", "hours": "#,##0.0", "years": "0.0", "usd": "\\$#,##0"}


def measure_name(key: str) -> str:
    return settings.metric_catalog()[key]["label"]


def undefined_dimensions(key: str) -> list[str]:
    """Model dimensions (other than fiscal year) this metric is not defined for."""
    allowed = settings.metric_catalog()[key]["dimensions"]
    return [d for d in DIMS if d != "fiscal_year" and d not in allowed]


# ------------------------------------------------------------------ data

def fact_sql() -> str:
    catalog = settings.metric_catalog()
    exprs = [(catalog[m][field], col) for col, (m, field) in COMPONENTS.items()]
    exprs += [(expr, col) for col, expr in EXTRA_COMPONENTS.items()]
    coalesced = ",\n               ".join(f"coalesce({expr}, 0) AS {col}" for expr, col in exprs)
    return f"""
        SELECT fiscal_year, agency_code, location, pay_basis,
               coalesce(tenure_band, '{UNKNOWN_BAND}') AS tenure_band,
               {coalesced}
        FROM fct_payroll
        GROUP BY ALL
        ORDER BY ALL
    """


def build_tables(con) -> dict[str, pd.DataFrame]:
    gov = settings.governance()
    fact = con.execute(fact_sql()).df()
    for c in DECIMAL_COMPONENTS:
        fact[c] = fact[c].astype(float).round(2)
    years = sorted(int(y) for y in fact["fiscal_year"].unique())
    dim_fy = pd.DataFrame({
        "fiscal_year": years, "fiscal_year_label": [f"FY{y}" for y in years],
        "start_date": [f"{y - 1}-07-01" for y in years], "end_date": [f"{y}-06-30" for y in years]})
    dim_agency = con.execute("SELECT agency_code, agency_name, source_name_history FROM dim_agency ORDER BY agency_name").df()
    boroughs = list(gov["locations"]["borough_map"].values())
    locs = sorted(fact["location"].unique(), key=lambda x: (boroughs.index(x) if x in boroughs else 99, x))
    dim_location = pd.DataFrame({
        "location": locs,
        "location_type": ["NYC borough" if x in boroughs else x for x in locs],
        "location_sort": range(1, len(locs) + 1)})
    pay_type = {"per Annum": "Salaried", "per Hour": "Hourly", "per Day": "Daily", "Prorated Annual": "Prorated annual"}
    pbs = sorted(fact["pay_basis"].unique())
    dim_pay = pd.DataFrame({"pay_basis": pbs, "pay_type": [pay_type.get(p, p) for p in pbs]})
    bands = [b[0] for b in gov["tenure_bands"]] + [UNKNOWN_BAND]
    dim_band = pd.DataFrame({"tenure_band": bands, "tenure_sort": range(1, len(bands) + 1)})

    by_agency = metrics.compute(con, MetricQuery("median_base_salary", group_by=["agency"])).df
    name_to_code = dict(zip(dim_agency["agency_name"], dim_agency["agency_code"]))
    med = pd.DataFrame({"fiscal_year": by_agency["fiscal_year"].astype(int),
                        "agency_code": by_agency["agency"].map(name_to_code),
                        "median_base_salary": by_agency["value"],
                        "salaried_headcount": by_agency["population"].astype(int)})
    tot = metrics.compute(con, MetricQuery("median_base_salary")).df
    med_total = pd.DataFrame({"fiscal_year": tot["fiscal_year"].astype(int), "median_base_salary": tot["value"],
                              "salaried_headcount": tot["population"].astype(int)})
    return {FACT: fact, "dim_fiscal_year": dim_fy, "dim_agency": dim_agency, "dim_location": dim_location,
            "dim_pay_basis": dim_pay, "dim_tenure_band": dim_band, "agg_median_salary": med,
            "agg_median_salary_total": med_total}


# ------------------------------------------------------------------ DAX

def dax_measure(key: str) -> str:
    spec = MEASURES[key]
    k = int(settings.governance()["privacy"]["min_cell_size"])
    undefined = [f"ISCROSSFILTERED ( {DIMS[d][0]} ) || ISFILTERED ( {FACT}[{DIMS[d][2]}] )"
                 for d in undefined_dimensions(key)]
    guard = " || ".join(["NOT HASONEVALUE ( dim_fiscal_year[fiscal_year] )"] + undefined)
    s = lambda col: f"SUM ( {FACT}[{col}] )"  # noqa: E731
    if spec["kind"] == "count":
        body = (f"VAR n = {s(spec['num'])}\n"
                f"VAR population = {s(spec['pop'])}\n"
                f"RETURN\n    IF ( {guard} || population < {k}, BLANK (), n )")
    elif spec["kind"] == "ratio":
        # Fixed-decimal (currency) sums keep cents exact, but fixed-decimal / integer stays fixed-decimal
        # (4 dp) in the engine, so the numerator is converted to DOUBLE before dividing.
        num = s(spec["num"])
        if spec["num"] in DECIMAL_COMPONENTS:
            num = f"CONVERT ( {num}, DOUBLE )"
        body = (f"VAR num = {num}\n"
                f"VAR den = {s(spec['den'])}\n"
                f"VAR population = {s(spec['pop'])}\n"
                f"RETURN\n    IF ( {guard} || population < {k}, BLANK (), DIVIDE ( num, den ) )")
    elif spec["kind"] == "turnover":
        body = ("VAR fy = SELECTEDVALUE ( dim_fiscal_year[fiscal_year] )\n"
                f"VAR separations = {s('separations')}\n"
                f"VAR hc_current = {s('headcount')}\n"
                f"VAR hc_prior =\n    CALCULATE (\n        {s('headcount')},\n"
                f"        REMOVEFILTERS ( dim_fiscal_year ),\n        REMOVEFILTERS ( {FACT}[fiscal_year] ),\n"
                "        dim_fiscal_year[fiscal_year] = fy - 1\n    )\n"
                "VAR avg_headcount = IF ( ISBLANK ( hc_prior ), BLANK (), ( hc_prior + hc_current ) / 2 )\n"
                f"RETURN\n    IF ( {guard} || ISBLANK ( avg_headcount ) || avg_headcount < {k}, BLANK (),\n"
                "        DIVIDE ( separations, avg_headcount ) )")
    elif spec["kind"] == "lookup":
        body = ("VAR by_agency = SELECTEDVALUE ( agg_median_salary[median_base_salary] )\n"
                "VAR all_agencies = SELECTEDVALUE ( agg_median_salary_total[median_base_salary] )\n"
                "RETURN\n"
                f"    IF ( {guard} || ISCROSSFILTERED ( dim_location ) || ISCROSSFILTERED ( dim_tenure_band ), BLANK (),\n"
                "        IF ( ISCROSSFILTERED ( dim_agency ),\n"
                "            IF ( HASONEVALUE ( dim_agency[agency_code] ), by_agency, BLANK () ),\n"
                "            all_agencies ) )")
    else:
        raise ValueError(spec["kind"])
    return body


def measures_dax_file() -> str:
    cat = settings.metric_catalog()
    out = ["// Governed measures generated from config/metrics.yml and config/governance.yml by `wfa export-powerbi`.",
           "// Each returns BLANK for: more than one fiscal year in context, groups below the minimum cell size,",
           "// and breakdowns the catalog does not define for that metric.", ""]
    for key in MEASURES:
        out += [f"// {cat[key]['definition']}", f"[{measure_name(key)}] =", dax_measure(key), ""]
    return "\n".join(out)


# ------------------------------------------------------------------ TMDL / PBIP

def _q(name: str) -> str:
    return f"'{name}'" if any(c in name for c in " .=:'()&-/") else name


def _indent(text: str, tabs: int) -> str:
    return "\n".join(("\t" * tabs + line) if line.strip() else "" for line in text.splitlines())


def _tmdl_type(df: pd.DataFrame, col: str) -> str:
    if col in DECIMAL_COMPONENTS:
        return "decimal"
    if col in DOUBLE_COMPONENTS or col == "median_base_salary":
        return "double"
    if pd.api.types.is_integer_dtype(df[col]) or col in ("fiscal_year", "location_sort", "tenure_sort",
                                                         "salaried_headcount"):
        return "int64"
    if pd.api.types.is_float_dtype(df[col]):
        return "double"
    return "string"


_M_TYPE = {"int64": "Int64.Type", "double": "type number", "decimal": "Currency.Type", "string": "type text"}


def _m_source(table: str, df: pd.DataFrame) -> str:
    types = ", ".join(f'{{"{c}", {_M_TYPE[_tmdl_type(df, c)]}}}' for c in df.columns)
    nullable = [c for c in df.columns if _tmdl_type(df, c) != "string" and df[c].isna().any()]
    steps = [f'Source = Csv.Document(File.Contents(DataFolder & "\\{table}.csv"), '
             '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),',
             "Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true]),"]
    last = "Promoted"
    if nullable:
        cols = ", ".join(f'"{c}"' for c in nullable)
        steps.append(f'Blanks = Table.ReplaceValue({last}, "", null, Replacer.ReplaceValue, {{{cols}}}),')
        last = "Blanks"
    steps.append(f'Typed = Table.TransformColumnTypes({last}, {{{types}}}, "en-US")')
    return "let\n" + "\n".join("\t" + x for x in steps) + "\nin\n\tTyped"


def _measure_table_tmdl() -> str:
    cat = settings.metric_catalog()
    lines = ["/// Governed metrics from config/metrics.yml. Every measure applies the suppression and",
             "/// breakdown rules in config/governance.yml; use these instead of summing columns.",
             f"table {MEASURE_TABLE}", ""]
    for key in MEASURES:
        m = cat[key]
        desc = f"{m['definition']} {m.get('caveats', '')}".replace("\n", " ").strip()
        lines += [f"\t/// {desc}", f"\tmeasure {_q(measure_name(key))} =", _indent(dax_measure(key), 3),
                  f"\t\tformatString: {FORMAT[m['format']]}", ""]
    empty = "let\n\tSource = #table(type table [Placeholder = text], {})\nin\n\tSource"
    lines += ["\tcolumn Placeholder", "\t\tdataType: string", "\t\tisHidden", "\t\tsummarizeBy: none",
              "\t\tsourceColumn: Placeholder", "",
              f"\tpartition {MEASURE_TABLE} = m", "\t\tmode: import", "\t\tsource =", _indent(empty, 4), ""]
    return "\n".join(lines)


def _table_tmdl(name: str, df: pd.DataFrame, description: str) -> str:
    lines = [f"/// {description}", f"table {_q(name)}", ""]
    is_fact = name == FACT
    for col in df.columns:
        t = _tmdl_type(df, col)
        lines += [f"\tcolumn {_q(col)}", f"\t\tdataType: {t}"]
        hidden = is_fact or (name.startswith("agg_")) or col in ("location_sort", "tenure_sort")
        if hidden:
            lines.append("\t\tisHidden")
        lines += ["\t\tsummarizeBy: none", f"\t\tsourceColumn: {col}"]
        if col == "location" and name == "dim_location":
            lines.append("\t\tsortByColumn: location_sort")
        if col == "tenure_band" and name == "dim_tenure_band":
            lines.append("\t\tsortByColumn: tenure_sort")
        if col == "fiscal_year_label":
            lines.append("\t\tsortByColumn: fiscal_year")
        lines.append("")
    lines += [f"\tpartition {_q(name)} = m", "\t\tmode: import", "\t\tsource =", _indent(_m_source(name, df), 4), ""]
    return "\n".join(lines)


DESCRIPTIONS = {
    FACT: "Additive workforce components at fiscal_year x agency x location x pay_basis x tenure_band. "
          "Use the governed measures; component columns are hidden.",
    "dim_fiscal_year": "Fiscal years (July 1 - June 30).",
    "dim_agency": "Agencies in scope, keyed on the stable payroll code; agency_name is the governed display name.",
    "dim_location": "Work locations: the five boroughs, Outside NYC, Unknown.",
    "dim_pay_basis": "Pay basis as recorded in payroll.",
    "dim_tenure_band": "Tenure bands at fiscal year end (Unknown = missing or invalid start date).",
    "agg_median_salary": "Median base salary by agency and fiscal year, pre-computed and suppressed by the metric layer.",
    "agg_median_salary_total": "Median base salary across all agencies in scope, by fiscal year.",
}


def write_pbip(tables: dict[str, pd.DataFrame], out_dir: Path, data_dir: Path) -> Path:
    sm = out_dir / f"{PROJECT}.SemanticModel"
    rp = out_dir / f"{PROJECT}.Report"
    for p in (sm, rp):
        if p.exists():
            shutil.rmtree(p)
    d = sm / "definition"
    (d / "tables").mkdir(parents=True)

    def w(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(text if text.endswith("\n") else text + "\n")

    def wj(path: Path, obj):
        w(path, json.dumps(obj, indent=2))

    wj(out_dir / f"{PROJECT}.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{PROJECT}.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    w(out_dir / ".gitignore", "**/.pbi/localSettings.json\n**/.pbi/cache.abf\n")
    wj(sm / "definition.pbism", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.0", "settings": {}})
    w(d / "database.tmdl", "database\n\tcompatibilityLevel: 1600\n")
    order = [MEASURE_TABLE, FACT, "dim_fiscal_year", "dim_agency", "dim_location", "dim_pay_basis", "dim_tenure_band",
             "agg_median_salary", "agg_median_salary_total"]
    w(d / "model.tmdl", "\n".join([
        "model Model", "\tculture: en-US", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
        "\tdiscourageImplicitMeasures", "\tsourceQueryCulture: en-US", "",
        "annotation __PBI_TimeIntelligenceEnabled = 0", ""] + [f"ref table {_q(t)}" for t in order]))
    folder = str(data_dir.resolve())
    w(d / "expressions.tmdl",
      f'/// Folder containing the exported CSV files. Change it in Transform data > Manage parameters.\n'
      f'expression DataFolder = "{folder}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')
    rels = []
    for dim, (table, key, fact_col) in DIMS.items():
        rels.append((f"{FACT}_{fact_col}", f"{FACT}.{fact_col}", f"{table}.{key}"))
    rels += [("median_fiscal_year", "agg_median_salary.fiscal_year", "dim_fiscal_year.fiscal_year"),
             ("median_agency", "agg_median_salary.agency_code", "dim_agency.agency_code"),
             ("median_total_fiscal_year", "agg_median_salary_total.fiscal_year", "dim_fiscal_year.fiscal_year")]
    w(d / "relationships.tmdl", "\n".join(
        f"relationship {name}\n\tfromColumn: {frm}\n\ttoColumn: {to}\n" for name, frm, to in rels))
    w(d / "tables" / f"{MEASURE_TABLE}.tmdl", _measure_table_tmdl())
    for name in order[1:]:
        w(d / "tables" / f"{name}.tmdl", _table_tmdl(name, tables[name], DESCRIPTIONS[name]))

    wj(rp / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{PROJECT}.SemanticModel"}}})
    rd = rp / "definition"
    wj(rd / "version.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
        "version": "2.0.0"})
    wj(rd / "report.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/1.0.0/schema.json",
        "layoutOptimization": "None", "themeCollection": {}})
    wj(rd / "pages" / "pages.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
        "pageOrder": ["overview"], "activePageName": "overview"})
    wj(rd / "pages" / "overview" / "page.json", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/1.0.0/schema.json",
        "name": "overview", "displayName": "Overview", "displayOption": "FitToPage", "height": 720, "width": 1280})
    return out_dir / f"{PROJECT}.pbip"


def export(con, out_dir: Path, is_sample: bool = False) -> dict:
    out_dir = Path(out_dir)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    tables = build_tables(con)
    for name, df in tables.items():
        df.to_csv(data_dir / f"{name}.csv", index=False, lineterminator="\n")
    pbip = write_pbip(tables, out_dir, data_dir)
    (out_dir / "measures.dax").write_text(measures_dax_file(), encoding="utf-8")
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "is_sample": is_sample,
        "grain": "fiscal_year x agency_code x location x pay_basis x tenure_band",
        "min_cell_size": settings.governance()["privacy"]["min_cell_size"],
        "tables": {name: len(df) for name, df in tables.items()},
        "pbip": pbip.name,
    }
    (data_dir / "export_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
