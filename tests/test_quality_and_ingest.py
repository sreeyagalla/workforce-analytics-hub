"""Data quality controls and ingest-time privacy (no network)."""
from __future__ import annotations

import pytest

from helpers import build, row
from wfa import ingest

GOOD = [row(2025, "846", "DEPT OF PARKS & RECREATION", "2015-07-01T00:00:00.000", "MANHATTAN", "CLERK", "ACTIVE",
            50000, "per Annum", 1820, 50000, 0, 0, 0)]


def dq(con):
    return {r[0]: r[1] for r in con.execute("SELECT check_name, status FROM dq_results").fetchall()}


def test_clean_data_has_no_failures(tmp_path):
    con, _ = build(tmp_path, GOOD)
    assert "fail" not in dq(con).values()


def test_row_count_mismatch_with_source_fails_reconciliation(tmp_path):
    con, _ = build(tmp_path, GOOD, manifest_counts={"payroll_fy2025.csv": 2})
    assert dq(con)["source_reconciliation"] == "fail"


def test_unknown_status_fails_domain_check(tmp_path):
    bad = GOOD + [row(2025, "846", "DEPT OF PARKS & RECREATION", "2015-07-01T00:00:00.000", "QUEENS", "CLERK",
                      "TERMINATED", 1, "per Annum", 1, 1, 0, 0, 0)]
    con, _ = build(tmp_path, bad)
    assert dq(con)["status_domain"] == "fail"


def test_pii_column_in_stored_data_fails(tmp_path):
    con, _ = build(tmp_path, GOOD, extra_columns={"last_name": "DOE"})
    assert dq(con)["pii_columns_absent"] == "fail"


def test_residual_ceased_records_are_measured(tmp_path):
    rows = GOOD + [row(2025, "846", "DEPT OF PARKS & RECREATION", "2010-01-01T00:00:00.000", "QUEENS", "CLERK",
                       "CEASED", 1, "per Annum", 0, 100, 0, 0, 0)]
    con, _ = build(tmp_path, rows)
    failing, total = con.execute("SELECT failing_rows, total_rows FROM dq_results "
                                 "WHERE check_name = 'ceased_with_zero_hours'").fetchone()
    assert (failing, total) == (1, 1)


def test_request_builder_refuses_pii_columns():
    with pytest.raises(ingest.PIIRequestError):
        ingest.build_select(["fiscal_year", "last_name"], ["last_name", "first_name"])
    assert ingest.build_select(["fiscal_year", "agency_name"], ["last_name"]) == "fiscal_year,agency_name"


def test_configured_request_contains_no_pii():
    from wfa import settings
    cols = settings.sources()["payroll"]["columns"]
    pii = settings.governance()["privacy"]["pii_columns"]
    assert not set(cols) & set(pii)
    ingest.build_select(cols, pii)  # does not raise


def test_where_clause_is_built_from_codes():
    assert ingest.build_where(2025, ["846", "56"]) == "fiscal_year=2025 AND payroll_number in ('56','846')"


def test_pagination_writes_every_page_once(tmp_path):
    pages = ["a,b\n1,x\n2,y\n", "a,b\n3,z\n"]

    class FakeResp:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    class FakeSession:
        def __init__(self):
            self.offsets = []

        def get(self, url, params, timeout):
            self.offsets.append(params["$offset"])
            return FakeResp(pages[len(self.offsets) - 1])

    s = FakeSession()
    out = tmp_path / "out.csv"
    n = ingest.fetch_fiscal_year(s, "https://example.invalid/x", "a,b", "TRUE", 2, out)
    assert n == 3 and s.offsets == [0, 2]
    assert out.read_text(encoding="utf-8").splitlines() == ["a,b", "1,x", "2,y", "3,z"]
