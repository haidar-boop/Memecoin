"""Why this bot pitched a coin rugcheck.xyz rated DANGER 65 — and what fixes it.

Operator screenshot, 2026-07-29. Mint GQwocy6HF47EUtPJzXbkQxwWZbwKdarReuJoTPHKpump:
single holder 88.45%, LP 100% unlocked, 209 holders. This bot scored its
security a PERFECT 100.0 and its rug engine fired nothing.

Neither component was wrong about the facts it had. GoPlus returns no holder
and no LP data for a mint that young, so `liquidity`, `distribution`,
`developer` and `manipulation` are all None and excluded from the weighted
mean, leaving `contract` (authorities cleanly renounced) to BECOME the entire
score at 25% coverage.

A coverage CAP was tried as the fix and measured against the operator's live
database before shipping (deploy/security_cap_impact.py): 499 of his 500
alerted coins sat at 25-49% coverage, and the cap would have blocked 338 of the
339 that cleared the security gate — 99% of his alerts. It was removed rather
than kept, because the same measurement showed it was not merely mis-tuned:

    with the real facts present and NO cap
        known-bad  (88% holder, 0% LP)  ->  73.8   BLOCKED
        known-good (3% holder, 95% LP)  -> 100.0   passes      26.2 apart
    with the cap on
        known-bad                       ->  73.8   BLOCKED
        known-good                      ->  82.5   passes       8.7 apart

The existing scoring already separates good from bad correctly, by a wide
margin, the moment it HAS the facts — and the cap compressed that separation.
The bug was never the scoring. It is that the facts are missing.

These tests therefore pin the diagnosis, and act as the acceptance criteria for
collecting holder concentration and LP status from chain: once those fields are
populated, `test_the_screenshot_coin_is_blocked_once_its_facts_are_known`
already describes the required outcome.

The payload in fixtures_goplus_thin_mint.json is the REAL GoPlus response for
that mint, captured live, so the fixture cannot drift into being kinder than
production.
"""

import dataclasses as dc
import json
import pathlib

import pytest

from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.collectors.security_data import GoPlusClient
from meme_intelligence.config.settings import Settings

_FIXTURE = pathlib.Path(__file__).with_name("fixtures_goplus_thin_mint.json")
MINT = "GQwocy6HF47EUtPJzXbkQxwWZbwKdarReuJoTPHKpump"
SETTINGS = Settings.from_env(env={})
GATE = SETTINGS.alerts.security


def thin_profile():
    """The real GoPlus view of a fresh pump.fun mint: contract authorities only."""
    return GoPlusClient._parse_solana("solana", MINT, json.loads(_FIXTURE.read_text()))


def assess(profile):
    return SecurityAnalyzer(SETTINGS.security, SETTINGS.security_weights).assess(profile)


def test_the_real_payload_still_has_no_holder_or_lp_data():
    """Guards the premise. If GoPlus ever starts returning this data, the whole
    diagnosis changes and we should find out from here rather than in the field."""
    p = thin_profile()
    assert p.top_holder_percent is None
    assert p.top10_holder_percent is None
    assert p.lp_locked_percent is None
    assert p.holder_count is None
    assert p.is_mintable is False and p.is_freezable is False   # ...contract DID resolve


def test_missing_facts_inflate_the_score_to_a_perfect_hundred():
    """The bug, stated as a fact about today's behaviour. This test is EXPECTED
    to keep passing until holder/LP collection lands — it documents the gap, it
    does not endorse it."""
    sec = assess(thin_profile())
    assert sec.coverage == pytest.approx(0.25)
    assert sec.overall_score == pytest.approx(100.0)
    assert sec.overall_score >= GATE, "and so it is pitched to the operator"


def test_the_screenshot_coin_is_blocked_once_its_facts_are_known():
    """THE ACCEPTANCE CRITERION for collecting holder/LP data from chain.

    Same coin, same analyzer, no new mechanism — just the two facts rugcheck.xyz
    used. It must fall below the security gate on its own."""
    known = dc.replace(thin_profile(), top_holder_percent=88.45,
                       top10_holder_percent=95.0, holder_count=209,
                       lp_locked_percent=0.0)
    sec = assess(known)
    assert sec.overall_score < GATE
    assert sec.overall_score == pytest.approx(73.8, abs=0.5)


def test_a_genuinely_healthy_coin_still_passes_with_those_facts_known():
    """The other half: collecting the data must not block good coins. This is
    what the coverage cap got wrong — it blocked 99% of real alerts."""
    healthy = dc.replace(thin_profile(), top_holder_percent=3.0,
                         top10_holder_percent=22.0, holder_count=2500,
                         lp_locked_percent=95.0)
    assert assess(healthy).overall_score >= GATE


def test_the_facts_separate_good_from_bad_by_a_wide_margin():
    """26 points apart. A margin this wide is why no extra capping mechanism is
    needed once the data exists — and why adding one only compressed it."""
    bad = dc.replace(thin_profile(), top_holder_percent=88.45,
                     top10_holder_percent=95.0, holder_count=209,
                     lp_locked_percent=0.0)
    good = dc.replace(thin_profile(), top_holder_percent=3.0,
                      top10_holder_percent=22.0, holder_count=2500,
                      lp_locked_percent=95.0)
    assert assess(good).overall_score - assess(bad).overall_score > 20.0


def test_a_destructive_coin_still_scores_zero():
    """The Part 4 override is untouched by any of this."""
    honeypot = dc.replace(thin_profile(), is_honeypot=True, cannot_sell_all=True)
    sec = assess(honeypot)
    assert sec.overall_score == 0.0 and sec.is_destructive
