"""Optional LLM parser. No API key is used anywhere in the test suite:
  * stub clients prove the LLM's output is only a query spec that still goes through
    validation, and that its numbers-free output is grounded the same way;
  * one test drives the real `anthropic` SDK against a local fake HTTP server to check
    the request we send (model, structured-output schema, fallbacks, beta header) and
    that we parse a real SDK response object.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from grounding import check_grounded
from wfa.qa import ask, llm
from wfa.qa.vocab import Vocabulary


def spec_payload(**overrides):
    base = {"action": "query", "refusal_category": "none", "metric": "turnover_rate", "intent": "value",
            "group_by": "none", "agencies": ["846"], "locations": [], "pay_basis": [], "fiscal_years": [2025],
            "order": "desc", "top_n": 3}
    base.update(overrides)
    return base


class StubClient:
    def __init__(self, payload=None, stop_reason="end_turn", raise_exc=None):
        self.payload, self.stop_reason, self.raise_exc, self.kwargs = payload, stop_reason, raise_exc, None
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                if outer.raise_exc:
                    raise outer.raise_exc
                text = json.dumps(outer.payload) if isinstance(outer.payload, dict) else outer.payload
                return SimpleNamespace(stop_reason=outer.stop_reason, content=[SimpleNamespace(type="text", text=text)])
        self.beta = SimpleNamespace(messages=_Messages())


def test_llm_spec_is_executed_by_governed_layer_and_grounded(sample_con, sample_ref):
    stub = StubClient(spec_payload(intent="rank", group_by="location", agencies=[], metric="ot_hours_per_worker"))
    ans = ask(sample_con, "where is overtime worst?", parser="llm", llm_client=stub)
    assert ans.parser.startswith("llm:")
    check_grounded(ans, sample_ref)


def test_llm_output_outside_catalog_is_rejected(sample_con):
    stub = StubClient(spec_payload(metric="engagement_score"))
    ans = ask(sample_con, "engagement at parks", parser="llm", llm_client=stub)
    # "engagement" is caught by the deterministic precheck before the LLM is even called
    assert ans.refused and stub.kwargs is None
    stub = StubClient(spec_payload(metric="engagement_score"))
    ans = ask(sample_con, "how are things at parks?", parser="llm", llm_client=stub)
    assert ans.refused and ans.category == "unsupported_metric"


def test_llm_unknown_agency_and_bad_dimension_are_rejected(sample_con):
    ans = ask(sample_con, "turnover at agency 999", parser="llm", llm_client=StubClient(spec_payload(agencies=["999"])))
    assert ans.refused and ans.category == "unknown_entity"
    ans = ask(sample_con, "turnover by tenure", parser="llm",
              llm_client=StubClient(spec_payload(intent="breakdown", group_by="tenure_band", agencies=[])))
    assert ans.refused and ans.category == "unsupported_dimension"


def test_llm_refusal_uses_canned_text_not_model_text(sample_con):
    stub = StubClient(spec_payload(action="refuse", refusal_category="causal", metric="none"))
    ans = ask(sample_con, "turnover story at parks", parser="llm", llm_client=stub)
    assert ans.refused and ans.text == llm.CANNED_REFUSALS["causal"]


@pytest.mark.parametrize("stub", [
    StubClient(raise_exc=__import__("anthropic").APIConnectionError(request=__import__("httpx2").Request("POST", "http://x"))),
    StubClient(payload=spec_payload(), stop_reason="refusal"),
    StubClient(payload="not json"),
])
def test_llm_failure_falls_back_to_rules(sample_con, sample_ref, stub):
    ans = ask(sample_con, "What was the turnover rate at Parks in FY2025?", parser="llm", llm_client=stub)
    assert ans.parser == "rules"
    assert any("LLM parser unavailable" in n for n in ans.notes)
    check_grounded(ans, sample_ref)


def test_no_key_means_rules(sample_con, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ans = ask(sample_con, "What was the turnover rate at Parks in FY2025?")
    assert ans.parser == "rules"
    assert not any("LLM" in n for n in ans.notes)


def test_real_sdk_request_against_local_fake_server(sample_con, sample_ref, monkeypatch):
    seen = {}
    body = {"id": "msg_test", "type": "message", "role": "assistant", "model": llm.DEFAULT_MODEL,
            "content": [{"type": "text", "text": json.dumps(spec_payload(intent="trend", fiscal_years=[2022, 2023, 2024, 2025]))}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["headers"] = dict(self.headers)
            seen["json"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            out = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key="test-key-not-real", base_url=f"http://127.0.0.1:{server.server_port}",
                                     max_retries=0)
        ans = ask(sample_con, "How has turnover at Parks changed since 2022?", parser="llm", llm_client=client)
    finally:
        server.shutdown()

    req = seen["json"]
    assert seen["path"].startswith("/v1/messages")
    assert req["model"] == llm.DEFAULT_MODEL
    assert req["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["headers"].get("anthropic-beta", "")
    fmt = req["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    vocab = Vocabulary.from_warehouse(sample_con)
    assert set(fmt["schema"]["properties"]["metric"]["enum"]) >= {"turnover_rate", "headcount"}
    assert set(fmt["schema"]["properties"]["agencies"]["items"]["enum"]) == set(vocab.agencies)
    assert "never state numbers" in req["system"]
    assert req["messages"] == [{"role": "user", "content": "How has turnover at Parks changed since 2022?"}]
    assert ans.parser == f"llm:{llm.DEFAULT_MODEL}"
    check_grounded(ans, sample_ref)
