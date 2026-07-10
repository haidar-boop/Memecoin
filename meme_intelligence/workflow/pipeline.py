"""Shared research pipeline (Spec Part 13 Section 2, Part 22 Section 2).

One implementation of the per-token analysis chain — security -> on-chain
-> token structure -> momentum -> narrative -> risk -> master assessment —
used by the daily routine, the continuous scanner, and the CLI. A single pipeline
keeps every entry point scoring tokens identically (Rule 4: one purpose
per module; Rule 18: extend, don't duplicate).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from meme_intelligence.ai.reasoning import AIJudgment, AIJudgmentService
from meme_intelligence.analyzers.community_analyzer import (
    CommunityAnalyzer,
    CommunityAssessment,
)
from meme_intelligence.analyzers.foundation_analyzer import (
    FoundationAnalyzer,
    FoundationAssessment,
)
from meme_intelligence.analyzers.momentum_analyzer import MomentumAnalyzer, MomentumAssessment
from meme_intelligence.analyzers.narrative_analyzer import (
    NarrativeAnalyzer,
    NarrativeAssessment,
    NarrativeInputs,
)
from meme_intelligence.analyzers.onchain_analyzer import (
    OnChainAnalyzer,
    OnChainAssessment,
    derive_onchain_profile,
)
from meme_intelligence.analyzers.opportunity_ranker import OpportunityRank, OpportunityRanker
from meme_intelligence.analyzers.risk_analyzer import RiskAnalyzer, RiskAssessment
from meme_intelligence.analyzers.scoring_engine import (
    MasterAssessment,
    ScoringEngine,
    derive_timing_score,
)
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer, SecurityAssessment
from meme_intelligence.analyzers.token_analyzer import TokenAnalyzer, TokenAssessment
from meme_intelligence.analyzers.wallet_intelligence import (
    WalletAssessment,
    WalletIntelligenceAnalyzer,
    enrich_onchain_profile,
)
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import MarketRegime, ResearchMode
from meme_intelligence.core.errors import CollectorError, InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import CommunityProfile, DexPair, SecurityProfile


@dataclass(frozen=True)
class PipelineResult:
    """Everything the analysis chain produced for one pair."""

    pair: DexPair
    security_profile: SecurityProfile
    security: SecurityAssessment
    onchain: OnChainAssessment | None
    token: TokenAssessment | None
    momentum: MomentumAssessment | None
    risk: RiskAssessment
    master: MasterAssessment
    wallet: WalletAssessment | None = None
    narrative: NarrativeAssessment | None = None
    foundation: FoundationAssessment | None = None
    community: CommunityAssessment | None = None
    community_profile: CommunityProfile | None = None
    ai_judgment: AIJudgment | None = None  # Part 23 reasoning-layer output
    opportunity: "OpportunityRank | None" = None  # Part 28 S5 watchlist ranking


class ResearchPipeline:
    """Runs the full analysis chain for one pair (Layers 2-3 + scoring)."""

    def __init__(
        self,
        settings: Settings,
        goplus_client,  # GoPlusClient-compatible (get_token_security)
        *,
        wallet_service=None,  # WalletDataService (Solana); costs metered credits
        jupiter_client=None,  # JupiterClient-compatible (check_round_trip_liquidity)
        community_client=None,  # CoinGeckoClient-compatible (get_community_profile)
        ai_service: AIJudgmentService | None = None,  # Part 23 reasoning layer
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._goplus = goplus_client
        self._wallet_service = wallet_service
        self._jupiter = jupiter_client
        self._liquidity_probe = settings.liquidity_probe
        self._community_client = community_client
        self._ai = ai_service
        self._now = now_func
        self._logger = get_logger("workflow.pipeline")

        self._security = SecurityAnalyzer(settings.security, settings.security_weights)
        self._onchain = OnChainAnalyzer(settings.onchain, settings.onchain_weights)
        self._token = TokenAnalyzer(settings.token, settings.token_weights)
        self._momentum = MomentumAnalyzer(settings.momentum, settings.momentum_weights,
                                          now_func=now_func)
        self._narrative = NarrativeAnalyzer(settings.narrative, settings.narrative_weights,
                                            settings.viral_weights)
        self._foundation = FoundationAnalyzer(settings.foundation_weights)
        self._community = CommunityAnalyzer(settings.community, settings.community_weights)
        self._wallet = WalletIntelligenceAnalyzer(settings.wallet, settings.smart_money_weights)
        self._risk = RiskAnalyzer(settings.risk_weights)
        self._scoring = ScoringEngine(settings.weights, settings.bands, now_func=now_func)
        self._opportunity = OpportunityRanker(settings.opportunity_weights)

    async def analyze_pair(
        self,
        pair: DexPair,
        regime: MarketRegime = MarketRegime.UNKNOWN,
        *,
        narrative_inputs: NarrativeInputs | None = None,
        research_mode: ResearchMode = ResearchMode.STANDARD,
    ) -> PipelineResult | None:
        """Full chain for one pair; ``None`` when security data is unavailable
        (a token that cannot be security-screened is not analyzable — Part 4).

        ``narrative_inputs`` carries explicit Part 19 analyst judgments;
        explicit inputs always win over the AI layer's. When an
        ``ai_service`` was provided, the reasoning layer runs AFTER the
        deterministic chain (Rule 10 — expensive analysis only after
        filtering) and fills whichever judgment slots remain empty; without
        it those categories honestly report "no data" (Rule 8).
        """
        try:
            profile = await self._goplus.get_token_security(pair.chain, pair.base_token.address)
        except CollectorError as exc:
            self._logger.info("security data unavailable for %s/%s: %s",
                              pair.chain, pair.base_token.address, exc)
            return None
        if profile is None:
            return None

        # Live round-trip sell test (Project 1): Solana-only, needs a Jupiter
        # API key, gated by the liquidity-probe config flag -- mirrors the
        # wallet-intelligence gating below (Rule 10). Runs before scoring so
        # the merged fields participate in the same destructive-override
        # logic as GoPlus's static honeypot fields (Rule 9 -- multi-source).
        if (
            self._jupiter is not None
            and self._liquidity_probe.enabled
            and pair.chain in ("solana", "sol")
        ):
            try:
                probe = await self._jupiter.check_round_trip_liquidity(
                    pair.base_token.address,
                    probe_sol_amount=self._liquidity_probe.probe_sol_amount,
                    slippage_bps=self._liquidity_probe.slippage_bps,
                )
                profile = dataclasses.replace(
                    profile,
                    live_buy_route_found=probe.live_buy_route_found,
                    live_sell_route_found=probe.live_sell_route_found,
                    live_round_trip_loss_percent=probe.live_round_trip_loss_percent,
                )
            except (CollectorError, ValueError) as exc:
                self._logger.info("Jupiter liquidity probe unavailable for %s: %s",
                                  pair.base_token.address, exc)

        try:
            security = self._security.assess(profile, pair)
        except InsufficientDataError:
            return None

        # Wallet intelligence (Part 17): Solana-only, costs metered credits,
        # so it only runs when a wallet service was provided (Rule 10).
        wallet = None
        onchain_profile = derive_onchain_profile(pair, profile)
        if self._wallet_service is not None and pair.chain in ("solana", "sol"):
            try:
                data = await self._wallet_service.gather(pair.base_token)
                wallet = self._wallet.assess(data, pair)
                onchain_profile = enrich_onchain_profile(onchain_profile, wallet)
            except (CollectorError, InsufficientDataError) as exc:
                self._logger.info("wallet intelligence unavailable for %s: %s",
                                  pair.base_token.address, exc)

        # Community data (Part 5): free CoinGecko community facts; a token
        # not listed there is an honest gap, not a failure (Rule 8).
        community = community_profile = None
        if self._community_client is not None:
            try:
                community_profile = await self._community_client.get_community_profile(
                    pair.base_token)
            except CollectorError as exc:
                self._logger.info("community data unavailable for %s: %s",
                                  pair.base_token.address, exc)
            if community_profile is not None:
                try:
                    community = self._community.assess(community_profile)
                except InsufficientDataError:
                    pass

        onchain = token = momentum = narrative = None
        try:
            onchain = self._onchain.assess(onchain_profile)
        except InsufficientDataError:
            pass
        try:
            token = self._token.assess(pair, profile)
        except InsufficientDataError:
            pass
        try:
            momentum = self._momentum.assess(pair, onchain=onchain)
        except InsufficientDataError:
            pass
        if narrative_inputs is not None:
            try:
                narrative = self._narrative.assess(
                    pair.base_token, narrative_inputs, community=community,
                    positive_sentiment_percent=(
                        community_profile.positive_sentiment_percent
                        if community_profile else None),
                )
            except (InsufficientDataError, ValueError):
                # A malformed provider value (out-of-range/NaN sentiment) is a
                # data-quality gap, not grounds to abort the whole analysis
                # (Rule 6) — the rest of the deterministic chain still stands.
                pass

        risk = self._risk.assess(security, pair=pair, token=token,
                                 onchain=onchain, regime=regime)
        master = self._scoring.evaluate(
            security,
            community=community,
            onchain=onchain,
            token_structure=token,
            risk=risk,
            momentum_score=momentum.overall_score if momentum else None,
            narrative_score=narrative.overall_score if narrative else None,
            timing_score=derive_timing_score(pair, token, onchain, now_func=self._now),
        )
        result = PipelineResult(
            pair=pair, security_profile=profile, security=security,
            onchain=onchain, token=token, momentum=momentum, risk=risk, master=master,
            wallet=wallet, narrative=narrative,
            community=community, community_profile=community_profile,
            opportunity=self._opportunity.rank(master.category_scores, risk.risk_score),
        )

        # AI reasoning layer (Part 23): only after the deterministic chain,
        # and never for tokens security has already invalidated (Rule 10).
        if self._ai is not None and not security.is_destructive:
            result = await self._enrich_with_ai(result, research_mode)
        return result

    # ---- AI enrichment (Part 23) ----

    async def enrich_with_ai(
        self,
        result: PipelineResult,
        *,
        mode: ResearchMode = ResearchMode.STANDARD,
        service=None,
    ) -> PipelineResult:
        """Run one AI judgment over a finished result and re-score.

        ``service`` overrides the pipeline's own judgment service — used by
        the continuous scanner's gate-passing verification mode (Part 32.5
        Section 8), where the AI judges ONLY tokens that already passed
        every review gate rather than every analyzed token. Idempotent: a
        result that already carries a judgment is returned unchanged.
        """
        ai = service if service is not None else self._ai
        if ai is None or result.security.is_destructive or result.ai_judgment is not None:
            return result
        return await self._enrich_with_ai(result, mode, service=ai)

    async def _enrich_with_ai(
        self,
        result: PipelineResult,
        mode: ResearchMode,
        service=None,
    ) -> PipelineResult:
        """Fill empty judgment slots from the reasoning layer, then re-score.

        The deterministic assessments are never altered — the judgment only
        feeds the foundation/narrative categories that had no data, and the
        master score is recomputed through the same locked weighting.
        Any AI failure leaves the deterministic result untouched (Rule 9).
        """
        judgment = await (service if service is not None else self._ai).judge(result, mode=mode)
        if judgment is None:
            return result

        narrative = result.narrative
        # Mirrors has_foundation_evidence below: an all-null AI judgment
        # must not fabricate a narrative score purely from the community-
        # creativity fallback NarrativeAnalyzer applies when its own inputs
        # are empty (Rule 8) — this guard existed for foundation already
        # but was missing here (bug-hunt finding). Checked explicitly
        # rather than via dataclasses.asdict(): category/stage/catalysts
        # default to UNKNOWN/() rather than None, so they are never "empty"
        # by an asdict-values None-check and would make that check a no-op.
        ni = judgment.narrative_inputs
        # Gate STRICTLY on the numeric 0-100 judgment slots — the only fields
        # that can anchor a score. Opening on flags/summary/catalysts let an
        # all-null judgment carrying just a stage guess mint a 90/100
        # narrative category (stage -> cultural_timing 90 -> viral_potential
        # 90) with zero actual narrative judgment, RAISING the master score
        # (bug-hunt finding, reproduced). Risk flags and the summary still
        # ride along once a real slot exists.
        has_narrative_evidence = any(v is not None for v in (
            ni.memorability, ni.shareability, ni.emotional_impact,
            ni.cultural_timing, ni.community_participation, ni.meme_strength,
            ni.community_creativity, ni.long_term_strength,
        ))
        if narrative is None and has_narrative_evidence:  # explicit analyst inputs always win
            try:
                narrative = self._narrative.assess(
                    result.pair.base_token, judgment.narrative_inputs,
                    community=result.community,
                    positive_sentiment_percent=(
                        result.community_profile.positive_sentiment_percent
                        if result.community_profile else None),
                )
            except (InsufficientDataError, ValueError):
                narrative = None

        foundation = None
        has_foundation_evidence = any(
            v is not None for v in dataclasses.asdict(judgment.foundation_inputs).values()
        )
        if has_foundation_evidence:
            # Only fold community_quality in when the AI actually contributed
            # a foundation judgment — otherwise this would fabricate a
            # foundation score from the community score alone (double-
            # counting community and moving the master score with zero new
            # evidence, Rule 8). An artificial community's score describes
            # bots, not brand/dev-communication quality, so exclude it here
            # too (mirrors the same guard in NarrativeAnalyzer).
            community_quality = None
            if result.community is not None and not result.community.is_artificial:
                community_quality = result.community.overall_score
            try:
                foundation = self._foundation.assess(
                    result.pair.base_token, judgment.foundation_inputs,
                    community_quality=community_quality,
                )
            except InsufficientDataError:
                pass

        master = self._scoring.evaluate(
            result.security,
            community=result.community,
            onchain=result.onchain,
            foundation=foundation,
            token_structure=result.token,
            risk=result.risk,
            momentum_score=result.momentum.overall_score if result.momentum else None,
            narrative_score=narrative.overall_score if narrative else None,
            timing_score=derive_timing_score(result.pair, result.token, result.onchain,
                                             now_func=self._now),
        )
        return dataclasses.replace(
            result, master=master, narrative=narrative,
            foundation=foundation, ai_judgment=judgment,
            opportunity=self._opportunity.rank(master.category_scores, result.risk.risk_score),
        )
