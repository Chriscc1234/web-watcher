"""The escalatable roles are actually wired to the cloud cascade.

The seam (llm.chat_smart: local first, Claude only on a demonstrated local failure) is tested in
test_llm.py. This file guards the OTHER half — that each rare, consequential call site actually
routes through it under its own role, instead of posting to Ollama directly (which could never
escalate). See llm.ESCALATABLE_ROLES."""

from __future__ import annotations

from web_watcher import llm


def _canned(text: str):
    """A chat_smart stand-in that records the role it was called with and returns fixed text."""
    seen = {}

    def _fake(messages, *, role, **kwargs):
        seen["role"] = role
        seen["messages"] = messages
        return {"text": text, "used": "local", "escalated": False, "why": ""}

    return seen, _fake


def test_comprehend_routes_through_the_cascade_as_comprehend(monkeypatch):
    from web_watcher import comprehend
    seen, fake = _canned('{"site_kind":"marketplace","is_listings_site":true,'
                         '"viable_for_watch":true,"search_box":{"purpose":"keyword-items"}}')
    monkeypatch.setattr(llm, "chat_smart", fake)
    monkeypatch.setattr("web_watcher.inspect.resolve_inspect_model", lambda cfg: "m")
    monkeypatch.setattr(comprehend, "_evidence_block", lambda struct: "evidence")

    u = comprehend.comprehend_from_structure({"x": 1}, cfg=None)
    assert seen["role"] == "comprehend"
    assert u["site_kind"] == "marketplace" and u["viable_for_watch"] is True


def test_vet_routes_through_the_cascade_as_vet(monkeypatch):
    from web_watcher import inspect as I
    seen, fake = _canned('{"deal_quality":4,"scam_risk":"low","deal_reason":"fair"}')
    monkeypatch.setattr(llm, "chat_smart", fake)

    v = I.verdict_from_text("A truck - $8,500", "runs great", "trucks", cfg=None, model="m")
    assert seen["role"] == "vet"
    assert v["deal_quality"] == 4 and v["scam_risk"] == "low"


def test_terms_routes_through_the_cascade_as_terms(monkeypatch):
    from web_watcher import search_terms as ST
    seen, fake = _canned('{"terms":["macgregor","sailboat","venture"]}')
    monkeypatch.setattr(llm, "chat_smart", fake)
    monkeypatch.setattr(ST, "get_term_expansion", lambda intent, db_path=None: None)   # cache miss
    monkeypatch.setattr(ST, "save_term_expansion", lambda *a, **k: None)

    terms = ST.expand_search_terms("macgregor sailboats", "m")
    assert seen["role"] == "terms"
    assert "macgregor" in terms and "sailboat" in terms


def test_every_wired_role_is_a_declared_escalatable_role():
    """A guard against typos: the roles we pass at call sites must be in the allow-list, or
    cloud_ready would silently refuse to escalate them."""
    for role in ("chat", "extract", "terms", "comprehend", "vet", "stuck"):
        assert role in llm.ESCALATABLE_ROLES, role
