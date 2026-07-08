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

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from meme_intelligence.analyzers.risk_analyzer import emergency_flags
from meme_intelligence.config.settings import AlertEngineSettings, AlertThresholds
from meme_intelligence.core.enums import AlertPriority, EntryZone
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

    def render(self) -> str:
        symbol = self.token.symbol or self.token.address[:8]
        lines = [f"[{self.priority.value.upper()}] {self.alert_type}: {symbol} ({self.token.chain})",
                 f"  {self.title}"]
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
        events: list[AlertEvent] = []
        events.extend(self._emergency_rule(result))
        opportunity = self._opportunity_rule(result)
        if opportunity is not None:
            events.append(opportunity)
        momentum = self._momentum_rule(result)
        if momentum is not None:
            events.append(momentum)
        drop = self._score_drop_rule(result, previous_score)
        if drop is not None:
            events.append(drop)
        return events

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
                scores={"security": result.security.overall_score},
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
                scores={"security": result.security.overall_score},
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
            "community": (None, self._t.community),  # collector pending; stays unverified
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
        """Send events not in cooldown; returns those actually delivered."""
        delivered: list[AlertEvent] = []
        now = self._time()
        for event in events:
            key = (event.token.chain, event.token.address.lower(), event.alert_type)
            last = self._last_sent.get(key)
            if last is not None and now - last < self._cooldown:
                self._logger.debug("alert suppressed by cooldown: %s %s",
                                   event.alert_type, event.token.address)
                continue
            self._last_sent[key] = now
            for sink in self._sinks:
                await sink.send(event)
            delivered.append(event)
        return delivered
