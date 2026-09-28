"""Offline test: channel discovery marks where each link came from, and drops nothing silently."""
import sys, json, os
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0, ".")
os.environ["ANTHROPIC_API_KEY"] = "test"
from pipeline.channel_discovery import discover_channels

def test_sources():
    text = json.dumps([
        {"label": "LinkedIn", "url": "https://www.linkedin.com/in/x/", "source": "search"},
        {"label": "Blog", "url": "https://x.com/blog", "source": "linked from her About page"},
        {"label": "bad", "url": "not a url", "source": ""}])
    resp = NS(stop_reason="end_turn", content=[
        NS(type="web_search_tool_result", content=[NS(url="https://www.linkedin.com/in/x", title="X on LinkedIn")]),
        NS(type="text", text=text)])
    with patch("anthropic.Anthropic") as A:
        A.return_value.messages.create.return_value = resp
        out = discover_channels("X")
    assert [c["label"] for c in out] == ["LinkedIn", "Blog"], out
    assert out[0]["source"] == "search result: X on LinkedIn"
    assert out[1]["source"].startswith("not a search result, check it") and "About page" in out[1]["source"]
    print("  PASS — matched links cite the search result; unmatched ones are flagged")

if __name__ == "__main__":
    print("Channel discovery — offline test"); test_sources(); print("ALL TESTS PASSED")


def test_the_same_name_is_searched_once_even_if_asked_twice_at_the_same_time():
    import json
    import threading
    import time
    from unittest.mock import MagicMock, patch
    from pipeline import channel_discovery

    calls = []
    result = MagicMock(type="web_search_tool_result", content=[MagicMock(url="https://val.com", title="Val")])
    text = MagicMock(type="text", text=json.dumps([{"label": "Website", "url": "https://val.com", "source": "x"}]))
    reply = MagicMock(content=[result, text], stop_reason="end_turn", usage=MagicMock(input_tokens=1, output_tokens=1))

    def slow_create(**kw):
        calls.append(1)
        time.sleep(0.3)  # the first search is still running when the second request arrives
        return reply

    os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
    out = []
    with patch("anthropic.Anthropic") as cls:
        cls.return_value.messages.create.side_effect = slow_create
        threads = [threading.Thread(target=lambda: out.append(channel_discovery.discover_channels("Val Nelson")))
                   for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        channel_discovery.discover_channels("val nelson")  # later, different spelling of the case: still cached
        channel_discovery.discover_channels("Val Nelson", refresh=True)  # refresh right after: too soon to repeat
    assert len(calls) == 1 and out[0] == out[1] and out[0][0]["url"] == "https://val.com"
