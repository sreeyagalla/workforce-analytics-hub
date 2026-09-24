"""Data quality controls. Each check returns one or more result rows:

  check_name, category, status (pass | warn | fail | info), failing_rows, total_rows, detail

Results are stored in the `dq_results` table so the dashboard and the Excel
export show the same run. `fail` means a metric would be wrong if we shipped;
`warn` means a known data issue that is handled or disclosed; `info` documents
a transformation decision with its measured impact.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb
import pandas as pd

from . import metrics, settings

KNOWN_STATUSES = {"ACTIVE", "CEASED", "ON LEAVE", "ON SEPARATION LEAVE", "SEASONAL"}
KNOWN_PAY_BASIS = {"per Annum", "per Day", "per Hour", "Prorated Annual"}


def _row(check, category, status, failing, total, detail):
    return {"check_name": check, "category": category, "status": status,
            "failing_rows": int(failing), "total_rows": int(total), "detail": detail}


def _scalar(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()[0]


def check_source_reconciliation(con, paths):
    rows = []
    if not paths.manifest.exists():
        return [_row("source_reconciliation", "completeness", "warn", 0, 0,
                     "No fetch manifest found; cannot reconcile against source row counts.")]
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    source = "sample manifest" if manifest.get("is_sample") else "source API"
    loaded = dict(con.execute("SELECT source_file, count(*) FROM raw_payroll GROUP BY 1").fetchall())
    for fname, meta in sorted(manifest["files"].items()):
        expected, got = meta["source_row_count"], loaded.get(fname, 0)
        status = "pass" if expected == got else "fail"
        rows.append(_row("source_reconciliation", "completeness", status, abs(expected - got), expected,
                         f"{fname}: {source} count {expected:,} vs loaded {got:,}"))
    return rows


def check_pii_absent(con, paths):
    pii = {c.lower() for c in settings.governance()["privacy"]["pii_columns"]}
    cols = {r[0].lower() for r in con.execute("DESCRIBE raw_payroll").fetchall()}
    leaked = sorted(cols & pii)
    total = _scalar(con, "SELECT count(*) FROM raw_payroll")
    return [_row("pii_columns_absent", "privacy", "fail" if leaked else "pass", total if leaked else 0, total,
                 f"PII columns present: {leaked}" if leaked else f"None of {sorted(pii)} present in stored data.")]


def check_required_fields(con, paths):
    total = _scalar(con, "SELECT count(*) FROM fct_payroll")
    bad = _scalar(con, "SELECT count(*) FROM fct_payroll WHERE fiscal_year IS NULL OR agency_code IS NULL OR status IS NULL")
    return [_row("required_fields_not_null", "validity", "fail" if bad else "pass", bad, total,
                 "fiscal_year, payroll_number, leave status must be populated.")]


def check_domains(con, paths):
    total = _scalar(con, "SELECT count(*) FROM fct_payroll")
    rows = []
    bad_status = con.execute("SELECT status, count(*) FROM fct_payroll WHERE NOT list_contains(?, status) GROUP BY 1",
                             [sorted(KNOWN_STATUSES)]).fetchall()
    n = sum(c for _, c in bad_status)
    rows.append(_row("status_domain", "validity", "fail" if n else "pass", n, total,
                     f"Unexpected statuses: {bad_status}" if n else f"All statuses in {sorted(KNOWN_STATUSES)}."))
    bad_pb = con.execute("SELECT pay_basis, count(*) FROM fct_payroll WHERE pay_basis IS NULL OR NOT list_contains(?, pay_basis) GROUP BY 1",
                         [sorted(KNOWN_PAY_BASIS)]).fetchall()
    n = sum(c for _, c in bad_pb)
    rows.append(_row("pay_basis_domain", "validity", "warn" if n else "pass", n, total,
                     f"Unexpected pay basis values: {bad_pb}" if n else "All pay basis values recognized."))
    in_scope = list(settings.sources()["agencies"].keys())
    n = _scalar(con, "SELECT count(*) FROM fct_payroll WHERE NOT list_contains(?, agency_code)", [in_scope])
    rows.append(_row("agency_in_scope", "validity", "fail" if n else "pass", n, total,
                     "Every record belongs to a configured agency code."))
    return rows


def check_start_dates(con, paths):
    total = _scalar(con, "SELECT count(*) FROM fct_payroll")
    bad = _scalar(con, "SELECT count(*) FROM fct_payroll WHERE NOT coalesce(start_date_valid, false)")
    return [_row("start_date_validity", "validity", "warn" if bad else "pass", bad, total,
                 f"{bad:,} records ({bad / max(total, 1):.2%}) have a missing, pre-1940, or post-FY-end agency start "
                 "date; they are excluded from tenure and new-hire metrics.")]


def check_negative_amounts(con, paths):
    total = _scalar(con, "SELECT count(*) FROM fct_payroll")
    parts = []
    n_any = 0
    for col in ["base_salary", "regular_hours", "regular_gross_paid", "ot_hours", "total_ot_paid", "total_other_pay"]:
        n = _scalar(con, f"SELECT count(*) FROM fct_payroll WHERE {col} < 0")
        if n:
            parts.append(f"{col}: {n:,}")
    n_any = _scalar(con, """SELECT count(*) FROM fct_payroll WHERE base_salary < 0 OR regular_hours < 0
                            OR regular_gross_paid < 0 OR ot_hours < 0 OR total_ot_paid < 0 OR total_other_pay < 0""")
    return [_row("negative_amounts", "validity", "warn" if n_any else "pass", n_any, total,
                 ("Negative values (payroll adjustments) kept as-is: " + "; ".join(parts)) if parts
                 else "No negative hours or pay amounts.")]


def check_agency_name_drift(con, paths):
    drift = con.execute("SELECT agency_code, source_name_history FROM dim_agency WHERE n_source_names > 1").fetchall()
    total = _scalar(con, "SELECT count(*) FROM dim_agency")
    detail = ("; ".join(f"{code}: {hist}" for code, hist in drift) + ". Reported under one governed name via dim_agency."
              if drift else "Each agency code has a single source name.")
    return [_row("agency_name_drift", "consistency", "info" if drift else "pass", len(drift), total, detail)]


def check_locations(con, paths):
    loc = settings.governance()["locations"]
    total = _scalar(con, "SELECT count(*) FROM fct_payroll")
    unknown = _scalar(con, "SELECT count(*) FROM fct_payroll WHERE location = ?", [loc["unknown_label"]])
    outside = _scalar(con, "SELECT count(*) FROM fct_payroll WHERE location = ?", [loc["outside_label"]])
    return [
        _row("location_missing", "completeness", "warn" if unknown else "pass", unknown, total,
             f"{unknown:,} records have a blank work location (reported as '{loc['unknown_label']}')."),
        _row("location_outside_nyc", "consistency", "info", outside, total,
             f"{outside:,} records work outside the five boroughs (e.g. upstate watershed sites); "
             f"grouped as '{loc['outside_label']}'."),
    ]


def check_residual_ceased(con, paths):
    ceased = _scalar(con, "SELECT count(*) FROM fct_payroll WHERE status = 'CEASED'")
    residual = _scalar(con, "SELECT count(*) FROM fct_payroll WHERE is_residual_payment")
    share = residual / ceased if ceased else 0
    return [_row("ceased_with_zero_hours", "accuracy", "info", residual, ceased,
                 f"{residual:,} of {ceased:,} CEASED records ({share:.1%}) have zero regular hours in the fiscal year "
                 "(residual/retro payments to earlier leavers). Excluded from separations.")]


def check_duplicates(con, paths):
    cols = [r[0] for r in con.execute("DESCRIBE raw_payroll").fetchall() if r[0] != "source_file"]
    col_list = ", ".join(f'"{c}"' for c in cols)
    total = _scalar(con, "SELECT count(*) FROM raw_payroll")
    groups = f"SELECT {col_list}, count(*) AS n FROM raw_payroll GROUP BY ALL HAVING count(*) > 1"
    dup = _scalar(con, f"SELECT coalesce(sum(n - 1), 0) FROM ({groups})")
    detail = "No row is identical to another on every fetched field."
    if dup:
        top = con.execute(f"""SELECT g.n, a.agency_name, g.title_description, left(g.agency_start_date, 10), g.fiscal_year
                              FROM ({groups}) g LEFT JOIN dim_agency a ON a.agency_code = g.payroll_number
                              ORDER BY g.n DESC LIMIT 1""").fetchone()
        detail = (f"{dup:,} rows are identical to another row on every fetched field. Largest group: {top[0]:,} "
                  f"{top[1]} '{top[2]}' records, FY{top[4]}, all starting {top[3]} at the same pay. Kept: without "
                  "names or employee IDs, identical pay records for people hired together cannot be told apart "
                  "from true duplicates.")
    return [_row("identical_rows", "uniqueness", "warn" if dup else "pass", dup, total, detail)]


def check_volume_yoy(con, paths):
    limit = settings.governance()["data_quality"]["volume_change_warn"]
    df = con.execute("""
        WITH v AS (SELECT agency_name, fiscal_year, count(*) AS n FROM fct_payroll GROUP BY ALL)
        SELECT c.agency_name, c.fiscal_year, p.n AS prior_n, c.n AS n, c.n / p.n - 1 AS change
        FROM v c JOIN v p ON p.agency_name = c.agency_name AND p.fiscal_year = c.fiscal_year - 1
        ORDER BY abs(c.n / p.n - 1) DESC
    """).df()
    flagged = df[df["change"].abs() > limit]
    detail = ("; ".join(f"{r.agency_name} FY{r.fiscal_year}: {r.prior_n:,} -> {r.n:,} ({r.change:+.0%})"
                        for r in flagged.itertuples()) if len(flagged)
              else f"No agency's record volume moved more than {limit:.0%} year over year.")
    return [_row("record_volume_yoy", "consistency", "warn" if len(flagged) else "pass", len(flagged), len(df), detail)]


def check_metric_reconciliation(con, paths):
    """The semantic layer must agree with direct counts, and agency cells must add up to the total."""
    direct = con.execute("""SELECT fiscal_year, count(*) FILTER (WHERE is_employed_eoy) AS hc,
                                   count(*) FILTER (WHERE is_separation) AS sep
                            FROM fct_payroll GROUP BY 1 ORDER BY 1""").df().set_index("fiscal_year")
    rows = []
    for metric, col in [("headcount", "hc"), ("separations", "sep")]:
        tot = metrics.compute(con, metrics.MetricQuery(metric)).df.set_index("fiscal_year")
        by_agency = metrics.compute(con, metrics.MetricQuery(metric, group_by=["agency"])).df
        comparable = ~tot["suppressed"]
        mismatches = int((tot.loc[comparable, "numerator"] != direct.loc[comparable[comparable].index, col]).sum())
        n_years, n_checked = len(direct), int(comparable.sum())
        suppressed = int(by_agency["suppressed"].sum())
        additive = by_agency.groupby("fiscal_year")["numerator"].sum(min_count=1)
        add_mismatch = int((additive != direct[col]).sum()) if not suppressed else 0
        if mismatches or add_mismatch:
            status = "fail"
        elif n_checked < n_years or suppressed:
            status = "warn"  # small data: suppression makes some comparisons impossible
        else:
            status = "pass"
        detail = (f"Semantic-layer {metric} matches direct SQL for {n_checked - mismatches}/{n_years} years"
                  + (f" ({n_years - n_checked} suppressed, not comparable)" if n_checked < n_years else "") + "; "
                  + ("agency cells sum to the total." if not suppressed and not add_mismatch
                     else f"{suppressed} agency cells suppressed; additivity not checked." if suppressed
                     else f"{add_mismatch} years where agency cells do not sum to the total."))
        rows.append(_row(f"metric_reconciliation_{metric}", "accuracy", status, mismatches + add_mismatch, n_years, detail))
    return rows


CHECKS = [check_source_reconciliation, check_pii_absent, check_required_fields, check_domains,
          check_start_dates, check_negative_amounts, check_agency_name_drift, check_locations,
          check_residual_ceased, check_duplicates, check_volume_yoy, check_metric_reconciliation]


def run_checks(con: duckdb.DuckDBPyConnection, paths: settings.DataPaths) -> pd.DataFrame:
    rows = []
    for check in CHECKS:
        rows.extend(check(con, paths))
    df = pd.DataFrame(rows)
    df["run_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con.register("dq_df", df)
    con.execute("CREATE OR REPLACE TABLE dq_results AS SELECT * FROM dq_df")
    con.unregister("dq_df")
    return df
