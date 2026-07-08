"""Final intelligence report renderer (Spec Part 12, Part 20 Section 5).

Assembles every engine's assessment into the canonical MEME COIN
INTELLIGENCE REPORT: executive summary, per-category sections, bull and
bear case, scoring table, final classification, trade planning, and the
final verdict.

The bull/bear cases here are **evidence-derived**: bullets are extracted
from actual findings, scores, and structural facts. The LLM layer (Parts
13/23) will later enrich them with narrative prose — it will never replace
the evidence basis (Rule 8, Part 16 Rule 1).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from meme_intelligence.analyzers.community_analyzer import CommunityAssessment
from meme_intelligence.analyzers.momentum_analyzer import MomentumAssessment
from meme_intelligence.analyzers.narrative_analyzer import NarrativeAssessment
from meme_intelligence.analyzers.onchain_analyzer import OnChainAssessment
from meme_intelligence.analyzers.risk_analyzer import RiskAssessment
from meme_intelligence.analyzers.scoring_engine import MasterAssessment
from meme_intelligence.analyzers.security_analyzer import SecurityAssessment
from meme_intelligence.analyzers.token_analyzer import TokenAssessment
from meme_intelligence.core.enums import (
    Classification,
    MarketCapStage,
    MarketPhase,
    NarrativeRisk,
    RiskTier,
)
from meme_intelligence.core.models import DexPair
from meme_intelligence.trading.trade_planner import TradePlan

_PASSING_CLASSIFICATIONS = {
    Classification.ELITE_OPPORTUNITY,
    Classification.STRONG_CANDIDATE,
    Classification.WATCHLIST,
}


@dataclass(frozen=True)
class IntelligenceReport:
    """The rendered final report plus its structured basis."""

    master: MasterAssessment
    text: str


def build_report(
    pair: DexPair,
    master: MasterAssessment,
    security: SecurityAssessment,
    *,
    onchain: OnChainAssessment | None = None,
    token: TokenAssessment | None = None,
    community: CommunityAssessment | None = None,
    narrative: NarrativeAssessment | None = None,
    momentum: MomentumAssessment | None = None,
    risk: RiskAssessment | None = None,
    plan: TradePlan | None = None,
) -> IntelligenceReport:
    """Assemble the canonical intelligence report (Part 12 section order)."""
    symbol = pair.base_token.symbol or pair.base_token.address[:8]

    def money(value: float | None) -> str:
        return f"${value:,.0f}" if value is not None else "unknown"

    lines: list[str] = []
    add = lines.append

    # 1. Executive summary (Part 12, Section 1)
    add("=" * 72)
    add(f"MEME COIN INTELLIGENCE REPORT — {symbol}")
    add("=" * 72)
    add(f"Chain: {pair.chain}    Contract: {pair.base_token.address}")
    launched = pair.pair_created_at.strftime("%Y-%m-%d %H:%M UTC") if pair.pair_created_at else "unknown"
    add(f"Pair created: {launched}")
    add(f"Market cap: {money(pair.market_cap)}    FDV: {money(pair.fdv)}    "
        f"Liquidity: {money(pair.liquidity_usd)}    24h volume: {money(pair.volume_24h)}")
    add(f"Overall score: {master.final_score:.0f}/100    "
        f"Classification: {master.classification.value.upper()}    "
        f"Confidence: {master.confidence.value}    "
        f"Evidence coverage: {master.coverage:.0%}")

    # 2. Investment thesis (Part 12, Section 2) — evidence-derived
    bull = _bull_case(security, onchain, token, community, narrative, pair)
    bear = _bear_case(security, onchain, token, community, narrative, risk, master)
    add("")
    add("WHY THIS TOKEN COULD SUCCEED")
    for bullet in bull or ["No positive evidence collected yet."]:
        add(f"  + {bullet}")
    add("")
    add("WHY THIS TOKEN COULD FAIL")
    for bullet in bear or ["No specific negative evidence collected — absence of evidence is not safety."]:
        add(f"  - {bullet}")

    # 3-10. Category sections (each engine renders its own report format)
    for section in (security, community, narrative, onchain, token, momentum, risk):
        if section is not None:
            add("")
            add(section.summary())

    # 11. Complete scoring table (Part 12, Section 11)
    add("")
    add("COMPLETE SCORING TABLE")
    for name, value in dataclasses.asdict(master.category_scores).items():
        rendered = f"{value:.0f}/100" if value is not None else "no data"
        add(f"  {name:>10}: {rendered}")
    add(f"  {'FINAL':>10}: {master.final_score:.0f}/100")

    # 12. Final classification + decision trace
    add("")
    add(f"FINAL CLASSIFICATION: {master.classification.value.upper()}")
    if master.overrides:
        for reason in master.overrides:
            add(f"  RED FLAG OVERRIDE: {reason}")
    for step in master.decision_trace:
        add(f"  [{step.answer:>7}] {step.question} -> {step.action}")

    # 13. Trade planning section (only when the token qualifies — Part 12, Section 13)
    if plan is not None and master.classification in _PASSING_CLASSIFICATIONS:
        add("")
        add(plan.render())
    elif plan is not None:
        add("")
        add("TRADE PLANNING: not applicable — token did not qualify "
            f"({master.classification.value}).")

    # 14. Final AI verdict (Part 12, Section 14)
    add("")
    add("FINAL VERDICT")
    passes = master.classification in _PASSING_CLASSIFICATIONS and not master.overrides
    add(f"  Would this pass a professional research filter? {'YES' if passes else 'NO'}")
    add(f"  Main reason: {_main_reason(master, security)}")
    add(f"  Biggest risk: {_biggest_risk(security, risk, bear)}")
    add(f"  Biggest opportunity: {_biggest_opportunity(master, bull)}")
    add(f"  What would change this opinion: {_opinion_changers(master, security, community)}")
    add("=" * 72)

    return IntelligenceReport(master=master, text="\n".join(lines))


# ---- Evidence extraction ----

def _bull_case(security, onchain, token, community, narrative, pair) -> list[str]:
    bullets: list[str] = []
    if security.overall_score >= 75 and not security.is_destructive:
        bullets.append(f"Security profile is {security.band} ({security.overall_score:.0f}/100)")
    if token is not None:
        if token.stage is MarketCapStage.EARLY:
            bullets.append("Early-stage market cap leaves structural room to grow")
        if token.valuation.value == "undervalued":
            bullets.append("Structure shows real demand and depth relative to size")
    if onchain is not None:
        if onchain.phase in (MarketPhase.ACCUMULATION, MarketPhase.EXPANSION):
            bullets.append(f"On-chain phase is {onchain.phase.value} (demand-side behavior)")
        volume_quality = onchain.sub_scores.get("volume_quality")
        if volume_quality is not None and volume_quality >= 70:
            bullets.append("Trading volume looks organic (many independent wallets)")
    if community is not None and community.overall_score >= 70 and not community.is_artificial:
        bullets.append(f"Community rated {community.rating.value} with organic engagement")
    if narrative is not None and narrative.overall_score >= 70:
        bullets.append(f"Narrative rated {narrative.rating.value} "
                       f"({narrative.overall_score:.0f}/100, stage: {narrative.stage.value})")
    if pair.liquidity_usd is not None and pair.liquidity_usd >= 50000:
        bullets.append(f"Liquidity depth ${pair.liquidity_usd:,.0f} supports entries and exits")
    return bullets


def _bear_case(security, onchain, token, community, narrative, risk, master) -> list[str]:
    bullets: list[str] = []
    for finding in security.findings:
        if finding.severity in (RiskTier.DESTRUCTIVE, RiskTier.SERIOUS_WARNING):
            bullets.append(finding.message)
    if community is not None and community.is_artificial:
        bullets.append("community engagement is artificial")
    if narrative is not None and narrative.narrative_risk is NarrativeRisk.HIGH:
        bullets.append("narrative risk is high: the story may not outlast current attention")
    if token is not None and token.valuation.value in ("expensive", "overvalued"):
        bullets.append(f"valuation reads {token.valuation.value} at the current stage")
    if onchain is not None and onchain.phase is MarketPhase.DISTRIBUTION:
        bullets.append("on-chain phase is distribution: sellers currently dominate")
    if master.coverage < 0.7:
        bullets.append(
            f"only {master.coverage:.0%} of the framework has data — "
            "unverified areas may hide risks"
        )
    if risk is not None:
        for item in risk.main_risks:
            if item not in bullets:
                bullets.append(item)
    return bullets[:8]


def _main_reason(master, security) -> str:
    if master.overrides:
        return master.overrides[0]
    scores = dataclasses.asdict(master.category_scores)
    known = {k: v for k, v in scores.items() if v is not None}
    if not known:
        return "insufficient data"
    weakest = min(known, key=known.get)
    strongest = max(known, key=known.get)
    if master.classification in _PASSING_CLASSIFICATIONS:
        return f"strongest category is {strongest} ({known[strongest]:.0f}/100) with no red flags"
    return f"weakest category is {weakest} ({known[weakest]:.0f}/100)"


def _biggest_risk(security, risk, bear: list[str]) -> str:
    if security.destructive_findings:
        return security.destructive_findings[0].message
    if bear:
        return bear[0]
    return "general early-stage meme coin risk"


def _biggest_opportunity(master, bull: list[str]) -> str:
    if bull:
        return bull[0]
    return "none identified from current evidence"


def _opinion_changers(master, security, community) -> str:
    changers: list[str] = []
    if security.unknown_fields:
        changers.append("verified security facts for the unknown fields")
    if community is None:
        changers.append("real community data")
    if master.category_scores.narrative is None:
        changers.append("a scored narrative assessment")
    if master.category_scores.momentum is None:
        changers.append("momentum confirmation")
    if master.overrides:
        changers.append("nothing short of the destructive findings being disproven")
    return "; ".join(changers) if changers else "material change in the evidence above"
