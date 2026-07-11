"""Shared building blocks for all analysis engines (Parts 4/5/6 and beyond).

Every analyzer follows the same evidence discipline:

* Facts are *observed* — unknown facts are recorded, never assumed safe or
  healthy (Rule 8).
* Quality metrics contribute 0-100 *signals* (averaged); risks apply
  *deductions*; confirmed fatal conditions raise *destructive* flags.
* Confidence reflects the ratio of known to total facts checked.
"""

from __future__ import annotations

from dataclasses import dataclass

from meme_intelligence.core.enums import ConfidenceLevel, RiskTier

# Minimum ratio of known-to-total facts for MEDIUM confidence; below this
# the assessment is flagged LOW (too much of the checklist was unknowable).
MEDIUM_CONFIDENCE_KNOWN_RATIO = 0.40


@dataclass(frozen=True)
class Finding:
    """One observation: what was found, how bad it is, what it cost."""

    category: str
    severity: RiskTier
    message: str
    deduction: float = 0.0


class SubScore:
    """Accumulates evidence for one assessment category.

    Two scoring styles, combinable:

    * **Deduction style** (risk analysis): start at 100, subtract per finding.
    * **Signal style** (quality analysis): average 0-100 signals, then
      subtract deductions.

    With no known facts the category scores ``None`` — absence of data is
    reported, not scored.
    """

    def __init__(self, category: str, *, requires_signal: bool = False):
        """``requires_signal=True`` opts a category out of the deduction-style
        base-100 default: with no explicit :meth:`signal` call, ``score()``
        returns ``None`` (unknown) rather than treating "no quality judgment,
        only risk flags" as a fabricated perfect score (Rule 8). Deduction-
        style analyzers (start at 100, subtract per finding) keep the
        original default.
        """
        self.category = category
        self.findings: list[Finding] = []
        self.unknowns: list[str] = []
        self.known_count = 0
        self._signals: list[float] = []
        self._requires_signal = requires_signal

    def observe(self, field_name: str, value: object) -> bool:
        """Record whether a fact is known; returns True when it can be evaluated."""
        if value is None:
            self.unknowns.append(field_name)
            return False
        self.known_count += 1
        return True

    def signal(self, points: float) -> None:
        """Contribute a 0-100 quality signal (clamped)."""
        self._signals.append(max(0.0, min(100.0, points)))

    def deduct(self, points: float, severity: RiskTier, message: str) -> None:
        self.findings.append(Finding(self.category, severity, message, points))

    def flag_destructive(self, message: str) -> None:
        self.findings.append(Finding(self.category, RiskTier.DESTRUCTIVE, message))

    def score(self) -> float | None:
        if self.known_count == 0:
            return None
        if self._requires_signal and not self._signals:
            return None
        base = sum(self._signals) / len(self._signals) if self._signals else 100.0
        total = base - sum(f.deduction for f in self.findings)
        return max(0.0, min(100.0, total))


def confidence_from_facts(known_count: int, unknown_count: int) -> ConfidenceLevel:
    """Confidence from the ratio of known facts to all facts checked.

    Capped at MEDIUM while data comes from a single source per category —
    HIGH requires multi-source confirmation (Rule 9), which arrives with
    the provider-pool integration.
    """
    total = known_count + unknown_count
    if total == 0:
        return ConfidenceLevel.LOW
    if known_count / total >= MEDIUM_CONFIDENCE_KNOWN_RATIO:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def scale(value: float, floor: float, ceiling: float) -> float:
    """Map [floor, ceiling] linearly onto [0, 100], clamped. Higher is better."""
    if ceiling <= floor:
        return 100.0
    fraction = (value - floor) / (ceiling - floor)
    return 100.0 * max(0.0, min(1.0, fraction))


def scale_inverted(value: float, best: float, worst: float) -> float:
    """Map [best, worst] linearly onto [100, 0], clamped. Lower value is better."""
    if worst <= best:
        return 100.0
    fraction = (value - best) / (worst - best)
    return 100.0 * (1.0 - max(0.0, min(1.0, fraction)))
