"""Security analysis engine (Spec Parts 4, 18, and 33).

Turns a normalized :class:`SecurityProfile` (plus optional market liquidity
data) into a scored, explainable :class:`SecurityAssessment`.

Risk philosophy (Part 33 Section 1 / Part 31 Consistency Lock):

* **Acceptable uncertainty** (new project, unknown owner, few holders)
  deducts lightly — it lowers confidence, it does not kill candidacy.
* **Serious warnings** (mint authority, unlocked LP, heavy concentration)
  deduct heavily and demand deeper review.
* **Destructive risks** (honeypot, non-sellable token, confirmed scam)
  force the overall score to 0 — security overrides opportunity
  (Part 4 final rule, Part 10 Section 5 red-flag overrides).

Unknown facts are *never* treated as safe: they are excluded from the
sub-score, reported in ``unknown_fields``, and lower the confidence rating
(Rule 8 — data before assumptions).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from meme_intelligence.analyzers.common import Finding, SubScore, confidence_from_facts
from meme_intelligence.config.settings import SecuritySubWeights, SecurityThresholds
from meme_intelligence.core.enums import ConfidenceLevel, RiskTier
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import DexPair, SecurityProfile, TokenIdentity

# Security band labels (Part 4, Section 11).
_BANDS = (
    (90.0, "Excellent"),
    (75.0, "Good"),
    (50.0, "Moderate Risk"),
    (25.0, "High Risk"),
    (0.0, "Extreme Risk"),
)


@dataclass(frozen=True)
class SecurityAssessment:
    """Full security verdict for one token (report format per Part 4, Section 12)."""

    token: TokenIdentity
    source: str
    sub_scores: dict[str, float | None]
    overall_score: float
    band: str
    tier: RiskTier
    confidence: ConfidenceLevel
    findings: tuple[Finding, ...]
    unknown_fields: tuple[str, ...]
    coverage: float  # fraction of sub-score weight backed by data

    @property
    def destructive_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is RiskTier.DESTRUCTIVE)

    @property
    def is_destructive(self) -> bool:
        return bool(self.destructive_findings)

    def summary(self) -> str:
        """Human-readable one-screen summary (full report generator arrives later)."""
        band = self.band if self.coverage >= 1.0 else f"{self.band} — partial data"
        lines = [
            f"Security assessment: {self.token.symbol or self.token.address} ({self.token.chain})",
            f"  Overall: {self.overall_score:.0f}/100 [{band}]  "
            f"tier={self.tier.value}  confidence={self.confidence.value}",
        ]
        if self.coverage < 1.0:
            lines.append(
                f"  NOTE: only {self.coverage:.0%} of security categories have data; "
                "unverified areas are NOT safe — treat as an unconfirmed candidate"
            )
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "no data"
            lines.append(f"  {name:>13}: {rendered}")
        if self.findings:
            lines.append("  Findings:")
            for finding in self.findings:
                lines.append(f"    [{finding.severity.value}] {finding.message}")
        if self.unknown_fields:
            lines.append(f"  Unknown: {', '.join(self.unknown_fields)}")
        return "\n".join(lines)


class SecurityAnalyzer:
    """Scores a token's security profile per the Part 4/18/33 investigation steps."""

    def __init__(self, thresholds: SecurityThresholds, weights: SecuritySubWeights):
        self._t = thresholds
        self._w = weights
        self._logger = get_logger("analyzers.security")

    def assess(self, profile: SecurityProfile, market: DexPair | None = None) -> SecurityAssessment:
        """Run the full security investigation.

        ``market`` supplies USD liquidity depth (from the market collectors);
        GoPlus reports LP lock structure but not pool depth (Rule 9 —
        combining sources gives the full liquidity picture).
        """
        contract = self._assess_contract(profile)
        liquidity = self._assess_liquidity(profile, market)
        distribution = self._assess_distribution(profile)
        developer = self._assess_developer(profile)
        manipulation = self._assess_manipulation(profile)

        parts = [contract, liquidity, distribution, developer, manipulation]
        weight_map = dataclasses.asdict(self._w)

        sub_scores: dict[str, float | None] = {}
        findings: list[Finding] = []
        unknowns: list[str] = []
        weighted_sum = 0.0
        available_weight = 0.0
        for part in parts:
            score = part.score()
            sub_scores[part.category] = score
            findings.extend(part.findings)
            unknowns.extend(part.unknowns)
            if score is not None:
                weight = weight_map[part.category]
                weighted_sum += score * weight
                available_weight += weight

        if available_weight == 0.0:
            raise InsufficientDataError(
                f"no security data available for {profile.token.address} on {profile.token.chain}"
            )

        overall = weighted_sum / available_weight

        # Unknown is not safe (Rule 8). The weighted mean renormalizes over the
        # sub-scores that HAVE data, so a mint whose holder/LP facts are simply
        # unavailable is scored on whichever category did resolve — and a fresh
        # pump.fun mint with cleanly renounced authorities and no holder data
        # scored a PERFECT 100 at 25% coverage while rugcheck.xyz independently
        # rated it DANGER 65 on an 88.45% single holder and 100% unlocked LP
        # (operator screenshot, 2026-07-29).
        #
        # The report already says "unverified areas are NOT safe" in its text,
        # but the alert gate reads `overall_score`, not the note. Capping is
        # what `RiskAnalyzer` has done since Part 9 for exactly this reason;
        # this makes the two consistent. Full coverage is unaffected.
        floor = self._t.min_coverage_for_full_score
        capped_from = None
        if floor > 0.0 and available_weight < 1.0:
            ceiling = 50.0 + 50.0 * available_weight
            if overall > ceiling:
                capped_from, overall = overall, ceiling

        destructive = any(f.severity is RiskTier.DESTRUCTIVE for f in findings)
        if destructive:
            overall = 0.0  # security overrides opportunity (Part 4 final rule)
        elif capped_from is not None:
            # Rule 13: a suppressed number must say so, or the operator sees a
            # mediocre score with no explanation of what was actually missing.
            unmeasured = ", ".join(
                name for name, value in sub_scores.items() if value is None)
            findings.append(Finding(
                category="coverage",
                severity=RiskTier.ACCEPTABLE_UNCERTAINTY,
                message=(f"scored {capped_from:.0f}/100 on only {available_weight:.0%} of "
                         f"the security categories; capped to {overall:.0f} because "
                         f"unverified is not safe (no data for: {unmeasured or 'unknown'})"),
            ))

        tier = self._tier(findings, overall)
        confidence = self._confidence(unknowns, parts)

        self._logger.info(
            "security assessment %s/%s: score=%.0f band=%s tier=%s findings=%d unknown=%d",
            profile.token.chain, profile.token.address, overall,
            self._band(overall), tier.value, len(findings), len(unknowns),
        )

        return SecurityAssessment(
            token=profile.token,
            source=profile.source,
            sub_scores=sub_scores,
            overall_score=overall,
            band=self._band(overall),
            tier=tier,
            confidence=confidence,
            findings=tuple(findings),
            unknown_fields=tuple(unknowns),
            coverage=available_weight,
        )

    # ---- Step 1-2: contract permissions & ownership (Part 4 Sections 2-3) ----

    def _assess_contract(self, p: SecurityProfile) -> SubScore:
        s = SubScore("contract")

        # Honeypot behavior invalidates everything else (Part 4 Section 3).
        if s.observe("is_honeypot", p.is_honeypot) and p.is_honeypot:
            s.flag_destructive("confirmed honeypot: buying allowed but selling blocked")
        if s.observe("cannot_buy", p.cannot_buy) and p.cannot_buy:
            s.flag_destructive("buying is blocked by the contract")
        if s.observe("cannot_sell_all", p.cannot_sell_all) and p.cannot_sell_all:
            s.flag_destructive("holders cannot sell their full balance")

        # Live round-trip sell test (Project 1 -- Jupiter quote simulation).
        # sell_route_found/round_trip_loss are only meaningful once a buy
        # route was found; they are structurally not-applicable (not
        # "unknown") otherwise, so they are only observed inside that branch.
        if s.observe("live_buy_route_found", p.live_buy_route_found) and p.live_buy_route_found:
            if s.observe("live_sell_route_found", p.live_sell_route_found) and not p.live_sell_route_found:
                s.flag_destructive(
                    "live Jupiter round-trip test: a buy route exists but no route to sell "
                    "the token back was found -- it cannot currently be sold"
                )
            if s.observe("live_round_trip_loss_percent", p.live_round_trip_loss_percent):
                loss = p.live_round_trip_loss_percent
                if loss >= self._t.extreme_round_trip_loss_percent:
                    s.flag_destructive(
                        f"live Jupiter round-trip test: buying then immediately selling back "
                        f"loses {loss:.0f}% of value"
                    )
                elif loss > self._t.max_round_trip_loss_percent:
                    s.deduct(25, RiskTier.SERIOUS_WARNING,
                             f"live Jupiter round-trip test shows an elevated {loss:.0f}% "
                             f"round-trip loss")

        if s.observe("is_open_source", p.is_open_source) and not p.is_open_source:
            s.deduct(20, RiskTier.SERIOUS_WARNING, "contract source code is not verified")
        if s.observe("is_proxy", p.is_proxy) and p.is_proxy:
            s.deduct(15, RiskTier.SERIOUS_WARNING, "proxy contract: logic can be upgraded after launch")
        if s.observe("is_mintable", p.is_mintable) and p.is_mintable:
            s.deduct(25, RiskTier.SERIOUS_WARNING, "mint authority active: supply can be inflated")
        if s.observe("is_freezable", p.is_freezable) and p.is_freezable:
            s.deduct(25, RiskTier.SERIOUS_WARNING, "freeze authority active: wallets can be frozen")
        if s.observe("balance_mutable", p.balance_mutable) and p.balance_mutable:
            s.deduct(25, RiskTier.SERIOUS_WARNING, "authority can modify wallet balances")
        if s.observe("ownership_renounced", p.ownership_renounced) and not p.ownership_renounced:
            s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY, "ownership not renounced: owner retains control")
        if s.observe("hidden_owner", p.hidden_owner) and p.hidden_owner:
            s.deduct(25, RiskTier.SERIOUS_WARNING, "hidden owner detected")
        if s.observe("can_take_back_ownership", p.can_take_back_ownership) and p.can_take_back_ownership:
            s.deduct(20, RiskTier.SERIOUS_WARNING, "renounced ownership can be reclaimed")
        if s.observe("has_blacklist", p.has_blacklist) and p.has_blacklist:
            s.deduct(10, RiskTier.SERIOUS_WARNING, "blacklist function: specific wallets can be blocked")
        if s.observe("trading_pausable", p.trading_pausable) and p.trading_pausable:
            s.deduct(15, RiskTier.SERIOUS_WARNING, "trading can be paused by the contract")
        if s.observe("selfdestruct", p.selfdestruct) and p.selfdestruct:
            s.deduct(30, RiskTier.SERIOUS_WARNING, "contract contains self-destruct capability")

        # Taxes (Part 4 Section 2 — tax functions).
        worst_tax = max((t for t in (p.buy_tax_percent, p.sell_tax_percent) if t is not None), default=None)
        if s.observe("tax_percent", worst_tax):
            if worst_tax >= self._t.extreme_tax_percent:
                s.deduct(30, RiskTier.SERIOUS_WARNING, f"extreme trading tax ({worst_tax:.0f}%)")
            elif worst_tax > self._t.max_tax_percent:
                s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY, f"elevated trading tax ({worst_tax:.0f}%)")
        if s.observe("tax_modifiable", p.tax_modifiable) and p.tax_modifiable:
            s.deduct(10, RiskTier.SERIOUS_WARNING, "trading tax can be changed by the owner")

        return s

    # ---- Step 3: liquidity safety (Part 4 Section 4) ----

    def _assess_liquidity(self, p: SecurityProfile, market: DexPair | None) -> SubScore:
        s = SubScore("liquidity")

        liquidity_usd = market.liquidity_usd if market is not None else None
        if s.observe("liquidity_usd", liquidity_usd):
            if liquidity_usd < self._t.min_liquidity_usd:
                s.deduct(40, RiskTier.SERIOUS_WARNING,
                         f"liquidity ${liquidity_usd:,.0f} below minimum ${self._t.min_liquidity_usd:,.0f}")
            elif liquidity_usd < self._t.healthy_liquidity_usd:
                s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"liquidity ${liquidity_usd:,.0f} below healthy level "
                         f"${self._t.healthy_liquidity_usd:,.0f}")

        if s.observe("lp_locked_percent", p.lp_locked_percent):
            if p.lp_locked_percent < self._t.min_lp_locked_percent:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         f"only {p.lp_locked_percent:.0f}% of LP locked/burned: liquidity can be pulled")
            elif p.lp_locked_percent < self._t.good_lp_locked_percent:
                s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"{p.lp_locked_percent:.0f}% of LP locked/burned (below "
                         f"{self._t.good_lp_locked_percent:.0f}% comfort level)")

        return s

    # ---- Step 4: holder distribution (Part 4 Section 5) ----

    def _assess_distribution(self, p: SecurityProfile) -> SubScore:
        s = SubScore("distribution")

        if s.observe("top_holder_percent", p.top_holder_percent):
            if p.top_holder_percent > self._t.max_top_holder_percent:
                s.deduct(25, RiskTier.SERIOUS_WARNING,
                         f"top holder controls {p.top_holder_percent:.1f}% of circulating supply")
            elif p.top_holder_percent > self._t.warn_top_holder_percent:
                s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"top holder holds {p.top_holder_percent:.1f}% of circulating supply")

        if s.observe("top10_holder_percent", p.top10_holder_percent):
            if p.top10_holder_percent > self._t.max_top10_holder_percent:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         f"top 10 holders control {p.top10_holder_percent:.1f}% of supply")
            elif p.top10_holder_percent > self._t.warn_top10_holder_percent:
                s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"top 10 holders hold {p.top10_holder_percent:.1f}% of supply")

        if s.observe("holder_count", p.holder_count) and p.holder_count < self._t.min_holder_count:
            s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY,
                     f"only {p.holder_count} holders (very early or very weak)")

        return s

    # ---- Step 6: developer risk (Part 4 Section 8) ----

    def _assess_developer(self, p: SecurityProfile) -> SubScore:
        s = SubScore("developer")

        if s.observe("creator_percent", p.creator_percent):
            if p.creator_percent > self._t.max_creator_percent:
                s.deduct(25, RiskTier.SERIOUS_WARNING,
                         f"creator wallet holds {p.creator_percent:.1f}% of supply")
            elif p.creator_percent > self._t.warn_creator_percent:
                s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY,
                         f"creator wallet holds {p.creator_percent:.1f}% of supply")

        if s.observe("owner_percent", p.owner_percent) and p.owner_percent > self._t.max_creator_percent:
            s.deduct(15, RiskTier.SERIOUS_WARNING,
                     f"owner wallet holds {p.owner_percent:.1f}% of supply")

        if (
            s.observe("honeypot_same_creator_count", p.honeypot_same_creator_count)
            and p.honeypot_same_creator_count > 0
        ):
            s.deduct(40, RiskTier.SERIOUS_WARNING,
                     f"creator previously deployed {p.honeypot_same_creator_count} honeypot token(s)")

        return s

    # ---- Manipulation indicators (Part 18 Sections 7-9) ----

    def _assess_manipulation(self, p: SecurityProfile) -> SubScore:
        s = SubScore("manipulation")

        if s.observe("fake_token", p.fake_token) and p.fake_token:
            s.flag_destructive("flagged as a counterfeit of another token")
        if s.observe("is_airdrop_scam", p.is_airdrop_scam) and p.is_airdrop_scam:
            s.flag_destructive("flagged as an airdrop scam")
        if (
            s.observe("personal_slippage_modifiable", p.personal_slippage_modifiable)
            and p.personal_slippage_modifiable
        ):
            s.deduct(15, RiskTier.SERIOUS_WARNING,
                     "per-wallet tax can be set: selective honeypot capability")
        if s.observe("slippage_modifiable", p.slippage_modifiable) and p.slippage_modifiable:
            s.deduct(10, RiskTier.SERIOUS_WARNING, "global slippage/tax is modifiable")
        if s.observe("anti_whale_modifiable", p.anti_whale_modifiable) and p.anti_whale_modifiable:
            s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY, "anti-whale limits can be modified")
        if s.observe("trading_cooldown", p.trading_cooldown) and p.trading_cooldown:
            s.deduct(10, RiskTier.ACCEPTABLE_UNCERTAINTY, "trading cooldown mechanism present")

        return s

    # ---- Verdict helpers ----

    @staticmethod
    def _tier(findings: list[Finding], overall: float) -> RiskTier:
        if any(f.severity is RiskTier.DESTRUCTIVE for f in findings):
            return RiskTier.DESTRUCTIVE
        if overall < 50.0 or any(f.severity is RiskTier.SERIOUS_WARNING for f in findings):
            return RiskTier.SERIOUS_WARNING
        return RiskTier.ACCEPTABLE_UNCERTAINTY

    @staticmethod
    def _confidence(unknowns: list[str], parts: list[SubScore]) -> ConfidenceLevel:
        known = sum(part.known_count for part in parts)
        return confidence_from_facts(known, len(unknowns))

    @staticmethod
    def _band(score: float) -> str:
        for minimum, label in _BANDS:
            if score >= minimum:
                return label
        return _BANDS[-1][1]
