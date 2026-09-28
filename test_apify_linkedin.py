"""Offline test for the Apify LinkedIn adapter: fallback rule, normalisation, lookback. No network."""
import sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, ".")
from adapters.apify_linkedin_adapter import ApifyLinkedInAdapter, ACTORS

NOW = datetime.now(timezone.utc)
RECENT = (NOW - timedelta(days=2)).isoformat()
OLD = (NOW - timedelta(days=40)).isoformat()
URL = "https://www.linkedin.com/in/taragentile/"

def test_falls_back_when_first_scraper_breaks():
    calls = []
    def runner(actor_id, actor_input, token, max_charge, timeout):
        calls.append(actor_id)
        if actor_id == ACTORS[0]["id"]:
            raise RuntimeError("run ended with status FAILED")
        return ([{"text": "A real post\nmore", "postUrl": "https://linkedin.com/posts/1", "posted_at": RECENT},
                 {"text": "Old post", "postUrl": "https://linkedin.com/posts/2", "posted_at": OLD}], 0.01)
    r = ApifyLinkedInAdapter(token="x", runner=runner).fetch_profile(URL, "Tara — LinkedIn", 7)
    assert calls == [ACTORS[0]["id"], ACTORS[1]["id"]], calls
    assert len(r["items"]) == 1 and r["items"][0]["title"] == "A real post"
    assert r["stats"]["tool"].endswith(ACTORS[1]["id"]) and r["stats"]["fallbacks"]
    assert abs(r["stats"]["cost_usd"] - 0.01) < 1e-9
    print("  PASS — first scraper failed, second took over, old post dropped, tool + cost recorded")

def test_all_scrapers_fail_never_raises():
    def runner(*a, **k): raise RuntimeError("boom")
    r = ApifyLinkedInAdapter(token="x", runner=runner).fetch_profile(URL, "Tara — LinkedIn", 7)
    assert r["items"] == [] and "all scrapers failed" in r["stats"]["error"]
    assert len(r["stats"]["fallbacks"]) == len(ACTORS)
    print("  PASS — every scraper failing returns an error result, no exception")

def test_unrecognised_output_triggers_fallback():
    def runner(actor_id, *a, **k):
        return ([{"weird": "shape"}], 0.0) if actor_id == ACTORS[0]["id"] else ([{"content": "hi", "url": "u", "date": RECENT}], 0.0)
    r = ApifyLinkedInAdapter(token="x", runner=runner).fetch_profile(URL, "T", 7)
    assert len(r["items"]) == 1
    print("  PASS — unrecognised output format falls through to the next scraper")

if __name__ == "__main__":
    print("Apify LinkedIn adapter — offline tests")
    test_falls_back_when_first_scraper_breaks(); test_all_scrapers_fail_never_raises(); test_unrecognised_output_triggers_fallback()
    print("ALL TESTS PASSED")
