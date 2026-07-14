"""Tests for wallet reputation: data-clock sightings × measured outcomes
(Part 17 Section 2 × Part 24 — the connector)."""

from datetime import datetime, timezone

import pytest

from meme_intelligence.analytics.wallet_reputation import (
    compute_wallet_reputations,
    render_reputation_report,
)
from meme_intelligence.config.settings import BacktestSettings
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.database.storage import Storage

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
SETTINGS = BacktestSettings()  # success +50%, failure -50%, survival $1000


def token(n: int) -> TokenIdentity:
    return TokenIdentity(chain="solana", address=f"Mint{n:03d}")


def seed(storage: Storage, *, wallet: str, tokens: list[TokenIdentity]) -> None:
    for tok in tokens:
        storage.record_wallet_sightings(
            tok, [(wallet, "hold_top10", None, 5.0)], source="goplus_holders")


def outcome(storage: Storage, tok: TokenIdentity, *, change: float | None,
            survived: bool | None = True, window: float = 24.0,
            at: datetime = NOW) -> None:
    """Record one measured outcome window for a token (needs a snapshot id —
    outcomes reference the prediction snapshot, so make a minimal one).
    ``at`` is the measurement time — the hindsight guard compares it against
    sighting times, so tests can measure outcomes before/after sightings."""
    token_id = storage.upsert_token(tok)
    cursor = storage._conn.execute(
        """INSERT INTO snapshots (token_id, created_at, final_score, classification,
                                  confidence, coverage, category_scores, overrides, source)
           VALUES (?, ?, 70.0, 'watchlist', 'medium', 0.5, '{}', '[]', 'test')""",
        (token_id, at.isoformat()),
    )
    storage._conn.commit()
    storage.record_outcome(
        snapshot_id=int(cursor.lastrowid), token_id=token_id, window_hours=window,
        target_at=at.isoformat(), measured_at=at.isoformat(),
        price_usd=1.0, price_change_percent=change, liquidity_usd=5000.0,
        survived=survived, source="test",
    )


@pytest.fixture
def storage():
    with Storage(":memory:", now_func=lambda: NOW) as s:
        yield s


def test_winning_wallet_scores_and_losers_drag(storage):
    """3 resolved tokens: 2 winners + 1 rug -> win_rate 2/3, rug avoidance 2/3.
    The formula runs on exactly the measurable components."""
    tokens = [token(1), token(2), token(3)]
    seed(storage, wallet="WinnerW", tokens=tokens)
    outcome(storage, tokens[0], change=120.0)               # win
    outcome(storage, tokens[1], change=80.0)                # win
    outcome(storage, tokens[2], change=-90.0, survived=False)  # death

    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    assert report.wallets_scored == 1
    entry = report.entries[0]
    assert entry.wallet == "WinnerW"
    assert entry.resolved_tokens == 3 and entry.wins == 2 and entry.deaths == 1
    # win_rate (25%): 66.7 -> weighted with rug_avoidance (20%): 66.7 and
    # consistency (15%): 45 (span 0 days, <30) = (16.7+13.3+6.75)/0.60 ≈ 61
    assert entry.score == pytest.approx(61.1, abs=0.5)
    assert entry.coverage == pytest.approx(0.60)  # timing/sizing honestly unmeasured


def test_min_resolved_gate_blocks_thin_records(storage):
    """One lucky pick is not a track record — below the gate, no score."""
    seed(storage, wallet="LuckyOnce", tokens=[token(1)])
    outcome(storage, token(1), change=500.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    assert report.wallets_scored == 0
    assert report.wallets_seen == 1
    assert "no wallet has ≥3 resolved tokens yet" in render_reputation_report(report)


def test_undetermined_tokens_count_as_pending_not_resolved(storage):
    """Sideways in the measured windows proves nothing (Rule 8): +10% hits
    neither threshold — the token is pending, not a loss against the wallet."""
    tokens = [token(1), token(2)]
    seed(storage, wallet="W", tokens=tokens)
    outcome(storage, tokens[0], change=10.0)   # measured, undetermined
    # tokens[1]: sighted, no outcomes at all
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=1)
    assert report.wallets_scored == 0
    assert report.tokens_resolved == 0
    assert report.tokens_pending == 2


def test_pump_then_rug_counts_as_both_win_and_death(storage):
    """A token that hit +60% in one window and then died carries BOTH: the
    win (an early holder could exit into it) and the death (rug avoidance
    takes the hit) — mirrors evaluate_predictions' independent thresholds."""
    tokens = [token(1), token(2), token(3)]
    seed(storage, wallet="W", tokens=tokens)
    outcome(storage, tokens[0], change=60.0, window=1.0)
    outcome(storage, tokens[0], change=-95.0, survived=False, window=168.0)
    outcome(storage, tokens[1], change=70.0)
    outcome(storage, tokens[2], change=55.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    entry = report.entries[0]
    assert entry.resolved_tokens == 3
    assert entry.wins == 3          # all three hit the success threshold
    assert entry.deaths == 1        # one of them still rugged


def test_duplicate_sightings_collapse_to_one_token(storage):
    """The append-only sightings table can hold restart duplicates; the
    reputation join must not double-count them."""
    seed(storage, wallet="W", tokens=[token(1)])
    seed(storage, wallet="W", tokens=[token(1)])  # restart re-record
    outcome(storage, token(1), change=90.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=1)
    assert report.entries[0].resolved_tokens == 1
    assert report.tokens_sighted == 1


def test_other_sighting_sources_are_not_blended(storage):
    """Reputation is per-source (Rule 9): manual CLI sightings (side 'buy',
    source NULL) must not leak into the data-clock's join."""
    storage.record_wallet_sightings(token(1), [("ManualW", "buy", 100.0)])  # no source
    outcome(storage, token(1), change=200.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=1)
    assert report.wallets_seen == 0


def test_unmeasured_dimensions_stay_none_never_fabricated(storage):
    """Holder snapshots carry no entry timing and no USD size — the entry's
    coverage must reflect only win/rug/consistency (0.60), never 1.0."""
    tokens = [token(1), token(2), token(3)]
    seed(storage, wallet="W", tokens=tokens)
    for t in tokens:
        outcome(storage, t, change=100.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    assert report.entries[0].coverage == pytest.approx(0.60)


def test_render_lists_top_wallets_with_honest_denominators(storage):
    tokens = [token(1), token(2), token(3)]
    seed(storage, wallet="GoodWallet111", tokens=tokens)
    for t in tokens:
        outcome(storage, t, change=100.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    text = render_reputation_report(report)
    assert "1 wallet(s) scored" in text
    assert "3 resolved: 3 win(s), 0 death(s)" in text
    assert "not a guarantee" in text


def test_death_with_no_price_data_still_counts_as_death(storage):
    """A rug measured only by liquidity collapse (all price changes NULL)
    must still resolve as a death — and an all-unknown token must NOT
    (unknown is not a rug, Rule 8). Pins the SQL aggregate semantics."""
    tokens = [token(1), token(2), token(3)]
    seed(storage, wallet="W", tokens=tokens)
    outcome(storage, tokens[0], change=None, survived=False)  # died, price unknown
    outcome(storage, tokens[1], change=None, survived=None)   # everything unknown
    outcome(storage, tokens[2], change=90.0)                  # win
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=2)
    entry = report.entries[0]
    assert entry.resolved_tokens == 2      # the death and the win
    assert entry.deaths == 1
    assert entry.wins == 1
    assert report.tokens_pending == 1      # the all-unknown token stays pending


# ---- Second-opinion review findings (2026-07-14, capped fleet) ----

def test_hindsight_sightings_earn_no_credit(storage):
    """A wallet first sighted AFTER a token's outcome was already measured
    cannot claim it was early — restart re-records and late-recheck
    snapshots capture post-pump chasers. Reproduces the review's exact
    scenario: sighted 07-14 on tokens whose wins were measured 07-01;
    previously scored 86/100, must now score nothing."""
    early = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    tokens = [token(1), token(2), token(3)]
    with Storage(":memory:", now_func=lambda: NOW) as db:
        for t in tokens:
            outcome(db, t, change=120.0, at=early)  # wins measured July 1 ...
        seed(db, wallet="ChaserW", tokens=tokens)   # ... sighted July 14
        report = compute_wallet_reputations(db, SETTINGS, min_resolved=1)
        assert report.wallets_scored == 0           # no hindsight credit
        assert report.pairs_hindsight == 3
        assert "excluded as hindsight" in render_reputation_report(report)


def test_same_instant_sighting_keeps_credit(storage):
    """The normal path: sighting at analysis time, outcome measured at or
    after it — credit stands (the guard only excludes strictly-later
    sightings)."""
    seed(storage, wallet="W", tokens=[token(1)])
    outcome(storage, token(1), change=90.0)         # same NOW instant
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=1)
    assert report.wallets_scored == 1
    assert report.pairs_hindsight == 0


def test_price_only_loss_resolves_without_death(storage):
    """Kills the mutant `lost = died`: a -60% worst window with liquidity
    intact is a RESOLVED loss (win_rate denominator grows) but NOT a death
    (rug avoidance untouched)."""
    tokens = [token(1), token(2), token(3)]
    seed(storage, wallet="W", tokens=tokens)
    outcome(storage, tokens[0], change=-60.0, survived=True)   # price loss, alive
    outcome(storage, tokens[1], change=80.0)
    outcome(storage, tokens[2], change=70.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    entry = report.entries[0]
    assert entry.resolved_tokens == 3
    assert entry.wins == 2
    assert entry.deaths == 0        # a crash is not a rug


def test_exact_thresholds_count(storage):
    """Pins >= / <= at the boundaries: exactly +50% is a win, exactly -50%
    is a loss (mirrors evaluate_predictions)."""
    tokens = [token(1), token(2)]
    seed(storage, wallet="W", tokens=tokens)
    outcome(storage, tokens[0], change=50.0)    # exactly the success threshold
    outcome(storage, tokens[1], change=-50.0, survived=True)  # exactly failure
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=2)
    entry = report.entries[0]
    assert entry.resolved_tokens == 2 and entry.wins == 1 and entry.deaths == 0


def test_entries_sorted_best_first(storage):
    """Kills the sort-direction mutant: the strongest wallet leads."""
    good, bad = [token(n) for n in (1, 2, 3)], [token(n) for n in (4, 5, 6)]
    seed(storage, wallet="GoodW", tokens=good)
    seed(storage, wallet="BadW", tokens=bad)
    for t in good:
        outcome(storage, t, change=100.0)
    for t in bad:
        outcome(storage, t, change=-90.0, survived=False)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    assert [e.wallet for e in report.entries] == ["GoodW", "BadW"]
    assert report.entries[0].score > report.entries[1].score


def test_min_resolved_below_one_is_rejected(storage):
    """The CLI can pass arbitrary overrides; the computation refuses a
    nonsense gate loudly instead of dividing by zero downstream."""
    with pytest.raises(ValueError, match="min_resolved"):
        compute_wallet_reputations(storage, SETTINGS, min_resolved=0)


def test_render_guards_nonsense_top_and_counts_honestly(storage):
    """--top 0/negative used to slice from the wrong end and fabricate the
    '… and N more' count; render clamps and stays truthful."""
    wallets = [f"Wallet{i:02d}" for i in range(3)]
    tokens = [token(n) for n in (1, 2, 3)]
    for t in tokens:
        outcome(storage, t, change=100.0)
    for w in wallets:
        seed(storage, wallet=w, tokens=tokens)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    text = render_reputation_report(report, top=-1)   # clamped to 1
    assert "… and 2 more" in text                     # 3 scored, 1 shown
    assert text.count("/100") == 1


def test_render_sanitizes_malicious_wallet_strings(storage):
    """Wallet strings originate in GoPlus responses; a short malicious one
    bypasses truncation and must still be neutralized in the report."""
    evil = "aa\n`@here`"
    tokens = [token(n) for n in (1, 2, 3)]
    seed(storage, wallet=evil, tokens=tokens)
    for t in tokens:
        outcome(storage, t, change=100.0)
    report = compute_wallet_reputations(storage, SETTINGS, min_resolved=3)
    text = render_reputation_report(report)
    assert "`" not in text and "aa\n" not in text
    assert "aa'@here'" in text
