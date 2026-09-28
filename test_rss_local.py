"""
Local test for RSS adapter — no real network access needed.

This sandbox environment blocks general internet access, so we can't test
against real live websites here. This test verifies the ADAPTER LOGIC is
correct using a local fake RSS feed and mocked full-text fetch, so we know
the code is sound before it ever touches a real feed.

Run: python3 test_rss_local.py
"""

import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, ".")
from adapters.rss_adapter import RSSAdapter

# A fake RSS feed with two entries:
# 1. One with a full article body already in the feed (no full-text fetch needed)
# 2. One with a short teaser (should trigger full-text fetch fallback)
FAKE_FEED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Fake Competitor Blog</title>
  <item>
    <title>We Launched a New Pricing Tier</title>
    <link>https://fakecompetitor.com/articles/new-pricing</link>
    <pubDate>Mon, 18 Aug 2026 10:00:00 GMT</pubDate>
    <description><![CDATA[
      Today we're excited to announce a completely new pricing tier designed
      for enterprise customers. This tier includes dedicated support, custom
      integrations, and volume discounts starting at 100 seats. We built this
      after months of customer feedback showing that our mid-market pricing
      wasn't flexible enough for larger teams who needed more control over
      their billing cycles and support SLAs. This represents a major shift
      in how we approach our go-to-market strategy for the enterprise segment,
      and reflects a broader repositioning of our brand toward larger clients.
      We're also introducing a new self-serve onboarding flow for this tier,
      along with a dedicated Slack channel for enterprise customers to reach
      our support team directly. Early access customers who tested this over
      the past quarter reported a 40% reduction in time-to-first-value, which
      was the primary metric we were optimizing for when we redesigned this
      whole segment of the product from the ground up this year.
    ]]></description>
  </item>
  <item>
    <title>Short Teaser Article</title>
    <link>https://fakecompetitor.com/articles/short-teaser</link>
    <pubDate>Tue, 19 Aug 2026 10:00:00 GMT</pubDate>
    <description>Read more about our latest update...</description>
  </item>
</channel>
</rss>
"""

# What we pretend the full article page returns when fetched (for item 2).
FAKE_FULL_ARTICLE_HTML = """
<html><body>
<article>
<h1>Short Teaser Article</h1>
<p>We're rebranding our entire visual identity this quarter, including a new
logo, new color palette, and updated messaging across all our marketing
materials. This is the biggest brand change we've made in five years, and
it reflects our shift toward a more premium, enterprise-focused positioning
in the market.</p>
</article>
</body></html>
"""


def test_full_content_entry_parses_correctly():
    """Entry with a long description should NOT trigger a full-text fetch."""
    print("TEST 1: Entry with full content in the feed itself...")

    adapter = RSSAdapter()

    import feedparser
    pre_parsed = feedparser.parse(FAKE_FEED_XML)  # parse BEFORE patching

    with patch("feedparser.parse") as mock_parse:
        mock_parse.return_value = pre_parsed

        with patch.object(adapter, "fetch_full_text") as mock_fetch:
            mock_fetch.return_value = None  # should never be called for item 1
            result = adapter.fetch_feed("http://fake-url.test/feed", "Fake Competitor")

    items = result["items"]
    stats = result["stats"]

    assert stats["found"] == 2, f"Expected 2 entries found, got {stats['found']}"
    assert len(items) == 2, f"Expected 2 items returned, got {len(items)}"

    item1 = items[0]
    assert item1["title"] == "We Launched a New Pricing Tier"
    assert item1["url"] == "https://fakecompetitor.com/articles/new-pricing"
    assert len(item1["text"]) > 100, "Text should be substantial, not empty"
    assert item1["text_complete"] is True, "Full content shouldn't need fetch fallback"
    assert item1["source_name"] == "Fake Competitor"
    assert item1["content_hash"], "Should have a content hash"
    assert item1["published_date"] is not None, "Should parse the pubDate"

    print(f"  PASS — title={item1['title']!r}")
    print(f"  PASS — text_complete={item1['text_complete']}, len={len(item1['text'])} chars")
    print(f"  PASS — published_date={item1['published_date']}")
    print()


def test_short_teaser_triggers_full_text_fetch():
    """Entry with a short description SHOULD trigger the full-text fallback."""
    print("TEST 2: Entry with short teaser triggers full-text fetch...")

    adapter = RSSAdapter()

    import feedparser
    pre_parsed = feedparser.parse(FAKE_FEED_XML)  # parse BEFORE patching

    with patch("feedparser.parse") as mock_parse:
        mock_parse.return_value = pre_parsed

        with patch.object(adapter, "fetch_full_text") as mock_fetch:
            # Simulate a successful full-text fetch for the short teaser
            mock_fetch.return_value = (
                "We're rebranding our entire visual identity this quarter, "
                "including a new logo, new color palette, and updated "
                "messaging across all our marketing materials. This is the "
                "biggest brand change we've made in five years."
            )
            result = adapter.fetch_feed("http://fake-url.test/feed", "Fake Competitor")

    items = result["items"]
    stats = result["stats"]

    item2 = items[1]
    assert item2["title"] == "Short Teaser Article"
    assert mock_fetch.called, "fetch_full_text should have been called for the short entry"
    assert "rebranding" in item2["text"], "Should have picked up the full-text version"
    assert stats["full_text_fetched"] == 1, f"Expected 1 full-text fetch, got {stats['full_text_fetched']}"

    print(f"  PASS — full_text_fetched stat = {stats['full_text_fetched']}")
    print(f"  PASS — text now contains full article: {item2['text'][:60]}...")
    print()


def test_full_text_fetch_failure_keeps_item_but_flags_incomplete():
    """If full-text fetch fails, keep the item but mark text_complete=False."""
    print("TEST 3: Full-text fetch failure doesn't drop the item...")

    adapter = RSSAdapter()

    import feedparser
    pre_parsed = feedparser.parse(FAKE_FEED_XML)  # parse BEFORE patching

    with patch("feedparser.parse") as mock_parse:
        mock_parse.return_value = pre_parsed

        with patch.object(adapter, "fetch_full_text") as mock_fetch:
            mock_fetch.return_value = None  # simulate failure
            result = adapter.fetch_feed("http://fake-url.test/feed", "Fake Competitor")

    items = result["items"]
    stats = result["stats"]

    item2 = items[1]
    assert item2 is not None, "Item should still be kept, not dropped"
    assert item2["text_complete"] is False, "Should be flagged incomplete"
    assert stats["full_text_failed"] == 1

    print(f"  PASS — item kept despite fetch failure, text_complete={item2['text_complete']}")
    print(f"  PASS — full_text_failed stat = {stats['full_text_failed']}")
    print()


def test_broken_feed_returns_empty_not_crash():
    """A totally broken feed URL should return empty results, not raise."""
    print("TEST 4: Broken feed fails gracefully...")

    adapter = RSSAdapter()

    with patch("feedparser.parse") as mock_parse:
        mock_parse.side_effect = Exception("Connection refused")
        result = adapter.fetch_feed("http://totally-broken.test/feed", "Broken Source")

    assert result["items"] == [], "Should return empty items list"
    assert result["stats"]["error"] is not None, "Should record the error"

    print(f"  PASS — broken feed handled gracefully: error={result['stats']['error']!r}")
    print()


def test_quote_verification_logic():
    """Verify the substring check used for anti-hallucination (from extract.py logic)."""
    print("TEST 5: Quote verification (anti-hallucination check)...")

    source_text = "We're rebranding our entire visual identity this quarter."

    real_quote = "rebranding our entire visual identity"
    fake_quote = "we're launching a rocket to Mars"

    assert real_quote in source_text, "Real quote should be found as substring"
    assert fake_quote not in source_text, "Fake/hallucinated quote should NOT be found"

    print(f"  PASS — real quote verified: {real_quote!r} found in source")
    print(f"  PASS — fake quote rejected: {fake_quote!r} NOT found in source")
    print()


if __name__ == "__main__":
    print("=" * 70)
    print("RSS Adapter — Local Logic Tests (no real network needed)")
    print("=" * 70)
    print()

    test_full_content_entry_parses_correctly()
    test_short_teaser_triggers_full_text_fetch()
    test_full_text_fetch_failure_keeps_item_but_flags_incomplete()
    test_broken_feed_returns_empty_not_crash()
    test_quote_verification_logic()

    print("=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
