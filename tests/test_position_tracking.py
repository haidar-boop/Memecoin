"""A live buy must register the position so the rug watch can guard it.

Kept out of ``test_execution.py`` on purpose: that module's tests build real
Solana keypairs and need ``solders``, which is not installed everywhere. The
executor itself imports ``solders`` lazily (inside ``_sign``), so the position
bookkeeping — a money-path behaviour — stays testable in every environment.
"""

import logging

from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.database.storage import Storage
from meme_intelligence.trading.execution import LiveExecutor

MINT = "So1MemeToken111111111111111111111111111111"


def make_executor(storage) -> LiveExecutor:
    """A LiveExecutor with only the bits _remember_position touches."""
    executor = LiveExecutor.__new__(LiveExecutor)
    executor._storage = storage
    executor._logger = logging.getLogger("test.execution")
    return executor


def test_a_broadcast_buy_registers_the_position(tmp_path):
    """set_holding was previously only ever called by /holding, so a coin
    bought through /buy or an alert's Buy button was never marked as held —
    and the rug watch, the interest gate and protective-alert priority all key
    off holdings. The bot could buy a coin and then never guard it."""
    with Storage(str(tmp_path / "s.sqlite3")) as storage:
        make_executor(storage)._remember_position(MINT, held=True)
        assert storage.is_holding(TokenIdentity(chain="solana", address=MINT))


def test_a_confirmed_sell_releases_the_position(tmp_path):
    with Storage(str(tmp_path / "s.sqlite3")) as storage:
        executor = make_executor(storage)
        executor._remember_position(MINT, held=True)
        executor._remember_position(MINT, held=False)
        assert not storage.is_holding(TokenIdentity(chain="solana", address=MINT))


def test_registering_twice_is_harmless(tmp_path):
    """Two buys of the same coin must not create a second live holding."""
    with Storage(str(tmp_path / "s.sqlite3")) as storage:
        executor = make_executor(storage)
        executor._remember_position(MINT, held=True)
        executor._remember_position(MINT, held=True)
        active = [h for h in storage.get_holdings(active_only=True)
                  if h["address"] == MINT]
        assert len(active) == 1


def test_a_storage_failure_never_breaks_a_completed_trade(tmp_path):
    """Rule 7: the trade already happened on-chain. A bookkeeping hiccup must
    not turn it into an exception the operator sees instead of his fill."""
    class BrokenStorage:
        def set_holding(self, token, note=None):
            raise RuntimeError("database is locked")

        def release_holding(self, token):
            raise RuntimeError("database is locked")

    executor = make_executor(BrokenStorage())
    executor._remember_position(MINT, held=True)     # must not raise
    executor._remember_position(MINT, held=False)    # must not raise
