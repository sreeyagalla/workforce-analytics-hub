"""Shared grounding assertions for Q&A answers.

check_grounded(answer, raw_ref) proves three things for one answer:
  1. The full result table the answer was built from equals an independent
     pandas computation (tests/reference.py) on the same raw rows.
  2. The numbers appearing in the answer text are exactly the numbers the
     composer recorded as facts (nothing else; nothing typed into a template).
  3. Every recorded fact equals the value computed independently for the same
     metric, fiscal year, group, and field (and rank order matches).
Numbers inside group labels (e.g. the tenure band "1-3 yrs", or a job title
that contains a date) and FY tokens are removed before extracting numbers:
they are names read from the data, not reported values.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

import reference
from wfa.metrics import fmt

NUM_RE = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?")


def norm(token: str) -> str:
    return token.lstrip("+-").replace("$", "").replace(",", "").rstrip(".")


def numbers_in(text: str, labels) -> list[str]:
    t = re.sub(r"\bFY\d{4}\b", " ", text)
    for lab in sorted({str(x) for x in labels if isinstance(x, str)}, key=len, reverse=True):
        t = t.replace(lab, " ")
    return sorted(norm(x) for x in NUM_RE.findall(t))


def _delta_text(a, z, f_key):
    if f_key == "pct":
        d = z - a
        return f"{'+' if d >= 0 else '-'}{abs(d) * 100:.1f} pts"
    return f"{(z / a - 1) * 100:+.1f}%"


def _key_frame(df: pd.DataFrame, group: str | None) -> pd.DataFrame:
    df = df.copy()
    if group:
        df[group] = df[group].astype(object).where(df[group].notna(), reference.NULL)
        return df.set_index(["fiscal_year", group]).sort_index()
    return df.set_index(["fiscal_year"]).sort_index()


def check_grounded(ans, raw_ref: pd.DataFrame) -> None:
    assert not ans.refused, f"unexpectedly refused: {ans.text}"
    spec = ans.spec
    g = spec.group_by
    ref = _key_frame(reference.compute(raw_ref, spec.metric, g, spec.filters, spec.fiscal_years), g)
    got = _key_frame(ans.result.df, g)

    # 1. whole result table == independent computation
    assert list(got.index) == list(ref.index), "different set of (year, group) cells"
    assert (got["suppressed"].values == ref["suppressed"].values).all(), "suppression flags differ"
    for col in ["value", "numerator", "denominator", "population"]:
        np.testing.assert_allclose(got[col].astype(float).values, ref[col].astype(float).values,
                                   rtol=1e-9, atol=1e-9, equal_nan=True, err_msg=col)

    # 2. numbers in text == recorded facts
    labels = set(vocab_labels(ans)) | {str(i[-1]) for i in got.index if isinstance(i, tuple)}
    fact_numbers = sorted(n for f in ans.facts for n in numbers_in(f["text"], []))
    assert numbers_in(ans.text, labels) == fact_numbers, ans.text

    # 3. each fact == independent value
    base = None
    if spec.intent in ("rank", "breakdown"):
        base = reference.compute(raw_ref, spec.metric, None, spec.filters, spec.fiscal_years).set_index("fiscal_year")
    value_facts = []
    for f in ans.facts:
        if f["source"] == "result" and f["field"] in ("value", "numerator", "denominator", "population"):
            key = (f["fy"], f["group"]) if g else f["fy"]
            assert fmt(ref.loc[key, f["field"]], f["fmt"]) == f["text"], f
            if f["field"] == "value":
                value_facts.append(f)
        elif f["source"] == "result" and f["field"] == "delta":
            fa, fz = f["fy"]
            ka, kz = ((fa, f["group"]), (fz, f["group"])) if g else (fa, fz)
            assert _delta_text(ref.loc[ka, "value"], ref.loc[kz, "value"], f["fmt"]) == f["text"], f
        elif f["source"] == "baseline":
            assert fmt(base.loc[f["fy"], "value"], f["fmt"]) == f["text"], f
        elif f["field"] == "n_suppressed":
            if spec.intent == "trend":
                expected = int(ref.groupby(level=1)["suppressed"].any().sum())
            else:
                expected = int(ref.loc[f["fy"]]["suppressed"].sum())
            assert f["value"] == expected, f
        elif f["field"] == "n_agencies":
            assert f["value"] == raw_ref["agency_code"].nunique()
        elif f["field"] == "min_cell_size":
            assert f["value"] == reference.K
        elif f["field"] == "n_more":
            cur = ref.loc[f["fy"]]
            assert f["value"] == int((~cur["suppressed"] & cur["value"].notna()).sum()) - 12
        else:
            raise AssertionError(f"unverified fact {f}")

    # rank order: the groups named, in order, are the reference's top N
    if spec.intent == "rank":
        cur = ref.loc[spec.fiscal_years[0]]
        cur = cur[~cur["suppressed"] & cur["value"].notna()].reset_index()
        cur = cur.sort_values(["value", g], ascending=[spec.order == "asc", True])
        assert [f["group"] for f in value_facts] == list(cur[g].head(spec.top_n)), "rank order differs"


def vocab_labels(ans):
    df = ans.result.df
    if ans.spec.group_by:
        return [x for x in df[ans.spec.group_by].dropna().unique()]
    return []


def allowed_refusal_numbers(question: str, years, k, n_agencies) -> set[str]:
    """A refusal may only mention loaded years (and the year before the first, for the turnover
    baseline), the suppression threshold, the number of agencies in scope, or numbers the user typed."""
    out = {str(y) for y in years} | {str(min(years) - 1), str(k), str(n_agencies)}
    return out | {norm(x) for x in NUM_RE.findall(question)}
