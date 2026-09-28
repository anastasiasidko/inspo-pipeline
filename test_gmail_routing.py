"""
Offline tests for the Gmail folder/filter routing — the Gmail API itself is
fully mocked (no real Google account, no network). Proves: idempotent label
and filter creation, the "wait for the first real email" behavior, and that a
missing Gmail setup degrades gracefully instead of crashing the app.
"""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")

import adapters.gmail_client as gc
import pipeline.newsletter_routing as routing


def _labels_service(existing=None):
    """A MagicMock shaped like Gmail's users().labels()/messages()/settings() API."""
    svc = MagicMock()
    labels = list(existing or [])

    def labels_list(**k):
        return MagicMock(execute=lambda: {"labels": labels})

    def labels_create(body, **k):
        new = {"id": f"Label_{len(labels)+1}", "name": body["name"]}
        labels.append(new)
        return MagicMock(execute=lambda: new)

    svc.users.return_value.labels.return_value.list = labels_list
    svc.users.return_value.labels.return_value.create = labels_create
    return svc, labels


def test_ensure_label_is_idempotent():
    print("TEST: ensure_label creates once, reuses on every call after")
    svc, labels = _labels_service()
    id1 = gc.ensure_label(svc, "Competitors")
    id2 = gc.ensure_label(svc, "Competitors")
    assert id1 == id2 and len(labels) == 1, labels
    print("  PASS")


def test_ensure_filter_is_idempotent():
    print("TEST: ensure_filter creates once per (sender, label), reuses after")
    svc = MagicMock()
    created = []
    svc.users.return_value.settings.return_value.filters.return_value.list.return_value.execute.return_value = {"filter": []}
    def create(body, **k):
        f = {"id": f"f{len(created)+1}", "criteria": body["criteria"], "action": body["action"]}
        created.append(f)
        svc.users.return_value.settings.return_value.filters.return_value.list.return_value.execute.return_value = {"filter": created}
        return MagicMock(execute=lambda: f)
    svc.users.return_value.settings.return_value.filters.return_value.create = create
    id1 = gc.ensure_filter(svc, "hello@x.com", "Label_1")
    id2 = gc.ensure_filter(svc, "hello@x.com", "Label_1")
    assert id1 == id2 and len(created) == 1
    action = created[0]["action"]
    assert action["removeLabelIds"] == ["INBOX"], "filter must skip the inbox (archive)"
    print("  PASS — one filter created, reused on repeat, and it archives (skips inbox)")


def test_find_sender_matches_domain_after_timestamp():
    print("TEST: find_sender_since only matches the right domain")
    svc = MagicMock()
    svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m1"}, {"id": "m2"}]}
    def get(id, **k):
        headers = {"m1": "From: Newsletter Bot <news@othersite.com>",
                   "m2": "From: Allie <hello@aiwithallie.beehiiv.com>"}[id]
        name, value = headers.split(": ", 1)
        return MagicMock(execute=lambda: {"payload": {"headers": [{"name": name, "value": value}]}})
    svc.users.return_value.messages.return_value.get = get
    sender = gc.find_sender_since(svc, "beehiiv.com", 0)
    assert sender == "hello@aiwithallie.beehiiv.com", sender
    print("  PASS")


def test_full_routing_flow_with_temp_state():
    print("TEST: record -> still pending (no mail yet) -> mail arrives -> routed, label+filter created")
    with tempfile.TemporaryDirectory() as d, patch.object(routing, "STATE_FILE", Path(d) / "state.json"):
        routing.record_subscription("Allie K. Miller", "https://aiwithallie.beehiiv.com/")
        assert routing.pending_count() == 1

        # first check: nothing has arrived yet
        with patch("pipeline.newsletter_routing.get_service", return_value=MagicMock(
                users=lambda: MagicMock(messages=lambda: MagicMock(
                    list=lambda **k: MagicMock(execute=lambda: {"messages": []}))))):
            r1 = routing.try_finish_pending()
        assert r1 == {"routed": [], "still_pending": 1, "not_configured": False}, r1

        # second check: her first email has now arrived
        svc = MagicMock()
        svc.users.return_value.messages.return_value.list.side_effect = [
            MagicMock(execute=lambda: {"messages": [{"id": "m1"}]}),   # find_sender_since
            MagicMock(execute=lambda: {"messages": [{"id": "m1"}]}),   # apply_label_to_existing
        ]
        svc.users.return_value.messages.return_value.get.return_value = MagicMock(
            execute=lambda: {"payload": {"headers": [
                {"name": "From", "value": "Allie <hello@aiwithallie.beehiiv.com>"}]}})
        svc.users.return_value.labels.return_value.list.return_value.execute.return_value = {"labels": []}
        svc.users.return_value.labels.return_value.create.return_value.execute.return_value = {"id": "Label_1"}
        svc.users.return_value.settings.return_value.filters.return_value.list.return_value.execute.return_value = {"filter": []}
        moved = {}
        def batch_modify(body, **k):
            moved["ids"] = body["ids"]
            return MagicMock(execute=lambda: {})
        svc.users.return_value.messages.return_value.batchModify = batch_modify

        with patch("pipeline.newsletter_routing.get_service", return_value=svc):
            r2 = routing.try_finish_pending()
        assert r2["routed"] == ["Allie K. Miller"], r2
        assert r2["still_pending"] == 0
        assert moved["ids"] == ["m1"], "the already-delivered first email should be moved into the folder too"

        state = json.loads((Path(d) / "state.json").read_text())
        assert state[0]["status"] == "routed" and state[0]["sender_address"] == "hello@aiwithallie.beehiiv.com"
    print("  PASS — stayed pending with no mail, then routed correctly once the first email showed up")


def test_missing_gmail_setup_degrades_gracefully():
    print("TEST: no Gmail client configured yet -> stays pending, no crash")
    with tempfile.TemporaryDirectory() as d, patch.object(routing, "STATE_FILE", Path(d) / "state.json"), \
         patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("GOOGLE_GMAIL_OAUTH_CLIENT", None)
        routing.record_subscription("Val Nelson", "https://valnelson.com/subscribe")
        result = routing.try_finish_pending()
    assert result == {"routed": [], "still_pending": 1, "not_configured": True}, result
    print("  PASS")


if __name__ == "__main__":
    print("Gmail routing — offline tests")
    test_ensure_label_is_idempotent()
    test_ensure_filter_is_idempotent()
    test_find_sender_matches_domain_after_timestamp()
    test_full_routing_flow_with_temp_state()
    test_missing_gmail_setup_degrades_gracefully()
    print("ALL TESTS PASSED")
