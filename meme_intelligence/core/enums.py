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


class WatchlistTier(str, Enum):
    """Watchlist tracking tiers (Spec Part 11, Section 5 / Part 3, Section 10)."""

    TIER_1_HIGH_PRIORITY = "tier_1_high_priority"  # monitor frequently
    TIER_2_DEVELOPING = "tier_2_developing"        # review daily
    TIER_3_RESEARCH_ONLY = "tier_3_research_only"  # monitor occasionally
    ARCHIVED = "archived"                          # failed criteria; kept for learning


class EntryZone(str, Enum):
    """Entry timing zone (Spec Part 14, Section 6)."""

    EARLY = "early"                # strong fundamentals, limited hype: highest upside
    CONFIRMATION = "confirmation"  # market validating: higher confidence, higher price
    LATE = "late"                  # extreme extension: risk of buying into distribution
    UNCLEAR = "unclear"


class PreferredAction(str, Enum):
    """Momentum-report preferred action (Spec Part 14, Section 14)."""

    MONITOR = "monitor"
    WAIT_FOR_CONFIRMATION = "wait_for_confirmation"
    CONSIDER_RESEARCH_ENTRY = "consider_research_entry"
    AVOID = "avoid"


class AlertPriority(str, Enum):
    """Alert priority levels (Spec Part 29, Section 2)."""

    CRITICAL = "critical"  # immediate attention: rug indicators, destructive risk
    HIGH = "high"          # important opportunity or risk
    MEDIUM = "medium"      # useful information
    LOW = "low"            # background information


class WhaleType(str, Enum):
    """Whale behavioral classification (Spec Part 17, Section 6)."""

    LONG_TERM = "long_term"   # holds through volatility, minimal selling
    TRADING = "trading"       # frequent buys/sells, shorter horizon
    RISK = "risk"             # concentration large enough to crash the price
    CUSTODIAL = "custodial"   # pool/exchange/program account, not a person


class AccumulationVerdict(str, Enum):
    """Accumulation pattern classification (Spec Part 17, Section 5)."""

    HEALTHY = "healthy"        # many independent wallets, gradual buying
    MIXED = "mixed"
    ARTIFICIAL = "artificial"  # coordinated/same-size/dominated buying
    UNKNOWN = "unknown"


class ScanLayer(str, Enum):
    """The four monitoring layers of the real-time architecture (Spec Part 2, Section 4)."""

    DISCOVERY = "discovery"        # every 5-10s: find newly emerging opportunities
    SECURITY = "security"          # immediately after discovery: reject dangerous projects
    INTELLIGENCE = "intelligence"  # deeper analysis: community, narrative, wallets
    ALERT = "alert"                # human review alerts for qualifying tokens only


class NarrativeCategory(str, Enum):
    """Narrative classification (Spec Part 19, Sections 2 and 12)."""

    INTERNET_CULTURE = "internet_culture"  # viral characters, online jokes, movements
    ANIMAL = "animal"
    AI = "ai"                              # AI / technology narratives
    GAMING = "gaming"
    CELEBRITY = "celebrity"                # celebrity / attention narratives
    CULTURAL_MOVEMENT = "cultural_movement"  # political / cultural narratives
    OTHER = "other"
    UNKNOWN = "unknown"                    # not yet classified


class NarrativeStage(str, Enum):
    """Narrative life-cycle stage (Spec Part 19, Section 7).

    Stage 5 is "Decline or Evolution" in the spec; a narrative that evolves
    successfully re-enters an earlier stage, so the enum only needs DECLINE.
    """

    CREATION = "creation"        # few people know about it; early community forming
    EXPANSION = "expansion"      # more users discovering it; growing attention
    MAINSTREAM = "mainstream"    # large discussions, speculation, volatility
    SATURATION = "saturation"    # excessive hype, community fatigue
    DECLINE = "decline"
    UNKNOWN = "unknown"


class NarrativeRating(str, Enum):
    """Final narrative rating (Spec Part 19, Section 12)."""

    EXCELLENT = "excellent"
    STRONG = "strong"
    AVERAGE = "average"
    WEAK = "weak"


class NarrativeRisk(str, Enum):
    """Narrative-specific risk level (Spec Part 19, Sections 10 and 12)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    UNKNOWN = "unknown"  # no risk factor has been assessed either way


class SentimentLabel(str, Enum):
    """Public sentiment classification (Spec Part 19, Section 6)."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    UNKNOWN = "unknown"


class CatalystLevel(str, Enum):
    """Viral-catalyst probability/impact grading (Spec Part 19, Section 9)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
