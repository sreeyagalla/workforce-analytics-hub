"""Rule-based parser: question text -> QuerySpec (or Refusal). No API key needed.

It recognizes metric synonyms from config/metrics.yml, agencies/locations/pay
basis from the warehouse vocabulary, fiscal years, a breakdown dimension, and a
question type (value, rank, breakdown, trend). Anything it cannot map is refused.
"""
from __future__ import annotations

import re

from .. import settings
from .spec import QuerySpec, Refusal
from .vocab import Vocabulary, find_all

_I = re.I
WORD_NUM = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}

GROUP_PATTERNS = [
    ("tenure_band", r"\b(?:by|per|across|each)\s+(?:tenure(?:\s+bands?)?|length of service|years of service|seniority)\b|\btenure bands?\b"),
    ("pay_basis", r"\b(?:by|per|across)\s+(?:pay basis|pay type|employment type)\b"
                  r"|\bhourly\s+(?:vs\.?|versus|and|or|compared (?:to|with))\s+salaried\b"
                  r"|\bsalaried\s+(?:vs\.?|versus|and|or|compared (?:to|with))\s+hourly\b"),
    ("title", r"\b(?:by|per|across|each)\s+(?:job\s+)?titles?\b|\b(?:which|what)\s+(?:job\s+)?titles?\b|\bjob titles\b"),
    ("location", r"\b(?:by|per|across|each|every)\s+(?:work\s+)?(?:boroughs?|locations?|sites?)\b"
                 r"|\b(?:which|what)\s+(?:boroughs?|locations?|sites?)\b|\bcompare\s+(?:the\s+)?(?:boroughs|locations)\b"),
    ("agency", r"\b(?:by|per|across|each|every)\s+(?:agency|agencies|department|departments)\b"
               r"|\b(?:which|what)\s+(?:agency|agencies|department|departments)\b"
               r"|\bcompare\s+(?:the\s+)?(?:agencies|departments)\b"),
]
RANK_DESC = r"\b(?:highest|most|top|largest|biggest|greatest|maximum|max)\b"
RANK_ASC = r"\b(?:lowest|least|fewest|smallest|minimum|min|bottom)\b"
TREND = (r"\b(?:trends?|trending|over time|over the years|each year|every year|by year|per year|year over year|yoy|"
         r"since|history|historical|changed?|changes|increase[sd]?|decrease[sd]?|grow|grew|growth|declined?|trajectory)\b")
TOP_N = r"\b(?:top|bottom)\s+(\d{1,2})\b"

RANGE = r"\b(?:from|between)\s+(?:fy\s*)?((?:19|20)\d{2})\s+(?:to|and|through|until|-)\s+(?:fy\s*)?((?:19|20)\d{2})\b"
DASH_RANGE = r"\b(?:fy\s*)?((?:19|20)\d{2})\s*(?:-|–|through)\s*(?:fy\s*)?((?:19|20)\d{2})\b"
SINCE = r"\b(?:since|after|starting(?:\s+in)?)\s+(?:fy\s*)?((?:19|20)\d{2})\b"
LAST_N = r"\b(?:last|past|previous)\s+(\d+|two|three|four|five|six)\s+(?:fiscal\s+)?years\b"
LATEST = r"\b(?:last year|latest|most recent|this year|current year)\b"
YEAR = r"\b(?:fy\s*'?|fiscal(?:\s+year)?\s+)?((?:19|20)\d{2})\b"
FY2 = r"\bfy\s*'?(\d{2})\b"


def _metric_terms():
    terms = {}
    for key, m in settings.metric_catalog().items():
        for s in m.get("synonyms", []) + [m["label"], key.replace("_", " ")]:
            terms[s.lower()] = key
    extra = {"how many employees left": "separations", "how many people left": "separations",
             "how many quit": "separations", "quit": "separations", "quits": "separations"}
    terms.update(extra)
    items = sorted(terms.items(), key=lambda kv: -len(kv[0]))
    return [(re.compile(rf"(?<![\w-]){re.escape(t)}(?![\w-])", _I), t, k) for t, k in items]


def _blank(text: str, spans) -> str:
    chars = list(text)
    for s, e in spans:
        chars[s:e] = " " * (e - s)
    return "".join(chars)


def _years(q: str, vocab: Vocabulary) -> tuple[list[int], list[str]]:
    notes = []
    lo, hi = vocab.years[0], vocab.years[-1]
    if m := re.search(RANGE, q, _I) or re.search(DASH_RANGE, q, _I):
        a, b = sorted((int(m.group(1)), int(m.group(2))))
        return list(range(a, b + 1)), notes
    if m := re.search(SINCE, q, _I):
        return list(range(int(m.group(1)), hi + 1)), notes
    if m := re.search(LAST_N, q, _I):
        n = int(WORD_NUM.get(m.group(1).lower(), m.group(1)))
        years = [y for y in range(hi - n + 1, hi + 1) if y >= lo]
        if len(years) < n:
            notes.append(f"Only FY{lo}-FY{hi} are loaded.")
        return years, notes
    years = [int(y) for y in re.findall(YEAR, q, _I)] + [2000 + int(y) for y in re.findall(FY2, q, _I)]
    if years:
        return sorted(set(years)), notes
    if re.search(LATEST, q, _I):
        return [hi], notes
    return [], notes


def parse(question: str, vocab: Vocabulary) -> QuerySpec | Refusal:
    q = " ".join(question.strip().split())
    notes: list[str] = []
    catalog = settings.metric_catalog()

    # 1. entities (then blank them so their words are not read as metrics)
    agency_hits = find_all(q, vocab.agency_terms)
    location_hits = find_all(q, vocab.location_terms)
    work = _blank(q, [(s, e) for s, e, _ in agency_hits + location_hits])
    group_by = None
    group_span = None
    for dim, pat in GROUP_PATTERNS:
        if m := re.search(pat, work, _I):
            group_by, group_span = dim, (m.start(), m.end())
            break
    pay_hits = [] if group_by == "pay_basis" else find_all(work, vocab.pay_terms)
    blanked = _blank(work, ([group_span] if group_span else []) + [(s, e) for s, e, _ in pay_hits])

    filters: dict[str, list[str]] = {}
    for dim, hits in (("agency", agency_hits), ("location", location_hits), ("pay_basis", pay_hits)):
        vals = list(dict.fromkeys(v for _, _, v in hits))
        if vals:
            filters[dim] = vals

    if not agency_hits and (m := re.search(r"\b(?:department|dept\.?|office|board|bureau|commission)\s+(?:of|for)\s+[\w&' ]+", q, _I)):
        return Refusal("unknown_entity",
                       f"'{m.group(0).strip()}' is not one of the {len(vocab.agencies)} agencies in scope: "
                       f"{', '.join(sorted(vocab.agencies.values()))}.",
                       ["What was the turnover rate at Parks in 2025?"])

    # 2. metric (earliest non-overlapping synonym)
    metric_hits = find_all(blanked, _metric_terms())
    if not metric_hits:
        labels = ", ".join(m["label"].lower() for m in catalog.values())
        return Refusal("unparsed", f"I couldn't match that to a governed metric. I can answer questions about: {labels}.",
                       ["What was the turnover rate at Parks in FY2025?",
                        "Which agency had the highest overtime hours per worker in 2025?",
                        "How has headcount changed since 2021?"])
    metric = metric_hits[0][2]
    chosen_syns = [x.lower() for x in catalog[metric].get("synonyms", [])]
    others = [k for s_, e_, k in metric_hits[1:]
              if k != metric and not any(blanked[s_:e_].lower() in syn for syn in chosen_syns)]
    if others:
        notes.append(f"Answered {catalog[metric]['label'].lower()} only; ask separately about "
                     f"{', '.join(dict.fromkeys(catalog[o]['label'].lower() for o in others))}.")
    surface = q[metric_hits[0][0]:metric_hits[0][1]].lower()
    if surface == "retention":
        notes.append("Retention is reported through the governed turnover rate.")
    if surface in ("quit", "quits", "how many quit"):
        notes.append("The source does not record separation reasons, so this counts all separations, not only quits.")
    if metric == "median_base_salary" and re.search(r"\b(?:average|mean)\b", q, _I):
        notes.append("Salary is reported as the governed median base salary for salaried employees.")

    # 3. years
    years, ynotes = _years(q, vocab)
    notes += ynotes
    bad = [y for y in years if y not in vocab.years]
    if bad:
        return Refusal("out_of_range", f"FY{bad[0]} is not loaded. Data covers FY{vocab.years[0]}-FY{vocab.years[-1]}.",
                       [f"What was the turnover rate in FY{vocab.years[-1]}?"])

    # 4. intent
    text_for_rank = re.sub(r"\bmost recent\b", " ", q, flags=_I)
    polarity = catalog[metric]["polarity"]
    order = None
    if re.search(RANK_DESC, text_for_rank, _I):
        order = "desc"
    elif re.search(RANK_ASC, text_for_rank, _I):
        order = "asc"
    elif re.search(r"\bworst\b", q, _I):
        order = "desc" if polarity == "higher_is_worse" else "asc"
    elif re.search(r"\bbest\b", q, _I):
        order = "asc" if polarity == "higher_is_worse" else "desc"
    top_n = int(m.group(1)) if (m := re.search(TOP_N, q, _I)) else 3

    multi_dim = next((d for d, v in filters.items() if len(v) > 1), None)
    if order:
        intent = "rank"
        if not group_by:
            group_by = multi_dim or "agency"
            if not multi_dim:
                notes.append("Ranked across agencies.")
    elif re.search(TREND, q, _I) or len(years) > 1:
        intent = "trend"
        group_by = group_by or multi_dim
    elif group_by or multi_dim:
        intent = "breakdown"
        group_by = group_by or multi_dim
    else:
        intent = "value"

    if group_by and len(filters.get(group_by, [])) == 1 and group_by != multi_dim:
        filters.pop(group_by)  # e.g. "which borough ... in Brooklyn": the breakdown wins

    latest = vocab.years[-1]
    if intent == "trend":
        if len(years) == 1:
            years = [y for y in (years[0] - 1, years[0]) if y in vocab.years]
            notes.append("Compared with the prior fiscal year.")
        elif not years:
            years = list(vocab.years)
        if catalog[metric]["type"] == "turnover" and years[0] == vocab.years[0]:
            years = years[1:]
            notes.append(f"Turnover starts in FY{vocab.years[1]} (it needs prior-year headcount).")
    else:
        if not years:
            years = [latest]
            notes.append(f"Assumed FY{latest}, the latest fiscal year loaded.")
        elif len(years) > 1:
            notes.append(f"Used FY{years[-1]}, the last year mentioned.")
            years = [years[-1]]

    return QuerySpec(metric=metric, intent=intent, group_by=group_by, filters=filters,
                     fiscal_years=years, order=order or "desc", top_n=max(1, min(top_n, 20)), notes=notes)
