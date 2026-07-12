"""Tests for the Facebook safety harness (#78): read-only action blocking + checkpoint
detection. These are the guardrails that keep an automated browser from getting the
buddy's account restricted, so the coverage is deliberately thorough on both the
must-block and must-NOT-block sides."""

from __future__ import annotations

from web_watcher.fb_safety import (
    SESSION_ACTION_CAP,
    checkpoint_reason,
    is_blocked_action,
    is_checkpoint,
    is_facebook,
)


def test_is_facebook():
    assert is_facebook("https://www.facebook.com/marketplace/seattle/search?query=truck")
    assert is_facebook("https://facebook.com/x")
    assert not is_facebook("https://seattle.craigslist.org/search/cta")
    assert not is_facebook("https://offerup.com/search?q=truck")
    assert not is_facebook("not a url")


def test_blocked_actions_are_caught():
    for label in ["Message", "Send message", "Message seller", "Make offer", "Make an offer",
                  "Buy now", "Buy", "Add to cart", "Check out", "Checkout", "Pay",
                  "Like", "React", "Comment", "Reply", "Share", "Post", "Publish",
                  "Create new listing", "Sell something", "Save", "Add to collection",
                  "Follow", "Add friend", "Friend request", "Join group",
                  "Report", "Block", "Mark as sold", "Delete", "Remove listing"]:
        assert is_blocked_action(label), f"should block: {label!r}"


def test_readonly_navigation_is_allowed():
    for label in ["See more", "Show more", "View more", "Load more", "See all",
                  "More like this", "More filters", "Marketplace", "Categories",
                  "Search Marketplace", "Filter", "Sort by", "Price", "Condition",
                  "Date listed", "Newest first", "Nearest", "Distance",
                  "2015 Ford F-150 XLT — $18,000", "Vehicles", ""]:
        assert not is_blocked_action(label), f"should ALLOW: {label!r}"


def test_blocked_action_is_word_boundary_not_substring():
    # "likely" contains "like" but must NOT be blocked; "commercial" contains "comment"-ish
    assert not is_blocked_action("Likely new condition")
    assert not is_blocked_action("Commercial vehicles")
    # but the standalone verb is blocked
    assert is_blocked_action("Like this post")


class _FakePage:
    def __init__(self, url="https://www.facebook.com/marketplace/seattle", body=""):
        self.url = url
        self._body = body
    def inner_text(self, sel, timeout=0):
        return self._body


def test_checkpoint_detected_by_url():
    assert is_checkpoint(_FakePage(url="https://www.facebook.com/checkpoint/1234"))
    assert is_checkpoint(_FakePage(url="https://www.facebook.com/confirmemail.php"))


def test_checkpoint_detected_by_text():
    for body in [
        "We've temporarily restricted your account",
        "You're temporarily blocked",
        "Please confirm your identity to continue",
        "We noticed unusual activity on your account",
        "Complete this security check to continue",
        "Enter the code we sent you",
        "Your account has been disabled",
        "You can't use this feature right now",
    ]:
        assert is_checkpoint(_FakePage(body=body)), f"should be a checkpoint: {body!r}"


def test_normal_marketplace_page_is_not_a_checkpoint():
    body = "Vehicles for sale near Seattle. 2015 Ford F-150 $18,000. Sort by newest."
    assert not is_checkpoint(_FakePage(body=body))


def test_checkpoint_reason_is_human_readable():
    r = checkpoint_reason(_FakePage(body="We've temporarily restricted your account for review"))
    assert "restricted" in r.lower()
    # falls back to a generic phrase when nothing matches
    assert checkpoint_reason(_FakePage(body="all good here")) == "Facebook security checkpoint"


def test_session_cap_is_conservative():
    assert 1 <= SESSION_ACTION_CAP <= 20
