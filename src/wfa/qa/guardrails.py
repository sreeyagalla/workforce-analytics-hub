"""Deterministic guardrails.

`precheck` runs on the raw question before any parser (rules or LLM) sees it:
individual-level, out-of-scope-topic, and causal questions are refused here, so
no parser can be talked into answering them. `validate` runs on the parsed
QuerySpec and rejects anything the governed metric layer does not support.
"""
from __future__ import annotations

import re

from .. import settings
from .spec import QuerySpec, Refusal
from .vocab import Vocabulary

_I = re.I

INDIVIDUAL_PATTERNS = [
    r"\b(?:which|what)\s+(?:specific\s+)?(?:employees?|people|persons?|workers?|staff members?|individuals?)\b",
    r"\blist\s+(?:of\s+)?(?:the\s+|all\s+)?(?:employees|people|workers|names|staff)\b",
    r"\bnames?\s+of\b",
    r"\bwho\s+(?:quit|left|resigned|got fired|was fired|were fired|earns?|makes?|is paid|are paid|will|might|is likely)\b",
    r"\b(?:employee|staff|worker)\s+(?:ids?|numbers?|records?|files?)\b",
    r"\b(?:ssn|social security|home address(?:es)?|date of birth|dob|phone numbers?|email address(?:es)?)\b",
    r"\b(?:flight risk|likely to (?:quit|leave)|at risk of leaving|going to (?:quit|leave))\b",
    r"\bindividuals\b",
    r"\bindividual(?:-level)?\s+(?:employees?|people|persons?|workers?|staff|salar(?:y|ies)|records?|pay|level)\b",
    r"\b(?:specific|particular)\s+(?:employee|person|people|worker)s?\b",
]
# Case-sensitive on the name itself (two capitalized words); the surrounding words are not.
NAME_PATTERNS = [
    r"\b([A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-z]+)'s\b",
    r"\b(?i:salary|pay|tenure|overtime)\s+(?i:of|for)\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\b",
    r"\b(?i:how much (?:does|did))\s+([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(?i:make|earn|get)\b",
]

UNSUPPORTED_TOPICS = [
    (r"\b(?:engagement|engaged|satisfaction|survey|enps|morale|sentiment)\b",
     "engagement or survey results", "The source is a payroll extract; it has no survey data."),
    (r"\b(?:performance|high performers?|low performers?|ratings?|reviews?)\b",
     "performance ratings", "The source has no performance data."),
    (r"\b(?:promotions?|promoted|internal mobility)\b",
     "promotions", "Records are not linked across years or titles, so moves cannot be tracked."),
    (r"\b(?:gender|sex|male|female|women|men|race|racial|ethnicity|ethnic|diversity|dei|age|ages|age groups?|older workers|younger workers|disability|disabilities|veterans?)\b",
     "demographic characteristics", "The source has no demographic fields."),
    (r"\b(?:absenteeism|absences?|sick leave|sick days|pto|vacation)\b",
     "absence or leave usage", "The source has no leave-usage data."),
    (r"\b(?:vacanc(?:y|ies)|job openings|open positions|requisitions?|time to fill|time-to-fill|applicants?|candidates?)\b",
     "recruiting or vacancy data", "The source only covers people on payroll."),
    (r"\b(?:voluntary|involuntary|resign(?:ed|ation|ations)?|fired|laid off|layoffs?|retire(?:d|ment|ments)?|reasons? for leaving)\b",
     "separation reasons", "The source does not record why people left. Ask about separations or turnover (all reasons) instead."),
    (r"\b(?:remote|hybrid|telework|work from home)\b",
     "work arrangements", "The source has no work-arrangement data."),
    (r"\b(?:forecast|predict(?:ion|ed)?|projections?|projected|next year)\b",
     "projections", "The Q&A answers from observed data only. A backtested separations outlook is on the Planning tab."),
]

CAUSAL_PATTERNS = [
    r"^\s*why\b", r"\bwhy\s+(?:is|are|did|does|do|has|have|was|were)\b",
    r"\bwhat\s+(?:caused|causes|is causing|drove|drives|is driving)\b",
    r"\breasons?\s+(?:for|behind|why)\b", r"\bbecause of\b", r"\b(?:impact|effect)\s+of\b",
]

SUPPORTED_EXAMPLES = [
    "What was the turnover rate at Parks in FY2025?",
    "Which agency had the highest new-hire attrition in 2024?",
    "Turnover by work location for Sanitation in 2025",
    "How has overtime per worker changed at Correction since 2021?",
    "Compare hourly vs salaried turnover at Parks in 2025",
]


def _supported_metrics_text() -> str:
    return ", ".join(m["label"].lower() for m in settings.metric_catalog().values())


def _mask_known_terms(question: str, vocab: Vocabulary) -> str:
    """Blank out agency/location names so e.g. "Staten Island's" is not read as a person's name."""
    out = question
    for terms in (vocab.agency_terms, vocab.location_terms):
        for pat, _, _ in terms:
            out = pat.sub(lambda m: " " * len(m.group(0)), out)
    return out


def precheck(question: str, vocab: Vocabulary) -> Refusal | None:
    q = question.strip()
    k = settings.governance()["privacy"]["min_cell_size"]
    individual_msg = (f"I only answer aggregate questions. Names and employee IDs are never loaded, and any group "
                      f"with fewer than {k} employees is suppressed, so I can't answer questions about specific "
                      f"people or produce lists of employees.")
    masked = _mask_known_terms(q, vocab)
    if any(re.search(p, masked, _I) for p in INDIVIDUAL_PATTERNS) or any(re.search(p, masked) for p in NAME_PATTERNS):
        return Refusal("individual_level", individual_msg,
                       ["What was the turnover rate at Sanitation in 2025?", "Median base salary by agency in 2025"])
    for pat, topic, why in UNSUPPORTED_TOPICS:
        if re.search(pat, masked, _I):
            return Refusal("unsupported_metric",
                           f"I can't answer questions about {topic}. {why} Governed metrics available: "
                           f"{_supported_metrics_text()}.", SUPPORTED_EXAMPLES[:3])
    if any(re.search(p, q, _I) for p in CAUSAL_PATTERNS):
        return Refusal("causal",
                       "I can show what the data says, but payroll records can't establish why something "
                       "happened. A breakdown can show where a change is concentrated.",
                       ["Turnover by work location for Correction in 2024",
                        "Compare hourly vs salaried turnover at Parks in 2025",
                        "How has turnover changed by agency since 2022?"])
    return None


def validate(spec: QuerySpec, vocab: Vocabulary) -> Refusal | None:
    catalog, dims = settings.metric_catalog(), settings.dimensions()
    if spec.metric not in catalog:
        return Refusal("unsupported_metric", f"'{spec.metric}' is not a governed metric. Available: "
                       f"{_supported_metrics_text()}.", SUPPORTED_EXAMPLES[:3])
    m = catalog[spec.metric]
    if spec.intent not in ("value", "rank", "breakdown", "trend"):
        return Refusal("unparsed", f"Unsupported question type '{spec.intent}'.", SUPPORTED_EXAMPLES[:3])
    why_not = m.get("not_defined_by", {})
    if spec.group_by and spec.group_by not in m["dimensions"]:
        dim_label = dims.get(spec.group_by, {}).get("label", spec.group_by).lower()
        alt = [k for k, v in catalog.items() if spec.group_by in v["dimensions"] and k != spec.metric][:2]
        reason = f": {why_not[spec.group_by]}" if spec.group_by in why_not else ""
        return Refusal("unsupported_dimension", f"{m['label']} is not defined by {dim_label}{reason}.",
                       [f"{catalog[a]['label']} by {dim_label} in {vocab.years[-1]}" for a in alt])
    for dim, values in spec.filters.items():
        if not values:
            continue
        if dim not in dims:
            return Refusal("unsupported_dimension", f"Cannot filter on '{dim}'.", SUPPORTED_EXAMPLES[:2])
        if dim not in m["dimensions"]:
            reason = f": {why_not[dim]}" if dim in why_not else ""
            return Refusal("unsupported_dimension",
                           f"{m['label']} can't be filtered by {dims[dim]['label'].lower()}{reason}.",
                           SUPPORTED_EXAMPLES[:2])
        known = {"agency": set(vocab.agencies), "location": set(vocab.locations),
                 "pay_basis": set(vocab.pay_basis)}.get(dim)
        if known is not None and not set(values) <= known:
            return Refusal("unknown_entity", f"Unknown {dims[dim]['label'].lower()}: {sorted(set(values) - known)}.",
                           SUPPORTED_EXAMPLES[:2])
    lo, hi = vocab.years[0], vocab.years[-1]
    bad = [y for y in spec.fiscal_years if y not in vocab.years]
    if bad:
        return Refusal("out_of_range", f"FY{bad[0]} is not loaded. Data covers FY{lo}-FY{hi}.",
                       [f"What was the turnover rate in FY{hi}?"])
    if m["type"] == "turnover" and spec.fiscal_years and all(y == lo for y in spec.fiscal_years):
        return Refusal("out_of_range",
                       f"Turnover for FY{lo} needs FY{lo - 1} headcount, which is not loaded. "
                       f"Turnover is available FY{lo + 1}-FY{hi}.", [f"What was the turnover rate in FY{lo + 1}?"])
    return None
