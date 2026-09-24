"""Q&A entry point: question -> guardrails -> parser -> validation -> governed metrics -> answer."""
from __future__ import annotations

import os

from . import guardrails, llm, rules
from .compose import Answer, compose
from .spec import Refusal
from .vocab import Vocabulary


def _refused(question: str, r: Refusal, parser: str, notes: list[str] | None = None) -> Answer:
    return Answer(question=question, text=r.message, refused=True, category=r.category,
                  parser=parser, suggestions=r.suggestions, notes=notes or [])


def ask(con, question: str, parser: str | None = None, llm_client=None, vocab: Vocabulary | None = None) -> Answer:
    """parser: 'rules', 'llm', or 'auto' (LLM if a key is configured, else rules). Default from WFA_PARSER."""
    vocab = vocab or Vocabulary.from_warehouse(con)
    if not question or not question.strip():
        return _refused(question, Refusal("unparsed", "Ask a question about a workforce metric."), "rules")

    pre = guardrails.precheck(question, vocab)
    if pre:
        return _refused(question, pre, "guardrail")

    mode = parser or os.environ.get("WFA_PARSER", "auto")
    notes: list[str] = []
    parsed, used = None, "rules"
    if mode in ("auto", "llm") and llm.available(llm_client):
        try:
            parsed = llm.parse(question, vocab, client=llm_client)
            used = f"llm:{llm.model_name()}"
        except llm.LLMUnavailable as e:
            notes.append(f"LLM parser unavailable ({e}); used the rule-based parser.")
    if parsed is None:
        parsed = rules.parse(question, vocab)

    if isinstance(parsed, Refusal):
        return _refused(question, parsed, used, notes)
    problem = guardrails.validate(parsed, vocab)
    if problem:
        return _refused(question, problem, used, notes)
    answer = compose(con, question, parsed, vocab, parser=used)
    answer.notes = notes + answer.notes
    return answer
