"""Framework enumerations (Spec Part 1, Part 10/20, Part 31 Consistency Lock)."""

from __future__ import annotations

from enum import Enum


class Classification(str, Enum):
    """Final opportunity classification (Spec Part 20, Section 4).

    Score bands are configurable via ``ClassificationBands`` in settings;
    the canonical defaults are 90/80/70/60 (Parts 10, 12, 20).
    """

    ELITE_OPPORTUNITY = "elite_opportunity"    # 90-100
    STRONG_CANDIDATE = "strong_candidate"      # 80-89
    WATCHLIST = "watchlist"                    # 70-79
    SPECULATIVE = "speculative"                # 60-69
    AVOID = "avoid"                            # below 60, or red-flag override


class RiskTier(str, Enum):
    """Permanent risk taxonomy (Spec Part 31 Consistency Lock, Part 33).

    ACCEPTABLE_UNCERTAINTY  -- normal early-stage risk: new, small, unknown team.
                               Reduces confidence, never auto-rejects.
    SERIOUS_WARNING         -- unusual wallet behavior, poor distribution,
                               suspicious permissions. Requires deeper review.
    DESTRUCTIVE             -- honeypot, fake liquidity, hidden control,
                               clear fraud. Invalidates the opportunity.
    """

    ACCEPTABLE_UNCERTAINTY = "acceptable_uncertainty"
    SERIOUS_WARNING = "serious_warning"
    DESTRUCTIVE = "destructive"


class ConfidenceLevel(str, Enum):
    """Analysis confidence (Spec Part 13 Section 9, Part 23 Section 6)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MarketPhase(str, Enum):
    """Accumulation vs distribution classification (Spec Part 6, Section 14)."""

    ACCUMULATION = "accumulation"    # price stable, buying pressure, holders growing
    EXPANSION = "expansion"          # rising demand, volume, and attention
    DISTRIBUTION = "distribution"    # volume without price progress; sellers active
    UNCLEAR = "unclear"              # insufficient evidence to classify


class CommunityRating(str, Enum):
    """Community quality rating (Spec Part 12, Section 5)."""

    EXCELLENT = "excellent"
    STRONG = "strong"
    AVERAGE = "average"
    WEAK = "weak"
    ARTIFICIAL = "artificial"  # fake community detected — red-flag override


class MarketCapStage(str, Enum):
    """Market-cap maturity staging (Spec Part 7, Section 2)."""

    EARLY = "early"        # low cap, low awareness: higher upside, higher risk
    GROWTH = "growth"      # increasing attention and liquidity
    MATURE = "mature"      # large cap: lower upside, lower volatility
    UNKNOWN = "unknown"


class ValuationClassification(str, Enum):
    """Structural valuation assessment (Spec Part 7, Section 14)."""

    UNDERVALUED = "undervalued"
    FAIRLY_VALUED = "fairly_valued"
    EXPENSIVE = "expensive"
    OVERVALUED = "overvalued"
    UNKNOWN = "unknown"


class SetupType(str, Enum):
    """Trading setup classification (Spec Part 8, Section 2)."""

    EARLY_DISCOVERY = "early_discovery"        # highest upside, highest uncertainty
    CONFIRMATION = "confirmation"              # evidence appeared, higher entry
    TREND_CONTINUATION = "trend_continuation"  # established momentum
    WATCH_ONLY = "watch_only"                  # not currently tradable


class ConvictionLevel(str, Enum):
    """Position conviction driving size guidance (Spec Part 8, Section 5)."""

    HIGH = "high"
    MEDIUM = "medium"
    SPECULATIVE = "speculative"
    NO_TRADE = "no_trade"  # destructive risk or failed gates


class MarketRegime(str, Enum):
    """Overall market environment (Spec Part 8, Section 10)."""

    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"
    UNKNOWN = "unknown"


class CheckStatus(str, Enum):
    """Entry-checklist item status (Spec Part 8, Section 3)."""

    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"  # unverified is not a pass (Rule 8)


class RiskCategory(str, Enum):
    """Overall risk classification (Spec Part 9, Section 7). Higher = riskier."""

    LOW_RELATIVE = "low_relative"  # still speculative — meme coins always are
    MODERATE = "moderate"
    HIGH = "high"
    EXTREME = "extreme"            # avoid


class RiskPosture(str, Enum):
    """Portfolio operating mode under drawdown (Spec Part 9, Sections 8-9)."""

    NORMAL = "normal"
    REDUCED = "reduced"        # smaller positions, fewer trades, more confirmation
    DEFENSIVE = "defensive"    # capital preservation only


class ScanLayer(str, Enum):
    """The four monitoring layers of the real-time architecture (Spec Part 2, Section 4)."""

    DISCOVERY = "discovery"        # every 5-10s: find newly emerging opportunities
    SECURITY = "security"          # immediately after discovery: reject dangerous projects
    INTELLIGENCE = "intelligence"  # deeper analysis: community, narrative, wallets
    ALERT = "alert"                # human review alerts for qualifying tokens only
