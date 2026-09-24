"""The structured query a question is translated into, and the refusal type.

Both parsers (rules and LLM) produce one of these. Neither parser ever produces
a number: numbers only come from executing a QuerySpec through wfa.metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, field

INTENTS = ("value", "rank", "breakdown", "trend")

REFUSAL_CATEGORIES = (
    "individual_level",       # asks about a named person or a list of people
    "unsupported_metric",     # topic has no governed metric / no data in the source
    "unsupported_dimension",  # metric is not defined for the requested breakdown
    "causal",                 # asks "why"; descriptive data cannot establish causes
    "out_of_range",           # fiscal year not loaded
    "unknown_entity",         # agency/location that is not in scope
    "unparsed",               # could not map to any supported metric
)


@dataclass
class QuerySpec:
    metric: str
    intent: str = "value"
    group_by: str | None = None
    filters: dict[str, list[str]] = field(default_factory=dict)
    fiscal_years: list[int] = field(default_factory=list)
    order: str = "desc"
    top_n: int = 3
    notes: list[str] = field(default_factory=list)


@dataclass
class Refusal:
    category: str
    message: str
    suggestions: list[str] = field(default_factory=list)
