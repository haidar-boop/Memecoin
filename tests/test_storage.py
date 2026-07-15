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


def test_top_opportunities_ranks_by_latest_opportunity_rank(storage):
    """Part 28 S5/S6: active watchlist tokens ordered by their latest
    opportunity rank; archived excluded; NULL-rank tokens sort last."""
    import dataclasses as _dc
    from meme_intelligence.core.models import TokenIdentity
    a = TokenIdentity(chain="solana", address="TokA", symbol="AAA")
    b = TokenIdentity(chain="solana", address="TokB", symbol="BBB")
    c = TokenIdentity(chain="solana", address="TokC", symbol="CCC")
    dead = TokenIdentity(chain="solana", address="TokDead", symbol="DED")
    for tok in (a, b, c, dead):
        storage.update_watchlist(tok, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
    storage.record_snapshot(_dc.replace(make_master(), token=a),
                            source="t", opportunity_rank=55.0)
    storage.record_snapshot(_dc.replace(make_master(), token=b),
                            source="t", opportunity_rank=88.0)
    # c gets no opportunity_rank (NULL) -> sorts last but not dropped
    storage.record_snapshot(_dc.replace(make_master(), token=c), source="t")
    storage.archive(dead, "gone")  # archived must be excluded

    ranked = storage.top_opportunities(limit=10)
    addresses = [r["address"] for r in ranked]
    assert "TokDead" not in addresses            # archived excluded
    assert addresses[0] == "TokB"                # highest rank first (88)
    assert addresses[1] == "TokA"                # then 55
    assert addresses[-1] == "TokC"               # NULL rank sorts last, still present


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


def test_update_watchlist_upsert_atomic_no_race_window(storage):
    """Bug-hunt: a separate SELECT-then-INSERT/UPDATE had a race window
    between two processes sharing this database — both could see 'no
    existing row' for a brand-new token and both attempt INSERT, the
    second raising IntegrityError on the token_id primary key. The
    single UPSERT statement can no longer race."""
    change1 = storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
    assert change1.change == "added"
    # Simulate what a second concurrent process's call would do: the same
    # INSERT...ON CONFLICT statement runs again for the same token_id.
    change2 = storage.update_watchlist(TOKEN, WatchlistTier.TIER_1_HIGH_PRIORITY, score=85.0)
    assert change2.change == "tier_changed"
    entries = storage.get_watchlist()
    assert len(entries) == 1
    assert entries[0].tier is WatchlistTier.TIER_1_HIGH_PRIORITY
    assert entries[0].last_score == 85.0


def test_get_watchlist_archived_sorts_after_active_tiers(storage):
    """Bug-hunt: ORDER BY w.tier sorted the raw TEXT alphabetically,
    putting 'archived' ('a'...) before 'tier_1_high_priority' ('t'...) —
    with include_archived=True, dead tokens outranked the highest
    priority ones."""
    token2 = TokenIdentity(chain="solana", address="TokenAddr2", symbol="TWO")
    storage.update_watchlist(TOKEN, WatchlistTier.TIER_1_HIGH_PRIORITY, score=90.0)
    storage.archive(token2, "dead")
    entries = storage.get_watchlist(include_archived=True)
    tiers = [e.tier for e in entries]
    assert tiers.index(WatchlistTier.TIER_1_HIGH_PRIORITY) < tiers.index(WatchlistTier.ARCHIVED)


class _RacingConnProxy:
    """Forwards to a real sqlite3.Connection except for one intercepted
    method — sqlite3.Connection.execute is a read-only C attribute and
    cannot be monkeypatched directly."""

    def __init__(self, real_conn, alter_error):
        self._real = real_conn
        self._alter_error = alter_error

    def execute(self, sql, *args):
        if sql.strip().startswith("PRAGMA table_info"):
            return self._real.execute("PRAGMA table_info(_nonexistent_probe_)")
        if sql.strip().startswith("ALTER TABLE"):
            raise self._alter_error
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_migrate_tolerates_concurrent_duplicate_column(storage, monkeypatch):
    """Bug-hunt: two processes starting up against a pre-upgrade database
    at once could both see a migration column missing (PRAGMA check) and
    both attempt ALTER TABLE; the loser's 'duplicate column name' crashed
    Storage.__init__ entirely instead of being treated as already-
    migrated by the winner (Rule 7). Forces the exact race: the PRAGMA
    check reports the column missing (as it would for a genuinely stale
    process) while the ALTER TABLE itself fails as a concurrent winner
    already added it."""
    import sqlite3 as _sqlite3
    monkeypatch.setattr(storage, "_conn", _RacingConnProxy(
        storage._conn, _sqlite3.OperationalError("duplicate column name: regime")))
    storage._migrate()  # must not raise


def test_migrate_reraises_unrelated_operational_errors(storage, monkeypatch):
    """The duplicate-column tolerance must not swallow a genuinely
    different, real database error."""
    import sqlite3 as _sqlite3
    monkeypatch.setattr(storage, "_conn", _RacingConnProxy(
        storage._conn, _sqlite3.OperationalError("database is locked")))
    with pytest.raises(_sqlite3.OperationalError, match="database is locked"):
        storage._migrate()


# ---- Project 2: holdings, mute, operator feedback ----

HOLD_TOKEN = TokenIdentity(chain="solana", address="HoldMe111111111111111111111111111111111111",
                           symbol="HODL")


def test_holdings_lifecycle(storage):
    assert not storage.is_holding(HOLD_TOKEN)
    assert storage.set_holding(HOLD_TOKEN) is True
    assert storage.set_holding(HOLD_TOKEN) is False   # idempotent
    assert storage.is_holding(HOLD_TOKEN)
    rows = storage.get_holdings(active_only=True)
    assert len(rows) == 1 and rows[0]["symbol"] == "HODL"
    assert storage.release_holding(HOLD_TOKEN) is True
    assert storage.release_holding(HOLD_TOKEN) is False
    assert not storage.is_holding(HOLD_TOKEN)
    assert storage.get_holdings(active_only=True) == []
    assert len(storage.get_holdings(active_only=False)) == 1  # history retained


def test_mute_idempotency(storage):
    assert storage.mute_token(HOLD_TOKEN) is True
    assert storage.mute_token(HOLD_TOKEN) is False
    assert storage.is_muted(HOLD_TOKEN)
    assert len(storage.muted_list()) == 1
    assert storage.unmute_token(HOLD_TOKEN) is True
    assert storage.unmute_token(HOLD_TOKEN) is False
    assert not storage.is_muted(HOLD_TOKEN)


def test_feedback_summary_and_validation(storage):
    storage.record_feedback(HOLD_TOKEN, "up")
    storage.record_feedback(HOLD_TOKEN, "up", alert_id=None)
    storage.record_feedback(HOLD_TOKEN, "down")
    assert storage.feedback_summary() == {"up": 2, "down": 1}
    rows = storage.feedback_for_token(HOLD_TOKEN)
    assert len(rows) == 3 and rows[0]["verdict"] == "down"
    with pytest.raises(ValueError):
        storage.record_feedback(HOLD_TOKEN, "sideways")


def test_find_token_matches_exact_then_case_insensitive_evm(storage):
    sol = TokenIdentity(chain="solana", address="CaseSensitive11111111111111111111111111111",
                        symbol="CS")
    evm = TokenIdentity(chain="ethereum", address="0x" + "AB" * 20, symbol="EV")
    storage.upsert_token(sol)
    storage.upsert_token(evm)
    assert storage.find_token(sol.address).symbol == "CS"
    assert storage.find_token(sol.address.lower()) is None     # base58 is case-sensitive
    assert storage.find_token(("0x" + "ab" * 20)).symbol == "EV"  # EVM is not
    assert storage.find_token("Unknown11111111111111111111111111111111111") is None


def test_table_counts_includes_new_tables(storage):
    storage.set_holding(HOLD_TOKEN)
    counts = storage.table_counts()
    assert counts["holdings"] == 1
    assert set(counts) >= {"tokens", "alerts", "watchlist", "holdings"}


def test_mute_and_hold_match_by_address_across_chains(storage):
    """Project 2 fix: a mute/hold filed under a guessed chain must still apply
    when the scanner sees the token on its real chain (unscanned EVM address
    can't have its chain inferred reliably)."""
    guessed = TokenIdentity(chain="ethereum", address="0x" + "cd" * 20, symbol="X")
    real = TokenIdentity(chain="base", address="0x" + "CD" * 20, symbol="X")  # same addr, real chain
    storage.mute_token(guessed)
    assert storage.is_muted(real) is True          # honored despite chain + case mismatch
    storage.set_holding(guessed)
    assert storage.is_holding(real) is True
    # A different address is unaffected.
    other = TokenIdentity(chain="base", address="0x" + "ef" * 20, symbol="Y")
    assert storage.is_muted(other) is False and storage.is_holding(other) is False


# ---- Wallet sightings (Part 17: the smart-wallet data clock) ----

def test_wallet_sightings_roundtrip_with_source_and_percent(storage):
    written = storage.record_wallet_sightings(
        TOKEN,
        [("WalletA", "hold_top10", None, 12.5), ("WalletB", "hold_top10", None, 3.0)],
        source="goplus_holders",
    )
    assert written == 2
    history = storage.wallet_history("WalletA")
    assert len(history) == 1
    assert history[0]["address"] == TOKEN.address
    assert history[0]["side"] == "hold_top10"
    assert history[0]["source"] == "goplus_holders"
    assert history[0]["percent"] == 12.5
    assert history[0]["usd_value"] is None
    assert set(storage.wallets_seen_on(TOKEN)) == {"WalletA", "WalletB"}


def test_wallet_sightings_legacy_three_tuples_still_accepted(storage):
    """Rule 18: the pre-existing (wallet, side, usd_value) shape keeps working."""
    written = storage.record_wallet_sightings(TOKEN, [("WalletC", "buy", 150.0)])
    assert written == 1
    history = storage.wallet_history("WalletC")
    assert history[0]["usd_value"] == 150.0
    assert history[0]["source"] is None
    assert history[0]["percent"] is None


def test_wallet_sightings_append_only_no_dedup(storage):
    """Documented contract: this table never dedups — callers dedup upstream
    and reputation queries aggregate with DISTINCT."""
    storage.record_wallet_sightings(TOKEN, [("WalletD", "hold_top10", None, 9.0)],
                                    source="goplus_holders")
    storage.record_wallet_sightings(TOKEN, [("WalletD", "hold_top10", None, 9.0)],
                                    source="goplus_holders")
    assert len(storage.wallet_history("WalletD")) == 2
    assert storage.wallets_seen_on(TOKEN) == ["WalletD"]  # DISTINCT collapses


def test_wallet_sightings_columns_exist_on_fresh_database(storage):
    """Fresh schema carries the new columns directly."""
    columns = {row["name"] for row in
               storage._conn.execute("PRAGMA table_info(wallet_sightings)")}
    assert {"source", "percent"} <= columns


def test_old_database_migrates_wallet_sighting_columns(tmp_path):
    """The droplet's EXISTING database must gain source/percent via
    _MIGRATIONS: CREATE TABLE IF NOT EXISTS is a no-op there, and the
    rewritten INSERT names the new columns unconditionally — without the
    migration every sighting write (including the pre-existing report CLI
    caller) dies with 'no column named source' (Rule 3/18). Mirrors
    test_backtesting.test_old_database_migrates_new_columns."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tokens (
            id INTEGER PRIMARY KEY, chain TEXT NOT NULL, address TEXT NOT NULL,
            symbol TEXT, name TEXT, first_seen TEXT NOT NULL, UNIQUE(chain, address));
        CREATE TABLE wallet_sightings (
            id INTEGER PRIMARY KEY, wallet TEXT NOT NULL, chain TEXT NOT NULL,
            token_id INTEGER NOT NULL REFERENCES tokens(id),
            side TEXT NOT NULL, usd_value REAL, seen_at TEXT NOT NULL);
    """)
    conn.execute("INSERT INTO tokens (chain, address, first_seen) "
                 "VALUES ('solana', 'TokenAddr1', '2026-01-01')")
    conn.execute("INSERT INTO wallet_sightings "
                 "(wallet, chain, token_id, side, usd_value, seen_at) "
                 "VALUES ('LegacyWallet', 'solana', 1, 'buy', 10.0, '2026-01-01')")
    conn.commit()
    conn.close()

    with Storage(str(path), now_func=lambda: NOW) as migrated:
        # New-style write works on the migrated table...
        migrated.record_wallet_sightings(
            TOKEN, [("NewWallet", "hold_top10", None, 7.5)], source="goplus_holders")
        fresh = migrated.wallet_history("NewWallet")
        assert fresh[0]["source"] == "goplus_holders"
        assert fresh[0]["percent"] == 7.5
        # ...and pre-migration rows survive with honest NULLs.
        legacy = migrated.wallet_history("LegacyWallet")
        assert legacy[0]["usd_value"] == 10.0
        assert legacy[0]["source"] is None and legacy[0]["percent"] is None


def test_wallet_sighting_stats_groups_by_source(storage):
    other = TokenIdentity(chain="solana", address="TokenAddr2", symbol="OTH")
    storage.record_wallet_sightings(
        TOKEN, [("W1", "hold_top10", None, 10.0), ("W2", "hold_top10", None, 5.0)],
        source="goplus_holders")
    storage.record_wallet_sightings(other, [("W1", "buy", 50.0)])  # no source
    stats = {row["source"]: row for row in storage.wallet_sighting_stats()}
    assert stats["goplus_holders"]["sightings"] == 2
    assert stats["goplus_holders"]["wallets"] == 2
    assert stats["goplus_holders"]["tokens"] == 1
    assert stats["unlabeled"]["sightings"] == 1


def test_wallet_sighting_stats_empty_table(storage):
    assert storage.wallet_sighting_stats() == []


def test_peak_score_is_all_time_high(storage):
    """Feeds peak-decline suppression: must be the MAX over ALL snapshots,
    not the most recent one — the whole point is catching a coin far below
    its former best (operator complaint 2026-07-14)."""
    assert storage.peak_score(TOKEN) is None          # no history yet
    for score in (90.0, 60.0, 63.0, 66.0):            # collapse then slow creep
        storage.record_snapshot(make_master(score=score), source="test")
    assert storage.peak_score(TOKEN) == 90.0


def test_resurrection_from_archive_restarts_the_tracking_clock():
    """Re-review finding 2026-07-14: the UPSERT kept the ORIGINAL added_at
    when a coin came back from 'archived', so the staleness door instantly
    re-archived every genuinely revived coin forever. Re-entry must open a
    fresh tracking window; a live entry's added_at stays untouched."""
    from datetime import timedelta
    clock = {"now": NOW}
    with Storage(":memory:", now_func=lambda: clock["now"]) as storage:
        storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING, score=70.0)
        original = storage.get_watchlist()[0].added_at
        # A later refresh of a LIVE entry keeps the original added_at.
        clock["now"] = NOW + timedelta(days=1)
        storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING, score=71.0)
        assert storage.get_watchlist()[0].added_at == original
        # Archive, then resurrect 4 days later: added_at must be the
        # resurrection time, not day 0.
        storage.archive(TOKEN, "watchlist staleness door: tracked 4d")
        clock["now"] = NOW + timedelta(days=4)
        storage.update_watchlist(TOKEN, WatchlistTier.TIER_2_DEVELOPING, score=75.0)
        entry = storage.get_watchlist()[0]
        assert entry.added_at == NOW + timedelta(days=4)


def test_token_first_seen_matches_evm_case_variants():
    """Same 0x fallback as find_token/is_holding: a checksummed re-analysis
    of a token first recorded lowercased must still find its first_seen —
    a case-variant miss silently disables the tracked-age half of the
    freshness gate. Solana base58 stays exact-match (case-sensitive)."""
    evm_lower = TokenIdentity(chain="ethereum", address="0xabc123def456", symbol="EVM")
    evm_checksum = TokenIdentity(chain="ethereum", address="0xAbC123dEf456", symbol="EVM")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.upsert_token(evm_lower)
        assert storage.token_first_seen(evm_lower) == NOW
        assert storage.token_first_seen(evm_checksum) == NOW   # case variant found
        # Unknown token stays None (Rule 8).
        other = TokenIdentity(chain="solana", address="NeverSeen1", symbol="NEW")
        assert storage.token_first_seen(other) is None
