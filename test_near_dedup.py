"""Offline test for the near-duplicate step (no network, no AI)."""
import sys
sys.path.insert(0, ".")
from pipeline.dedup import run_dedup_phase
from storage.fake_backend import InMemoryBackend

BASE = ("Burnout is not a personal failure. It is what happens when success arrives faster than the systems "
        "that should carry it, and when the person at the centre never gets to rest. In my work with solopreneurs "
        "I see the same pattern again and again: growth first, structure later, and a body that quietly sends the bill. ")
OTHER = ("Pricing is a story you tell about value. Most freelancers undercharge because they anchor on hours rather "
         "than on the change they create for a client, so the first step is to describe the outcome in the buyer's own "
         "words before any number is ever mentioned in a proposal or a call. ")

def item(url, title, text, h):
    return {"source_url": url, "source_name": "T", "title": title, "text": text, "content_hash": h, "stream": "s",
            "published_date": "2026-09-20T00:00:00+00:00", "fetched_at": "2026-09-20T00:00:00+00:00", "text_complete": True}

def test_near_dups():
    st = InMemoryBackend()
    run = st.start_run("c", "s", "t", [], "typed_only")
    # earlier run already collected BASE
    st.save_collected_items(run, "s", [item("https://a/1", "Burnout post", BASE, "h1")])
    run2 = st.start_run("c", "s", "t", [], "typed_only")
    items = [
        item("https://b/2", "Repost with edits", BASE.replace("quietly", "silently"), "h2"),  # near dup of collected
        item("https://b/3", "Pricing", OTHER, "h3"),                                                                                     # new
        item("https://b/4", "Pricing again (LinkedIn echo)", OTHER + "Thanks for reading.", "h4"),                                        # near dup of item 3, same run
        item("https://b/5", "Tiny", "Starts Monday! Join us.", "h5"),                                                                    # short: kept
        item("https://b/6", "Tiny 2", "Starts Monday! Join us.", "h6"),                                                                  # short + same words: kept (only exact matches count)
    ]
    r = run_dedup_phase(st, "c", "s", run2, items)
    kept = [i["source_url"] for i in r["new_items"]]
    assert kept == ["https://b/3", "https://b/5", "https://b/6"], kept
    assert r["near_duplicates_found"] == 2, r
    print("  PASS — 2 near-duplicates dropped (one vs an earlier run, one within the run); different + short texts kept")

if __name__ == "__main__":
    print("Near-duplicate dedup — offline test"); test_near_dups(); print("ALL TESTS PASSED")
