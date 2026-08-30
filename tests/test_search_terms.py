"""Search-term expansion must not turn an instruction's LOCATION/CONSTRAINT fragments into search
terms. A live MacGregor watch got URLs for "any model", "within 300 miles", "near Anacortes" — each
a literal search that pulls in every random boat mentioning those words. See search_terms."""

from __future__ import annotations

from web_watcher import search_terms as ST
from web_watcher.search_terms import _is_junk_term, expand_search_terms


def test_location_and_constraint_fragments_are_junk():
    for junk in ["any model", "within 300 miles", "near Anacortes", "300 miles", "radius 150",
                 "no price limit", "under $5000", "over $10000", "nearby", "any make", "no limit"]:
        assert _is_junk_term(junk) is True, junk


def test_real_item_terms_survive():
    for good in ["MacGregor sailboat", "MacGregor 26", "sailboat", "Miata", "sofa",
                 "26 foot boat", "aluminum fishing boat", "Boston Whaler", "Corvette"]:
        assert _is_junk_term(good) is False, good


def test_cached_junk_is_scrubbed_on_read(monkeypatch):
    """A watch already carrying junk in its cache self-heals — the junk is filtered on read."""
    monkeypatch.setattr(ST, "get_term_expansion",
                        lambda intent, db_path=None: ["MacGregor sailboat", "any model", "near Anacortes"])
    assert expand_search_terms("macgregor sailboats", "m") == ["MacGregor sailboat"]


def test_generated_junk_is_dropped(monkeypatch):
    from web_watcher import llm
    monkeypatch.setattr(ST, "get_term_expansion", lambda intent, db_path=None: None)   # cache miss
    monkeypatch.setattr(ST, "save_term_expansion", lambda *a, **k: None)
    monkeypatch.setattr(llm, "chat_smart", lambda *a, **k: {
        "text": '{"terms":["MacGregor sailboat","within 300 miles","MacGregor 26","near Anacortes"]}'})
    out = expand_search_terms("macgregor sailboats", "m")
    assert "MacGregor sailboat" in out and "MacGregor 26" in out
    assert "within 300 miles" not in out and "near Anacortes" not in out


# ── modifier variants are junk only next to their sibling ───────────────────────

def test_modifier_variants_collapse_into_the_real_term():
    """Charlie's expansion, verbatim: three dressed-up Sailrites next to the real term.
    "Seattle Sailrite" is a city typed as a keyword — the same bot-tell the location-box
    guard exists for."""
    from web_watcher.search_terms import _drop_modifier_variants
    got = _drop_modifier_variants(["Sailrite sewing machine", "affordable Sailrite",
                                   "northwest Sailrite", "Seattle Sailrite"])
    assert got == ["Sailrite sewing machine"]


def test_brands_that_look_like_modifiers_survive():
    """"Western Flyer" is a bicycle brand; a bare compass rule would eat it. Junk is only
    junk RELATIVE TO A SIBLING that already carries the item."""
    from web_watcher.search_terms import _drop_modifier_variants
    terms = ["western flyer bicycle", "vintage bicycle"]
    assert _drop_modifier_variants(terms) == terms
    # And a lone term is never eaten, whatever it starts with.
    assert _drop_modifier_variants(["Seattle Sailrite"]) == ["Seattle Sailrite"]
