"""Pipeline integration for the on-chain holder/LP facts.

This is the file that matters for alert behaviour: the collector only helps if
its facts reach the security analyzer BEFORE it scores, and it must only cost
RPC calls on coins that could still earn an alert. The acceptance criterion for
the whole piece of work — a known-bad coin blocked by the existing analyzer with
no new mechanism — is pinned at the bottom.
"""

import dataclasses as dc
from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.collectors.onchain_security import OnChainSecurityFacts
from meme_intelligence.config.settings import (
    AlertThresholds,
    OnChainSecuritySettings,
    Settings,
)
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity
from meme_intelligence.workflow.pipeline import ResearchPipeline

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="MintAddr1", symbol="MEME")


def make_pair(**overrides) -> DexPair:
    defaults = dict(chain="solana", pair_address="Pool1", base_token=TOKEN,
                    liquidity_usd=80_000.0, market_cap=250_000.0,
                    pair_created_at=NOW - timedelta(minutes=10))
    defaults.update(overrides)
    return DexPair(**defaults)


def thin_profile(**overrides) -> SecurityProfile:
    """What GoPlus actually returns for a fresh mint: contract facts only."""
    defaults = dict(
        token=TOKEN, source="goplus",
        is_honeypot=False, cannot_buy=False, cannot_sell_all=False,
        is_mintable=False, is_freezable=False, ownership_renounced=True,
        holder_count=None, top_holder_percent=None, top10_holder_percent=None,
        lp_locked_percent=None,
    )
    defaults.update(overrides)
    return SecurityProfile(**defaults)


class FakeGoPlus:
    def __init__(self, profile):
        self._profile = profile

    async def get_token_security(self, chain, address):
        return self._profile


class FakeOnChain:
    """Stands in for OnChainSecurityCollector.collect."""

    def __init__(self, facts=None, error=None):
        self._facts = facts if facts is not None else OnChainSecurityFacts()
        self._error = error
        self.calls: list[tuple] = []

    async def collect(self, mint, pool_address=None):
        self.calls.append((mint, pool_address))
        if self._error is not None:
            raise self._error
        return self._facts


def make_pipeline(facts=None, *, error=None, settings=None, onchain=None):
    onchain_settings = onchain or OnChainSecuritySettings(enabled=True)
    base = settings or Settings()
    settings = dc.replace(base, onchain_security=onchain_settings)
    collector = FakeOnChain(facts, error)
    pipeline = ResearchPipeline(settings, FakeGoPlus(thin_profile()),
                               onchain_security_collector=collector,
                               now_func=lambda: NOW)
    return pipeline, collector


# --------------------------------------------------------------------------
# The facts must land before scoring
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chain_facts_reach_the_security_profile_and_the_score():
    """The whole point: these fields are what the analyzer was missing."""
    pipeline, collector = make_pipeline(OnChainSecurityFacts(
        top_holder_percent=88.45, top10_holder_percent=95.0, lp_burned_percent=0.0))

    result = await pipeline.analyze_pair(make_pair())

    assert collector.calls == [(TOKEN.address, "Pool1")]
    assert result.security_profile.top_holder_percent == pytest.approx(88.45)
    assert result.security_profile.top10_holder_percent == pytest.approx(95.0)
    assert result.security_profile.lp_locked_percent == pytest.approx(0.0)
    # Coverage rose because distribution and liquidity now resolve, and the
    # score fell because the facts are damning.
    assert result.security.coverage > 0.25
    assert result.security.overall_score < 100.0


@pytest.mark.asyncio
async def test_the_census_never_writes_a_holder_count():
    """getTokenLargestAccounts sees at most 20 accounts. A census size written to
    holder_count would be a fabricated population figure (Rule 8)."""
    pipeline, _ = make_pipeline(OnChainSecurityFacts(
        top_holder_percent=5.0, census_owner_count=17))
    result = await pipeline.analyze_pair(make_pair())
    assert result.security_profile.holder_count is None


@pytest.mark.asyncio
async def test_no_facts_leaves_the_outcome_identical_to_having_no_collector():
    """A withheld census must be indistinguishable from the layer being absent —
    no partial write, no coverage change, no score change (Rule 18)."""
    withheld, _ = make_pipeline(OnChainSecurityFacts(
        notes=("concentration withheld",)))
    result = await withheld.analyze_pair(make_pair())

    baseline = ResearchPipeline(Settings(), FakeGoPlus(thin_profile()),
                               now_func=lambda: NOW)
    unchanged = await baseline.analyze_pair(make_pair())

    assert result.security_profile == unchanged.security_profile
    assert result.security.coverage == pytest.approx(unchanged.security.coverage)
    assert result.security.overall_score == pytest.approx(
        unchanged.security.overall_score)


@pytest.mark.asyncio
async def test_a_collector_failure_never_breaks_analysis():
    """An RPC outage degrades to the old behaviour, it does not lose the coin."""
    pipeline, _ = make_pipeline(error=CollectorError("RPC exploded"))
    result = await pipeline.analyze_pair(make_pair())
    assert result is not None
    assert result.security_profile.top_holder_percent is None


@pytest.mark.asyncio
async def test_an_unexpected_error_still_propagates():
    """Only collector errors are absorbed; a genuine bug must not be swallowed."""
    pipeline, _ = make_pipeline(error=RuntimeError("programming error"))
    with pytest.raises(RuntimeError):
        await pipeline.analyze_pair(make_pair())


# --------------------------------------------------------------------------
# Rule 9 — one source never silently overwrites another's red flag
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_more_cautious_concentration_wins_over_the_provider():
    """Chain says 60%, provider said 12%. Keep 60% — never soften a red flag."""
    settings = Settings()
    collector = FakeOnChain(OnChainSecurityFacts(top_holder_percent=60.0,
                                                top10_holder_percent=80.0))
    pipeline = ResearchPipeline(
        dc.replace(settings, onchain_security=OnChainSecuritySettings(enabled=True)),
        FakeGoPlus(thin_profile(top_holder_percent=12.0, top10_holder_percent=40.0)),
        onchain_security_collector=collector, now_func=lambda: NOW)

    result = await pipeline.analyze_pair(make_pair())
    assert result.security_profile.top_holder_percent == pytest.approx(60.0)
    assert result.security_profile.top10_holder_percent == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_a_kinder_chain_reading_cannot_erase_the_providers_warning():
    """The direction that matters most: chain reads 4%, provider flagged 71%.
    The chain read is a top-20 census and can MISS supply, so it must not be
    allowed to clear a provider's concentration flag."""
    settings = Settings()
    collector = FakeOnChain(OnChainSecurityFacts(top_holder_percent=4.0))
    pipeline = ResearchPipeline(
        dc.replace(settings, onchain_security=OnChainSecuritySettings(enabled=True)),
        FakeGoPlus(thin_profile(top_holder_percent=71.0)),
        onchain_security_collector=collector, now_func=lambda: NOW)

    result = await pipeline.analyze_pair(make_pair())
    assert result.security_profile.top_holder_percent == pytest.approx(71.0)


@pytest.mark.asyncio
async def test_the_lower_lp_lock_wins():
    """For LP the cautious direction is DOWN: a 0% burn must not be raised by a
    provider's optimistic 90% lock claim, and vice versa."""
    settings = Settings()
    collector = FakeOnChain(OnChainSecurityFacts(lp_burned_percent=0.0))
    pipeline = ResearchPipeline(
        dc.replace(settings, onchain_security=OnChainSecuritySettings(enabled=True)),
        FakeGoPlus(thin_profile(lp_locked_percent=90.0)),
        onchain_security_collector=collector, now_func=lambda: NOW)

    result = await pipeline.analyze_pair(make_pair())
    assert result.security_profile.lp_locked_percent == pytest.approx(0.0)


# --------------------------------------------------------------------------
# The spend gate (Rule 11)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disabled_by_default_costs_nothing():
    pipeline, collector = make_pipeline(
        OnChainSecurityFacts(top_holder_percent=90.0),
        onchain=OnChainSecuritySettings())        # enabled defaults to False
    result = await pipeline.analyze_pair(make_pair())
    assert collector.calls == []
    assert result.security_profile.top_holder_percent is None


@pytest.mark.asyncio
async def test_a_non_solana_pair_is_never_censused():
    pipeline, collector = make_pipeline(OnChainSecurityFacts(top_holder_percent=9.0))
    await pipeline.analyze_pair(make_pair(chain="ethereum"))
    assert collector.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("pair_kwargs", [
    {"liquidity_usd": 0.0},
    {"liquidity_usd": None},
    {"market_cap": None, "fdv": None},
    {"liquidity_usd": float("nan")},
])
async def test_an_untradeable_coin_is_never_censused(pair_kwargs):
    """No point paying to analyse a coin whose buy-side alert would be
    suppressed anyway. Unknown liquidity/mcap counts as untradeable."""
    pipeline, collector = make_pipeline(OnChainSecurityFacts(top_holder_percent=9.0))
    await pipeline.analyze_pair(make_pair(**pair_kwargs))
    assert collector.calls == []


@pytest.mark.asyncio
async def test_a_coin_past_the_freshness_window_is_never_censused():
    settings = dc.replace(Settings(),
                          alerts=AlertThresholds(opportunity_max_age_hours=1.0))
    pipeline, collector = make_pipeline(
        OnChainSecurityFacts(top_holder_percent=9.0), settings=settings)
    await pipeline.analyze_pair(make_pair(pair_created_at=NOW - timedelta(hours=5)))
    assert collector.calls == []


@pytest.mark.asyncio
async def test_an_unknown_pool_age_does_not_block_the_census():
    """Rule 8, matching AutomationRules._too_old: absent data is not evidence
    of age."""
    settings = dc.replace(Settings(),
                          alerts=AlertThresholds(opportunity_max_age_hours=1.0))
    pipeline, collector = make_pipeline(
        OnChainSecurityFacts(top_holder_percent=9.0), settings=settings)
    await pipeline.analyze_pair(make_pair(pair_created_at=None))
    assert len(collector.calls) == 1


@pytest.mark.asyncio
async def test_an_oversized_coin_is_never_censused():
    settings = dc.replace(
        Settings(), alerts=AlertThresholds(opportunity_max_liquidity_usd=50_000.0))
    pipeline, collector = make_pipeline(
        OnChainSecurityFacts(top_holder_percent=9.0), settings=settings)
    await pipeline.analyze_pair(make_pair(liquidity_usd=900_000.0))
    assert collector.calls == []


@pytest.mark.asyncio
async def test_the_per_token_cooldown_stops_a_watchlist_recheck_re_spending():
    pipeline, collector = make_pipeline(
        OnChainSecurityFacts(top_holder_percent=9.0),
        onchain=OnChainSecuritySettings(enabled=True, cooldown_minutes=30.0))
    await pipeline.analyze_pair(make_pair())
    await pipeline.analyze_pair(make_pair())
    assert len(collector.calls) == 1


@pytest.mark.asyncio
async def test_the_daily_budget_caps_the_drain():
    pipeline, collector = make_pipeline(
        OnChainSecurityFacts(top_holder_percent=9.0),
        onchain=OnChainSecuritySettings(enabled=True, cooldown_minutes=0.0,
                                        max_lookups_per_day=2))
    for index in range(5):
        token = dc.replace(TOKEN, address=f"Mint{index}")
        await pipeline.analyze_pair(make_pair(base_token=token))
    assert len(collector.calls) == 2


@pytest.mark.asyncio
async def test_a_forced_lookup_bypasses_cooldown_and_budget():
    """/check and holdings are deliberate operator spends and are never starved
    by a scanner budget."""
    pipeline, collector = make_pipeline(
        OnChainSecurityFacts(top_holder_percent=9.0),
        onchain=OnChainSecuritySettings(enabled=True, cooldown_minutes=60.0,
                                        max_lookups_per_day=1))
    await pipeline.analyze_pair(make_pair())
    await pipeline.analyze_pair(make_pair(), force_onchain_security=True)
    await pipeline.analyze_pair(make_pair(), force_onchain_security=True)
    assert len(collector.calls) == 3


@pytest.mark.asyncio
async def test_a_forced_lookup_runs_even_on_an_untradeable_coin():
    """The operator asking about a coin outranks the quality screen — that is
    exactly when he most needs the facts."""
    pipeline, collector = make_pipeline(OnChainSecurityFacts(top_holder_percent=9.0))
    await pipeline.analyze_pair(make_pair(liquidity_usd=None),
                               force_onchain_security=True)
    assert len(collector.calls) == 1


# --------------------------------------------------------------------------
# THE ACCEPTANCE CRITERION
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_known_bad_coin_is_blocked_and_a_known_good_one_still_passes():
    """End to end, through the real pipeline, with no new scoring mechanism.

    This is the claim the whole piece of work rests on: supply the two facts and
    the EXISTING analyzer separates them by a wide margin. The coverage cap that
    was tried instead compressed this separation to 8.7 points and would have
    blocked 99% of the operator's alerts.
    """
    gate = Settings().alerts.security

    bad_pipeline, _ = make_pipeline(OnChainSecurityFacts(
        top_holder_percent=88.45, top10_holder_percent=95.0, lp_burned_percent=0.0))
    bad = await bad_pipeline.analyze_pair(make_pair())

    good_pipeline, _ = make_pipeline(OnChainSecurityFacts(
        top_holder_percent=3.0, top10_holder_percent=22.0, lp_burned_percent=95.0))
    good = await good_pipeline.analyze_pair(make_pair())

    assert bad.security.overall_score < gate, "the damning coin must be blocked"
    assert good.security.overall_score >= gate, "the healthy coin must still pass"
    assert good.security.overall_score - bad.security.overall_score > 20.0
