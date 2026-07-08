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


class ScanLayer(str, Enum):
    """The four monitoring layers of the real-time architecture (Spec Part 2, Section 4)."""

    DISCOVERY = "discovery"        # every 5-10s: find newly emerging opportunities
    SECURITY = "security"          # immediately after discovery: reject dangerous projects
    INTELLIGENCE = "intelligence"  # deeper analysis: community, narrative, wallets
    ALERT = "alert"                # human review alerts for qualifying tokens only
