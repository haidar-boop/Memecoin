"""Shared research pipeline (Spec Part 13 Section 2, Part 22 Section 2).

One implementation of the per-token analysis chain — security -> on-chain
-> token structure -> momentum -> risk -> master assessment — used by the
daily routine, the continuous scanner, and the CLI. A single pipeline
keeps every entry point scoring tokens identically (Rule 4: one purpose
per module; Rule 18: extend, don't duplicate).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from meme_intelligence.analyzers.momentum_analyzer import MomentumAnalyzer, MomentumAssessment
from meme_intelligence.analyzers.onchain_analyzer import (
    OnChainAnalyzer,
    OnChainAssessment,
    derive_onchain_profile,
)
from meme_intelligence.analyzers.risk_analyzer import RiskAnalyzer, RiskAssessment
from meme_intelligence.analyzers.scoring_engine import (
    MasterAssessment,
    ScoringEngine,
    derive_timing_score,
)
from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer, SecurityAssessment
from meme_intelligence.analyzers.token_analyzer import TokenAnalyzer, TokenAssessment
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.enums import MarketRegime
from meme_intelligence.core.errors import CollectorError, InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair, SecurityProfile


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


class ResearchPipeline:
    """Runs the full analysis chain for one pair (Layers 2-3 + scoring)."""

    def __init__(
        self,
        settings: Settings,
        goplus_client,  # GoPlusClient-compatible (get_token_security)
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._goplus = goplus_client
        self._now = now_func
        self._logger = get_logger("workflow.pipeline")

        self._security = SecurityAnalyzer(settings.security, settings.security_weights)
        self._onchain = OnChainAnalyzer(settings.onchain, settings.onchain_weights)
        self._token = TokenAnalyzer(settings.token, settings.token_weights)
        self._momentum = MomentumAnalyzer(settings.momentum, settings.momentum_weights,
                                          now_func=now_func)
        self._risk = RiskAnalyzer(settings.risk_weights)
        self._scoring = ScoringEngine(settings.weights, settings.bands, now_func=now_func)

    async def analyze_pair(
        self,
        pair: DexPair,
        regime: MarketRegime = MarketRegime.UNKNOWN,
    ) -> PipelineResult | None:
        """Full chain for one pair; ``None`` when security data is unavailable
        (a token that cannot be security-screened is not analyzable — Part 4)."""
        try:
            profile = await self._goplus.get_token_security(pair.chain, pair.base_token.address)
        except CollectorError as exc:
            self._logger.info("security data unavailable for %s/%s: %s",
                              pair.chain, pair.base_token.address, exc)
            return None
        if profile is None:
            return None

        try:
            security = self._security.assess(profile, pair)
        except InsufficientDataError:
            return None

        onchain = token = momentum = None
        try:
            onchain = self._onchain.assess(derive_onchain_profile(pair, profile))
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

        risk = self._risk.assess(security, pair=pair, token=token,
                                 onchain=onchain, regime=regime)
        master = self._scoring.evaluate(
            security,
            onchain=onchain,
            token_structure=token,
            risk=risk,
            momentum_score=momentum.overall_score if momentum else None,
            timing_score=derive_timing_score(pair, token, onchain, now_func=self._now),
        )
        return PipelineResult(
            pair=pair, security_profile=profile, security=security,
            onchain=onchain, token=token, momentum=momentum, risk=risk, master=master,
        )
