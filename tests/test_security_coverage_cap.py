"""A security score built on a quarter of its evidence must not read 100.

Regression for the coin in the operator's 2026-07-29 screenshot. rugcheck.xyz
independently rated that mint DANGER 65 — single holder 88.45%, LP 100%
unlocked, 209 holders — while this bot scored its security a perfect 100.0 and
its rug engine fired NOTHING.

Neither component was wrong about the facts it had. GoPlus returns no holder
and no LP data for a mint that young, so `liquidity`, `distribution`,
`developer` and `manipulation` were all None and excluded from the weighted
mean, leaving `contract` (authorities cleanly renounced) to become the entire
score at 25% coverage. The assessment even PRINTS "unverified areas are NOT
safe" — but the alert gate reads the number, not the note.

`RiskAnalyzer` has capped on exactly this basis since Part 9
(`_MIN_COVERAGE_FOR_LOW_RISK`, "Unknown is not safe"). These tests pin the
security analyzer to the same rule.

The payload in `fixtures_goplus_thin_mint.json` is the REAL GoPlus response
for that mint, captured live, so the fixture cannot drift into being kinder
than production.
"""

import json
import pathlib

import pytest

from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.collectors.security_data import GoPlusClient
from meme_intelligence.config.settings import Settings

_FIXTURE = pathlib.Path(__file__).with_name("fixtures_goplus_thin_mint.json")
MINT = "GQwocy6HF47EUtPJzXbkQxwWZbwKdarReuJoTPHKpump"
SETTINGS = Settings.from_env(env={})


def thin_profile():
    """The real GoPlus view of a fresh pump.fun mint: authorities only."""
    return GoPlusClient._parse_solana("solana", MINT, json.loads(_FIXTURE.read_text()))


def assess(profile, settings=SETTINGS):
    return SecurityAnalyzer(settings.security, settings.security_weights).assess(profile)


def test_the_real_payload_still_has_no_holder_or_lp_data():
    """Guards the premise: if GoPlus starts returning this data the test below
    stops testing anything, and we should find out from here."""
    p = thin_profile()
    assert p.top_holder_percent is None
    assert p.top10_holder_percent is None
    assert p.lp_locked_percent is None
    assert p.holder_count is None
    # ...while the contract authorities DID resolve, and resolved clean.
    assert p.is_mintable is False and p.is_freezable is False


def test_a_quarter_measured_coin_cannot_score_a_perfect_hundred():
    sec = assess(thin_profile())
    assert sec.coverage == pytest.approx(0.25)
    assert sec.overall_score < 100.0
    assert sec.overall_score == pytest.approx(62.5)


def test_it_no_longer_clears_the_buy_side_security_gate():
    """The operative consequence: this coin stops being pitched."""
    sec = assess(thin_profile())
    assert sec.overall_score < SETTINGS.alerts.security


def test_the_cap_explains_itself(caplog):
    """Rule 13 — a suppressed number with no explanation is its own bug."""
    sec = assess(thin_profile())
    capped = [f for f in sec.findings if "capped" in f.message]
    assert capped, "the cap must leave a finding"
    detail = capped[0].message
    assert "25%" in detail
    for missing in ("liquidity", "distribution", "developer", "manipulation"):
        assert missing in detail


def test_full_coverage_is_completely_unaffected():
    """Rule 18: a coin we CAN measure scores exactly as it always did."""
    import dataclasses as dc

    full = dc.replace(thin_profile(), holder_count=2500, top_holder_percent=3.0,
                      top10_holder_percent=22.0, creator_percent=1.5,
                      lp_locked_percent=95.0, buy_tax_percent=0.0,
                      sell_tax_percent=0.0, is_honeypot=False,
                      cannot_sell_all=False,
                      # manipulation sub-score inputs
                      anti_whale_modifiable=False, slippage_modifiable=False,
                      personal_slippage_modifiable=False, trading_cooldown=False,
                      tax_modifiable=False, has_blacklist=False,
                      trading_pausable=False)
    sec = assess(full)
    assert sec.coverage == pytest.approx(1.0)
    assert sec.overall_score == pytest.approx(100.0)
    assert not [f for f in sec.findings if "capped" in f.message]


def test_a_destructive_coin_still_scores_zero_not_the_cap():
    """The Part 4 override must win over the cap, never be raised by it."""
    import dataclasses as dc

    honeypot = dc.replace(thin_profile(), is_honeypot=True, cannot_sell_all=True)
    sec = assess(honeypot)
    assert sec.overall_score == 0.0
    assert sec.is_destructive


def test_the_cap_is_configurable_and_can_be_disabled():
    off = Settings.from_env(env={"MEMEINTEL_SECURITY_MIN_COVERAGE_FOR_FULL_SCORE": "0"})
    assert assess(thin_profile(), off).overall_score == pytest.approx(100.0)


def test_the_cap_scales_smoothly_rather_than_stepping():
    """50 + 50*coverage: half-measured tops out at 75, three-quarters at 87.5 —
    so a coin near the boundary does not flip verdicts on one field."""
    import dataclasses as dc

    half = dc.replace(thin_profile(), lp_locked_percent=95.0, buy_tax_percent=0.0,
                      sell_tax_percent=0.0, is_honeypot=False, cannot_sell_all=False)
    sec = assess(half)
    assert 0.25 < sec.coverage < 1.0
    assert sec.overall_score == pytest.approx(50.0 + 50.0 * sec.coverage)
