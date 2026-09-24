"""Entities the Q&A engine can recognize, built from the warehouse + config."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .. import metrics, settings

LOCATION_TERMS = {
    "manhattan": "Manhattan", "brooklyn": "Brooklyn", "queens": "Queens",
    "the bronx": "Bronx", "bronx": "Bronx", "staten island": "Staten Island", "richmond": "Staten Island",
    "outside nyc": "Outside NYC", "outside new york city": "Outside NYC", "outside the city": "Outside NYC",
    "upstate": "Outside NYC",
}
PAY_TERMS = {
    "hourly": "per Hour", "per hour": "per Hour", "salaried": "per Annum", "per annum": "per Annum",
    "daily": "per Day", "per day": "per Day", "per diem": "per Day", "prorated": "Prorated Annual",
}


def _terms(mapping: dict[str, str]) -> list[tuple[re.Pattern, str, str]]:
    """(compiled word-boundary pattern, surface term, canonical value), longest term first."""
    items = sorted(mapping.items(), key=lambda kv: -len(kv[0]))
    return [(re.compile(rf"(?<![\w&]){re.escape(t)}(?![\w&])", re.I), t, v) for t, v in items]


@dataclass
class Vocabulary:
    agencies: dict[str, str]       # code -> governed display name
    locations: list[str]
    pay_basis: list[str]
    years: list[int]
    agency_terms: list
    location_terms: list
    pay_terms: list

    @classmethod
    def from_warehouse(cls, con) -> "Vocabulary":
        cfg = settings.sources()["agencies"]
        rows = con.execute("SELECT agency_code, agency_name, source_name_history FROM dim_agency").fetchall()
        agencies = {code: name for code, name, _ in rows}
        surface: dict[str, str] = {}
        for code, name, history in rows:
            surface[name.lower()] = code
            surface[name.lower().replace("&", "and")] = code
            for part in (history or "").split("; "):
                src = re.sub(r"\s*\(FY\d{4}-FY\d{4}\)$", "", part).strip().lower()
                if src:
                    surface[src] = code
            for alias in cfg.get(code, {}).get("alias", []):
                surface[str(alias).lower()] = code
        locations = metrics.dimension_values(con, "location")
        pay_basis = metrics.dimension_values(con, "pay_basis")
        return cls(
            agencies=agencies, locations=locations, pay_basis=pay_basis,
            years=metrics.available_fiscal_years(con),
            agency_terms=_terms(surface),
            location_terms=_terms({k: v for k, v in LOCATION_TERMS.items() if v in locations}),
            pay_terms=_terms({k: v for k, v in PAY_TERMS.items() if v in pay_basis}),
        )


def find_all(text: str, terms: list) -> list[tuple[int, int, str]]:
    """Non-overlapping matches, preferring longer terms: [(start, end, canonical)]."""
    taken: list[tuple[int, int]] = []
    out = []
    for pat, _, value in terms:
        for m in pat.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            out.append((m.start(), m.end(), value))
    return sorted(out)
