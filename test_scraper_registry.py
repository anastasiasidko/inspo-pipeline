"""Offline tests: platform detection, X scrapers, and finding a scraper for a platform that has none."""
import json, sys, tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, ".")
import adapters.scraper_registry as reg
from adapters.apify_linkedin_adapter import X_ACTORS, handle_from_url

def test_platforms_and_x_input():
    assert reg.platform_of("https://x.com/olgandrienko") == "x" and reg.platform_of("https://twitter.com/a") == "x"
    assert reg.platform_of("https://www.instagram.com/a/") == "instagram" and reg.platform_of("https://valnelson.com") is None
    assert handle_from_url("https://x.com/@Olgandrienko?s=20") == "Olgandrienko"
    assert X_ACTORS[0]["input"]("https://x.com/olga", 30, 7) == {"twitterHandles": ["olga"], "maxItems": 30, "sort": "Latest"}
    print("  PASS — platforms detected; X scraper input built from the profile link")

def test_pick_candidates():
    def item(name, users, price, desc="No login needed", model="PAY_PER_EVENT", dep=False):
        ev = {"post": {"eventPriceUsd": price}, "actor-start": {"eventPriceUsd": 0.5}}
        return {"id": name, "username": "u", "name": name, "title": name, "description": desc, "isDeprecated": dep,
                "stats": {"totalUsers": users}, "actorReviewRating": 4,
                "currentPricingInfo": {"pricingModel": model, "pricingPerEvent": {"actorChargeEvents": ev}}}
    got = reg.pick_candidates([item("cheap-small", 100, 0.001), item("popular", 5000, 0.002),
                               item("dear", 9000, 0.5), item("rental", 9000, 0.001, model="FLAT_PRICE_PER_MONTH"),
                               item("cookies", 9000, 0.001, desc="requires login cookies"), item("dead", 9000, 0.001, dep=True)])
    assert [c["name"] for c in got] == ["popular", "cheap-small"], [c["name"] for c in got]
    print("  PASS — picks popular, cheap, no-login scrapers; skips dear, rental, cookie-based and deprecated ones")

def test_unknown_platform_is_found_saved_and_reported():
    with tempfile.TemporaryDirectory() as d, patch.object(reg, "REGISTRY_FILE", Path(d) / "scrapers.json"):
        calls = []
        def finder(platform, url, token):
            calls.append(platform)
            return ([{"id": "acme/ig", "template": {"usernames": ["{handle}"], "limit": "{max_items}"}, "verified": False,
                      "price_per_item_usd": 0.002, "users": 900}], ["Instagram: no scraper existed yet. Found acme/ig"])
        adapter, notes = reg.get_adapter("instagram", "https://instagram.com/olga", token="t", finder=finder)
        assert adapter is not None and notes and calls == ["instagram"]
        assert reg.has_scraper("instagram") and json.loads((Path(d) / "scrapers.json").read_text())["instagram"]["actors"][0]["verified"] is False
        # second time: already known, no new search, no notice
        adapter2, notes2 = reg.get_adapter("instagram", "https://instagram.com/o", token="t", finder=finder)
        assert notes2 == [] and calls == ["instagram"]
        # the saved template fills in the input
        actor = adapter2.actors[0]
        assert actor["input"]("https://instagram.com/olga", 20, 7) == {"usernames": ["olga"], "limit": 20}
        assert actor["max_charge"] == reg.DISCOVERED_MAX_CHARGE
        # a successful scrape marks it verified
        adapter2.on_success("acme/ig")
        assert json.loads((Path(d) / "scrapers.json").read_text())["instagram"]["actors"][0]["verified"] is True
    print("  PASS — new platform: scraper found once, saved, user notified, capped, marked verified after real data")

def test_nothing_found_returns_a_notice():
    with tempfile.TemporaryDirectory() as d, patch.object(reg, "REGISTRY_FILE", Path(d) / "scrapers.json"):
        adapter, notes = reg.get_adapter("tiktok", "https://tiktok.com/@a", token="t", finder=lambda *a: ([], ["TikTok: none found"]))
        assert adapter is None and notes == ["TikTok: none found"]
    print("  PASS — no suitable scraper: skipped with a notice, run continues")

if __name__ == "__main__":
    print("Scraper registry — offline tests")
    test_platforms_and_x_input(); test_pick_candidates(); test_unknown_platform_is_found_saved_and_reported(); test_nothing_found_returns_a_notice()
    print("ALL TESTS PASSED")
