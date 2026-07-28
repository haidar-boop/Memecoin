"""Automation rules and alert dispatch (Spec Part 13 Sections 4/7, Part 29 preview).

``AutomationRules`` turns pipeline results into alert events using the
Part 2 Section 4 human-review gates and the Part 13 Section 7 rule
patterns (high-priority watchlist rule; emergency-review rule).

Gate philosophy with partial data: the spec's gates assume every category
is measurable. Social collectors are not integrated yet, so the rules
distinguish **verified** qualification (every gate has data and passes ->
HIGH priority) from **provisional** qualification (all gates *with data*
pass, some are unverified -> MEDIUM priority, with the unverified gates
named). Unknown never counts as a pass (Rule 8).

``NotificationEngine`` dispatches events to sinks with a per-token,
per-type cooldown so repeated detections collapse instead of spamming
(Part 29, Section 6). Alert history persistence and Telegram/Discord
sinks arrive with the Part 29 build.
"""

from __future__ import annotations

import dataclasses
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from meme_intelligence.analyzers.risk_analyzer import emergency_flags
from meme_intelligence.config.settings import AlertEngineSettings, AlertThresholds
from meme_intelligence.core.enums import AccumulationVerdict, AlertPriority, EntryZone
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.workflow.pipeline import PipelineResult

# Gates a fresh launch may legitimately leave unverified and still earn a
# HIGH "strong candidate" alert: community data comes from CoinGecko,
# which does not list pump.fun-era tokens for days. Every other gate
# (overall/security/on-chain/liquidity) must have data and pass — only
# this one may be missing (Rule 8: the gap is surfaced, not assumed).
_STRONG_CANDIDATE_ALLOWED_UNVERIFIED = frozenset({"community"})

# ---- Interest gate (Part 29 Section 1: alerts protect decisions) ----
#
# The system never trades, and the operator only hears about a token when it
# earns a HIGH opportunity alert. Every other tracked token is internal
# research state — so a HIGH risk warning / score drop / emergency on one of
# those protects no decision the operator could possibly have made. Live
# failure mode this closes: dying pump.fun garbage (liquidity a few $k, one
# wallet holding 65–97%) sat above the token-death floor and re-warned the
# phone every recheck as HIGH "RISK WARNING" / "SCORE DROP REVIEW" spam.
#
# Alert types that RECOMMEND a token to the operator — receiving one means
# the operator may have acted, so protective alerts stay at full priority
# afterwards. Since 2026-07-28 this is EVERY buy-side type, not just the two
# HIGH tiers: only DELIVERED alerts are recorded to history, so membership
# here means the pitch actually reached the operator's phone — and his
# standing rule is that any coin he was sent must report its outcome
# ("when it sends me a coin and it gets rugged I want it to acknowledge
# that it was rugged"). The old exclusion of the MEDIUM tiers as
# "provisional research notes" predates his medium delivery floor, under
# which those tiers are real pitches he sees and can act on.
INTEREST_ALERT_TYPES = frozenset({
    "high_priority_opportunity", "strong_candidate",
    "early_opportunity", "momentum", "smart_money_accumulation",
})

# Alert types that exist to PROTECT a holder/decision rather than surface a
# new opportunity. On a token with no operator interest they demote to LOW:
# still printed by the console and recorded in history (Rule 13), but below
# the external sinks' minimum priority, so the phone never buzzes for them.
_PROTECTIVE_ALERT_TYPES = frozenset({
    "emergency_review", "risk_warning", "score_drop_review", "token_death",
    "whale_exit", "insider_risk", "community_fake", "security_change",
})

# BUY-SIDE alert types — a positive "this is worth entering" signal. These are
# the only ones subject to the operator liquidity/market-cap floor: below a
# tradeable pool depth, such a signal is a pump artifact on a coin you could
# not actually buy, not an opportunity. Protective types (above) are never
# floored — a dying/thin coin's holder still needs the warning.
_BUY_SIDE_ALERT_TYPES = frozenset({
    "high_priority_opportunity", "strong_candidate", "early_opportunity",
    "momentum", "smart_money_accumulation",
})

# The WEAK/provisional buy-side tiers — everything that does not require
# every serious gate to be independently verified. These are what an
# actively-declining coin re-pitches as "new" (bug-hunt finding): a coin
# whose score just fell can still clear a loose provisional threshold.
# ``high_priority_opportunity``/``strong_candidate`` are deliberately
# excluded — clearing THAT bar despite a decline is a rare enough signal
# that both alerts reaching the operator at full priority (the existing
# same-batch-interest contract for ``score_drop_review``) is more useful
# than hiding it; the operator sees the contradiction and decides.
_DECLINE_SUPPRESSED_TYPES = frozenset({
    "early_opportunity", "momentum", "smart_money_accumulation",
})

_NO_INTEREST_NOTE = ("informational only: this token never reached the "
                     "operator as a buy signal, so no operator decision is "
                     "exposed to it (interest gate)")

# ---- Safety checklist (operator rule 2026-07-12) ---------------------------
# Each buy-side alert that SURVIVES the rug veto carries a small checklist so
# the operator sees what passed and what fell short. The design principle: gate
# (suppress) only on a rug — the rug engine's COMBINED verdict, handled upstream
# via ``deterministic_risk_veto`` — and let every individual soft signal merely
# ANNOTATE. A single missed line never drops the alert ("if just one thing
# misses the checklist, send it through and let me know"). Notes framed
# "normal for a new launch" keep a fresh, naturally-concentrated coin from
# looking like a scam purely for being young (Rule 8).
_CHECK_ICON = {"pass": "✅", "warn": "⚠️", "note": "ℹ️", "unknown": "❔"}


@dataclass(frozen=True)
class _SafetyCheck:
    """One checklist line. ``status`` is pass/warn/note/unknown; ``detail`` is
    the human-readable text (rendered with the matching icon). A ``warn`` is a
    soft miss that annotates but NEVER suppresses; ``note`` is informational
    (no pass/fail); ``unknown`` is missing data surfaced honestly (Rule 8)."""

    status: str
    detail: str


def _render_checklist(checks: list[_SafetyCheck]) -> tuple[str, ...]:
    """Render checks into display lines led by a "passed X/Y" header. Only
    definitively-evaluated lines (pass/warn) count toward the denominator;
    notes and unknowns are shown but not scored."""
    if not checks:
        return ()
    scored = [c for c in checks if c.status in ("pass", "warn")]
    passed = sum(1 for c in scored if c.status == "pass")
    header = (f"Safety checklist — passed {passed}/{len(scored)}"
              if scored else "Safety checklist")
    lines = [header]
    lines += [f"{_CHECK_ICON.get(c.status, '•')} {c.detail}" for c in checks]
    return tuple(lines)


def gate_events_by_interest(
    events: list["AlertEvent"], *, operator_interest: bool, enabled: bool = True,
) -> list["AlertEvent"]:
    """Demote protective alerts to LOW when the operator was never pointed
    at this token (see the interest-gate rationale above).

    A buy-side alert in the SAME batch grants interest immediately —
    contradictory signals on a token being recommended right now must both
    arrive at full priority. (Any tier: a surviving buy-side event in this
    batch is about to be delivered, so the operator IS being pitched.)
    Idempotent: already-LOW events pass untouched.
    """
    if not enabled or operator_interest:
        return events
    if any(e.alert_type in INTEREST_ALERT_TYPES for e in events):
        return events
    gated: list[AlertEvent] = []
    for event in events:
        if (event.alert_type in _PROTECTIVE_ALERT_TYPES
                and event.priority is not AlertPriority.LOW):
            gated.append(dataclasses.replace(
                event, priority=AlertPriority.LOW,
                reasons=event.reasons + (_NO_INTEREST_NOTE,)))
        else:
            gated.append(event)
    return gated


@dataclass(frozen=True)
class AlertEvent:
    """One alert, carrying its evidence (interpretation format per Part 16
    Section 9 / Part 29 Section 7: what happened, why it matters, evidence,
    and what to monitor next)."""

    priority: AlertPriority
    alert_type: str
    token: TokenIdentity
    title: str
    reasons: tuple[str, ...]
    scores: dict[str, float | None] = field(default_factory=dict)
    monitoring: tuple[str, ...] = ()  # recommended next checks
    why_it_matters: str = ""          # potential impact (Part 29, Section 7)
    detected_at: datetime | None = None  # stamped at dispatch when unset
    # Safety checklist lines (already rendered with ✅/⚠/ℹ/❔ icons) attached to
    # buy-side alerts so the operator sees what passed and what fell short — a
    # missed soft check annotates rather than suppresses (operator rule
    # 2026-07-12). Empty for protective/informational alerts.
    checklist: tuple[str, ...] = ()

    def render(self) -> str:
        symbol = self.token.symbol or self.token.address[:8]
        lines = [f"[{self.priority.value.upper()}] {self.alert_type}: {symbol} ({self.token.chain})",
                 f"  {self.title}"]
        if self.why_it_matters:
            lines.append(f"  why it matters: {self.why_it_matters}")
        for reason in self.reasons:
            lines.append(f"  - {reason}")
        for line in self.checklist:
            lines.append(f"  {line}")
        if self.scores:
            rendered = ", ".join(
                f"{k}={v:.0f}" if v is not None else f"{k}=?" for k, v in self.scores.items()
            )
            lines.append(f"  scores: {rendered}")
        for item in self.monitoring:
            lines.append(f"  watch next: {item}")
        return "\n".join(lines)


class AutomationRules:
    """Turns pipeline results into alert decisions (Part 13, Section 7)."""

    def __init__(self, thresholds: AlertThresholds, settings: AlertEngineSettings,
                 *, now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self._t = thresholds
        self._s = settings
        self._now = now_func

    def evaluate(
        self,
        result: PipelineResult,
        *,
        previous_score: float | None = None,
        ai_verification_inconclusive: bool = False,
        deterministic_risk_veto: str | None = None,
        operator_interest: bool = True,
    ) -> list[AlertEvent]:
        """``ai_verification_inconclusive`` — the caller ran AI verification
        but no usable judgment came back (discarded below the confidence
        floor, or the call failed). Without this flag a judgment of 15/100
        vanished entirely and fired the HIGH tier, while 22/100 attached and
        vetoed it — inverted protection. The scanner is the only caller that
        knows verification ran, so it must say so.

        ``deterministic_risk_veto`` — a zero-cost check (rug engine, risk
        alerts already firing) vetoed this token before any paid call. It
        downgrades BOTH HIGH opportunity tiers (bug-hunt finding: the
        fully-verified tier used to bypass every veto — a blacklisted
        deployer with community data still fired HIGH, unchecked).

        ``operator_interest`` — whether the operator was ever POINTED at this
        token (a HIGH opportunity alert was delivered for it). When False,
        protective alerts demote to LOW via the interest gate (see
        :func:`gate_events_by_interest`); the default True preserves full
        priority for every caller that does not track alert history."""
        # A dead token is a closed case (Part 29 Section 1 — alerts protect
        # decisions, and no entry/exit decision remains once liquidity has
        # collapsed): one MEDIUM post-mortem replaces the warning/drop pair,
        # and opportunity/momentum/accumulation signals on the corpse are
        # pump artifacts, not information. Only a confirmed destructive
        # finding still matters — anyone already holding needs to know.
        death = self._token_death_rule(result, previous_score)
        if death is not None:
            events = [event for event in self._emergency_rule(result)
                      if event.priority is AlertPriority.CRITICAL]
            events.append(death)
            return gate_events_by_interest(
                events, operator_interest=operator_interest,
                enabled=self._s.risk_alerts_require_interest)

        events: list[AlertEvent] = []
        events.extend(self._emergency_rule(result))
        opportunity = self._opportunity_rule(
            result, ai_verification_inconclusive=ai_verification_inconclusive,
            deterministic_risk_veto=deterministic_risk_veto)
        if opportunity is not None:
            events.append(opportunity)
        momentum = self._momentum_rule(result)
        if momentum is not None:
            events.append(momentum)
        events.extend(self._smart_money_rules(result))
        community = self._community_rule(result)
        if community is not None:
            events.append(community)
        drop = self._score_drop_rule(result, previous_score)
        if drop is not None:
            events.append(drop)
        # Three things suppress a buy-side alert ENTIRELY, whatever tier it
        # cleared:
        #
        # 1) RUG VETO ("if it's a rug pull don't send it at all"). The
        #    ``deterministic_risk_veto`` IS the rug signal: the rug engine's
        #    COMBINED score, an already-firing risk alert, honeypot/unsellable,
        #    or the earned mind-layer p(rug) vote — so a single soft flag never
        #    trips it; only a real rug does.
        # 2) NOT A REAL, TRADEABLE COIN — a 0 / missing liquidity or market cap
        #    (operator rule 2026-07-12: "it's still giving me coins with 0
        #    liquidity or 0 market cap"). This is NOT a thin coin the operator
        #    might still want (that sends with a ⚠ checklist note); it is a
        #    non-opportunity you could not buy, size, or value at all — noise,
        #    not a lead. See ``_untradeable``.
        # 3) ALREADY TOO BIG — an operator-set liquidity/market-cap CEILING. A
        #    coin whose pool or market cap has grown past the ceiling is no
        #    longer an early opportunity (the move already happened), so its
        #    buy-side alert is suppressed. OFF by default. See ``_oversized``.
        # 3b) ALREADY TOO OLD — the operator's freshness window (2026-07-17:
        #    "only send me coins less than 1 hour old"). A pool older than
        #    ``opportunity_max_age_hours`` is past the entry window, so its
        #    buy-side alert is suppressed. ON by default at 1h. See ``_too_old``.
        #
        # A fourth condition suppresses only the WEAK/provisional tiers
        # (``_DECLINE_SUPPRESSED_TYPES``):
        #
        # 4) ACTIVELY DECLINING — the score fell at least ``score_drop_review_
        #    points`` since the last look, the same signal ``score_drop_rule``
        #    already reports. Bug found in the field: a previously-alerted,
        #    now-thinning coin (liquidity cut roughly in half, score 90 -> 65
        #    a day later) still numerically cleared the loose provisional
        #    gates and fired a fresh MEDIUM "early opportunity" in the SAME
        #    cycle the bot's own score_drop_review flagged it as collapsing —
        #    re-pitching a dying coin as new. A coin that still clears the
        #    much stricter ``strong_candidate``/``high_priority_opportunity``
        #    bar despite the decline is exempt: that is a rare enough signal
        #    that showing both alerts at full priority (the existing
        #    same-batch-interest contract for ``score_drop_review``) beats
        #    hiding it — the operator sees the contradiction and decides.
        #    ``score_drop_review`` is PROTECTIVE, not buy-side, so it always
        #    still fires; this only stops the weak-tier re-pitch. A coin's
        #    first-ever look has no ``previous_score`` and is never penalized
        #    for being new. See ``_score_declining``.
        #
        # Everything else soft (thin-but-real liquidity, mint/freeze authority,
        # sell tax, deployer history) only ANNOTATES via the checklist. Protective
        # alerts always pass — a flagged/dying coin's holder still needs the
        # warning.
        if (deterministic_risk_veto is not None
                or self._untradeable(result) or self._oversized(result)
                or self._too_old(result)):
            events = [e for e in events if e.alert_type not in _BUY_SIDE_ALERT_TYPES]
        else:
            if self._score_declining(result, previous_score):
                events = [e for e in events if e.alert_type not in _DECLINE_SUPPRESSED_TYPES]
            # NOT a hard veto: individual soft checks (liquidity/market-cap
            # floor, mint/freeze authority, sell tax, deployer history) no
            # longer SUPPRESS — that used to drop the whole alert on a single
            # thin-pool miss. Instead the safety checklist rides ON each
            # surviving buy-side alert so the operator sees what passed and
            # what fell short and decides ("if just one thing misses the
            # checklist, send it through and let me know").
            checklist = _render_checklist(self._safety_checklist(result))
            if checklist:
                events = [dataclasses.replace(e, checklist=checklist)
                          if e.alert_type in _BUY_SIDE_ALERT_TYPES else e
                          for e in events]
        return gate_events_by_interest(
            events, operator_interest=operator_interest,
            enabled=self._s.risk_alerts_require_interest)

    def _untradeable(self, result: PipelineResult) -> bool:
        """True when the coin is not a REAL, tradeable opportunity — a 0, negative,
        NaN, or missing liquidity OR market cap (operator rule 2026-07-12). Such a
        "coin" cannot be bought, sized, or valued, so a buy-side alert on it is
        noise and is suppressed outright — distinct from the comfort floor, which
        only annotates a thin-but-real coin (a $4k pool still sends with a ⚠ note;
        a $0/None pool never sends). A missing value counts as untradeable, never
        as tradeable (Rule 8: absent data is not a green light)."""
        liq = result.pair.liquidity_usd
        if liq is None or not math.isfinite(liq) or liq <= 0.0:
            return True
        mcap = result.pair.market_cap
        if mcap is None or not math.isfinite(mcap) or mcap <= 0.0:
            return True
        return False

    def _oversized(self, result: PipelineResult) -> bool:
        """True when the coin has already grown past the operator's buy-side
        ceiling — liquidity or market cap above a SET maximum. A coin this large
        is no longer an early opportunity (the move the operator wants to catch
        already happened — e.g. a multi-million-dollar pool firing an "early
        opportunity"), so its opportunity/momentum/smart-money alerts are
        suppressed. Distinct from ``_untradeable``: here unknown liquidity/mcap
        NEVER trips the ceiling (Rule 8 — absent data is not evidence a coin is
        too big), and protective alerts still fire (a large coin can still rug).
        Both ceilings default 0.0 = OFF, so behavior is unchanged until set."""
        max_liq = self._t.opportunity_max_liquidity_usd
        liq = result.pair.liquidity_usd
        if max_liq > 0.0 and liq is not None and math.isfinite(liq) and liq > max_liq:
            return True
        max_mcap = self._t.opportunity_max_market_cap_usd
        mcap = result.pair.market_cap
        if max_mcap > 0.0 and mcap is not None and math.isfinite(mcap) and mcap > max_mcap:
            return True
        return False

    def _too_old(self, result: PipelineResult) -> bool:
        """True when the pool is older than the operator's freshness window
        (``opportunity_max_age_hours``, 2026-07-17: "only send me coins less
        than 1 hour old"). An old coin is past the entry window the operator
        trades, so its opportunity/momentum/smart-money alerts are suppressed.
        Protective alerts still fire (an old coin can still rug). An UNKNOWN
        pool age or a bad timestamp NEVER trips the gate (Rule 8 — absent data
        is not evidence of age; same convention as ``_oversized``). 0 = OFF."""
        max_age = self._t.opportunity_max_age_hours
        if max_age <= 0.0:
            return False
        created = result.pair.pair_created_at
        if created is None:
            return False
        try:
            age_hours = (self._now() - created).total_seconds() / 3600.0
        except Exception:  # noqa: BLE001 — a bad timestamp must never break alerting
            return False
        return age_hours > max_age

    def _safety_checklist(self, result: PipelineResult) -> list[_SafetyCheck]:
        """Build the per-alert safety checklist (operator rule 2026-07-12).

        Reads contract facts already collected (``SecurityProfile``) plus pool
        depth — zero extra API calls. Every line ANNOTATES; none suppresses (the
        rug veto upstream is the only gate). Unknown data is surfaced honestly as
        ``❔`` rather than assumed safe (Rule 8), and top-wallet concentration is
        an informational note — never a fail — framed "normal for a new launch"
        on a young pool, because a fresh coin is naturally concentrated and must
        not look like a scam purely for being young."""
        p = result.security_profile
        checks: list[_SafetyCheck] = []

        # 1) Sellable — a confirmed honeypot is handled upstream (destructive ->
        # no opportunity + emergency warning); this line reassures on the survivors.
        if p is None or (p.is_honeypot is None and p.cannot_sell_all is None):
            checks.append(_SafetyCheck("unknown", "Sellable: not verified"))
        elif p.is_honeypot or p.cannot_sell_all:
            checks.append(_SafetyCheck("warn", "Cannot sell — honeypot indicators present"))
        else:
            checks.append(_SafetyCheck("pass", "Sellable"))

        # 2) Mint authority renounced — if live, the dev can mint more and dilute.
        checks.append(self._authority_check(
            p.is_mintable if p else None,
            pass_text="Mint authority renounced",
            warn_text="Mint authority still active — dev can mint more supply",
            unknown_text="Mint authority: not verified"))

        # 3) Freeze authority renounced — if live, the dev could freeze transfers.
        checks.append(self._authority_check(
            p.is_freezable if p else None,
            pass_text="Freeze authority renounced",
            warn_text="Freeze authority still active — dev could freeze transfers",
            unknown_text="Freeze authority: not verified"))

        # 4) Sell tax under the comfort ceiling.
        tax = p.sell_tax_percent if p else None
        ceiling = self._t.checklist_sell_tax_max_percent
        if tax is None or not math.isfinite(tax):
            checks.append(_SafetyCheck("unknown", "Sell tax: not verified"))
        elif tax > ceiling:
            checks.append(_SafetyCheck(
                "warn", f"Sell tax {tax:.0f}% — above the {ceiling:.0f}% comfort line"))
        else:
            checks.append(_SafetyCheck("pass", f"Sell tax {tax:.0f}%"))

        # 5) Deployer history — a creator tied to past honeypots is a real flag
        # (a serial scammer's fresh coin is still dangerous); clean or unknown
        # otherwise (Rule 8).
        same_creator = p.honeypot_same_creator_count if p else None
        if same_creator is None:
            checks.append(_SafetyCheck("unknown", "Deployer history: not verified"))
        elif same_creator > 0:
            checks.append(_SafetyCheck(
                "warn", f"Deployer linked to {same_creator} past honeypot(s)"))
        else:
            checks.append(_SafetyCheck("pass", "Deployer clean (no known honeypots)"))

        # 6) Tradeable liquidity — the old suppressing floor, now a comfort line.
        checks.append(self._liquidity_check(result))

        # 7) Market cap — only shown when the operator set a comfort floor.
        mcap_check = self._market_cap_check(result)
        if mcap_check is not None:
            checks.append(mcap_check)

        # Informational: top-wallet concentration — NEVER a fail (a new launch is
        # naturally concentrated). Shown with youth context when the pool is young.
        top = p.top_holder_percent if p else None
        if top is not None and math.isfinite(top):
            detail = f"{top:.0f}% held by the top wallet"
            if self._is_new_launch(result):
                detail += " — normal for a new launch"
            checks.append(_SafetyCheck("note", detail))

        return checks

    def _authority_check(self, live: bool | None, *, pass_text: str,
                         warn_text: str, unknown_text: str) -> _SafetyCheck:
        """A renounced-authority line: ``live`` True means the authority is still
        active (a soft ⚠ flag), False means renounced (✅), None unknown (❔)."""
        if live is None:
            return _SafetyCheck("unknown", unknown_text)
        return _SafetyCheck("warn", warn_text) if live else _SafetyCheck("pass", pass_text)

    def _liquidity_check(self, result: PipelineResult) -> _SafetyCheck:
        """Pool-depth line against the operator's comfort floor. With no floor
        set (0.0) it is an informational note; with a floor set it passes/⚠ and
        an unknown depth surfaces as ❔ (a buy you cannot size — Rule 8)."""
        liq = result.pair.liquidity_usd
        floor = self._t.opportunity_min_liquidity_usd
        if liq is None or not math.isfinite(liq):
            if floor > 0.0:
                return _SafetyCheck("unknown", "Liquidity: unknown (cannot size an entry)")
            return _SafetyCheck("note", "Liquidity: unknown")
        if floor <= 0.0:
            return _SafetyCheck("note", f"Liquidity ${liq:,.0f}")
        if liq < floor:
            return _SafetyCheck(
                "warn", f"Liquidity ${liq:,.0f} — below your ${floor:,.0f} comfort floor")
        return _SafetyCheck("pass", f"Liquidity ${liq:,.0f}")

    def _market_cap_check(self, result: PipelineResult) -> _SafetyCheck | None:
        """Market-cap comfort line — only produced when a floor is set (0.0 =
        off), so it adds no noise for operators who only care about liquidity.
        Unknown cap surfaces as ❔ against a set floor (Rule 8)."""
        floor = self._t.opportunity_min_market_cap_usd
        if floor <= 0.0:
            return None
        mcap = result.pair.market_cap
        if mcap is None or not math.isfinite(mcap):
            return _SafetyCheck("unknown", "Market cap: unknown")
        if mcap < floor:
            return _SafetyCheck(
                "warn", f"Market cap ${mcap:,.0f} — below your ${floor:,.0f} floor")
        return _SafetyCheck("pass", f"Market cap ${mcap:,.0f}")

    def _is_new_launch(self, result: PipelineResult) -> bool:
        """True when the pool is younger than the checklist's new-launch window —
        used only to phrase the concentration note, never to gate anything."""
        created = result.pair.pair_created_at
        if created is None:
            return False
        window = self._t.checklist_new_launch_minutes
        try:
            age_minutes = (self._now() - created).total_seconds() / 60.0
        except Exception:  # noqa: BLE001 — a bad timestamp must never break alerting
            return False
        return 0.0 <= age_minutes <= window

    # IF liquidity has collapsed below the dead floor THEN the failure is a
    # completed event, not a warning — emit one post-mortem (Part 29 S1).
    def _token_death_rule(self, result: PipelineResult,
                          previous_score: float | None) -> AlertEvent | None:
        liquidity = result.pair.liquidity_usd
        # Unknown liquidity is NOT death — absence of data never becomes a
        # conclusion (Rule 8). NaN must bail out here too: `nan >= floor`
        # is always False (same hazard as `nan <= 0` elsewhere), so without
        # the explicit isfinite check a NaN liquidity value fell through
        # to "dead" instead of being excluded, misclassifying a token with
        # simply-unmeasurable liquidity and suppressing every real alert.
        if liquidity is None or not math.isfinite(liquidity) or liquidity >= self._s.dead_liquidity_usd:
            return None
        reasons = [f"liquidity collapsed to ${liquidity:,.0f} "
                   f"(dead floor ${self._s.dead_liquidity_usd:,.0f})"]
        if previous_score is not None:
            reasons.append(f"score history: {previous_score:.0f} -> "
                           f"{result.master.final_score:.0f}")
        return AlertEvent(
            priority=AlertPriority.MEDIUM,
            alert_type="token_death",
            token=result.pair.base_token,
            title="Token appears dead: liquidity has collapsed",
            reasons=tuple(reasons),
            scores={"master": result.master.final_score},
            why_it_matters="A completed rug/abandonment closes the case: the "
                           "outcome is recorded for performance grading, and "
                           "no further tracking is useful.",
            monitoring=("none — archived from active tracking",),
        )

    # IF destructive risk appears THEN trigger emergency review (Part 13 Section 7).
    def _emergency_rule(self, result: PipelineResult) -> list[AlertEvent]:
        critical, high = emergency_flags(result.security, result.onchain)
        events = []
        if critical:
            events.append(AlertEvent(
                priority=AlertPriority.CRITICAL,
                alert_type="emergency_review",
                token=result.pair.base_token,
                title="Destructive risk detected — do not enter; review any exposure now",
                reasons=tuple(critical),
                scores={"security": result.security.overall_score,
                        "master": result.master.final_score},
                why_it_matters="Destructive findings invalidate the opportunity outright; "
                               "capital in this token is at immediate structural risk.",
                monitoring=("verify LP status and contract permissions immediately",
                            "if holding, decide exit before anything else"),
            ))
        elif high:
            events.append(AlertEvent(
                priority=AlertPriority.HIGH,
                alert_type="risk_warning",
                token=result.pair.base_token,
                title="Serious risk indicators appeared",
                reasons=tuple(high[:4]),
                scores={"security": result.security.overall_score,
                        "master": result.master.final_score},
                monitoring=("watch liquidity and top-holder movements closely",),
            ))
        return events

    # IF gates pass THEN move to high-priority watchlist (Parts 2/13).
    def _opportunity_rule(self, result: PipelineResult, *,
                          ai_verification_inconclusive: bool = False,
                          deterministic_risk_veto: str | None = None) -> AlertEvent | None:
        if result.security.is_destructive:
            return None

        gates: dict[str, tuple[float | None, float]] = {
            "overall": (result.master.final_score, self._t.overall),
            "security": (result.security.overall_score, self._t.security),
            "onchain": (result.onchain.overall_score if result.onchain else None, self._t.onchain),
            "liquidity": (result.security.sub_scores.get("liquidity"), self._t.liquidity),
            # Live since the community collector landed; unlisted tokens
            # still report None and stay honestly unverified (Rule 8).
            "community": (result.community.overall_score if result.community else None,
                          self._t.community),
        }

        unverified = [name for name, (value, _) in gates.items() if value is None]
        failed = [name for name, (value, minimum) in gates.items()
                  if value is not None and value < minimum]
        if failed or "overall" in unverified or "security" in unverified:
            return None

        scores = {name: value for name, (value, _) in gates.items()}
        veto_caveat = ([f"deterministic risk veto: {deterministic_risk_veto}"]
                       if deterministic_risk_veto else [])
        if not unverified:
            # The fully-verified tier honors the deterministic vetoes too
            # (depth floor, lukewarm AI, rug-engine/risk veto) — it used to
            # bypass all of them (bug-hunt finding). It does NOT downgrade on
            # a merely-unavailable AI: with every gate verified by data,
            # deterministic evidence stands on its own (Rule 9).
            full_caveats = self._strong_candidate_caveats(result) + veto_caveat
            if not full_caveats:
                return AlertEvent(
                    priority=AlertPriority.HIGH,
                    alert_type="high_priority_opportunity",
                    token=result.pair.base_token,
                    title=f"All review gates passed (score {result.master.final_score:.0f})",
                    reasons=(f"classification: {result.master.classification.value}",),
                    scores=scores,
                    why_it_matters="Every measurable human-review gate passed with data — "
                                   "the rare setup the scanner exists to find.",
                    monitoring=("track holder growth and volume quality for continuation",),
                )
            return AlertEvent(
                priority=AlertPriority.MEDIUM,
                alert_type="early_opportunity",
                token=result.pair.base_token,
                title=f"Provisional opportunity (score {result.master.final_score:.0f}) — "
                      f"held back by vetoes",
                reasons=(f"classification: {result.master.classification.value}",
                         *full_caveats),
                scores=scores,
                monitoring=("re-evaluate once the named vetoes clear",),
            )

        # Strong-candidate tier (Part 2 S4): a fresh launch whose ONLY
        # unverified gate is community (no CoinGecko listing yet) but which
        # clears a raised overall bar with every measurable gate passing
        # still earns a HIGH alert — otherwise a genuinely strong pump.fun-
        # era launch is permanently capped at MEDIUM and hidden behind a
        # HIGH delivery filter. The missing gate is named, never assumed
        # passed (Rule 8), so this stays honest confirmation-with-a-caveat.
        overall_score = result.master.final_score
        caveats: list[str] = []
        if (set(unverified) <= _STRONG_CANDIDATE_ALLOWED_UNVERIFIED
                and overall_score >= self._t.strong_candidate_overall):
            caveats = self._strong_candidate_caveats(
                result, ai_verification_inconclusive=ai_verification_inconclusive)
            caveats += veto_caveat
            if not caveats:
                return AlertEvent(
                    priority=AlertPriority.HIGH,
                    alert_type="strong_candidate",
                    token=result.pair.base_token,
                    title=f"Strong candidate (score {overall_score:.0f}) — "
                          f"community unverified",
                    reasons=(f"classification: {result.master.classification.value}",
                             "every measurable gate passed strongly; "
                             "community data not yet available (Rule 8)"),
                    scores=scores,
                    why_it_matters="A fresh launch clearing security, on-chain and "
                                   "liquidity strongly — the early setup worth watching, "
                                   "pending community confirmation.",
                    monitoring=("confirm community traction before sizing any position",
                                "watch holder growth and volume quality for continuation"),
                )

        return AlertEvent(
            priority=AlertPriority.MEDIUM,
            alert_type="early_opportunity",
            token=result.pair.base_token,
            title=f"Provisional opportunity (score {result.master.final_score:.0f}) — "
                  f"unverified gates: {', '.join(unverified)}",
            reasons=(f"classification: {result.master.classification.value}",
                     "unverified categories are NOT confirmation (Part 31 Section 6)",
                     *caveats),
            scores=scores,
            monitoring=tuple(f"verify the {name} gate before sizing any position"
                             for name in unverified),
        )

    def _strong_candidate_caveats(self, result: PipelineResult, *,
                                  ai_verification_inconclusive: bool = False) -> list[str]:
        """Vetoes keeping a gate-passing fresh launch out of the HIGH tier.

        Two live failure modes both produced HIGH alerts on junk: the
        "liquidity" gate scores lock safety, not DEPTH, so a ~$16k pool
        (trivially manipulable) cleared every gate; and a lukewarm AI
        verification rode along as a footnote instead of counting. Each veto
        downgrades the alert to MEDIUM with the reason named (Rule 8), so a
        HIGH-filtered phone never sees it. Unknown liquidity vetoes too —
        unverified depth is not depth.
        """
        caveats: list[str] = []
        liquidity = result.pair.liquidity_usd
        floor = self._t.strong_candidate_min_liquidity_usd
        if liquidity is None or not math.isfinite(liquidity) or liquidity < floor:
            shown = (f"${liquidity:,.0f}" if liquidity is not None
                     and math.isfinite(liquidity) else "unknown")
            caveats.append(f"liquidity depth {shown} is below the strong-candidate "
                           f"floor (${floor:,.0f}) — thin pools are easily manipulated")
        judgment = result.ai_judgment
        if (judgment is not None
                and judgment.confidence < self._t.strong_candidate_min_ai_confidence):
            caveats.append(
                f"AI verification confidence {judgment.confidence:.0f}/100 is below "
                f"the strong-candidate floor "
                f"({self._t.strong_candidate_min_ai_confidence:.0f})")
        elif ai_verification_inconclusive:
            # Verification ran but no usable judgment survived (discarded
            # below the confidence floor, or the call failed). That is even
            # LESS confirmation than a lukewarm judgment — without this, a
            # 15/100 judgment vanished and fired HIGH while 22/100 vetoed.
            caveats.append("AI verification ran but produced no usable judgment "
                           "— treated as unconfirmed, not as a pass (Rule 8)")
        return caveats

    # IF momentum accelerates through the gate in a sane entry zone THEN
    # surface it (Part 15 Section 5 — momentum alert).
    def _momentum_rule(self, result: PipelineResult) -> AlertEvent | None:
        momentum = result.momentum
        if momentum is None or result.security.is_destructive:
            return None
        # Momentum was the ONE buy-side type with no security-score bar: the
        # opportunity tiers require security >= 80, but a coin scoring 40-49
        # purely on soft flags (no rug signal fired) could ride bot-painted
        # volume straight to a MEDIUM momentum alert — unscreened by wallet
        # intelligence, whose credit gate rightly skips sub-50 coins
        # (2026-07-17 review finding). Rising price on a coin with bad
        # security is bait, not a signal.
        if result.security.overall_score < self._t.momentum_min_security_score:
            return None
        if momentum.overall_score < self._t.momentum:
            return None
        if momentum.entry_zone is EntryZone.LATE:
            return None  # accelerating into a blow-off is not an opportunity signal
        components = ", ".join(
            f"{k}={v:.0f}" if v is not None else f"{k}=?"
            for k, v in momentum.sub_scores.items()
        )
        return AlertEvent(
            priority=AlertPriority.MEDIUM,
            alert_type="momentum",
            token=result.pair.base_token,
            title=f"Momentum {momentum.overall_score:.0f}/100 in "
                  f"{momentum.entry_zone.value} zone ({momentum.preferred_action.value})",
            reasons=(components,
                     f"momentum coverage {momentum.coverage:.0%} — "
                     "confirm before treating as validated"),
            scores={"momentum": momentum.overall_score,
                    "master": result.master.final_score},
            monitoring=("watch for volume continuation vs one-hour spike reversal",),
        )

    # Smart-money alerts (Part 17, Section 13): high-quality accumulation,
    # whale exit, and insider risk — only when wallet data was gathered.
    def _smart_money_rules(self, result: PipelineResult) -> list[AlertEvent]:
        wallet = result.wallet
        if wallet is None:
            return []
        events: list[AlertEvent] = []

        if (
            wallet.accumulation is AccumulationVerdict.HEALTHY
            and not result.security.is_destructive
            and (wallet.sub_scores.get("quality_wallets") or 0) >= 60
        ):
            events.append(AlertEvent(
                priority=AlertPriority.MEDIUM,
                alert_type="smart_money_accumulation",
                token=result.pair.base_token,
                title=f"Healthy accumulation: {wallet.accumulating_wallets} independent "
                      "wallets are net buyers",
                reasons=(f"smart-money score {wallet.overall_score:.0f}/100 "
                         f"(coverage {wallet.coverage:.0%})",
                         "wallet track records not yet established — behavior-based signal"),
                scores={"smart_money": wallet.overall_score,
                        "master": result.master.final_score},
                monitoring=("watch whether accumulating wallets hold through volatility",),
            ))

        if wallet.whales_selling >= 2 or (
            wallet.whale_net_flow_usd is not None and wallet.whale_net_flow_usd < 0
            and wallet.whales_selling >= 1
        ):
            events.append(AlertEvent(
                priority=AlertPriority.HIGH,
                alert_type="whale_exit",
                token=result.pair.base_token,
                title=f"{wallet.whales_selling} whale(s) selling"
                      + (f"; net flow ${wallet.whale_net_flow_usd:,.0f}"
                         if wallet.whale_net_flow_usd is not None else ""),
                why_it_matters="Large-holder distribution can absorb all organic demand "
                               "and often precedes sharp drawdowns.",
                reasons=tuple(f"{w.owner[:8]}… {w.percent:.1f}% [{w.classification.value}]"
                              for w in wallet.whales[:4]),
                scores={"smart_money": wallet.overall_score,
                        "master": result.master.final_score},
                monitoring=("check exchange inflows and holder-count trend next",),
            ))

        if wallet.accumulation is AccumulationVerdict.ARTIFICIAL:
            events.append(AlertEvent(
                priority=AlertPriority.HIGH,
                alert_type="insider_risk",
                token=result.pair.base_token,
                title="Artificial accumulation pattern detected",
                reasons=tuple(f.message for f in wallet.findings[:3]),
                scores={"smart_money": wallet.overall_score,
                        "master": result.master.final_score},
                monitoring=("treat volume and holder growth as untrustworthy until this clears",),
            ))
        return events

    # Community alert (Part 29, Section 3): a confirmed-fake community is a
    # decision-changing event — the master score already forces Avoid, and
    # this surfaces WHY to anyone tracking the token.
    def _community_rule(self, result: PipelineResult) -> AlertEvent | None:
        community = result.community
        if community is None or not community.is_artificial:
            return None
        return AlertEvent(
            priority=AlertPriority.HIGH,
            alert_type="community_fake",
            token=result.pair.base_token,
            title="Community engagement is artificial",
            reasons=tuple(f.message for f in community.findings[:4]),
            scores={"community": community.overall_score,
                    "master": result.master.final_score},
            why_it_matters="Fake communities exist to exit on real buyers; social "
                           "traction cannot be trusted as demand evidence here.",
            monitoring=("treat all social signals for this token as untrustworthy",),
        )

    # IF the score drops sharply vs the last snapshot THEN review (Part 13 Section 7).
    def _score_drop_rule(self, result: PipelineResult,
                         previous_score: float | None) -> AlertEvent | None:
        if not self._score_declining(result, previous_score):
            return None
        drop = previous_score - result.master.final_score
        return AlertEvent(
            priority=AlertPriority.HIGH,
            alert_type="score_drop_review",
            token=result.pair.base_token,
            title=f"Score dropped {drop:.0f} points "
                  f"({previous_score:.0f} -> {result.master.final_score:.0f})",
            reasons=tuple(f.message for f in result.security.findings[:3]) or
                    ("re-assessment weakened; review the thesis",),
            scores={"master": result.master.final_score},
            monitoring=("re-read the original thesis; archive if it no longer holds",),
        )

    def _score_declining(self, result: PipelineResult,
                         previous_score: float | None) -> bool:
        """True when the score fell by at least the review threshold since the
        last look. Shared by ``_score_drop_rule`` (the informational alert)
        and the buy-side suppression below: a coin caught mid-decline is not
        a fresh early opportunity, whatever its current absolute score still
        clears. ``previous_score is None`` (a coin's first-ever look) never
        counts as declining — a fresh discovery is never penalized for being
        new."""
        if previous_score is None:
            return False
        return (previous_score - result.master.final_score) >= self._s.score_drop_review_points


def events_from_security_changes(
    token: TokenIdentity, changes, *, master_score: float | None = None,
) -> list[AlertEvent]:
    """Convert detected security-fact changes into alert events (Part 18, Section 10).

    Changes arrive worst-first; one event is emitted per severity level so
    a critical change is never buried inside a medium digest. ``master_score``
    is the master assessment score at detection time, so these alerts are
    not permanently excluded from Part 29's score-drift performance
    measurement (Rule 8/13 — the score existed, it just wasn't threaded through).
    """
    by_severity: dict[AlertPriority, list] = {}
    for change in changes:
        by_severity.setdefault(change.severity, []).append(change)

    titles = {
        AlertPriority.CRITICAL: "SECURITY CHANGE — high-priority review required now",
        AlertPriority.HIGH: "Security facts worsened — review promptly",
        AlertPriority.MEDIUM: "Security facts drifting — keep watching",
    }
    monitoring = {
        AlertPriority.CRITICAL: ("re-verify the contract and LP status before anything else",
                                 "if holding, decide exit before anything else"),
        AlertPriority.HIGH: ("re-run a full security assessment",),
        AlertPriority.MEDIUM: ("compare again at the next recheck; archive if the drift continues",),
    }

    events: list[AlertEvent] = []
    for severity, group in by_severity.items():
        events.append(AlertEvent(
            priority=severity,
            alert_type="security_change",
            token=token,
            title=titles.get(severity, "Security facts changed"),
            reasons=tuple(c.message for c in group[:5]),
            scores={"master": master_score} if master_score is not None else {},
            monitoring=monitoring.get(severity, ()),
        ))
    return events


# Alert ranking components (Part 29, Section 10). The spec fixes the
# weights (impact 40 / confidence 30 / urgency 20 / novelty 10); the
# component scales are documented implementation choices: impact and
# urgency derive from the priority level (Section 2 defines priority AS
# the impact/urgency grading), confidence from evidence density, novelty
# from whether this token+type was alerted before.
_RANK_WEIGHTS = {"impact": 0.40, "confidence": 0.30, "urgency": 0.20, "novelty": 0.10}
_IMPACT_POINTS = {AlertPriority.CRITICAL: 100.0, AlertPriority.HIGH: 75.0,
                  AlertPriority.MEDIUM: 50.0, AlertPriority.LOW: 25.0}
_URGENCY_POINTS = {AlertPriority.CRITICAL: 100.0, AlertPriority.HIGH: 70.0,
                   AlertPriority.MEDIUM: 40.0, AlertPriority.LOW: 10.0}


def rank_alert(event: AlertEvent, *, is_novel: bool) -> float:
    """0-100 dispatch rank (Part 29, Section 10). Higher ranks send first."""
    confidence = min(100.0, 30.0 + 20.0 * len(event.reasons))
    return (
        _RANK_WEIGHTS["impact"] * _IMPACT_POINTS[event.priority]
        + _RANK_WEIGHTS["confidence"] * confidence
        + _RANK_WEIGHTS["urgency"] * _URGENCY_POINTS[event.priority]
        + _RANK_WEIGHTS["novelty"] * (100.0 if is_novel else 25.0)
    )


class AlertSink(Protocol):
    async def send(self, event: AlertEvent) -> None: ...


class ConsoleSink:
    """Prints alerts to stdout and the log (default sink until Part 29)."""

    # A local log, not a delivery target: when external sinks (Telegram/
    # Discord) are configured, the console's unconditional success must not
    # make a lost phone alert count as delivered (see dispatch()).
    external = False

    def __init__(self):
        self._logger = get_logger("alerts.console")

    async def send(self, event: AlertEvent) -> bool:
        print(event.render())
        self._logger.info("alert dispatched: %s %s %s",
                          event.priority.value, event.alert_type, event.token.address)
        return True


class NotificationEngine:
    """Dispatches alert events to sinks with per-token/type cooldown."""

    def __init__(
        self,
        sinks: list[AlertSink],
        settings: AlertEngineSettings,
        *,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        if not sinks:
            raise ValueError("NotificationEngine requires at least one sink")
        self._sinks = sinks
        self._cooldown = settings.cooldown_seconds
        self._time = time_func
        self._last_sent: dict[tuple[str, str, str], float] = {}
        self._logger = get_logger("alerts.engine")

    async def dispatch(self, events: list[AlertEvent]) -> list[AlertEvent]:
        """Send events not in cooldown; returns those actually delivered.

        Events are ranked before sending (Part 29 Section 10) so the most
        decision-relevant alert always arrives first, and each is stamped
        with its detection time for the Section 7 message format.
        """
        now = self._time()

        def key_of(event: AlertEvent) -> tuple[str, str, str, str]:
            # Priority is part of the cooldown key: events_from_security_changes
            # deliberately emits one event per severity for the same
            # alert_type, and a lower-priority alert's cooldown must never
            # suppress a later higher-priority one for the same token/type
            # (a CRITICAL rug warning silently eaten by an earlier MEDIUM
            # drift alert would be a permanent, unrecoverable loss — Rule 13).
            return (event.token.chain, event.token.address.lower(),
                    event.alert_type, event.priority.value)

        # Evict cooldown entries that have expired: once older than the
        # cooldown they can never suppress anything, so keeping them is pure
        # memory growth in the weeks-long monitor process (bug-hunt finding).
        # A just-expired key is treated as novel again anyway, so pruning is
        # behaviour-preserving.
        if len(self._last_sent) > 256:
            self._last_sent = {k: t for k, t in self._last_sent.items()
                               if now - t < self._cooldown}

        ranked = sorted(
            events,
            key=lambda e: rank_alert(e, is_novel=key_of(e) not in self._last_sent),
            reverse=True,
        )

        delivered: list[AlertEvent] = []
        for event in ranked:
            key = key_of(event)
            last = self._last_sent.get(key)
            if last is not None and now - last < self._cooldown:
                self._logger.debug("alert suppressed by cooldown: %s %s",
                                   event.alert_type, event.token.address)
                continue
            if event.detected_at is None:
                event = dataclasses.replace(event, detected_at=datetime.now(timezone.utc))
            # Per-sink isolation: one sink raising must not abort the rest
            # of this event's sinks NOR every remaining event in the batch
            # (previously an unhandled exception from one sink propagated
            # out of dispatch() entirely). Cooldown is stamped only after
            # at least one sink actually delivered — stamping it
            # unconditionally beforehand meant a total delivery outage
            # (e.g. Telegram down) permanently lost the alert instead of
            # letting it retry once the cooldown window elapsed (Rule 7).
            # Delivery accounting (bug-hunt finding): sinks used to swallow
            # their failures, and the always-successful console made a LOST
            # phone alert count as delivered — cooldown stamped, recorded in
            # history, never retried. Now a sink returns True (delivered),
            # False (failed), or None (filtered / legacy sink, treated as
            # success for compatibility); and when any EXTERNAL sink is
            # configured, only external sinks decide delivery — the console
            # is a log, not the operator's phone.
            has_external = any(getattr(s, "external", False) for s in self._sinks)
            any_delivered = False
            for sink in self._sinks:
                try:
                    outcome = await sink.send(event)
                except Exception as exc:  # noqa: BLE001 — one sink's bug must not sink the batch
                    outcome = False
                    self._logger.error("sink %s failed to deliver %s alert for %s: %s",
                                       type(sink).__name__, event.alert_type,
                                       event.token.address, exc)
                counts = getattr(sink, "external", False) or not has_external
                if counts and outcome is not False:
                    any_delivered = True
            if any_delivered:
                self._last_sent[key] = now
                delivered.append(event)
            else:
                self._logger.warning(
                    "all sinks failed for %s %s; not marking delivered (will retry)",
                    event.alert_type, event.token.address)
        return delivered
