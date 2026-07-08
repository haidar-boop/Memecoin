"""AI reasoning layer (Spec Part 23; Part 22 Section 11; Part 13 Section 4).

Connects the intelligence pipeline to an LLM (Claude) that produces the
qualitative judgments the deterministic engines cannot compute from market
numbers: the :class:`~meme_intelligence.analyzers.foundation_analyzer.FoundationInputs`
and :class:`~meme_intelligence.analyzers.narrative_analyzer.NarrativeInputs`
slots, bull/bear reasoning prose, and a Part 23 Section 6 confidence score.

Architecture decisions (Rule 19; recorded in handoff/DECISIONS_LOG.md):

* **The deterministic engines stay canonical.** Part 23 Section 2 describes
  seven specialized agents; the security / blockchain / market / decision
  agents already exist as deterministic analyzers whose scoring is locked
  by Part 31. The LLM covers the judgment work of the Community and
  Narrative analyst agents plus report reasoning — it feeds *inputs* into
  the locked framework and never overrides a computed score.
* **The AI never receives raw data** (Part 23 mission): the pipeline's
  assessments are condensed into the Section 3 structured snapshot
  (token / market / security / community / wallets) before the request.
* **Structured output, validated twice.** The response is constrained to a
  JSON schema at the API level, then range-checked and passed through the
  Part 16 banned-language guard here. A judgment that fails validation is
  discarded — the pipeline continues on deterministic evidence alone
  (Rules 6/9: degrade gracefully).
* **Unknown stays unknown** (Rule 8): every judgment slot is nullable, and
  the prompt requires null over guessing when evidence is missing.

Sections 7-8 (AI memory and the feedback loop) ride on the existing
``snapshots`` journal tables and become active learning in Part 24.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from meme_intelligence.ai.prompts import ANALYST_SYSTEM_PROMPT, check_language
from meme_intelligence.analyzers.foundation_analyzer import FoundationInputs
from meme_intelligence.analyzers.narrative_analyzer import NarrativeInputs
from meme_intelligence.config.settings import AISettings
from meme_intelligence.core.enums import (
    NarrativeCategory,
    NarrativeStage,
    ResearchMode,
)
from meme_intelligence.core.errors import ConfigurationError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.rate_limiter import RateLimiter

_JUDGMENT_SLOTS_0_100 = (
    # FoundationInputs (Part 5 Section 12 slots)
    "meme_strength", "narrative", "brand", "dev_communication", "long_term",
    # NarrativeInputs (Part 19 Sections 3/11 slots)
    "memorability", "shareability", "emotional_impact", "cultural_timing",
    "community_participation", "community_creativity", "long_term_strength",
)

_RISK_FLAGS = ("short_term_hype_risk", "trend_dependency_risk", "copycat_risk")

# JSON schema for the structured judgment (Part 23 Section 4 output format).
# Every judgment is nullable: null means "insufficient evidence" (Rule 8).
# Structured outputs reject numeric minimum/maximum constraints, so the
# 0-100 range lives in the description and is enforced when parsing.
_NULLABLE_SCORE = {"type": ["number", "null"],
                   "description": "0-100 judgment, or null when evidence is insufficient"}
_NULLABLE_BOOL = {"type": ["boolean", "null"]}
JUDGMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        **{name: _NULLABLE_SCORE for name in _JUDGMENT_SLOTS_0_100},
        **{name: _NULLABLE_BOOL for name in _RISK_FLAGS},
        "narrative_category": {
            "type": "string",
            "enum": [c.value for c in NarrativeCategory],
        },
        "narrative_stage": {
            "type": "string",
            "enum": [s.value for s in NarrativeStage],
        },
        "narrative_summary": {"type": ["string", "null"]},
        "bull_case": {"type": "array", "items": {"type": "string"}},
        "bear_case": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number",
                       "description": "0-100 overall judgment confidence (Part 23 Section 6)"},
        "confidence_reason": {"type": "string"},
    },
    "required": [
        *(name for name in _JUDGMENT_SLOTS_0_100),
        *(name for name in _RISK_FLAGS),
        "narrative_category", "narrative_stage", "narrative_summary",
        "bull_case", "bear_case", "confidence", "confidence_reason",
    ],
    "additionalProperties": False,
}

_MODE_INSTRUCTIONS = {
    ResearchMode.FAST_SCAN: (
        "MODE: FAST SCAN (Part 23 Section 10). Quick filtering only: judge "
        "the slots you have clear evidence for, list only the major risks in "
        "the bear case, keep prose minimal. Critical red flags are NEVER "
        "skipped for speed."
    ),
    ResearchMode.STANDARD: (
        "MODE: STANDARD RESEARCH. Normal full evaluation of every judgment "
        "slot the evidence supports."
    ),
    ResearchMode.DEEP_INVESTIGATION: (
        "MODE: DEEP INVESTIGATION. High-conviction research: weigh the "
        "historical patterns, wallet evidence, community texture, and "
        "competitive position carefully before each judgment; explain the "
        "decisive evidence in the bull/bear cases."
    ),
}

_JUDGMENT_TASK = """\
TASK
Using ONLY the structured intelligence snapshot below, produce the
qualitative judgments the deterministic scoring framework cannot compute.
Process in order (Part 23 Section 5): verify what data is present; identify
immediate risks; then analyze opportunity; then judge.

RULES
- A judgment slot must be null when the snapshot lacks the evidence to
  support it. Null is the correct answer for unknown — never guess a number.
- Judgments are 0-100. 50 is neutral; reserve 80+ and 20- for clear evidence.
- Avoid confirmation bias, recency bias, popularity bias, and wallet bias
  (Part 23 Section 9): actively look for contradictions and negative
  evidence; recent price growth does not mean future success; large
  communities are not automatically real communities; large wallets are not
  automatically correct.
- Set confidence (0-100, Part 23 Section 6) from data quality, source
  agreement, market stability, and analysis complexity — partial snapshots
  mean low confidence.
- bull_case / bear_case: 2-5 short evidence-grounded bullets each, tied to
  facts in the snapshot. Both sides are always required.
- narrative_summary: 1-2 sentences explaining the story behind the token,
  or null if the snapshot gives no basis to know it.

INTELLIGENCE SNAPSHOT
"""


def build_intelligence_snapshot(result) -> dict:
    """Condense a ``PipelineResult`` into the Part 23 Section 3 input format.

    Never sends raw API payloads — only the normalized facts and the
    deterministic engines' findings. Unknown values stay ``None`` so the
    model sees exactly what is and is not verified (Rule 8).
    """
    pair = result.pair
    profile = result.security_profile
    security = result.security

    def findings(assessment) -> list[str]:
        if assessment is None:
            return []
        return [f"[{f.severity.value}] {f.message}" for f in assessment.findings]

    snapshot = {
        "token": {
            "name": pair.base_token.name,
            "symbol": pair.base_token.symbol,
            "chain": pair.chain,
            "contract": pair.base_token.address,
            "pair_created_at": pair.pair_created_at.isoformat() if pair.pair_created_at else None,
        },
        "market": {
            "price_usd": pair.price_usd,
            "market_cap": pair.market_cap,
            "fdv": pair.fdv,
            "liquidity_usd": pair.liquidity_usd,
            "volume_24h": pair.volume_24h,
            "price_change_24h_percent": pair.price_change_24h,
            "price_change_1h_percent": pair.price_change_1h,
            "buys_24h": pair.buys_24h,
            "sells_24h": pair.sells_24h,
        },
        "security": {
            "score": round(security.overall_score),
            "destructive": security.is_destructive,
            "ownership_renounced": profile.ownership_renounced,
            "honeypot": profile.is_honeypot,
            "lp_locked_percent": profile.lp_locked_percent,
            "top10_holder_percent": profile.top10_holder_percent,
            "findings": findings(security),
        },
        "community": _community_section(result),
        "wallets": {
            "holder_count": profile.holder_count,
            "onchain_score": round(result.onchain.overall_score) if result.onchain else None,
            "onchain_phase": result.onchain.phase.value if result.onchain else None,
            "onchain_findings": findings(result.onchain),
            "wallet_findings": findings(result.wallet),
            "smart_money_score": round(result.wallet.overall_score) if result.wallet else None,
        },
        "structure": {
            "token_score": round(result.token.overall_score) if result.token else None,
            "stage": result.token.stage.value if result.token else None,
            "valuation": result.token.valuation.value if result.token else None,
        },
        "momentum": {
            "score": round(result.momentum.overall_score) if result.momentum else None,
            "entry_zone": result.momentum.entry_zone.value if result.momentum else None,
            "findings": findings(result.momentum),
        },
    }
    return snapshot


def _community_section(result) -> dict:
    """Community facts for the snapshot; explicit about what is untracked."""
    profile = getattr(result, "community_profile", None)
    community = getattr(result, "community", None)
    if profile is None:
        return {
            "assessed": False,
            "note": "no community data source reported this token "
                    "(unlisted or collectors unavailable)",
        }
    section = {
        "assessed": community is not None,
        "source": profile.source,
        "telegram_members": profile.telegram_members,
        "reddit_subscribers": profile.reddit_subscribers,
        "user_content_per_day": profile.user_content_per_day,
        "positive_sentiment_percent": profile.positive_sentiment_percent,
        "note": "twitter engagement, discord, and bot detection are not "
                "tracked by the current source",
    }
    if community is not None:
        section["score"] = round(community.overall_score)
        section["rating"] = community.rating.value
        section["findings"] = [f"[{f.severity.value}] {f.message}"
                               for f in community.findings]
    return section


@dataclass(frozen=True)
class AIJudgment:
    """Validated qualitative judgments from the reasoning layer (Part 23)."""

    foundation_inputs: FoundationInputs
    narrative_inputs: NarrativeInputs
    bull_case: tuple[str, ...]
    bear_case: tuple[str, ...]
    confidence: float          # 0-100, Part 23 Section 6
    confidence_reason: str
    mode: ResearchMode
    model: str

    def summary(self) -> str:
        lines = [
            f"AI analyst judgment ({self.model}, mode={self.mode.value})",
            f"  Judgment confidence: {self.confidence:.0f}/100 — {self.confidence_reason}",
        ]
        if self.bull_case:
            lines.append("  Bull case (AI):")
            for bullet in self.bull_case:
                lines.append(f"    + {bullet}")
        if self.bear_case:
            lines.append("  Bear case (AI):")
            for bullet in self.bear_case:
                lines.append(f"    - {bullet}")
        return "\n".join(lines)


class AIJudgmentError(Exception):
    """The model's response could not be validated into a judgment."""


class AIJudgmentService:
    """Produces structured qualitative judgments via the Anthropic API.

    ``client`` is an ``anthropic.AsyncAnthropic``-compatible object (tests
    inject a fake); use :func:`build_judgment_service` for the real one.
    Requests are rate-limited (Rule 11) and failures degrade gracefully:
    callers treat ``None`` judgments as "AI unavailable" and continue on
    deterministic evidence (Rule 9).
    """

    def __init__(self, settings: AISettings, client) -> None:
        self._s = settings
        self._client = client
        self._limiter = RateLimiter.per_minute(settings.requests_per_minute)
        self._logger = get_logger("ai.reasoning")

    async def judge(
        self,
        result,
        mode: ResearchMode = ResearchMode.STANDARD,
    ) -> AIJudgment | None:
        """Judgment for one ``PipelineResult``; ``None`` when the model is
        unavailable, refuses, or returns an invalid judgment."""
        snapshot = build_intelligence_snapshot(result)
        token = result.pair.base_token
        await self._limiter.acquire()
        try:
            response = await self._client.messages.create(
                model=self._s.model,
                max_tokens=self._s.max_tokens,
                system=ANALYST_SYSTEM_PROMPT,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self._s.effort,
                    "format": {"type": "json_schema", "schema": JUDGMENT_SCHEMA},
                },
                messages=[{
                    "role": "user",
                    "content": _MODE_INSTRUCTIONS[mode] + "\n\n" + _JUDGMENT_TASK
                    + json.dumps(snapshot, indent=2, sort_keys=True),
                }],
                timeout=self._s.timeout_seconds,
            )
        except Exception as exc:  # API/network errors: degrade, never crash (Rule 6)
            self._logger.warning("AI judgment unavailable for %s/%s: %s",
                                 token.chain, token.address, exc)
            return None

        if getattr(response, "stop_reason", None) == "refusal":
            self._logger.warning("AI judgment refused for %s/%s", token.chain, token.address)
            return None

        try:
            judgment = self._parse(response, mode)
        except AIJudgmentError as exc:
            self._logger.warning("AI judgment invalid for %s/%s: %s",
                                 token.chain, token.address, exc)
            return None

        if judgment.confidence < self._s.min_confidence:
            self._logger.info(
                "AI judgment below confidence floor for %s/%s (%.0f < %.0f): discarded",
                token.chain, token.address, judgment.confidence, self._s.min_confidence,
            )
            return None

        self._logger.info("AI judgment %s/%s: confidence=%.0f mode=%s",
                          token.chain, token.address, judgment.confidence, mode.value)
        return judgment

    # ---- Response validation (Part 23 Section 4 output requirements) ----

    def _parse(self, response, mode: ResearchMode) -> AIJudgment:
        text = next(
            (block.text for block in response.content if getattr(block, "type", "") == "text"),
            None,
        )
        if not text:
            raise AIJudgmentError("response contained no text block")
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise AIJudgmentError(f"response is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise AIJudgmentError("response JSON is not an object")

        def score(name: str) -> float | None:
            value = data.get(name)
            if value is None:
                return None
            if not isinstance(value, (int, float)) or not (0.0 <= value <= 100.0):
                raise AIJudgmentError(f"judgment '{name}' out of range: {value!r}")
            return float(value)

        def flag(name: str) -> bool | None:
            value = data.get(name)
            if value is not None and not isinstance(value, bool):
                raise AIJudgmentError(f"risk flag '{name}' is not a boolean: {value!r}")
            return value

        prose_parts = [
            str(data.get("narrative_summary") or ""),
            str(data.get("confidence_reason") or ""),
            *(str(b) for b in data.get("bull_case") or []),
            *(str(b) for b in data.get("bear_case") or []),
        ]
        violations = check_language("\n".join(prose_parts))
        if violations:
            raise AIJudgmentError(f"banned language in judgment prose: {violations}")

        try:
            category = NarrativeCategory(data.get("narrative_category", "unknown"))
            stage = NarrativeStage(data.get("narrative_stage", "unknown"))
        except ValueError as exc:
            raise AIJudgmentError(f"invalid narrative classification: {exc}") from exc

        confidence = score("confidence")
        if confidence is None:
            raise AIJudgmentError("judgment omitted the required confidence score")

        foundation = FoundationInputs(
            meme_strength=score("meme_strength"),
            narrative=score("narrative"),
            brand=score("brand"),
            dev_communication=score("dev_communication"),
            long_term=score("long_term"),
        )
        narrative = NarrativeInputs(
            narrative_summary=data.get("narrative_summary") or None,
            memorability=score("memorability"),
            shareability=score("shareability"),
            emotional_impact=score("emotional_impact"),
            cultural_timing=score("cultural_timing"),
            community_participation=score("community_participation"),
            meme_strength=score("meme_strength"),
            community_creativity=score("community_creativity"),
            long_term_strength=score("long_term_strength"),
            category=category,
            stage=stage,
            short_term_hype_risk=flag("short_term_hype_risk"),
            trend_dependency_risk=flag("trend_dependency_risk"),
            copycat_risk=flag("copycat_risk"),
        )
        return AIJudgment(
            foundation_inputs=foundation,
            narrative_inputs=narrative,
            bull_case=tuple(str(b) for b in data.get("bull_case") or []),
            bear_case=tuple(str(b) for b in data.get("bear_case") or []),
            confidence=confidence,
            confidence_reason=str(data.get("confidence_reason") or ""),
            mode=mode,
            model=self._s.model,
        )


def build_judgment_service(settings) -> AIJudgmentService | None:
    """Build the real service from root :class:`Settings`; ``None`` without a key.

    Imports the Anthropic SDK lazily so the rest of the system runs without
    the dependency installed (Rule 9 — degrade when a provider is missing).
    """
    if not settings.anthropic_api_key:
        return None
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise ConfigurationError(
            "MEMEINTEL_ANTHROPIC_API_KEY is set but the 'anthropic' package is "
            "not installed — run: pip install anthropic"
        ) from exc
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return AIJudgmentService(settings.ai, client)
