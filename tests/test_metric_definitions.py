"""Each governed definition, checked against answers worked out by hand on a tiny dataset."""
from __future__ import annotations

from datetime import date

import pytest

from helpers import build, governance_with, row
from wfa import metrics
from wfa.metrics import MetricQuery as Q

P, PN = "846", "DEPT OF PARKS & RECREATION"
ROWS = [
    # FY2024 Parks: 4 employed at year end, 1 separation
    row(2024, P, PN, "2015-07-01T00:00:00.000", "MANHATTAN", "CLERK", "ACTIVE", 50000, "per Annum", 1820, 50000, 10, 500, 0),
    row(2024, P, PN, "2020-06-30T00:00:00.000", "BROOKLYN", "CLERK", "ACTIVE", 60000, "per Annum", 1820, 60000, 0, 0, 0),
    row(2024, P, PN, "2023-09-01T00:00:00.000", "QUEENS", "CLERK", "ACTIVE", 40000, "per Annum", 1820, 40000, 0, 0, 0),
    row(2024, P, PN, "2010-01-01T00:00:00.000", "QUEENS", "CLERK", "CEASED", 70000, "per Annum", 900, 35000, 0, 0, 0),
    row(2024, P, PN, "2012-01-01T00:00:00.000", "BRONX", "CLERK", "ON LEAVE", 55000, "per Annum", 1000, 30000, 0, 0, 0),
    # FY2025 Parks
    row(2025, P, PN, "2015-07-01T00:00:00.000", "MANHATTAN", "CLERK", "ACTIVE", 52000, "per Annum", 1820, 52000, 20, 1000, 100),  # a1
    row(2025, P, PN, "2020-06-30T00:00:00.000", "BROOKLYN", "CLERK", "ACTIVE", 62000, "per Annum", 1820, 62000, 0, 0, 0),         # a2
    row(2025, P, PN, "2023-09-01T00:00:00.000", "QUEENS", "CLERK", "CEASED", 41000, "per Annum", 500, 10000, 5, 200, 0),           # a3 separation
    row(2025, P, PN, "2024-08-01T00:00:00.000", "QUEENS", "LIFEGUARD", "CEASED", 20, "per Hour", 300, 6000, 0, 0, 0),             # a4 new hire + separation
    row(2025, P, PN, "2024-09-15T00:00:00.000", "BRONX", "LIFEGUARD", "ACTIVE", 20, "per Hour", 800, 16000, 40, 1200, 0),         # a5 new hire
    row(2025, P, PN, "2010-01-01T00:00:00.000", "QUEENS", "CLERK", "CEASED", 70000, "per Annum", 0, 1500, 0, 0, 0),               # a6 residual payment
    row(2025, P, PN, "2012-01-01T00:00:00.000", "BRONX", "CLERK", "ON SEPARATION LEAVE", 55000, "per Annum", 1000, 30000, 0, 0, 0),  # a7
    row(2025, P, PN, "2024-06-01T00:00:00.000", "BRONX", "LIFEGUARD", "SEASONAL", 20, "per Hour", 200, 4000, 0, 0, 0),            # a8 seasonal
    row(2025, P, PN, "1900-01-01T00:00:00.000", "MANHATTAN", "CLERK", "ACTIVE", 58000, "per Annum", 1820, 58000, 0, 0, 0),         # a9 invalid start
    row(2025, P, PN, "2018-03-01T00:00:00.000", "ULSTER", "CLERK", "ACTIVE", 65000, "per Annum", 1820, 65000, 100, 5000, 0),      # a10 upstate
    # agency 858 renamed between years
    row(2024, "858", "DEPT OF INFO TECH & TELECOMM", "2019-01-01T00:00:00.000", "RICHMOND", "ANALYST", "ACTIVE", 90000, "per Annum", 1820, 90000, 0, 0, 0),
    row(2025, "858", "TECHNOLOGY & INNOVATION", "2019-01-01T00:00:00.000", "RICHMOND", "ANALYST", "ACTIVE", 92000, "per Annum", 1820, 92000, 0, 0, 0),
]


@pytest.fixture
def con(tmp_path, monkeypatch):
    governance_with(monkeypatch, min_cell_size=1)
    c, _ = build(tmp_path, ROWS)
    yield c
    c.close()


def value(con, metric, fy=2025, agency=P, **kw):
    df = metrics.compute(con, Q(metric, filters={"agency": [agency]}, fiscal_years=[fy], **kw)).df
    return df.iloc[0]


def tenure(start: date, fy: int = 2025) -> float:
    return (date(fy, 6, 30) - start).days / 365.25


def test_headcount_counts_active_on_leave_and_separation_leave_only(con):
    assert value(con, "headcount", 2024)["value"] == 4
    assert value(con, "headcount")["value"] == 6  # a1 a2 a5 a7 a9 a10; not ceased, not seasonal


def test_separations_exclude_zero_hour_residual_payments(con):
    assert value(con, "separations")["value"] == 2  # a3 a4, not a6


def test_turnover_uses_average_of_prior_and_current_headcount(con):
    r = value(con, "turnover_rate")
    assert r["numerator"] == 2 and r["denominator"] == 5.0
    assert r["value"] == pytest.approx(0.4)


def test_turnover_unavailable_without_prior_year(con):
    assert value(con, "turnover_rate", 2024)["value"] != value(con, "turnover_rate", 2024)["value"]  # NaN


def test_new_hires_and_same_year_attrition(con):
    assert value(con, "new_hires")["value"] == 2  # a4 a5; a8 started in FY2024
    r = value(con, "new_hire_attrition_rate")
    assert (r["numerator"], r["denominator"], r["value"]) == (1, 2, 0.5)


def test_average_tenure_excludes_invalid_start_dates(con):
    expected = [tenure(date(2015, 7, 1)), tenure(date(2020, 6, 30)), tenure(date(2024, 9, 15)),
                tenure(date(2012, 1, 1)), tenure(date(2018, 3, 1))]  # a1 a2 a5 a7 a10 (a9 invalid)
    r = value(con, "avg_tenure_years")
    assert r["population"] == 5
    assert r["value"] == pytest.approx(sum(expected) / len(expected))


def test_median_salary_is_salaried_year_end_headcount_only(con):
    assert value(con, "median_base_salary")["value"] == 58000  # 52k 55k 58k 62k 65k


def test_overtime_per_worker_and_share_of_pay(con):
    r = value(con, "ot_hours_per_worker")
    assert (r["numerator"], r["denominator"]) == (165, 9)  # every record with hours > 0 (not a6)
    r = value(con, "ot_share_of_pay")
    assert r["numerator"] == 7400 and r["denominator"] == 310500
    assert r["value"] == pytest.approx(7400 / 310500)


def test_locations_are_standardized(con):
    df = metrics.compute(con, Q("headcount", group_by=["location"], fiscal_years=[2025])).df
    shown = df[~df["suppressed"]]
    assert dict(zip(shown["location"], shown["value"])) == {
        "Bronx": 2, "Brooklyn": 1, "Manhattan": 2, "Outside NYC": 1, "Staten Island": 1}  # ULSTER, RICHMOND mapped
    # Queens only has leavers in FY2025: headcount 0 is below even a threshold of 1, so it is suppressed
    assert df.loc[df["location"] == "Queens", "suppressed"].item()


def test_renamed_agency_reports_under_one_governed_name(con):
    df = metrics.compute(con, Q("headcount", group_by=["agency"], filters={"agency": ["858"]})).df
    assert list(df["agency"]) == ["Technology & Innovation", "Technology & Innovation"]
    n = con.execute("SELECT n_source_names FROM dim_agency WHERE agency_code = '858'").fetchone()[0]
    assert n == 2


def test_metric_not_defined_by_dimension_raises(con):
    with pytest.raises(metrics.MetricError):
        metrics.compute(con, Q("turnover_rate", group_by=["tenure_band"]))


def test_small_cells_are_suppressed_at_default_threshold(tmp_path, monkeypatch):
    governance_with(monkeypatch, min_cell_size=10)
    c, _ = build(tmp_path, ROWS)
    df = metrics.compute(c, Q("headcount", group_by=["agency"], fiscal_years=[2025])).df
    assert df["suppressed"].all()
    assert df["value"].isna().all() and df["numerator"].isna().all()
    c.close()
