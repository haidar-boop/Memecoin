"""Smart money & whale intelligence engine (Spec Part 17).

Turns a normalized :class:`WalletIntelData` snapshot into a scored
:class:`WalletAssessment`: the Smart Money Confidence Score (Section 11,
five lenses at 20% each), whale classification (Section 6), accumulation
verdict (Section 5), and exchange-flow estimates (Section 10).

Doctrine (Section 1 / final rule): smart money is defined by being
*consistently right*, not by being large or early. Until the outcome
tracking of Part 24 accumulates wallet track records, the
``historical_success`` lens honestly reports "no data" — wallet size and
entry order alone never earn that score.

Known limitations, stated rather than hidden (Rule 8):

* Top holders can include pools and custodians; the pair address and
  known exchange wallets are classified CUSTODIAL and excluded from
  personal-whale math, but unknown custodians may remain.
* Exchange-flow figures only cover a small list of known exchange
  wallets; real flows are at least the reported number.
"""

from __future__ import annotations

import dataclasses
from collections import Counter, defaultdict
from dataclasses import dataclass

from meme_intelligence.analyzers.common import (
    Finding,
    SubScore,
    confidence_from_facts,
    scale,
)
from meme_intelligence.config.settings import SmartMoneySubWeights, WalletIntelSettings
from meme_intelligence.core.enums import (
    AccumulationVerdict,
    ConfidenceLevel,
    RiskTier,
    WhaleType,
)
from meme_intelligence.core.errors import InsufficientDataError
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import (
    DexPair,
    OnChainProfile,
    TokenIdentity,
    WalletIntelData,
)

# Known Solana exchange hot-wallet owners (heuristic, deliberately small;
# extend as verified). Unknown exchanges mean flows are LOWER BOUNDS.
KNOWN_EXCHANGE_OWNERS: dict[str, str] = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "binance",
    "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "coinbase",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "coinbase",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "bybit",
}

_DUST_USD = 10.0  # trades below this are noise for wallet-quality math

# Entry-timing signals: where in the observed price range did buying happen?
_TIMING_ACCUMULATION_SIGNAL = 80.0  # buys concentrated in the lower half
_TIMING_MIXED_SIGNAL = 60.0
_TIMING_CHASING_SIGNAL = 40.0       # buys concentrated after the run-up
_TIMING_FLAT_RANGE_FRACTION = 0.02  # <2% price spread = consolidation

# Promotion-and-exit pattern (Part 18, Section 9): price extended while the
# largest holders distribute into the attention.
_PUMP_EXIT_PRICE_CHANGE_PERCENT = 50.0
_PUMP_EXIT_MIN_WHALE_OUTFLOW_USD = 500.0


@dataclass(frozen=True)
class WhaleInfo:
    """One classified large holder (Part 17, Section 6)."""

    owner: str
    percent: float
    classification: WhaleType
    note: str = ""


@dataclass(frozen=True)
class WalletAssessment:
    """Smart-money verdict for one token (report format per Part 17, Section 12)."""

    token: TokenIdentity
    sources: tuple[str, ...]
    sub_scores: dict[str, float | None]
    overall_score: float
    accumulation: AccumulationVerdict
    whales: tuple[WhaleInfo, ...]
    net_flows: tuple[tuple[str, float], ...]  # (wallet, net USD in window), largest first
    accumulating_wallets: int | None      # distinct net buyers above dust
    whales_selling: int                    # personal whales seen selling recently
    whale_net_flow_usd: float | None       # whale buys minus sells in the window
    exchange_inflow_usd: float | None      # lower bound (known exchanges only)
    exchange_outflow_usd: float | None
    confidence: ConfidenceLevel
    findings: tuple[Finding, ...]
    unknown_fields: tuple[str, ...]
    coverage: float

    def summary(self) -> str:
        lines = [
            f"Smart money assessment: {self.token.symbol or self.token.address} "
            f"(sources: {', '.join(self.sources) or 'none'})",
            f"  Overall: {self.overall_score:.0f}/100  accumulation={self.accumulation.value}  "
            f"confidence={self.confidence.value}",
        ]
        if self.coverage < 1.0:
            lines.append(f"  NOTE: only {self.coverage:.0%} of smart-money lenses have data "
                         "(wallet track records accumulate over time)")
        for name, score in self.sub_scores.items():
            rendered = f"{score:.0f}/100" if score is not None else "no data"
            lines.append(f"  {name:>18}: {rendered}")
        if self.whales:
            lines.append("  Whales (top holders above threshold):")
            for whale in self.whales[:8]:
                note = f" — {whale.note}" if whale.note else ""
                lines.append(f"    {whale.owner[:8]}… {whale.percent:5.2f}% "
                             f"[{whale.classification.value}]{note}")
        if self.whale_net_flow_usd is not None:
            direction = "accumulating" if self.whale_net_flow_usd >= 0 else "distributing"
            lines.append(f"  Whale net flow: ${self.whale_net_flow_usd:,.0f} ({direction})")
        if self.exchange_inflow_usd is not None or self.exchange_outflow_usd is not None:
            lines.append(f"  Exchange flow (known wallets only, lower bound): "
                         f"in ${self.exchange_inflow_usd or 0:,.0f} / "
                         f"out ${self.exchange_outflow_usd or 0:,.0f}")
        if self.findings:
            lines.append("  Findings:")
            for finding in self.findings:
                lines.append(f"    [{finding.severity.value}] {finding.message}")
        return "\n".join(lines)


class WalletIntelligenceAnalyzer:
    """Scores wallet behavior per the Part 17 framework."""

    def __init__(self, settings: WalletIntelSettings, weights: SmartMoneySubWeights):
        self._s = settings
        self._w = weights
        self._logger = get_logger("analyzers.wallet")

    def assess(
        self,
        data: WalletIntelData,
        pair: DexPair | None = None,
        *,
        reputations: dict[str, float] | None = None,
    ) -> WalletAssessment:
        """``reputations`` maps wallet -> historical reputation score (0-100),
        supplied once Part 24's outcome tracking has built track records."""
        net_by_wallet = self._net_usd_by_wallet(data)
        whales = self._classify_whales(data, pair, net_by_wallet)
        personal_whales = [w for w in whales if w.classification is not WhaleType.CUSTODIAL]

        parts = [
            self._assess_quality_wallets(data, net_by_wallet),
            self._assess_historical_success(net_by_wallet, reputations),
            self._assess_entry_timing(data),
            self._assess_holding_behavior(personal_whales),
            self._assess_risk_signals(data, personal_whales, net_by_wallet),
        ]
        weight_map = dataclasses.asdict(self._w)

        sub_scores: dict[str, float | None] = {}
        findings: list[Finding] = []
        unknowns: list[str] = []
        weighted_sum = 0.0
        available = 0.0
        for part in parts:
            score = part.score()
            sub_scores[part.category] = score
            findings.extend(part.findings)
            unknowns.extend(part.unknowns)
            if score is not None:
                weighted_sum += score * weight_map[part.category]
                available += weight_map[part.category]

        if available == 0.0:
            raise InsufficientDataError(
                f"no wallet intelligence available for {data.token.address}"
            )

        pump_exit = self._check_pump_and_exit(pair, personal_whales, net_by_wallet)
        if pump_exit is not None:
            findings.append(pump_exit)

        overall = weighted_sum / available
        accumulation = self._accumulation_verdict(data, net_by_wallet, findings)
        inflow, outflow = self._exchange_flow(data, pair)
        whale_net = self._whale_net_flow(personal_whales, net_by_wallet)
        whales_selling = sum(
            1 for w in personal_whales
            if net_by_wallet.get(w.owner, 0.0) < -_DUST_USD
        )
        accumulating = self._accumulating_count(net_by_wallet) if data.recent_trades else None

        self._logger.info(
            "wallet assessment %s: score=%.0f accumulation=%s whales=%d selling=%d",
            data.token.address, overall, accumulation.value,
            len(personal_whales), whales_selling,
        )

        net_flows = tuple(sorted(net_by_wallet.items(),
                                 key=lambda kv: abs(kv[1]), reverse=True)[:50])
        return WalletAssessment(
            token=data.token,
            sources=data.sources,
            sub_scores=sub_scores,
            overall_score=overall,
            accumulation=accumulation,
            whales=tuple(whales),
            net_flows=net_flows,
            accumulating_wallets=accumulating,
            whales_selling=whales_selling,
            whale_net_flow_usd=whale_net,
            exchange_inflow_usd=inflow,
            exchange_outflow_usd=outflow,
            confidence=confidence_from_facts(sum(p.known_count for p in parts), len(unknowns)),
            findings=tuple(findings),
            unknown_fields=tuple(unknowns),
            coverage=available,
        )

    # ---- Shared trade math ----

    @staticmethod
    def _net_usd_by_wallet(data: WalletIntelData) -> dict[str, float]:
        """Net USD bought (positive) or sold (negative) per wallet in the window."""
        net: dict[str, float] = defaultdict(float)
        for trade in data.recent_trades:
            if trade.volume_usd is None:
                continue
            net[trade.owner] += trade.volume_usd if trade.side == "buy" else -trade.volume_usd
        return dict(net)

    def _accumulating_count(self, net_by_wallet: dict[str, float]) -> int:
        return sum(1 for value in net_by_wallet.values() if value > _DUST_USD)

    # ---- Whale classification (Section 6) ----

    def _classify_whales(
        self, data: WalletIntelData, pair: DexPair | None, net_by_wallet: dict[str, float],
    ) -> list[WhaleInfo]:
        whales: list[WhaleInfo] = []
        pool_address = pair.pair_address if pair is not None else None
        for holding in data.top_holders:
            if holding.percent < self._s.whale_min_percent:
                continue
            if holding.owner == pool_address:
                whales.append(WhaleInfo(holding.owner, holding.percent,
                                        WhaleType.CUSTODIAL, "trading pool"))
                continue
            exchange = KNOWN_EXCHANGE_OWNERS.get(holding.owner)
            if exchange:
                whales.append(WhaleInfo(holding.owner, holding.percent,
                                        WhaleType.CUSTODIAL, f"{exchange} custody"))
                continue
            net = net_by_wallet.get(holding.owner, 0.0)
            if holding.percent >= self._s.risk_whale_percent:
                whales.append(WhaleInfo(holding.owner, holding.percent, WhaleType.RISK,
                                        "large enough to move the market alone"))
            elif net < -_DUST_USD or (net > _DUST_USD and holding.owner in net_by_wallet):
                whales.append(WhaleInfo(holding.owner, holding.percent, WhaleType.TRADING,
                                        "active in recent trades"))
            else:
                whales.append(WhaleInfo(holding.owner, holding.percent, WhaleType.LONG_TERM))
        return whales

    # ---- Lens 1: quality wallets (how many independent net buyers?) ----

    def _assess_quality_wallets(self, data: WalletIntelData,
                                net_by_wallet: dict[str, float]) -> SubScore:
        s = SubScore("quality_wallets")
        if not data.recent_trades:
            s.unknowns.append("recent_trades")
            return s
        if s.observe("accumulating_wallets", self._accumulating_count(net_by_wallet)):
            count = self._accumulating_count(net_by_wallet)
            s.signal(scale(count, 0.0, float(self._s.target_accumulating_wallets)))
        return s

    # ---- Lens 2: historical success (needs Part 24 track records) ----

    def _assess_historical_success(self, net_by_wallet: dict[str, float],
                                   reputations: dict[str, float] | None) -> SubScore:
        s = SubScore("historical_success")
        if not reputations:
            s.unknowns.append("wallet_track_records")
            return s
        known = [reputations[w] for w in net_by_wallet if w in reputations]
        if s.observe("participant_reputations", known or None):
            s.signal(sum(known) / len(known))
        return s

    # ---- Lens 3: entry timing (buying the dip or chasing the run?) ----

    def _assess_entry_timing(self, data: WalletIntelData) -> SubScore:
        s = SubScore("entry_timing")
        priced_buys = [t for t in data.recent_trades
                       if t.side == "buy" and t.price_usd is not None and t.volume_usd]
        prices = [t.price_usd for t in data.recent_trades if t.price_usd is not None]
        if not priced_buys or len(prices) < 5:
            s.unknowns.append("priced_trades")
            return s
        s.observe("buy_price_positioning", True)

        low, high = min(prices), max(prices)
        if high <= 0 or (high - low) / high < _TIMING_FLAT_RANGE_FRACTION:
            s.signal(_TIMING_ACCUMULATION_SIGNAL)  # buying during consolidation
            return s

        midpoint = (low + high) / 2.0
        buy_volume_low = sum(t.volume_usd for t in priced_buys if t.price_usd <= midpoint)
        buy_volume_total = sum(t.volume_usd for t in priced_buys)
        low_fraction = buy_volume_low / buy_volume_total if buy_volume_total else 0.0
        if low_fraction >= 0.6:
            s.signal(_TIMING_ACCUMULATION_SIGNAL)
        elif low_fraction <= 0.4:
            s.signal(_TIMING_CHASING_SIGNAL)
        else:
            s.signal(_TIMING_MIXED_SIGNAL)
        return s

    # ---- Lens 4: holding behavior (are the whales staying?) ----

    def _assess_holding_behavior(self, personal_whales: list[WhaleInfo]) -> SubScore:
        s = SubScore("holding_behavior")
        if not personal_whales:
            s.unknowns.append("personal_whales")
            return s
        s.observe("whale_holding", True)
        trading = sum(1 for w in personal_whales
                      if w.classification in (WhaleType.TRADING, WhaleType.RISK))
        fraction_stable = 1.0 - trading / len(personal_whales)
        s.signal(30.0 + 60.0 * fraction_stable)
        risk_whales = [w for w in personal_whales if w.classification is WhaleType.RISK]
        for whale in risk_whales[:2]:
            s.deduct(10, RiskTier.SERIOUS_WARNING,
                     f"wallet {whale.owner[:8]}… holds {whale.percent:.1f}% "
                     "and can move the market alone")
        return s

    # ---- Lens 5: risk signals (manipulation patterns, Sections 5/8) ----

    def _assess_risk_signals(self, data: WalletIntelData,
                             personal_whales: list[WhaleInfo],
                             net_by_wallet: dict[str, float]) -> SubScore:
        s = SubScore("risk_signals")
        trades = [t for t in data.recent_trades if t.volume_usd]
        if not trades:
            s.unknowns.append("recent_trades")
            return s
        s.observe("trade_patterns", True)

        # Same-size repeated trades = scripted activity (Section 5).
        sizes = Counter(round(t.volume_usd, 2) for t in trades)
        most_common_count = sizes.most_common(1)[0][1]
        if most_common_count / len(trades) >= self._s.artificial_same_size_fraction:
            s.deduct(30, RiskTier.SERIOUS_WARNING,
                     f"{most_common_count}/{len(trades)} recent trades are identical size: "
                     "scripted trading pattern")

        # One wallet dominating buy volume = artificial demand (Section 8).
        buy_by_wallet = defaultdict(float)
        for t in trades:
            if t.side == "buy":
                buy_by_wallet[t.owner] += t.volume_usd
        total_buys = sum(buy_by_wallet.values())
        # Dominance over dust volume proves nothing; only flag when the
        # window carries enough real money for the share to mean something.
        if total_buys >= self._s.min_buy_volume_for_dominance_usd:
            top_share = max(buy_by_wallet.values()) / total_buys
            if top_share >= self._s.dominant_buyer_volume_fraction:
                s.deduct(30, RiskTier.SERIOUS_WARNING,
                         f"one wallet is {top_share:.0%} of recent buy volume: "
                         "artificial demand risk")

        # A top holder aggressively adding via recent buys = possible insider
        # accumulation building hidden concentration (Section 8).
        whale_owners = {w.owner for w in personal_whales}
        insider_buyers = [w for w in whale_owners if net_by_wallet.get(w, 0.0) > _DUST_USD]
        if len(insider_buyers) >= 2:
            s.deduct(15, RiskTier.ACCEPTABLE_UNCERTAINTY,
                     f"{len(insider_buyers)} top holders are still adding: "
                     "watch concentration growth")
        return s

    def _check_pump_and_exit(self, pair: DexPair | None,
                             personal_whales: list[WhaleInfo],
                             net_by_wallet: dict[str, float]) -> Finding | None:
        """Promotion-and-exit pattern (Part 18 Section 9): the on-chain half —
        attention drives price up while the largest holders sell into it.
        The social half (promotion spike) joins when social collectors land."""
        if pair is None or pair.price_change_24h is None:
            return None
        if pair.price_change_24h < _PUMP_EXIT_PRICE_CHANGE_PERCENT:
            return None
        whale_outflow = -sum(
            net_by_wallet.get(w.owner, 0.0) for w in personal_whales
            if net_by_wallet.get(w.owner, 0.0) < 0
        )
        if whale_outflow < _PUMP_EXIT_MIN_WHALE_OUTFLOW_USD:
            return None
        return Finding(
            "risk_signals", RiskTier.SERIOUS_WARNING,
            f"price up {pair.price_change_24h:.0f}% in 24h while top holders sold "
            f"${whale_outflow:,.0f}: promotion-and-exit pattern",
        )

    # ---- Accumulation verdict (Section 5) ----

    def _accumulation_verdict(self, data: WalletIntelData,
                              net_by_wallet: dict[str, float],
                              findings: list[Finding]) -> AccumulationVerdict:
        if not data.recent_trades:
            return AccumulationVerdict.UNKNOWN
        artificial = any(
            f.severity is RiskTier.SERIOUS_WARNING and f.category == "risk_signals"
            for f in findings
        )
        if artificial:
            return AccumulationVerdict.ARTIFICIAL
        accumulating = self._accumulating_count(net_by_wallet)
        total_buy = sum(v for v in net_by_wallet.values() if v > 0)
        total_sell = -sum(v for v in net_by_wallet.values() if v < 0)
        if (accumulating >= max(3, self._s.target_accumulating_wallets // 2)
                and total_buy > total_sell):
            return AccumulationVerdict.HEALTHY
        return AccumulationVerdict.MIXED

    # ---- Exchange flow (Section 10; lower bounds) ----

    def _exchange_flow(self, data: WalletIntelData,
                       pair: DexPair | None) -> tuple[float | None, float | None]:
        if not data.recent_transfers:
            return None, None
        price = pair.price_usd if pair is not None else None
        if price is None:
            return None, None
        inflow = outflow = 0.0
        for transfer in data.recent_transfers:
            if transfer.ui_amount is None:
                continue
            if transfer.to_owner in KNOWN_EXCHANGE_OWNERS:
                inflow += transfer.ui_amount * price
            if transfer.from_owner in KNOWN_EXCHANGE_OWNERS:
                outflow += transfer.ui_amount * price
        return inflow, outflow

    @staticmethod
    def _whale_net_flow(personal_whales: list[WhaleInfo],
                        net_by_wallet: dict[str, float]) -> float | None:
        flows = [net_by_wallet[w.owner] for w in personal_whales if w.owner in net_by_wallet]
        if not flows:
            return None
        return sum(flows)


@dataclass(frozen=True)
class WalletTrackRecord:
    """A wallet's measurable history (Part 17, Sections 2-3).

    Populated by Part 24's outcome tracking: sightings recorded today are
    joined against token outcomes later. Every field is optional — an
    unmeasured dimension stays out of the score instead of being guessed.
    """

    wallet: str
    tokens_traded: int | None = None
    win_rate: float | None = None                # 0-1: profitable entries / entries
    early_entry_rate: float | None = None        # 0-1: entries before the token's peak attention
    rug_avoidance_rate: float | None = None      # 0-1: fraction of entries that were not rugs
    median_position_usd: float | None = None
    active_span_days: float | None = None


def wallet_reputation(record: WalletTrackRecord) -> tuple[float, float] | None:
    """Wallet Reputation Score per Part 17 Section 2: (score 0-100, coverage 0-1).

    Weights: historical performance 25, entry timing 20, risk management 20,
    project selection 20, consistency 15. Returns ``None`` when nothing is
    measurable — a wallet with no track record has no reputation, not a
    neutral one (smart money is defined by being consistently right).
    """
    components: list[tuple[float, float]] = []  # (weight, 0-100 value)

    if record.win_rate is not None:
        components.append((0.25, 100.0 * record.win_rate))
    if record.early_entry_rate is not None:
        components.append((0.20, 100.0 * record.early_entry_rate))
    if record.median_position_usd is not None:
        # Risk management proxy: sane position sizes; extremes score lower.
        sized_ok = _DUST_USD * 5 <= record.median_position_usd <= 250_000.0
        components.append((0.20, 75.0 if sized_ok else 40.0))
    if record.rug_avoidance_rate is not None:
        components.append((0.20, 100.0 * record.rug_avoidance_rate))
    if record.active_span_days is not None and record.tokens_traded is not None:
        survived = record.active_span_days >= 30 and record.tokens_traded >= 5
        components.append((0.15, 80.0 if survived else 45.0))

    if not components:
        return None
    total_weight = sum(w for w, _ in components)
    score = sum(w * v for w, v in components) / total_weight
    return score, total_weight


def sightings_from_assessment(
    assessment: WalletAssessment,
) -> list[tuple[str, str, float | None]]:
    """Flatten an assessment into (wallet, side, usd) sighting rows for storage."""
    rows: list[tuple[str, str, float | None]] = []
    for wallet, net in assessment.net_flows:
        if abs(net) > _DUST_USD:
            rows.append((wallet, "buy" if net > 0 else "sell", abs(net)))
    for whale in assessment.whales:
        if whale.classification is not WhaleType.CUSTODIAL:
            rows.append((whale.owner, "hold_whale", None))
    return rows


def enrich_onchain_profile(profile: OnChainProfile,
                           assessment: WalletAssessment) -> OnChainProfile:
    """Fill the wallet-level OnChainProfile slots from a wallet assessment,
    lighting up the smart-money / whale / token-flow sub-scores (Part 6)."""
    return dataclasses.replace(
        profile,
        smart_wallet_count=assessment.accumulating_wallets,
        smart_wallet_net_flow_usd=None,  # needs reputation-tagged wallets (Part 24)
        whale_net_flow_usd=assessment.whale_net_flow_usd,
        exchange_inflow_usd=assessment.exchange_inflow_usd,
        exchange_outflow_usd=assessment.exchange_outflow_usd,
    )
