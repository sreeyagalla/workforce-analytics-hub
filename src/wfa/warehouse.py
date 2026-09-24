"""Build the DuckDB warehouse: raw -> conformed agency dimension -> fact table.

Tables
  raw_payroll    raw CSV rows as fetched (all VARCHAR) + source_file
  dim_agency     payroll_number -> governed display name + history of source names
  fct_payroll    typed, standardized records with derived flags used by metrics.yml
  bls_benchmark  BLS JOLTS fiscal-year separation / quit rates (if available)
  dq_results     written by quality.run_checks()
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from . import settings

EMPLOYED_STATUSES = ("ACTIVE", "ON LEAVE", "ON SEPARATION LEAVE")


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def location_case_sql(col: str = "work_location_borough") -> str:
    loc = settings.governance()["locations"]
    whens = " ".join(f"WHEN {_sql_str(k)} THEN {_sql_str(v)}" for k, v in loc["borough_map"].items())
    return (f"CASE WHEN nullif(trim({col}), '') IS NULL THEN {_sql_str(loc['unknown_label'])} "
            f"ELSE CASE upper(trim({col})) {whens} ELSE {_sql_str(loc['outside_label'])} END END")


def tenure_band_case_sql(col: str = "tenure_years") -> str:
    bands = settings.governance()["tenure_bands"]
    whens = " ".join(f"WHEN {col} >= {lo} AND {col} < {hi} THEN {_sql_str(label)}" for label, lo, hi in bands)
    return f"CASE {whens} END"


def connect(path: Path | str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path), read_only=read_only)
    con.execute("SET enable_progress_bar = false")
    return con


def build(paths: settings.DataPaths | None = None) -> Path:
    paths = paths or settings.get_paths()
    files = sorted(paths.raw_dir.glob("payroll_fy*.csv"))
    if not files:
        raise FileNotFoundError(f"No raw payroll files in {paths.raw_dir}. Run `wfa fetch` first "
                                f"(or use --sample for the committed sample).")
    paths.warehouse.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.warehouse.with_suffix(".building.duckdb")
    tmp.unlink(missing_ok=True)
    min_start = settings.governance()["data_quality"]["min_valid_start_date"]
    glob = (paths.raw_dir / "payroll_fy*.csv").as_posix()

    con = connect(tmp)
    try:
        con.execute(f"""
            CREATE TABLE raw_payroll AS
            SELECT * EXCLUDE (filename), regexp_extract(filename, '[^/\\\\]+$') AS source_file
            FROM read_csv('{glob}', all_varchar = true, header = true, filename = true,
                          delim = ',', quote = '"', escape = '"')
        """)

        agencies = settings.sources()["agencies"]
        con.register("cfg_agency", pd.DataFrame(
            [{"agency_code": k, "agency_name": v["name"]} for k, v in agencies.items()]))
        con.execute("""
            CREATE TABLE dim_agency AS
            WITH names AS (
                SELECT payroll_number AS agency_code, trim(agency_name) AS source_name,
                       min(CAST(fiscal_year AS INT)) AS first_fy, max(CAST(fiscal_year AS INT)) AS last_fy,
                       count(*) AS records
                FROM raw_payroll GROUP BY ALL
            )
            SELECT n.agency_code, c.agency_name,
                   arg_max(n.source_name, n.last_fy) AS source_name_latest,
                   string_agg(n.source_name || ' (FY' || n.first_fy || '-FY' || n.last_fy || ')', '; '
                              ORDER BY n.first_fy) AS source_name_history,
                   count(*) AS n_source_names
            FROM names n LEFT JOIN cfg_agency c USING (agency_code)
            GROUP BY n.agency_code, c.agency_name
        """)

        con.execute(f"""
            CREATE TABLE fct_payroll AS
            WITH typed AS (
                SELECT
                    CAST(fiscal_year AS INT)                          AS fiscal_year,
                    payroll_number                                    AS agency_code,
                    {location_case_sql()}                             AS location,
                    nullif(upper(trim(title_description)), '')        AS title,
                    upper(trim(leave_status_as_of_june_30))           AS status,
                    trim(pay_basis)                                   AS pay_basis,
                    TRY_CAST(TRY_CAST(agency_start_date AS TIMESTAMP) AS DATE) AS agency_start_date,
                    TRY_CAST(base_salary AS DOUBLE)                   AS base_salary,
                    coalesce(TRY_CAST(regular_hours AS DOUBLE), 0)    AS regular_hours,
                    -- summed columns are exact to the cent so totals do not depend on summation order
                    coalesce(TRY_CAST(regular_gross_paid AS DECIMAL(18, 2)), 0) AS regular_gross_paid,
                    coalesce(TRY_CAST(ot_hours AS DECIMAL(18, 2)), 0)           AS ot_hours,
                    coalesce(TRY_CAST(total_ot_paid AS DECIMAL(18, 2)), 0)      AS total_ot_paid,
                    coalesce(TRY_CAST(total_other_pay AS DECIMAL(18, 2)), 0)    AS total_other_pay,
                    source_file
                FROM raw_payroll
            ), dated AS (
                SELECT *,
                    make_date(fiscal_year - 1, 7, 1) AS fy_start,
                    make_date(fiscal_year, 6, 30)    AS fy_end
                FROM typed
            ), flagged AS (
                SELECT *,
                    (agency_start_date >= DATE '{min_start}' AND agency_start_date <= fy_end) AS start_date_valid
                FROM dated
            ), derived AS (
                SELECT *,
                    CASE WHEN start_date_valid THEN date_diff('day', agency_start_date, fy_end) / 365.25 END AS tenure_years,
                    regular_hours > 0                                            AS worked_in_fy,
                    status IN {EMPLOYED_STATUSES}                                AS is_employed_eoy,
                    status = 'CEASED' AND regular_hours > 0                      AS is_separation,
                    status = 'CEASED' AND regular_hours <= 0                     AS is_residual_payment,
                    status = 'SEASONAL'                                          AS is_seasonal,
                    coalesce(start_date_valid AND agency_start_date >= fy_start, false) AS is_new_hire
                FROM flagged
            )
            SELECT row_number() OVER (ORDER BY fiscal_year, agency_code, source_file) AS record_id,
                   d.fiscal_year, d.agency_code, a.agency_name, d.location, d.title, d.status, d.pay_basis,
                   d.agency_start_date, d.fy_start, d.fy_end, d.start_date_valid,
                   d.tenure_years, {tenure_band_case_sql('d.tenure_years')} AS tenure_band,
                   d.base_salary, d.regular_hours, d.regular_gross_paid, d.ot_hours, d.total_ot_paid, d.total_other_pay,
                   d.worked_in_fy, d.is_employed_eoy, d.is_separation, d.is_residual_payment, d.is_seasonal, d.is_new_hire,
                   d.source_file
            FROM derived d LEFT JOIN dim_agency a USING (agency_code)
        """)

        if paths.benchmark_csv.exists():
            con.execute(f"CREATE TABLE bls_benchmark AS SELECT * FROM read_csv('{paths.benchmark_csv.as_posix()}')")

        from . import quality  # local import: quality depends on metrics, which reads the warehouse
        quality.run_checks(con, paths)
    finally:
        con.close()
    try:
        tmp.replace(paths.warehouse)
    except PermissionError as e:
        raise PermissionError(f"Could not replace {paths.warehouse}: another process (usually the running "
                              f"dashboard) has it open. Stop the dashboard and rebuild. The new build is at {tmp}.") from e
    return paths.warehouse
