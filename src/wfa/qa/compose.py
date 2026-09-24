"""Turn an executed QuerySpec into answer text.

Every number in the text is produced through `_Facts.put`, which formats a value
taken from the MetricResult (or from the all-in-scope baseline computed the same
way) and records where it came from. Templates contain no numbers of their own.
The grounding test (tests/test_qa_grounding.py) checks that the numbers in the
text are exactly the recorded facts, and that every fact matches an independent
pandas computation on the same raw data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import metrics, settings
from ..metrics import MetricQuery, fmt, fmt_delta
from .spec import QuerySpec
from .vocab import Vocabulary


@dataclass
class Answer:
    question: str
    text: str
    refused: bool = False
    category: str | None = None
    spec: QuerySpec | None = None
    table: pd.DataFrame | None = None
    sql: str | None = None
    parser: str = "rules"
    notes: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    result: metrics.MetricResult | None = None
    baseline: metrics.MetricResult | None = None
    facts: list[dict] = field(default_factory=list)


class _Facts:
    def __init__(self):
        self.items: list[dict] = []

    def put(self, value, fmt_key: str, *, field: str = "value", source: str = "result", fy=None, group=None) -> str:
        text = fmt(value, fmt_key)
        self.items.append({"source": source, "field": field, "fy": fy, "group": group,
                           "fmt": fmt_key, "value": value, "text": text})
        return text

    def delta(self, a_val, z_val, fmt_key: str, *, fy=None, group=None) -> str:
        if fmt_key == "pct":
            text = fmt_delta(z_val - a_val, "pct")
        else:
            text = f"{(z_val / a_val - 1) * 100:+.1f}%" if a_val else "n/a"
        self.items.append({"source": "result", "field": "delta", "fy": fy, "group": group,
                           "fmt": fmt_key, "value": (a_val, z_val), "text": text})
        return text


def _join(items: list[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def scope_text(spec: QuerySpec, vocab: Vocabulary, facts: _Facts) -> str:
    f = spec.filters
    if f.get("agency"):
        s = _join([vocab.agencies[c] for c in f["agency"]])
    else:
        s = f"all {facts.put(len(vocab.agencies), 'count', field='n_agencies', source='config')} agencies in scope"
    if f.get("location"):
        s += " in " + _join(f["location"])
    if f.get("pay_basis"):
        s += " (" + _join(f["pay_basis"]) + " employees)"
    return s


def _explain(template: str, row, facts: _Facts, fy, group=None) -> str:
    def sub(m):
        return facts.put(row[m.group(1)], m.group(2), field=m.group(1), fy=fy, group=group)
    return re.sub(r"\{(\w+):(\w+)\}", sub, template)


def compose(con, question: str, spec: QuerySpec, vocab: Vocabulary, parser: str = "rules") -> Answer:
    catalog, dims = settings.metric_catalog(), settings.dimensions()
    m = catalog[spec.metric]
    f_key, label = m["format"], m["label"]
    group = [spec.group_by] if spec.group_by else []
    res = metrics.compute(con, MetricQuery(spec.metric, group_by=group, filters=spec.filters,
                                           fiscal_years=spec.fiscal_years))
    facts = _Facts()
    base = None
    df = res.df
    k = res.min_cell_size
    scope = lambda: scope_text(spec, vocab, facts)  # noqa: E731  (each use records its own fact)
    kk = lambda: facts.put(k, "count", field="min_cell_size", source="governance")  # noqa: E731
    sentences: list[str] = []

    if spec.intent == "value":
        fy = spec.fiscal_years[0]
        row = df[df.fiscal_year == fy]
        if row.empty:
            sentences.append(f"There are no records for {scope()} in FY{fy}.")
        else:
            r = row.iloc[0]
            if r["suppressed"]:
                sentences.append(f"{label} for {scope()} in FY{fy} is suppressed: the group has fewer than {kk()} "
                                 f"employees, so it is not shown.")
            elif pd.isna(r["value"]):
                sentences.append(f"{label} for {scope()} in FY{fy} is not available (no prior-year headcount).")
            else:
                sentences.append(f"{label} for {scope()} in FY{fy}: {facts.put(r['value'], f_key, fy=fy)} "
                                 f"({_explain(m['explain'], r, facts, fy)}).")

    elif spec.intent in ("rank", "breakdown"):
        fy = spec.fiscal_years[0]
        g = spec.group_by
        dim_label, dim_plural = dims[g]["label"], dims[g]["plural"]
        cur = df[df.fiscal_year == fy]
        shown = cur[~cur["suppressed"] & cur["value"].notna()]
        ascending = spec.intent == "rank" and spec.order == "asc"
        shown = shown.sort_values(["value", g], ascending=[ascending, True])
        base = metrics.compute(con, MetricQuery(spec.metric, filters=spec.filters, fiscal_years=[fy]))
        b = base.df.iloc[0] if len(base.df) else None
        if shown.empty:
            sentences.append(f"No {dim_label.lower()} groups for {scope()} in FY{fy} are large enough to report.")
        elif spec.intent == "rank":
            top = shown.head(spec.top_n)
            first = top.iloc[0]
            word = "highest" if spec.order == "desc" else "lowest"
            sentences.append(f"In FY{fy}, {first[g]} had the {word} {label.lower()} among {dim_plural} ({scope()}): "
                             f"{facts.put(first['value'], f_key, fy=fy, group=first[g])}.")
            if len(top) > 1:
                rest = ", ".join(f"{r[g]} ({facts.put(r['value'], f_key, fy=fy, group=r[g])})"
                                 for _, r in top.iloc[1:].iterrows())
                sentences.append(f"Next: {rest}.")
        else:
            limit = 12
            parts = [f"{r[g]} {facts.put(r['value'], f_key, fy=fy, group=r[g])}" for _, r in shown.head(limit).iterrows()]
            more = ""
            if len(shown) > limit:
                more = f"; plus {facts.put(len(shown) - limit, 'count', field='n_more', fy=fy)} more in the table"
            sentences.append(f"{label} by {dim_label.lower()} for {scope()}, FY{fy}: {'; '.join(parts)}{more}.")
        if b is not None and not b["suppressed"] and pd.notna(b["value"]):
            sentences.append(f"Overall for {scope()}: {facts.put(b['value'], f_key, source='baseline', fy=fy)}.")
        n_supp = int(cur["suppressed"].sum())
        if n_supp:
            sentences.append(f"{facts.put(n_supp, 'count', field='n_suppressed', fy=fy)} {dim_label.lower()} "
                             f"group{'s' if n_supp != 1 else ''} suppressed (fewer than {kk()} employees).")

    elif spec.intent == "trend":
        if spec.group_by:
            g = spec.group_by
            dim_label = dims[g]["label"]
            lines = []
            n_supp = 0
            for name, gdf in df.groupby(g, sort=True):
                ok = gdf[~gdf["suppressed"] & gdf["value"].notna()].sort_values("fiscal_year")
                n_supp += int(gdf["suppressed"].any())
                if len(ok) >= 2:
                    a, z = ok.iloc[0], ok.iloc[-1]
                    lines.append((z["value"] - a["value"], name, a, z))
            lines.sort(key=lambda x: (-x[0] if not np.isnan(x[0]) else 0, x[1]))
            texts = []
            for _, name, a, z in lines[:16]:
                fa, fz = int(a.fiscal_year), int(z.fiscal_year)
                texts.append(f"{name}: FY{fa} {facts.put(a['value'], f_key, fy=fa, group=name)} -> "
                             f"FY{fz} {facts.put(z['value'], f_key, fy=fz, group=name)} "
                             f"({facts.delta(a['value'], z['value'], f_key, fy=(fa, fz), group=name)})")
            sentences.append(f"{label} by {dim_label.lower()} for {scope()}: " + "; ".join(texts) + "."
                             if texts else f"Not enough reportable years for {scope()}.")
            if n_supp:
                sentences.append(f"{facts.put(n_supp, 'count', field='n_suppressed')} {dim_label.lower()} "
                                 f"group{'s' if n_supp != 1 else ''} had suppressed years (fewer than {kk()} employees).")
        else:
            ok = df[~df["suppressed"] & df["value"].notna()].sort_values("fiscal_year")
            if ok.empty:
                sentences.append(f"No reportable values for {scope()}.")
            else:
                series = ", ".join(f"FY{int(r.fiscal_year)} {facts.put(r['value'], f_key, fy=int(r.fiscal_year))}"
                                   for _, r in ok.iterrows())
                sentences.append(f"{label} for {scope()}: {series}.")
                if len(ok) >= 2:
                    a, z = ok.iloc[0], ok.iloc[-1]
                    fa, fz = int(a.fiscal_year), int(z.fiscal_year)
                    sentences.append(f"Change FY{fa} to FY{fz}: {facts.delta(a['value'], z['value'], f_key, fy=(fa, fz))}.")
            if df["suppressed"].any():
                sentences.append(f"Some years are suppressed (fewer than {kk()} employees).")

    table = df.drop(columns=["suppressed"]).copy() if len(df) else df
    return Answer(question=question, text=" ".join(sentences), spec=spec, table=table, sql=res.sql,
                  parser=parser, notes=list(spec.notes), result=res, baseline=base, facts=facts.items)
