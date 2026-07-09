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

    def render(self) -> str:
        symbol = self.token.symbol or self.token.address[:8]
        lines = [f"[{self.priority.value.upper()}] {self.alert_type}: {symbol} ({self.token.chain})",
                 f"  {self.title}"]
        if self.why_it_matters:
            lines.append(f"  why it matters: {self.why_it_matters}")
        for reason in self.reasons:
            lines.append(f"  - {reason}")
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

    def __init__(self, thresholds: AlertThresholds, settings: AlertEngineSettings):
        self._t = thresholds
        self._s = settings

    def evaluate(
        self,
        result: PipelineResult,
        *,
        previous_score: float | None = None,
    ) -> list[AlertEvent]:
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
            return events

        events: list[AlertEvent] = []
        events.extend(self._emergency_rule(result))
        opportunity = self._opportunity_rule(result)
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
        return events

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
    def _opportunity_rule(self, result: PipelineResult) -> AlertEvent | None:
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
        if not unverified:
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
                  f"unverified gates: {', '.join(unverified)}",
            reasons=(f"classification: {result.master.classification.value}",
                     "unverified categories are NOT confirmation (Part 31 Section 6)"),
            scores=scores,
            monitoring=tuple(f"verify the {name} gate before sizing any position"
                             for name in unverified),
        )

    # IF momentum accelerates through the gate in a sane entry zone THEN
    # surface it (Part 15 Section 5 — momentum alert).
    def _momentum_rule(self, result: PipelineResult) -> AlertEvent | None:
        momentum = result.momentum
        if momentum is None or result.security.is_destructive:
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
        if previous_score is None:
            return None
        drop = previous_score - result.master.final_score
        if drop < self._s.score_drop_review_points:
            return None
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

    def __init__(self):
        self._logger = get_logger("alerts.console")

    async def send(self, event: AlertEvent) -> None:
        print(event.render())
        self._logger.info("alert dispatched: %s %s %s",
                          event.priority.value, event.alert_type, event.token.address)


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
            any_delivered = False
            for sink in self._sinks:
                try:
                    await sink.send(event)
                    any_delivered = True
                except Exception as exc:  # noqa: BLE001 — one sink's bug must not sink the batch
                    self._logger.error("sink %s failed to deliver %s alert for %s: %s",
                                       type(sink).__name__, event.alert_type,
                                       event.token.address, exc)
            if any_delivered:
                self._last_sent[key] = now
                delivered.append(event)
            else:
                self._logger.warning(
                    "all sinks failed for %s %s; not marking delivered (will retry)",
                    event.alert_type, event.token.address)
        return delivered
