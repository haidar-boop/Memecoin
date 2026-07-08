"""Tests for the SQLite persistence layer (Spec Parts 11/13/21)."""

from datetime import datetime, timezone

import pytest

from meme_intelligence.analyzers.scoring_engine import MasterAssessment
from meme_intelligence.core.enums import Classification, ConfidenceLevel, WatchlistTier
from meme_intelligence.core.models import CategoryScores, TokenIdentity
from meme_intelligence.database.storage import Storage

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", symbol="MEME", name="Test Meme")


@pytest.fixture
def storage():
    with Storage(":memory:", now_func=lambda: NOW) as s:
        yield s


def make_master(score=75.0, classification=Classification.WATCHLIST) -> MasterAssessment:
    return MasterAssessment(
        token=TOKEN,
        generated_at=NOW,
        category_scores=CategoryScores(security=80, blockchain=70),
        final_score=score,
        coverage=0.30,
        classification=classification,
        overrides=(),
        decision_trace=(),
        confidence=ConfidenceLevel.MEDIUM,
    )


def test_upsert_token_is_idempotent(storage):
    first = storage.upsert_token(TOKEN)
    second = storage.upsert_token(TOKEN)
    assert first == second


def test_upsert_preserves_symbol_when_update_lacks_it(storage):
    storage.upsert_token(TOKEN)
    storage.upsert_token(TokenIdentity(chain="solana", address="TokenAddr1"))
    entries = storage.get_watchlist(include_archived=True)  # no watchlist yet
    history = storage.score_history(TOKEN)
    assert entries == [] and history == []  # just verifying no crash; symbol check below
    storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING)
    assert storage.get_watchlist()[0].token.symbol == "MEME"


def test_snapshot_roundtrip(storage):
    storage.record_snapshot(make_master(), source="test")
    history = storage.score_history(TOKEN)
    assert len(history) == 1
    assert history[0]["final_score"] == 75.0
    assert history[0]["classification"] == "watchlist"
    assert history[0]["source"] == "test"


def test_watchlist_add_update_tier_change(storage):
    added = storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING,
                                     score=72.0, classification=Classification.WATCHLIST)
    assert added.change == "added"

    refreshed = storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING, score=74.0)
    assert refreshed.change == "updated"

    promoted = storage.update_watchlist(TOKEN, WatchlistTier.TIER_1_HIGH_PRIORITY,
                                        score=85.0, classification=Classification.STRONG_CANDIDATE)
    assert promoted.change == "tier_changed"
    assert "tier_2_developing -> tier_1_high_priority" in promoted.detail

    entries = storage.get_watchlist()
    assert len(entries) == 1
    assert entries[0].tier is WatchlistTier.TIER_1_HIGH_PRIORITY
    assert entries[0].last_score == 85.0


def test_archive_excluded_from_default_watchlist(storage):
    storage.update_watchlist(TOKEN, WatchlistTier.TIER_3_RESEARCH_ONLY)
    storage.archive(TOKEN, "score collapsed")
    assert storage.get_watchlist() == []
    archived = storage.get_watchlist(include_archived=True)
    assert len(archived) == 1
    assert archived[0].tier is WatchlistTier.ARCHIVED
    entries = storage.journal_entries(TOKEN)
    assert any("archived" in e["content"] for e in entries)


def test_archive_preserves_last_known_score(storage):
    """Regression: archival (an update without a score) must not erase history."""
    storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING,
                             score=72.0, classification=Classification.WATCHLIST)
    storage.archive(TOKEN, "deteriorated")
    entry = storage.get_watchlist(include_archived=True)[0]
    assert entry.last_score == 72.0
    assert entry.last_classification == "watchlist"


def test_journal_roundtrip(storage):
    storage.add_journal(TOKEN, "thesis", "strong meme, early community")
    storage.add_journal(None, "daily_report", "market neutral today")
    token_entries = storage.journal_entries(TOKEN)
    assert len(token_entries) == 1
    assert token_entries[0]["kind"] == "thesis"
    all_entries = storage.journal_entries()
    assert len(all_entries) == 2


def test_file_backed_storage_uses_wal_and_tolerates_second_writer(tmp_path):
    """The 24/7 monitor and scheduled jobs (daily, backtest --refresh) share
    the database file; WAL + busy timeout let them coexist (Rule 7)."""
    path = str(tmp_path / "shared.sqlite3")
    with Storage(path, now_func=lambda: NOW) as first:
        mode = first._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        # A second process-style connection writes while the first is open.
        with Storage(path, now_func=lambda: NOW) as second:
            second.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING,
                                    score=70.0, classification=Classification.WATCHLIST)
            first.add_journal(TOKEN, "thesis", "written by the other connection's peer")
        assert first.get_watchlist()  # sees the other writer's row
