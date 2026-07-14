"""Core data models shared across the system (Spec Parts 1, 2, 7, 31).

Missing data is always represented as ``None`` — never a fabricated value
(Rule 8). Score aggregation reports *coverage* so downstream confidence
ratings can reflect how much of the framework was actually backed by data.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from meme_intelligence.config.settings import ClassificationBands, ScoringWeights
from meme_intelligence.core.enums import Classification
from meme_intelligence.core.errors import InsufficientDataError


@dataclass(frozen=True)
class TokenIdentity:
    """Minimal identity of a token: chain + contract address, plus display info."""

    chain: str
    address: str
    name: str | None = None
    symbol: str | None = None


@dataclass(frozen=True)
class DexPair:
    """A normalized DEX trading pair snapshot (Spec Part 2 — DexScreener fields).

    Every metric is optional: providers frequently omit fields for young
    pairs, and absence must stay observable rather than default to zero.
    """

    chain: str
    pair_address: str
    base_token: TokenIdentity
    dex_id: str | None = None
    quote_symbol: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    fdv: float | None = None
    market_cap: float | None = None
    volume_24h: float | None = None
    price_change_24h: float | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None
    buyers_24h: int | None = None   # unique buying wallets (GeckoTerminal only)
    sellers_24h: int | None = None  # unique selling wallets (GeckoTerminal only)
    # Shorter windows for momentum/acceleration analysis (Parts 14/26)
    price_change_1h: float | None = None
    price_change_6h: float | None = None
    volume_1h: float | None = None
    volume_6h: float | None = None
    buys_1h: int | None = None
    sells_1h: int | None = None
    pair_created_at: datetime | None = None
    url: str | None = None


@dataclass(frozen=True)
class PumpFunLaunch:
    """One token-creation event from a launchpad stream (Spec Part 32.5 Section 3).

    Emitted by the PumpPortal WebSocket the moment a Pump.fun (or
    compatible launchpad) token is created. This is a *discovery* record
    only — per Part 32.5 Section 2 a launch is never confirmation; it
    enters analysis only after independent market data exists.

    Monetary values are SOL-denominated because the launch event carries
    no USD conversion; converting with an assumed SOL price would be
    fabrication (Rule 8).
    """

    token: TokenIdentity          # chain="solana", address=mint
    source: str                   # e.g. "pumpportal"
    launchpad: str | None = None  # provider pool id, e.g. "pump", "bonk"
    creator: str | None = None    # creator wallet (the launch transaction signer)
    created_at: datetime | None = None
    initial_buy_tokens: float | None = None    # creator dev-buy, token units
    initial_buy_sol: float | None = None       # creator dev-buy, SOL spent
    initial_buy_percent: float | None = None   # dev-buy as % of total supply (0-100)
    market_cap_sol: float | None = None        # implied market cap at creation
    bonding_curve: str | None = None           # bonding curve account address
    signature: str | None = None               # creation transaction signature


@dataclass(frozen=True)
class PumpFunCoinState:
    """Traction snapshot of one launchpad token (Spec Part 32.5 Sections 3/8).

    Normalized from the Pump.fun frontend API by the launch monitor's
    recheck pass to judge whether a tracked launch shows the "evidence of
    organic interest / increasing attention" Section 8 requires before
    deep analysis. Every field is optional — the frontend API is an
    unofficial surface and absent fields must stay observable (Rule 8).
    """

    token: TokenIdentity
    source: str
    fetched_at: datetime
    market_cap_sol: float | None = None
    usd_market_cap: float | None = None
    reply_count: int | None = None             # community comments on the coin page
    complete: bool | None = None               # bonding curve finished (graduated)
    curve_progress_percent: float | None = None  # 0-100, derived from curve reserves
    is_banned: bool | None = None
    nsfw: bool | None = None
    created_at: datetime | None = None
    last_trade_at: datetime | None = None
    ath_market_cap_sol: float | None = None
    creator: str | None = None


@dataclass(frozen=True)
class TopHolder:
    """One circulating top-holder entry from a security provider (Part 17).

    The raw material for wallet reputation: which wallets held a large
    share of a token early in its life. Burn addresses and locked
    holdings are excluded at parse time; program/infrastructure accounts
    (bonding curves, pools) are NOT — identifying them needs cross-token
    context the collector doesn't have, so filtering belongs to the
    later reputation-scoring step, not the raw record (Rule 8).
    """

    address: str
    percent: float | None = None  # share of supply held, 0-100


@dataclass(frozen=True)
class SecurityProfile:
    """Normalized contract-security facts about one token (Spec Parts 4/18/33).

    Collectors (GoPlus today; Token Sniffer and honeypot services later;
    plus the live Jupiter round-trip probe, Project 1) normalize their
    provider-specific payloads into this shape so the security analyzer
    never sees raw API responses (Part 32, Rule 3). ``None`` always means
    "the source did not report this" — the analyzer treats unknowns as
    reduced confidence, never as safe (Rule 8).

    Percentages are expressed 0-100.
    """

    token: TokenIdentity
    source: str

    # Honeypot / tradability (destructive when confirmed — Part 4 Section 3)
    is_honeypot: bool | None = None
    cannot_buy: bool | None = None
    cannot_sell_all: bool | None = None

    # Contract permissions (Part 4 Section 2)
    is_open_source: bool | None = None
    is_proxy: bool | None = None
    is_mintable: bool | None = None
    ownership_renounced: bool | None = None
    hidden_owner: bool | None = None
    can_take_back_ownership: bool | None = None
    has_blacklist: bool | None = None
    trading_pausable: bool | None = None
    is_freezable: bool | None = None       # Solana freeze authority
    balance_mutable: bool | None = None    # Solana balance-mutable authority
    selfdestruct: bool | None = None

    # Taxes (Part 4 Section 2 — tax functions)
    buy_tax_percent: float | None = None
    sell_tax_percent: float | None = None
    tax_modifiable: bool | None = None

    # Manipulation indicators (Part 18 Sections 7-9)
    fake_token: bool | None = None
    is_airdrop_scam: bool | None = None
    anti_whale_modifiable: bool | None = None
    slippage_modifiable: bool | None = None
    personal_slippage_modifiable: bool | None = None
    trading_cooldown: bool | None = None
    honeypot_same_creator_count: int | None = None

    # Distribution (Part 4 Section 5; excludes burn/locked addresses where possible)
    holder_count: int | None = None
    top_holder_percent: float | None = None
    top10_holder_percent: float | None = None
    # Circulating top-holder wallets (Part 17): same exclusion rules as the
    # percentages above, but keeping the addresses so wallet reputation can
    # accumulate (empty when the source reported no holder list).
    top_holders: tuple[TopHolder, ...] = ()

    # Developer (Part 4 Section 8)
    creator_percent: float | None = None
    owner_percent: float | None = None
    creator_address: str | None = None  # deployer wallet (feeds reputation checks)

    # Liquidity safety (Part 4 Section 4; USD depth comes from market data)
    lp_locked_percent: float | None = None

    # Live round-trip sell test (Project 1 -- Jupiter quote simulation; Solana only)
    live_buy_route_found: bool | None = None
    live_sell_route_found: bool | None = None
    live_round_trip_loss_percent: float | None = None


@dataclass(frozen=True)
class CommunityProfile:
    """Normalized community metrics for one token (Spec Part 5).

    Populated by the social collectors (X/Twitter, Telegram, Discord,
    Reddit) once their API integrations land; until then analyzers receive
    partially-filled profiles and report reduced coverage. Percentages are
    0-100; rates are per the unit named in the field.
    """

    token: TokenIdentity
    source: str

    # X/Twitter (Part 5 Section 6)
    twitter_followers: int | None = None
    twitter_engagement_rate_percent: float | None = None  # interactions / followers
    twitter_growth_rate_7d_percent: float | None = None
    bot_follower_percent: float | None = None

    # Telegram (Part 5 Section 7)
    telegram_members: int | None = None
    telegram_active_members: int | None = None
    telegram_admin_only_talk: bool | None = None
    duplicate_message_percent: float | None = None

    # Discord (Part 5 Section 8)
    discord_members: int | None = None
    discord_active_percent: float | None = None

    # Reddit (Part 5 Section 9)
    reddit_subscribers: int | None = None
    reddit_posts_per_day: float | None = None

    # Loyalty & creativity signals (Part 5 Sections 10-11)
    member_retention_30d_percent: float | None = None
    positive_sentiment_percent: float | None = None
    user_content_per_day: float | None = None

    # Developer relationship (Part 5 Section 5)
    dev_updates_per_week: float | None = None
    dev_responds_to_community: bool | None = None
    dev_appears_only_on_pumps: bool | None = None


@dataclass(frozen=True)
class OnChainProfile:
    """Normalized on-chain behavior metrics for one token (Spec Part 6).

    Today this is partially derivable from market + security collectors
    (holders, concentration, trade counts, unique traders). Smart-money,
    whale-movement, and exchange-flow fields are populated once the wallet
    intelligence collectors (Helius/Birdeye, Part 17) land — until then
    they stay ``None`` and their sub-scores honestly report "no data".
    """

    token: TokenIdentity
    source: str

    # Holder structure & growth (Part 6 Sections 2-3)
    holder_count: int | None = None
    holder_count_24h_ago: int | None = None
    top_holder_percent: float | None = None
    top10_holder_percent: float | None = None

    # Developer wallet (Part 6 Section 5)
    creator_percent: float | None = None
    owner_percent: float | None = None

    # Trading behavior (Part 6 Sections 11-12)
    buys_24h: int | None = None
    sells_24h: int | None = None
    unique_buyers_24h: int | None = None
    unique_sellers_24h: int | None = None
    volume_24h_usd: float | None = None
    liquidity_usd: float | None = None
    price_change_24h_percent: float | None = None

    # Wallet intelligence (Part 6 Sections 6-10; needs Part 17 collectors)
    smart_wallet_count: int | None = None
    smart_wallet_net_flow_usd: float | None = None
    whale_net_flow_usd: float | None = None
    exchange_inflow_usd: float | None = None
    exchange_outflow_usd: float | None = None


@dataclass(frozen=True)
class WalletHolding:
    """One holder's position in a token (Spec Part 17, Section 6)."""

    owner: str                    # wallet (owner) address
    percent: float                # share of supply, 0-100
    ui_amount: float | None = None


@dataclass(frozen=True)
class TokenTrade:
    """One recent trade in a token's market (Spec Part 17, Sections 4-5)."""

    owner: str                    # trading wallet
    side: str                     # "buy" or "sell"
    volume_usd: float | None
    timestamp: datetime | None
    price_usd: float | None = None


@dataclass(frozen=True)
class TokenTransfer:
    """One token transfer (Spec Part 17, Section 10 — exchange flow)."""

    from_owner: str | None
    to_owner: str | None
    ui_amount: float | None
    timestamp: datetime | None


@dataclass(frozen=True)
class LiquidityProbeResult:
    """Live buy-then-sell round-trip test via Jupiter's swap router (Project 1).

    Independent of GoPlus's static contract analysis (Rule 9 — multi-source):
    this asks the router for a real quote rather than reading the contract's
    stated logic. ``live_sell_route_found`` and ``live_round_trip_loss_percent``
    are only meaningful once a buy route was found — they stay ``None`` (not
    applicable, not "unknown") when the buy leg itself found no route, which
    commonly just means Jupiter hasn't indexed a very new pool yet.
    """

    token: TokenIdentity
    source: str
    live_buy_route_found: bool | None = None
    live_sell_route_found: bool | None = None
    live_round_trip_loss_percent: float | None = None


@dataclass(frozen=True)
class WalletIntelData:
    """Normalized wallet-level facts for one token, combined from the
    wallet collectors (Helius + Birdeye). ``None``/empty means the source
    did not report it — never assume zero activity (Rule 8)."""

    token: TokenIdentity
    sources: tuple[str, ...]
    top_holders: tuple[WalletHolding, ...] = ()
    recent_trades: tuple[TokenTrade, ...] = ()
    recent_transfers: tuple[TokenTransfer, ...] = ()
    holder_count: int | None = None
    unique_wallets_24h: int | None = None


@dataclass(frozen=True)
class CategoryScores:
    """Per-category scores on a 0-100 scale; ``None`` means "not yet analyzed".

    Field names match :class:`~meme_intelligence.config.settings.ScoringWeights`
    one-to-one so the two can be zipped mechanically.
    """

    foundation: float | None = None
    security: float | None = None
    community: float | None = None
    blockchain: float | None = None
    momentum: float | None = None
    narrative: float | None = None
    timing: float | None = None

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if value is not None and not (0.0 <= value <= 100.0):
                raise ValueError(f"category score '{name}' must be within 0-100, got {value}")


@dataclass(frozen=True)
class WeightedScoreResult:
    """Outcome of combining category scores with the framework weights.

    ``coverage`` is the fraction of total weight backed by real data (1.0 =
    every category was scored). Low coverage should lower confidence, not
    silently produce an authoritative-looking number.
    """

    total: float
    coverage: float
    missing: tuple[str, ...]


def compute_weighted_score(scores: CategoryScores, weights: ScoringWeights) -> WeightedScoreResult:
    """Combine category scores using the framework weights (Part 31, Section 4).

    Categories without data are excluded and the remaining weights are
    renormalized, so a partially-analyzed token still gets a comparable
    0-100 total — with ``coverage`` exposing how partial it was.

    Raises :class:`InsufficientDataError` when no category has been scored.
    """
    score_map = dataclasses.asdict(scores)
    weight_map = dataclasses.asdict(weights)

    available_weight = 0.0
    weighted_sum = 0.0
    missing: list[str] = []
    for name, weight in weight_map.items():
        value = score_map[name]
        if value is None:
            missing.append(name)
        else:
            available_weight += weight
            weighted_sum += value * weight

    if available_weight == 0.0:
        raise InsufficientDataError("cannot compute a weighted score: no category has been scored")

    return WeightedScoreResult(
        total=weighted_sum / available_weight,
        coverage=available_weight,
        missing=tuple(missing),
    )


def classify(total_score: float, bands: ClassificationBands) -> Classification:
    """Map a final 0-100 score onto its classification band (Part 20, Section 4).

    This is pure banding. Red-flag overrides (confirmed honeypot, removable
    liquidity, ...) are applied by the scoring engine before this is called
    and force :attr:`Classification.AVOID` regardless of score (Part 10, Section 5).
    """
    if not (0.0 <= total_score <= 100.0):
        raise ValueError(f"total score must be within 0-100, got {total_score}")
    if total_score >= bands.elite:
        return Classification.ELITE_OPPORTUNITY
    if total_score >= bands.strong_candidate:
        return Classification.STRONG_CANDIDATE
    if total_score >= bands.watchlist:
        return Classification.WATCHLIST
    if total_score >= bands.speculative:
        return Classification.SPECULATIVE
    return Classification.AVOID
