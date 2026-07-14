"""Offline tests for the goal-watch restock engine. The live Shopify fetch is validated
against the real Thrive site; here we test URL/size parsing, variant matching, and the
state logic with a canned product (network monkeypatched)."""

from __future__ import annotations

from web_watcher import goalwatch as G
from web_watcher.config import AppConfig

PRODUCT = {
    "title": "7920FR-PRO Utility Pant",
    "variants": [
        {"id": 111, "title": "Navy / 34W / 30L", "available": False, "price": 15900},
        {"id": 222, "title": "Navy / 34W / 32L", "available": True,  "price": 15900},
        {"id": 333, "title": "Navy / 36W / 34L", "available": False, "price": 15900},
    ],
}

PROD_URL = "https://thriveworkwear.com/collections/x/products/utility-7920fr?variant=111"


def test_shopify_handle():
    assert G.shopify_handle(PROD_URL) == "utility-7920fr"
    assert G.shopify_handle("https://example.com/weather") is None


def test_variant_id_from_url():
    assert G._variant_id_from_url(PROD_URL) == 111
    assert G._variant_id_from_url("https://x.com/products/y") is None


def test_size_tokens():
    assert G._size_tokens("34W x 30L") == ["34w", "30l"]
    assert G._size_tokens("Size 34W / 30 L") == ["34w", "30l"]  # tolerates the space


def test_match_variant_by_id_then_size():
    assert G.match_variant(PRODUCT, variant_id=222)["id"] == 222
    assert G.match_variant(PRODUCT, size_text="34W x 30L")["id"] == 111   # matches both tokens
    assert G.match_variant(PRODUCT, size_text="99W x 99L") is None


def test_check_restock_out_of_stock(monkeypatch):
    monkeypatch.setattr(G, "fetch_shopify_product", lambda url, **k: PRODUCT)
    r = G.check_restock(PROD_URL, AppConfig.model_validate({}), size_text="34W x 30L")
    assert r["method"] == "shopify" and r["ok"] is True and r["available"] is False
    assert "out of stock" in r["note"]


def test_check_restock_in_stock(monkeypatch):
    monkeypatch.setattr(G, "fetch_shopify_product", lambda url, **k: PRODUCT)
    r = G.check_restock("https://thriveworkwear.com/products/utility-7920fr",
                        AppConfig.model_validate({}), size_text="34W x 32L")
    assert r["ok"] is True and r["available"] is True and "IN STOCK" in r["note"]


def test_check_restock_size_not_found_lists_available(monkeypatch):
    monkeypatch.setattr(G, "fetch_shopify_product", lambda url, **k: PRODUCT)
    # URL WITHOUT ?variant= so the (bogus) size is what's matched — and it isn't found.
    r = G.check_restock("https://thriveworkwear.com/products/utility-7920fr",
                        AppConfig.model_validate({}), size_text="50W x 50L")
    assert r["ok"] is False and r["available"] is None
    assert "34W / 32L" in r["note"]   # tells the user what IS in stock


def test_url_variant_id_wins_over_size(monkeypatch):
    # A URL ?variant= is the exact target — it wins even if a size hint disagrees.
    monkeypatch.setattr(G, "fetch_shopify_product", lambda url, **k: PRODUCT)
    r = G.check_restock(PROD_URL, AppConfig.model_validate({}), size_text="99W x 99L")
    assert r["ok"] is True and r["variant_title"] == "Navy / 34W / 30L"


def test_check_restock_non_shopify(monkeypatch):
    monkeypatch.setattr(G, "fetch_shopify_product", lambda url, **k: None)
    r = G.check_restock("https://example.com/some-item", AppConfig.model_validate({}))
    assert r["ok"] is False and r["method"] == "unsupported"


def test_goal_check_loop_alerts_on_flip(tmp_path, monkeypatch):
    """The scheduler loop: out-of-stock stays quiet; the flip to in-stock fires once;
    staying in-stock doesn't re-fire; an undetermined check leaves state untouched."""
    from web_watcher import scheduler as SCH
    from web_watcher import goalwatch as G
    from web_watcher import storage as S
    from web_watcher.config import Watch, AppConfig

    db = tmp_path / "t.db"; S.init_db(db)
    cfg = AppConfig.model_validate({})
    w = Watch(id="w1", name="Thrive pants 34x30", urls=["https://thriveworkwear.com/products/x?variant=1"],
              instruction="restock", goal_kind="restock", target_size="34W x 30L", interval_minutes=30)

    def fake(state):
        monkeypatch.setattr(G, "check_restock",
                            lambda url, cfg, **k: {"ok": True, "available": state,
                                                   "variant_title": "Navy / 34W / 30L",
                                                   "price": "$159.00", "note": "note"})

    def last_found():
        h = S.get_history(watch_name=w.name, limit=1, db_path=db)
        return h[0]["found"] if h else None

    fake(False); SCH._run_goal_check(w, cfg, "2026-01-01T00:00:00+00:00", db)
    assert last_found() == 0                                  # out of stock → no alert
    assert S.get_goal_state("w1", db)["available"] is False

    fake(True);  SCH._run_goal_check(w, cfg, "2026-01-01T00:30:00+00:00", db)
    assert last_found() == 1                                  # FLIP → would alert
    assert S.get_goal_state("w1", db)["available"] is True

    fake(True);  SCH._run_goal_check(w, cfg, "2026-01-01T01:00:00+00:00", db)
    assert last_found() == 0                                  # still in stock → no re-alert

    # an undetermined check must NOT wipe the remembered state
    monkeypatch.setattr(G, "check_restock", lambda url, cfg, **k: {"ok": False, "available": None, "note": "network"})
    SCH._run_goal_check(w, cfg, "2026-01-01T01:30:00+00:00", db)
    assert S.get_goal_state("w1", db)["available"] is True
