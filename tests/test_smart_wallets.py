"""Tests for the smart-wallet data clock (Part 17 groundwork)."""

from meme_intelligence.config.settings import SmartWalletSettings
from meme_intelligence.core.models import SecurityProfile, TokenIdentity, TopHolder
from meme_intelligence.workflow.smart_wallets import SmartWalletRecorder


def make_profile(address="MintAAA", holders=(("W1", 12.0), ("W2", 5.0))) -> SecurityProfile:
    return SecurityProfile(
        token=TokenIdentity(chain="solana", address=address),
        source="goplus",
        top_holders=tuple(TopHolder(address=a, percent=p) for a, p in holders),
    )


class FakeStorage:
    def __init__(self):
        self.calls = []

    def record_wallet_sightings(self, token, sightings, *, source=None):
        self.calls.append((token, list(sightings), source))
        return len(sightings)


def make_recorder(storage, **overrides) -> SmartWalletRecorder:
    settings = SmartWalletSettings(enabled=True, **overrides)
    return SmartWalletRecorder(storage, settings)


def test_records_top_holders_with_source_and_percent():
    storage = FakeStorage()
    recorder = make_recorder(storage)
    written = recorder.record(make_profile())
    assert written == 2
    token, sightings, source = storage.calls[0]
    assert token.address == "MintAAA"
    assert source == "goplus_holders"
    assert sightings == [("W1", "hold_top10", None, 12.0),
                         ("W2", "hold_top10", None, 5.0)]


def test_records_each_token_once():
    """Rechecks re-fetch the same holder list; only the FIRST (earliest)
    snapshot carries reputation signal, so repeats must not add rows."""
    storage = FakeStorage()
    recorder = make_recorder(storage)
    assert recorder.record(make_profile()) == 2
    assert recorder.record(make_profile()) == 0
    assert len(storage.calls) == 1


def test_disabled_records_nothing():
    storage = FakeStorage()
    recorder = SmartWalletRecorder(storage, SmartWalletSettings())  # enabled=False
    assert recorder.record(make_profile()) == 0
    assert storage.calls == []


def test_respects_max_holders_cap():
    storage = FakeStorage()
    recorder = make_recorder(storage, max_holders_per_token=1)
    assert recorder.record(make_profile()) == 1
    _, sightings, _ = storage.calls[0]
    assert [s[0] for s in sightings] == ["W1"]  # largest holder kept


def test_empty_holder_list_leaves_token_unmarked():
    """No holder data is a gap (Rule 8): a later recheck WITH data must
    still get to record — the token is not burned in the dedup set."""
    storage = FakeStorage()
    recorder = make_recorder(storage)
    assert recorder.record(make_profile(holders=())) == 0
    assert storage.calls == []
    assert recorder.record(make_profile()) == 2   # data arrived on recheck


def test_none_profile_is_a_noop():
    storage = FakeStorage()
    recorder = make_recorder(storage)
    assert recorder.record(None) == 0
    assert storage.calls == []


def test_storage_failure_never_raises_and_allows_retry():
    """Recording must never disturb the scan cycle (Rule 7) — and a failed
    write must not mark the token as recorded, so the next recheck retries."""
    class BoomStorage:
        def __init__(self):
            self.attempts = 0

        def record_wallet_sightings(self, token, sightings, *, source=None):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("database is locked")
            return len(sightings)

    storage = BoomStorage()
    recorder = make_recorder(storage)
    assert recorder.record(make_profile()) == 0   # swallowed, not raised
    assert recorder.record(make_profile()) == 2   # retried on recheck
