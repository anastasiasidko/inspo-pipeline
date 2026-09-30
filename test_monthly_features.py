"""
Offline tests for the video alert, the competitor map and the suggested new voices.
Nothing here touches the network, Apify, Anthropic or the Sheet.

Run: python3 -m pytest test_monthly_features.py
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

import engine
from test_engine_e2e import TEST_CONFIG, make_mock_anthropic_client
from test_rss_local import FAKE_FEED_XML
from adapters.apify_linkedin_adapter import normalize_post
from pipeline import competitor_map, media, suggestions
from storage.fake_backend import InMemoryBackend

BASE = {"content": "hello", "linkedinUrl": "https://linkedin.com/posts/1", "postedAt": "2026-09-20T10:00:00Z",
        "type": "post"}


# ---------------------------------------------------------------- video capture
def test_video_fields_are_kept_when_present_and_absent_otherwise():
    assert "video" not in normalize_post(BASE, "A — LinkedIn", "x")
    assert "video" not in normalize_post({**BASE, "postImages": [{"url": "https://i/1.jpg"}]}, "A — LinkedIn", "x")
    v = normalize_post({**BASE, "postVideo": {"videoUrl": "https://v/x.mp4", "duration": 600}}, "A", "x")["video"]
    assert v == {"url": "https://v/x.mp4", "duration_seconds": 600}
    x = normalize_post({**BASE, "extendedEntities": {"media": [
        {"type": "video", "video_info": {"duration_millis": 90000, "variants": [{"url": "https://t/v.mp4"}]}}]}},
        "A", "x")["video"]
    assert x == {"url": "https://t/v.mp4", "duration_seconds": 90.0}
    bare = normalize_post({**BASE, "type": "video"}, "A", "x")["video"]
    assert bare["url"] == BASE["linkedinUrl"] and bare["duration_seconds"] is None


def test_only_relevant_posts_with_video_are_offered():
    items = [
        {"source_url": "u1", "source_name": "Val — LinkedIn", "author": "Val", "video": {"url": "v1", "duration_seconds": 600}},
        {"source_url": "u2", "source_name": "Val — LinkedIn", "author": "Val", "video": {"url": "v2", "duration_seconds": 60}},
        {"source_url": "u3", "source_name": "Val — LinkedIn", "author": "Val"},
    ]
    got = media.video_candidates(items, verified_urls=["u1", "u3"])
    assert [c["post_url"] for c in got] == ["u1"]  # u2 not relevant, u3 has no video
    assert got[0]["length"] == "10-minute" and got[0]["author"] == "Val"


def test_transcript_fetch_falls_back_and_never_raises():
    cand = {"video_url": "https://v/x.mp4"}
    actors = [{"id": "a/broken", "template": {"url": "{url}"}}, {"id": "b/works", "template": {"url": "{url}"}}]

    def runner(actor_id, actor_input, token, max_charge, wait):
        assert actor_input == {"url": "https://v/x.mp4"} and max_charge <= 0.10
        if actor_id == "a/broken":
            raise RuntimeError("boom")
        return [{"transcript": [{"text": "first line"}, {"text": "second line"}]}], 0.02

    with patch("adapters.scraper_registry._mark_verified"):
        out = media.get_transcript(cand, token="t", runner=runner, finder=lambda tok: actors)
    assert out["text"] == "first line second line" and out["actor"] == "b/works"
    assert out["cost_usd"] == 0.02 and "a/broken" in out["notice"]

    none = media.get_transcript(cand, token="t", runner=runner, finder=lambda tok: [])
    assert none["text"] == "" and "No suitable" in none["notice"]
    failed = media.get_transcript(cand, token="t", runner=lambda *a: (_ for _ in ()).throw(RuntimeError("x")),
                                  finder=lambda tok: actors[:1])
    assert failed["text"] == "" and "failed" in failed["notice"]


# ---------------------------------------------------------------- competitor map
def test_map_counts_everything_returned_and_shows_zero_channels():
    sources = [{"name": "Val — LinkedIn", "kind": "linkedin"}, {"name": "Val — Newsletter", "kind": "rss"},
               {"name": "Ana — X", "kind": "x"}]
    items = [{"source_name": "Val — LinkedIn"}] * 30 + [{"source_name": "Val — Newsletter"}] * 2
    rows = competitor_map.build_map(sources, items, lookback_days=28, cap=30)
    by = {(r["competitor"], r["channel"]): r for r in rows}
    assert by[("Val", "LinkedIn")]["posts"] == 30 and by[("Val", "LinkedIn")]["capped"]
    assert by[("Val", "LinkedIn")]["per_week"] == 7.5
    assert by[("Val", "Newsletter")]["posts"] == 2 and not by[("Val", "Newsletter")]["capped"]
    assert by[("Ana", "X")]["posts"] == 0  # a channel that returned nothing still appears
    text = competitor_map.format_map(rows, 28, 30)
    assert "LinkedIn: 30 posts, 7.5 per week (at the 30-post limit" in text and "X: 0 posts" in text


# ---------------------------------------------------------------- suggestions
def _search_response(payload, urls):
    result = MagicMock(type="web_search_tool_result",
                       content=[MagicMock(url=u, title=f"title {u}") for u in urls])
    text = MagicMock(type="text", text=json.dumps(payload))
    return MagicMock(content=[result, text], stop_reason="end_turn")


def test_suggestions_keep_only_cited_new_people_with_stated_reach():
    payload = [
        {"name": "Ann New", "why": "Writes about burnout.", "url": "https://ann.com/about",
         "reach": "about 50,000 subscribers", "reach_source": "https://substack.com/ann"},
        {"name": "Val Nelson", "why": "already followed", "url": "https://val.com", "reach": None, "reach_source": None},
        {"name": "Ghost", "why": "no such page", "url": "https://made-up.example", "reach": None, "reach_source": None},
        {"name": "Bo Guess", "why": "x", "url": "https://bo.com", "reach": "1M followers", "reach_source": "https://not-searched.com"},
    ]
    resp = _search_response(payload, ["https://ann.com/about", "https://substack.com/ann", "https://val.com", "https://bo.com"])
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.return_value = resp
        got = suggestions.suggest_new_voices("digest text", ["Val Nelson"], n=5)
    names = [g["name"] for g in got]
    assert names == ["Ann New", "Bo Guess"]  # Val already followed, Ghost's page was never in the results
    assert got[0]["reach"] == "about 50,000 subscribers"
    assert got[1]["reach"] is None  # reach claimed without a source from the search results is dropped


# ---------------------------------------------------------------- engine
def test_video_transcript_goes_through_extraction_and_the_digest():
    storage = InMemoryBackend()
    storage.save_rules("ksenia", "general", "Flag ideas about work.", "test")
    transcript = "Today I want to say that freelancers should raise their prices every single year without apology."
    reply = json.dumps([{"signal_type": "content_theme", "summary": "Says freelancers should raise prices yearly.",
                         "evidence_quote": "freelancers should raise their prices every single year",
                         "confidence": 0.9, "themes": [], "value_claims": [], "target_audience": [], "terminology": []}])
    digest_reply = json.dumps({"Val Nelson": ["Says freelancers should raise prices yearly."]})
    client = MagicMock()
    client.messages.create.side_effect = [  # first call extracts, second writes the digest
        MagicMock(content=[MagicMock(text=r)], usage=MagicMock(input_tokens=100, output_tokens=50))
        for r in (reply, digest_reply)]
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    cfg = {"stream": "competitor_monitoring", "extraction_model": "claude-haiku-4-5-20251001",
           "signal_types": ["content_theme", "other"], "digest_path": None}
    cand = {"author": "Val Nelson", "video_url": "https://v/x.mp4", "published_date": "2026-09-20T10:00:00+00:00"}
    with patch("anthropic.Anthropic", return_value=client):
        result = engine.run_video(cand, transcript, "competitor_monitoring", 30, "test", storage=storage, config=cfg)
    assert result["stats"]["quote_verified"] == 1
    finding = [f for f in storage.findings if f["run_id"] == result["run_id"]][0]
    assert finding["entity"] == "Val Nelson" and finding["quote_verified"]
    assert storage.get_run(result["run_id"])["status"] == "done"
    assert "Val Nelson" in result["digest"] and "raise prices yearly" in result["digest"]


# ---------------------------------------------------------------- API usage log
def test_every_ai_call_is_logged_with_its_purpose_and_cost_and_flushed_to_the_sheet():
    from pipeline import channel_discovery, usage_log
    result = MagicMock(type="web_search_tool_result", content=[])
    reply = MagicMock(content=[result, MagicMock(type="text", text="[]")], stop_reason="end_turn",
                      usage=MagicMock(input_tokens=20000, output_tokens=500, server_tool_use=MagicMock(web_search_requests=6)))
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.return_value = reply
        channel_discovery.discover_channels("Val Nelson")
    (row,) = usage_log._pending
    assert row["purpose"] == "Find channels" and row["detail"] == "Val Nelson" and row["web_searches"] == 6
    assert row["model"] == "claude-haiku-4-5-20251001"  # channel discovery runs on Haiku
    assert row["est_cost_usd"] == round(20 * 0.001 + 0.5 * 0.005 + 6 * 0.01, 5)  # haiku prices + 6 searches
    assert usage_log.summary()[0]["est cost $"] == row["est_cost_usd"]

    storage = InMemoryBackend()
    usage_log.flush(storage)
    assert storage.api_usage[0]["purpose"] == "Find channels" and not usage_log._pending


# ---------------------------------------------------------------- hot topic (per-topic voice search)
def test_topics_by_entity_reuses_extracted_themes_deduped_and_capped():
    from pipeline.synthesis import topics_by_entity
    grouped = {
        "Val Nelson": [
            {"themes": ["AI overwhelm", "loss of control"]},
            {"themes": ["loss of control", " human connection "]},  # dup (case/space) + a new one
            {"themes": []},
        ],
        "Ana": [{"themes": [f"t{i}" for i in range(10)]}],  # more than the cap
    }
    out = topics_by_entity(grouped, cap=6)
    assert out["Val Nelson"] == ["AI overwhelm", "loss of control", "human connection"]
    assert len(out["Ana"]) == 6


def test_voices_on_topic_is_anchored_to_the_topic_not_the_whole_competitor():
    from pipeline import suggestions
    people = [{"name": "Dana New", "why": "writes on this exact idea", "url": "https://dana.com",
              "reach": None, "reach_source": None}]
    resp = _search_response(people, ["https://dana.com"])
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    captured = {}
    with patch("anthropic.Anthropic") as cls:
        def create(**kw):
            captured["prompt"] = kw["messages"][0]["content"]
            return resp
        cls.return_value.messages.create.side_effect = create
        got = suggestions.voices_on_topic("loss of control over AI", "she says AI took away our agency",
                                          "thought leadership rules", ["Val Nelson"], n=5)
    assert [p["name"] for p in got] == ["Dana New"]
    assert "loss of control over AI" in captured["prompt"] and "Val Nelson" in captured["prompt"]
    assert suggestions.voices_on_topic("", "ctx", "rules", []) == []  # empty topic: no search at all


def test_engine_returns_topics_by_entity_alongside_the_digest():
    import feedparser
    storage = InMemoryBackend()
    storage.save_rules("ksenia", "general", "Rules.", "test")
    client = make_mock_anthropic_client()
    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    with patch("feedparser.parse", return_value=feedparser.parse(FAKE_FEED_XML)), \
         patch("adapters.rss_adapter.RSSAdapter.fetch_full_text", return_value=None), \
         patch("anthropic.Anthropic", return_value=client):
        result = engine.run_stream(stream="competitor_monitoring", inputs=["Fake | http://fake-url.test/feed"],
                                   mode="typed_only", lookback_days=3650, started_by="test", storage=storage,
                                   config=dict(TEST_CONFIG))
    assert "FakeCompetitor" in result["topics_by_entity"]
    assert set(result["topics_by_entity"]["FakeCompetitor"]) >= {"pricing", "enterprise"}
