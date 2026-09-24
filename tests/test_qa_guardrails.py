"""Guardrails: questions the system cannot answer from governed data must be refused,
with no computed result and no invented numbers."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from grounding import NUM_RE, allowed_refusal_numbers, norm
from wfa import settings
from wfa.qa import ask

REFUSALS = [
    # individual-level / privacy
    ("What is John Smith's salary?", "individual_level"),
    ("How much does Maria Lopez make?", "individual_level"),
    ("List the employees who left Sanitation in 2024", "individual_level"),
    ("Which employees are a flight risk at Parks?", "individual_level"),
    ("Who quit the Police Department last year?", "individual_level"),
    ("Show me employee IDs for people with high overtime", "individual_level"),
    ("What are the individual salaries at Finance?", "individual_level"),
    # data the source does not have
    ("What is the engagement score for Parks?", "unsupported_metric"),
    ("Average performance rating by agency", "unsupported_metric"),
    ("Turnover by gender at NYPD", "unsupported_metric"),
    ("How many promotions happened at FDNY in 2024?", "unsupported_metric"),
    ("What share of separations were voluntary resignations?", "unsupported_metric"),
    ("How many open positions does DOT have?", "unsupported_metric"),
    ("Forecast turnover for next year", "unsupported_metric"),
    # causal
    ("Why did turnover go up at Correction?", "causal"),
    ("What is driving overtime at the Police Department?", "causal"),
    # outside the loaded range / scope
    ("What was turnover in 2015?", "out_of_range"),
    ("Turnover in FY2020", "out_of_range"),
    ("Turnover at the Department of Education", "unknown_entity"),
    # not defined for this breakdown
    ("Turnover by tenure band in 2024", "unsupported_dimension"),
    ("Median salary for hourly workers at Parks", "unsupported_dimension"),
    ("New hires by tenure band", "unsupported_dimension"),
    # nothing to map
    ("What's the weather like?", "unparsed"),
    ("blah blah", "unparsed"),
]


@pytest.mark.parametrize("question,category", REFUSALS, ids=[q for q, _ in REFUSALS])
def test_refuses_with_expected_category_and_no_invented_numbers(sample_con, question, category):
    ans = ask(sample_con, question, parser="rules")
    assert ans.refused, f"answered instead of refusing: {ans.text}"
    assert ans.category == category, ans.text
    assert ans.result is None and ans.table is None
    years = [2020, 2021, 2022, 2023, 2024, 2025]
    k = settings.governance()["privacy"]["min_cell_size"]
    allowed = allowed_refusal_numbers(question, years, k, len(settings.sources()["agencies"]))
    assert {norm(x) for x in NUM_RE.findall(ans.text)} <= allowed, ans.text


def test_names_of_places_are_not_mistaken_for_people(sample_con):
    ans = ask(sample_con, "What is Staten Island's turnover rate in 2025?", parser="rules")
    assert not ans.refused
    assert ans.spec.filters == {"location": ["Staten Island"]}


class _SpyLLM:
    """Stub client that would happily answer anything, and records whether it was called."""
    def __init__(self, payload):
        self.calls = 0
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.calls += 1
                return SimpleNamespace(stop_reason="end_turn",
                                       content=[SimpleNamespace(type="text", text=json.dumps(payload))])
        self.beta = SimpleNamespace(messages=_Messages())


def test_guardrails_run_before_the_llm_is_called(sample_con):
    spy = _SpyLLM({"action": "query", "refusal_category": "none", "metric": "median_base_salary",
                   "intent": "value", "group_by": "none", "agencies": [], "locations": [], "pay_basis": [],
                   "fiscal_years": [2025], "order": "desc", "top_n": 3})
    ans = ask(sample_con, "What is John Smith's salary?", parser="llm", llm_client=spy)
    assert ans.refused and ans.category == "individual_level"
    assert spy.calls == 0
