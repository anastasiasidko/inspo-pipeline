"""Offline tests: newsletter detection, and the signup form submitter. No network, no real signups."""
import sys
from unittest.mock import patch, MagicMock
sys.path.insert(0, ".")

from adapters.feed_discovery import newsletter_needing_subscription
from adapters.newsletter_signup import submit_signup

SIGNUP_PAGE = """<html><body><h1>Join the list</h1>
<form action="/subscribe" method="POST">
  <input type="email" name="email_address" />
  <input type="hidden" name="list_id" value="42" />
  <button type="submit">Subscribe</button>
</form></body></html>"""

SUCCESS_PAGE = "<html><body>Thanks! Check your email to confirm your subscription.</body></html>"
PLAIN_SUCCESS_PAGE = "<html><body>You're subscribed. Welcome aboard!</body></html>"
CAPTCHA_PAGE = '<html><body><div class="g-recaptcha" data-sitekey="x"></div><form><input name="email"></form></body></html>'
NO_FORM_PAGE = "<html><body>Enter your email in the widget below (loaded by JavaScript).</body></html>"


def _mock_get_post(pages):
    """pages: {url_or_'*': html}. Returns a function usable for requests.get/post side_effect."""
    def _resp(html, status=200):
        r = MagicMock(status_code=status, text=html, ok=status < 400)
        r.raise_for_status = MagicMock() if status < 400 else MagicMock(side_effect=Exception(f"HTTP {status}"))
        return r
    def fn(url, *a, **k):
        return _resp(pages.get(url, pages.get("*", "")))
    return fn


def test_captcha_is_never_attempted():
    print("TEST: a CAPTCHA-protected page is detected and the form is never submitted")
    with patch("requests.get", side_effect=_mock_get_post({"*": CAPTCHA_PAGE})) as get, \
         patch("requests.post") as post:
        r = submit_signup("https://example.com/join", "a@b.com")
    assert r["status"] == "captcha", r
    assert not post.called, "must never submit a form on a CAPTCHA-protected page"
    print("  PASS")


def test_no_static_form_fails_gracefully():
    print("TEST: a JS-only widget with no static <form> fails with a manual link, no crash")
    with patch("requests.get", side_effect=_mock_get_post({"*": NO_FORM_PAGE})):
        r = submit_signup("https://example.com/join", "a@b.com")
    assert r["status"] == "no_form" and "https://example.com/join" in r["message"], r
    print("  PASS")


def test_successful_submit_with_confirmation_needed():
    print("TEST: a real form submits, and a 'confirm your email' response is recognised")
    with patch("requests.get", side_effect=_mock_get_post({"https://example.com/join": SIGNUP_PAGE})), \
         patch("requests.post", side_effect=_mock_get_post({"https://example.com/subscribe": SUCCESS_PAGE})) as post:
        r = submit_signup("https://example.com/join", "reader@x.com")
    assert r["status"] == "pending_confirmation", r
    args, kwargs = post.call_args
    assert kwargs["data"]["email_address"] == "reader@x.com", kwargs["data"]
    assert kwargs["data"]["list_id"] == "42", "hidden fields must be preserved"
    print("  PASS — email field filled, hidden fields preserved, confirmation step recognised")


def test_successful_submit_no_confirmation_needed():
    print("TEST: a form that subscribes immediately (no double opt-in) is reported as subscribed")
    with patch("requests.get", side_effect=_mock_get_post({"https://example.com/join": SIGNUP_PAGE})), \
         patch("requests.post", side_effect=_mock_get_post({"https://example.com/subscribe": PLAIN_SUCCESS_PAGE})):
        r = submit_signup("https://example.com/join", "reader@x.com")
    assert r["status"] == "subscribed", r
    print("  PASS")


def test_newsletter_with_public_feed_needs_no_button():
    print("TEST: a newsletter that already has a public feed is not flagged for subscription")
    with patch("adapters.feed_discovery.resolve_channel", return_value={"kind": "rss", "feed_url": "u", "note": "RSS feed"}):
        assert newsletter_needing_subscription([("Newsletter", "https://x.substack.com")]) is None
    print("  PASS")


def test_newsletter_with_no_feed_needs_a_button():
    print("TEST: a newsletter with no public feed IS flagged for subscription")
    with patch("adapters.feed_discovery.resolve_channel", return_value={"kind": None, "feed_url": None, "note": "no feed found"}):
        got = newsletter_needing_subscription([("Website", "https://x.com"), ("Newsletter", "https://x.com/join")])
    assert got == ("Newsletter", "https://x.com/join"), got
    print("  PASS — only the link explicitly labeled 'Newsletter' is considered")


if __name__ == "__main__":
    print("Newsletter signup — offline tests")
    test_captcha_is_never_attempted()
    test_no_static_form_fails_gracefully()
    test_successful_submit_with_confirmation_needed()
    test_successful_submit_no_confirmation_needed()
    test_newsletter_with_public_feed_needs_no_button()
    test_newsletter_with_no_feed_needs_a_button()
    print("ALL TESTS PASSED")
