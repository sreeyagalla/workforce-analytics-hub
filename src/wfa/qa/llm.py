"""Optional LLM parser (Claude via the Anthropic SDK).

Used only when ANTHROPIC_API_KEY is set and the `anthropic` package is
installed. The model's only job is to map the question onto the same QuerySpec
the rule parser produces, using a JSON schema whose enums come from the metric
catalog and warehouse vocabulary. It never sees data, never produces numbers,
and its free text is never shown: refusals use canned messages. The spec it
returns still goes through guardrails.validate() and the governed metric layer.
"""
from __future__ import annotations

import json
import os

from .. import settings
from .spec import INTENTS, REFUSAL_CATEGORIES, QuerySpec, Refusal
from .vocab import Vocabulary

DEFAULT_MODEL = "claude-opus-5"


class LLMUnavailable(RuntimeError):
    pass


def model_name() -> str:
    return os.environ.get("WFA_LLM_MODEL", DEFAULT_MODEL)


def available(client=None) -> bool:
    if client is not None:
        return True
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def build_schema(vocab: Vocabulary) -> dict:
    catalog, dims = settings.metric_catalog(), settings.dimensions()
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["query", "refuse"]},
            "refusal_category": {"type": "string", "enum": ["none", *REFUSAL_CATEGORIES]},
            "metric": {"type": "string", "enum": ["none", *catalog.keys()]},
            "intent": {"type": "string", "enum": list(INTENTS)},
            "group_by": {"type": "string", "enum": ["none", *dims.keys()]},
            "agencies": {"type": "array", "items": {"type": "string", "enum": sorted(vocab.agencies)}},
            "locations": {"type": "array", "items": {"type": "string", "enum": vocab.locations}},
            "pay_basis": {"type": "array", "items": {"type": "string", "enum": vocab.pay_basis}},
            "fiscal_years": {"type": "array", "items": {"type": "integer"}},
            "order": {"type": "string", "enum": ["desc", "asc"]},
            "top_n": {"type": "integer"},
        },
        "required": ["action", "refusal_category", "metric", "intent", "group_by", "agencies", "locations",
                     "pay_basis", "fiscal_years", "order", "top_n"],
        "additionalProperties": False,
    }


def build_system_prompt(vocab: Vocabulary) -> str:
    catalog, dims = settings.metric_catalog(), settings.dimensions()
    metric_lines = "\n".join(
        f"- {k}: {m['label']}. {m['definition']} Breakdowns allowed: {', '.join(m['dimensions'])}."
        for k, m in catalog.items())
    agency_lines = "\n".join(f"- {code}: {name}" for code, name in sorted(vocab.agencies.items(), key=lambda x: x[1]))
    return f"""You translate a workforce analytics question into a structured query over a governed metric catalog.
You do not answer the question and you never state numbers; the application computes every value itself.

Metrics:
{metric_lines}

Breakdown dimensions: {', '.join(f"{k} ({v['label']})" for k, v in dims.items())}.
Agencies (use the code):
{agency_lines}
Work locations: {', '.join(vocab.locations)}.
Pay basis values: {', '.join(vocab.pay_basis)} ("hourly" = per Hour, "salaried" = per Annum).
Fiscal years loaded: FY{vocab.years[0]}-FY{vocab.years[-1]} (July 1 - June 30). "Latest" or "last year" means FY{vocab.years[-1]}.

Intents: value (one number), rank (highest/lowest groups; needs group_by), breakdown (all groups of one dimension),
trend (several fiscal years). For value/rank/breakdown with no year given, use [{vocab.years[-1]}]. For trend with no
years given, use every loaded year. If two agencies are compared, list both and set group_by to agency.

Set action to "refuse" (and metric to "none") when the question asks about specific people or lists of employees
(individual_level), about data this catalog does not contain such as engagement, performance, demographics,
promotions or separation reasons (unsupported_metric), a breakdown the metric does not allow (unsupported_dimension),
why something happened (causal), a year outside the loaded range (out_of_range), an agency not listed
(unknown_entity), or anything you cannot map (unparsed). Otherwise set action to "query" and refusal_category to "none".
Use empty lists for filters that are not mentioned. top_n defaults to 3."""


CANNED_REFUSALS = {
    "individual_level": "I only answer aggregate questions about groups, not about specific people.",
    "unsupported_metric": "That topic is not covered by the governed metric catalog or the source data.",
    "unsupported_dimension": "That metric is not defined for the requested breakdown.",
    "causal": "Payroll records can show what changed and where, but not why.",
    "out_of_range": "That fiscal year is not loaded.",
    "unknown_entity": "That agency or location is not in scope.",
    "unparsed": "I couldn't map that question to a governed metric.",
}


def to_spec(data: dict, vocab: Vocabulary) -> QuerySpec | Refusal:
    if data.get("action") == "refuse" or data.get("metric") in (None, "none"):
        cat = data.get("refusal_category")
        cat = cat if cat in CANNED_REFUSALS else "unparsed"
        return Refusal(cat, CANNED_REFUSALS[cat])
    filters = {k: list(v) for k, v in (("agency", data.get("agencies") or []),
                                       ("location", data.get("locations") or []),
                                       ("pay_basis", data.get("pay_basis") or [])) if v}
    years = sorted({int(y) for y in data.get("fiscal_years") or []}) or [vocab.years[-1]]
    group_by = None if data.get("group_by") in (None, "none") else data["group_by"]
    return QuerySpec(metric=data["metric"], intent=data.get("intent", "value"), group_by=group_by,
                     filters=filters, fiscal_years=years, order=data.get("order", "desc"),
                     top_n=max(1, min(int(data.get("top_n") or 3), 20)),
                     notes=[f"Question parsed by {model_name()}; values computed by the governed metric layer."])


def parse(question: str, vocab: Vocabulary, client=None) -> QuerySpec | Refusal:
    try:
        import anthropic
    except ImportError as e:
        raise LLMUnavailable("anthropic package not installed") from e
    client = client or anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=model_name(),
            max_tokens=4096,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=build_system_prompt(vocab),
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": build_schema(vocab)}},
            messages=[{"role": "user", "content": question}],
        )
    except anthropic.AuthenticationError as e:
        raise LLMUnavailable("authentication failed") from e
    except anthropic.RateLimitError as e:
        raise LLMUnavailable("rate limited") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"API error {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable("network error") from e
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise LLMUnavailable(f"model stopped with {resp.stop_reason}")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMUnavailable("no text block in response")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMUnavailable("response was not valid JSON") from e
    return to_spec(data, vocab)
