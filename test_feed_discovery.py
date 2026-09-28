"""Offline test: channel-line parsing and link classification (no network for these cases)."""
import sys
sys.path.insert(0, ".")
from adapters.feed_discovery import parse_channel_lines, resolve_channel, format_channel_lines

def test_all():
    text = "Website: https://valnelson.com/\nLinkedIn: not found\nYouTube - https://www.youtube.com/@Valnelson).\n\nhttps://x.com/valnelson"
    got = parse_channel_lines(text)
    assert got == [("Website", "https://valnelson.com/"), ("YouTube", "https://www.youtube.com/@Valnelson"), ("x.com", "https://x.com/valnelson")], got
    assert parse_channel_lines(format_channel_lines(got)) == got
    assert resolve_channel("https://www.linkedin.com/in/valnelson/")["kind"] == "linkedin"
    assert resolve_channel("https://www.linkedin.com/company/x/")["kind"] is None
    assert resolve_channel("https://www.instagram.com/x/")["kind"] == "instagram"
    assert resolve_channel("https://x.com/olgandrienko")["kind"] == "x"
    assert resolve_channel("https://insighttimer.com/ValNelson")["kind"] is None
    print("  PASS — lines parse; LinkedIn, X and other social links go to Apify; unsupported sites are flagged")

if __name__ == "__main__":
    print("Feed discovery — offline test"); test_all(); print("ALL TESTS PASSED")
