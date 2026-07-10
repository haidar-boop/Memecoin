"""Trade-execution scaffold (Project 2) — DRY RUN ONLY, deliberately incomplete.

THIS MODULE NEVER TRADES. It exists so the buy-from-Telegram button has a
place to route to, and it stops at recording the operator's intent:

* NO wallet keys are read, stored, or expected anywhere in this codebase.
* NO transaction is built, signed, or sent; there are NO network calls here.
* The only side effect of a "buy" is a journal entry (``trade_intent``) so
  the intent is on the record for later evaluation (Project 3).

Why the real executor is deliberately absent: the system's standing
doctrine is decision-support / never-AUTO-trades. A button pressed by the
operator is a manual decision — the doctrine stands — but actually
executing it would require holding a hot wallet private key on the
DigitalOcean droplet, which is a key-custody security decision the owner
must make explicitly and has not made. Until that conversation happens,
``DryRunExecutor`` is the only executor, and even setting
``MEMEINTEL_EXECUTION_DRY_RUN=false`` changes nothing because there is no
live code path to fall through to. The same decision is recorded in
handoff/DECISIONS_LOG.md (2026-07-10).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity


@dataclass(frozen=True)
class TradeIntent:
    """One operator-initiated buy intent (never an order)."""

    token_address: str
    chain: str
    sol_amount: float
    requested_at: datetime
    source: str = "telegram"


class DryRunExecutor:
    """Records buy intents and answers honestly that nothing was executed.

    ``execute_buy`` journals the intent via storage (kind ``trade_intent``)
    and returns a human-readable message for the operator's phone. Any
    storage failure is logged and swallowed — a journaling hiccup must not
    turn a no-op dry run into an error in the operator's face (Rule 7);
    the returned message says whether the intent was recorded.
    """

    def __init__(self, storage) -> None:
        self._storage = storage
        self._logger = get_logger("trading.execution")

    async def execute_buy(self, intent: TradeIntent) -> str:
        message = (
            f"DRY RUN — no real trade executed. Would buy {intent.sol_amount:g} SOL "
            f"of {intent.token_address} on {intent.chain}. Live execution is not "
            "built yet (pending an explicit key-custody decision by the owner)."
        )
        try:
            token = TokenIdentity(chain=intent.chain, address=intent.token_address)
            self._storage.add_journal(
                token, "trade_intent",
                f"dry-run buy intent via {intent.source}: {intent.sol_amount:g} SOL "
                f"at {intent.requested_at.isoformat()}",
            )
            self._logger.info("dry-run buy intent recorded for %s (%s SOL)",
                              intent.token_address, intent.sol_amount)
            message += " Intent journaled."
        except Exception as exc:  # noqa: BLE001 — journaling must not break the reply
            self._logger.warning("failed to journal trade intent for %s: %s",
                                 intent.token_address, exc)
            message += " (Journaling the intent failed; see logs.)"
        return message
