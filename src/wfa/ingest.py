"""Fetch payroll records from NYC Open Data (Socrata SODA API, no auth).

Data minimization happens at the source: the request's $select lists only the
columns in config/sources.yml, and `build_select` refuses to build a request
containing any governance-listed PII column. Names never reach this machine.
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import settings


class PIIRequestError(ValueError):
    pass


def build_select(columns: list[str], pii_columns: list[str]) -> str:
    leaked = sorted(set(c.lower() for c in columns) & set(c.lower() for c in pii_columns))
    if leaked:
        raise PIIRequestError(f"Refusing to request PII columns: {leaked}")
    return ",".join(columns)


def build_where(fiscal_year: int, agency_codes: list[str]) -> str:
    codes = ",".join(f"'{c}'" for c in sorted(agency_codes, key=int))
    return f"fiscal_year={int(fiscal_year)} AND payroll_number in ({codes})"


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["Accept"] = "text/csv"
    return s


def source_count(session: requests.Session, base_url: str, where: str) -> int:
    r = session.get(f"{base_url}.json", params={"$select": "count(*) AS n", "$where": where}, timeout=120)
    r.raise_for_status()
    return int(r.json()[0]["n"])


def fetch_fiscal_year(session: requests.Session, base_url: str, select: str, where: str,
                      page_size: int, out_path: Path) -> int:
    """Page through one fiscal year and write a single CSV. Returns rows written."""
    written = 0
    offset = 0
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = None
        while True:
            params = {"$select": select, "$where": where, "$order": ":id",
                      "$limit": page_size, "$offset": offset}
            r = session.get(f"{base_url}.csv", params=params, timeout=300)
            r.raise_for_status()
            rows = list(csv.reader(io.StringIO(r.text)))
            header, body = rows[0], rows[1:]
            if writer is None:
                writer = csv.writer(f)
                writer.writerow(header)
            writer.writerows(body)
            written += len(body)
            if len(body) < page_size:
                break
            offset += page_size
            time.sleep(0.5)  # be polite to an unauthenticated public API
    return written


def fetch_payroll(raw_dir: Path | None = None, fiscal_years: list[int] | None = None) -> dict:
    src = settings.sources()["payroll"]
    gov = settings.governance()
    raw_dir = raw_dir or settings.get_paths(sample=False).raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    base_url = f"{src['api_base']}/{src['dataset_id']}"
    select = build_select(src["columns"], gov["privacy"]["pii_columns"])
    codes = list(settings.sources()["agencies"].keys())
    session = _session()

    manifest = {"dataset_id": src["dataset_id"], "source": src["landing_page"],
                "columns_requested": src["columns"], "agency_codes": codes, "files": {}}
    for fy in fiscal_years or src["fiscal_years"]:
        where = build_where(fy, codes)
        expected = source_count(session, base_url, where)
        out = raw_dir / f"payroll_fy{fy}.csv"
        print(f"FY{fy}: source reports {expected:,} rows; fetching...", flush=True)
        got = fetch_fiscal_year(session, base_url, select, where, src["page_size"], out)
        print(f"FY{fy}: wrote {got:,} rows -> {out.name}", flush=True)
        manifest["files"][out.name] = {
            "fiscal_year": fy, "where": where, "source_row_count": expected,
            "rows_written": got, "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    with open(raw_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest
