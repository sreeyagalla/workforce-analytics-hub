"""THE grounding test: every number the Q&A engine says must equal a number computed
independently (pandas, tests/reference.py) from the same raw rows. See grounding.py.

Runs on the committed real-data sample always, and on the full fetched dataset
when it has been built locally (marked full_data).
"""
from __future__ import annotations

import pytest

from grounding import check_grounded
from wfa import settings
from wfa.qa import ask
from wfa.qa.vocab import Vocabulary

LATEST, FIRST = 2025, 2020
ALIASES = ["Parks", "NYPD", "Sanitation", "DOT", "HRA", "Correction", "OTI", "ACS"]
PHRASE = {  # one natural phrase per governed metric
    "headcount": "headcount", "separations": "separations", "turnover_rate": "turnover",
    "new_hires": "new hires", "new_hire_attrition_rate": "new-hire attrition", "avg_tenure_years": "average tenure",
    "median_base_salary": "median salary", "ot_hours_per_worker": "overtime hours per worker",
    "ot_share_of_pay": "overtime share of pay",
}


def generated_questions() -> list[tuple[str, str, str]]:
    """(question, expected metric, expected intent) covering every metric x question type."""
    out = []
    for i, (key, m) in enumerate(settings.metric_catalog().items()):
        p = PHRASE[key]
        a, b = ALIASES[i % len(ALIASES)], ALIASES[(i + 3) % len(ALIASES)]
        dims = m["dimensions"]
        out += [
            (f"What was the {p} at {a} in FY{LATEST}?", key, "value"),
            (f"{p} for {b} in {LATEST - 1}", key, "value"),
            (f"What is the {p} in Staten Island in {LATEST}?", key, "value"),
            (f"Which agency had the highest {p} in {LATEST - 1}?", key, "rank"),
            (f"Which borough had the lowest {p} in {LATEST}?", key, "rank"),
            (f"Show the top 5 agencies by {p} in {LATEST}", key, "rank"),
            (f"{p} by work location for {a} in {LATEST}", key, "breakdown"),
            (f"Compare {p} for Parks and Sanitation in {LATEST}", key, "breakdown"),
            (f"How has {p} changed at {b} since {FIRST + 1}?", key, "trend"),
            (f"{p} trend by borough", key, "trend"),
            (f"{p} {LATEST - 2} vs {LATEST} for {a}", key, "trend"),
        ]
        if "pay_basis" in dims:
            out.append((f"{p} by pay basis at Parks in {LATEST}", key, "breakdown"))
            out.append((f"{p} for hourly employees at Parks in {LATEST}", key, "value"))
        if "title" in dims:
            out.append((f"Which job titles had the highest {p} at Correction in {LATEST}?", key, "rank"))
            out.append((f"{p} by job title at Finance in {LATEST}", key, "breakdown"))
        if "tenure_band" in dims:
            out.append((f"{p} by tenure band at Sanitation in {LATEST}", key, "breakdown"))
    return out


QUESTIONS = generated_questions()
IDS = [q for q, _, _ in QUESTIONS]


def _check(con, raw_ref, question, metric, intent):
    ans = ask(con, question, parser="rules")
    assert not ans.refused, f"{question!r} refused: {ans.text}"
    assert ans.spec.metric == metric, f"parsed {ans.spec.metric}, expected {metric}"
    assert ans.spec.intent == intent, f"parsed intent {ans.spec.intent}, expected {intent}"
    check_grounded(ans, raw_ref)
    return ans


@pytest.mark.parametrize("question,metric,intent", QUESTIONS, ids=IDS)
def test_answer_numbers_match_independent_computation_sample(sample_con, sample_ref, question, metric, intent):
    _check(sample_con, sample_ref, question, metric, intent)


@pytest.mark.full_data
@pytest.mark.parametrize("question,metric,intent", QUESTIONS, ids=IDS)
def test_answer_numbers_match_independent_computation_full(full_con, full_ref, question, metric, intent):
    _check(full_con, full_ref, question, metric, intent)


def test_question_bank_covers_every_metric_and_intent():
    covered = {(m, i) for _, m, i in QUESTIONS}
    for key in settings.metric_catalog():
        for intent in ("value", "rank", "breakdown", "trend"):
            assert (key, intent) in covered


def test_suppressed_cells_never_leak_into_text(sample_con, sample_ref):
    """Job-title cells in the sample are mostly below the threshold; none of their values may appear."""
    ans = ask(sample_con, "separations by job title at Finance in 2025", parser="rules")
    check_grounded(ans, sample_ref)
    df = ans.result.df
    assert df["suppressed"].any(), "expected some suppressed title cells in the sample"
    assert df.loc[df["suppressed"], "value"].isna().all()
    assert "suppressed (fewer than 10 employees)" in ans.text


def test_value_question_on_small_group_is_suppressed(sample_con):
    v = Vocabulary.from_warehouse(sample_con)
    ans = ask(sample_con, "What was the headcount at Finance in Staten Island in 2025?", parser="rules", vocab=v)
    assert not ans.refused
    row = ans.result.df.iloc[0]
    assert row["suppressed"]
    assert "is suppressed" in ans.text
    assert [f["field"] for f in ans.facts] == ["min_cell_size"]
