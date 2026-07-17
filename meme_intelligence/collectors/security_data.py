"""Security data collectors (Spec Part 15 Section 3, Part 32 Category 4).

First provider: **GoPlus Security** — free contract-security API covering
EVM chains and Solana. Responses are normalized into
:class:`~meme_intelligence.core.models.SecurityProfile` so the security
analyzer never sees provider-specific shapes. Additional providers
(Token Sniffer, honeypot checkers) can join behind the same interface and
be combined through the provider pool (Rule 9 — multi-source).

GoPlus quirks handled here:

* Booleans arrive as strings ``"1"`` / ``"0"``; empty string means unknown.
* Percentages arrive as fractions of 1 (``"0.05"`` == 5%).
* EVM and Solana use different endpoints AND different field layouts.
"""

from __future__ import annotations

from typing import Any

from meme_intelligence.collectors.base import BaseCollector
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import SecurityProfile, TokenIdentity

# Chain-name aliases (DexScreener and GeckoTerminal ids) -> GoPlus numeric chain id.
CHAIN_TO_GOPLUS_ID: dict[str, str] = {
    "ethereum": "1", "eth": "1",
    "bsc": "56", "bnb": "56",
    "polygon": "137", "polygon_pos": "137",
    "base": "8453",
    "arbitrum": "42161",
    "avalanche": "43114", "avax": "43114",
    "optimism": "10",
}

SOLANA_CHAINS = {"solana", "sol"}

# Addresses that mean "ownership renounced" when set as the contract owner.
_RENOUNCED_OWNERS = {
    "",
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
}

_BURN_ADDRESS_MARKERS = ("0x0000000000000000000000000000000000000000", "dead")


def _flag(value: Any) -> bool | None:
    """Parse a GoPlus boolean: "1"/"0" strings (or ints); empty/missing -> unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip() == "1"


def _fraction_to_percent(value: Any) -> float | None:
    """Parse a GoPlus fraction-of-1 string into a 0-100 percentage."""
    if value is None or value == "":
        return None
    try:
        return float(value) * 100.0
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_burn_address(address: str) -> bool:
    lowered = address.lower()
    return any(marker in lowered for marker in _BURN_ADDRESS_MARKERS)


def _holder_percents(holders: Any) -> list[float]:
    """Extract circulating-holder percentages, excluding locked and burn addresses."""
    if not isinstance(holders, list):
        return []
    percents: list[float] = []
    for holder in holders:
        if not isinstance(holder, dict):
            continue
        if _flag(holder.get("is_locked")):
            continue
        address = str(holder.get("address") or "")
        if address and _is_burn_address(address):
            continue
        percent = _fraction_to_percent(holder.get("percent"))
        if percent is not None:
            percents.append(percent)
    return sorted(percents, reverse=True)


def _lp_locked_percent(lp_holders: Any) -> float | None:
    """Percentage of LP supply that is locked or burned (higher is safer)."""
    if not isinstance(lp_holders, list) or not lp_holders:
        return None
    locked = 0.0
    seen_any = False
    for lp in lp_holders:
        if not isinstance(lp, dict):
            continue
        percent = _fraction_to_percent(lp.get("percent"))
        if percent is None:
            continue
        seen_any = True
        address = str(lp.get("address") or "")
        if _flag(lp.get("is_locked")) or (address and _is_burn_address(address)):
            locked += percent
    return locked if seen_any else None


class GoPlusClient(BaseCollector):
    """Client for the public GoPlus token-security API."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("name", "goplus")
        kwargs.setdefault("base_url", "https://api.gopluslabs.io")
        super().__init__(**kwargs)

    async def get_token_security(self, chain: str, token_address: str) -> SecurityProfile | None:
        """Fetch and normalize the security profile, or ``None`` if GoPlus has no data."""
        if not chain or not token_address:
            raise ValueError("chain and token_address must be non-empty")

        chain_key = chain.lower()
        if chain_key in SOLANA_CHAINS:
            payload = await self._get_json(
                "api/v1/solana/token_security",
                params={"contract_addresses": token_address},
                cache_key=f"goplus:solana:{token_address}",
                cache_ttl=300.0,  # contract facts change rarely (Part 32 low-priority refresh)
            )
            raw = self._extract_result(payload, token_address)
            if raw is None:
                return None
            return self._parse_solana(chain_key, token_address, raw)

        chain_id = CHAIN_TO_GOPLUS_ID.get(chain_key)
        if chain_id is None:
            raise CollectorError(f"{self.name}: unsupported chain '{chain}'")
        payload = await self._get_json(
            f"api/v1/token_security/{chain_id}",
            params={"contract_addresses": token_address},
            cache_key=f"goplus:evm:{chain_id}:{token_address.lower()}",
            cache_ttl=300.0,
        )
        raw = self._extract_result(payload, token_address)
        if raw is None:
            return None
        return self._parse_evm(chain_key, token_address, raw)

    def _extract_result(self, payload: Any, token_address: str) -> dict[str, Any] | None:
        """Unwrap GoPlus's ``{code, message, result: {address: {...}}}`` envelope."""
        if not isinstance(payload, dict):
            raise CollectorError(f"{self.name}: expected JSON object, got {type(payload).__name__}")
        try:
            code = int(payload.get("code"))
        except (TypeError, ValueError):
            code = None
        if code not in (1, 2):  # 1 = complete, 2 = partial data (still usable)
            raise CollectorError(
                f"{self.name}: API error code {payload.get('code')}: {payload.get('message')}"
            )
        result = payload.get("result")
        if not isinstance(result, dict) or not result:
            return None
        # Result keys may differ in case from the request (EVM addresses).
        for key, value in result.items():
            if key.lower() == token_address.lower():
                return value if isinstance(value, dict) else None
        return None

    @staticmethod
    def _parse_evm(chain: str, address: str, raw: dict[str, Any]) -> SecurityProfile:
        owner = raw.get("owner_address")
        ownership_renounced: bool | None = None
        if owner is not None:
            ownership_renounced = str(owner).lower() in _RENOUNCED_OWNERS

        holder_percents = _holder_percents(raw.get("holders"))
        return SecurityProfile(
            token=TokenIdentity(
                chain=chain,
                address=address,
                name=raw.get("token_name") or None,
                symbol=raw.get("token_symbol") or None,
            ),
            source="goplus",
            is_honeypot=_flag(raw.get("is_honeypot")),
            cannot_buy=_flag(raw.get("cannot_buy")),
            cannot_sell_all=_flag(raw.get("cannot_sell_all")),
            is_open_source=_flag(raw.get("is_open_source")),
            is_proxy=_flag(raw.get("is_proxy")),
            is_mintable=_flag(raw.get("is_mintable")),
            ownership_renounced=ownership_renounced,
            hidden_owner=_flag(raw.get("hidden_owner")),
            can_take_back_ownership=_flag(raw.get("can_take_back_ownership")),
            has_blacklist=_flag(raw.get("is_blacklisted")),
            trading_pausable=_flag(raw.get("transfer_pausable")),
            selfdestruct=_flag(raw.get("selfdestruct")),
            buy_tax_percent=_fraction_to_percent(raw.get("buy_tax")),
            sell_tax_percent=_fraction_to_percent(raw.get("sell_tax")),
            tax_modifiable=_flag(raw.get("slippage_modifiable")),
            fake_token=_flag(raw.get("fake_token")),
            is_airdrop_scam=_flag(raw.get("is_airdrop_scam")),
            anti_whale_modifiable=_flag(raw.get("anti_whale_modifiable")),
            slippage_modifiable=_flag(raw.get("slippage_modifiable")),
            personal_slippage_modifiable=_flag(raw.get("personal_slippage_modifiable")),
            trading_cooldown=_flag(raw.get("trading_cooldown")),
            honeypot_same_creator_count=_to_int(raw.get("honeypot_with_same_creator")),
            holder_count=_to_int(raw.get("holder_count")),
            top_holder_percent=holder_percents[0] if holder_percents else None,
            top10_holder_percent=sum(holder_percents[:10]) if holder_percents else None,
            creator_percent=_fraction_to_percent(raw.get("creator_percent")),
            owner_percent=_fraction_to_percent(raw.get("owner_percent")),
            creator_address=raw.get("creator_address") or None,
            lp_locked_percent=_lp_locked_percent(raw.get("lp_holders")),
        )

    @staticmethod
    def _parse_solana(chain: str, address: str, raw: dict[str, Any]) -> SecurityProfile:
        def authority_status(field: str) -> bool | None:
            value = raw.get(field)
            if isinstance(value, dict):
                return _flag(value.get("status"))
            return _flag(value)

        holder_percents = _holder_percents(raw.get("holders"))
        creators = raw.get("creators")
        creator_percent = None
        creator_address = None
        if isinstance(creators, list) and creators and isinstance(creators[0], dict):
            creator_percent = _fraction_to_percent(creators[0].get("percent"))
            creator_address = creators[0].get("address") or None

        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        return SecurityProfile(
            token=TokenIdentity(
                chain=chain,
                address=address,
                name=(metadata or {}).get("name") or None,
                symbol=(metadata or {}).get("symbol") or None,
            ),
            source="goplus",
            # SPL tokens marked non-transferable cannot be sold — honeypot equivalent.
            cannot_sell_all=_flag(raw.get("non_transferable")),
            is_mintable=authority_status("mintable"),
            is_freezable=authority_status("freezable"),
            selfdestruct=authority_status("closable"),
            balance_mutable=authority_status("balance_mutable_authority"),
            tax_modifiable=_flag(raw.get("transfer_fee_upgradable")),
            holder_count=_to_int(raw.get("holder_count")),
            top_holder_percent=holder_percents[0] if holder_percents else None,
            top10_holder_percent=sum(holder_percents[:10]) if holder_percents else None,
            creator_percent=creator_percent,
            creator_address=creator_address,
            lp_locked_percent=_lp_locked_percent(raw.get("lp_holders")),
        )
