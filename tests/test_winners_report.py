"""Tests for the read-only /winners comparison card (operator request, 2026-07-28)."""

from datetime import datetime, timezone

from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.learning.models import (
    CoinRecord,
    CoinSnapshot,
    OutcomeBucket,
    OutcomeLabel,
    RugSignal,
)
from meme_intelligence.learning.winners_report import MIN_WINNERS, build_winners_report

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def make_record(address, *, snapshots=(), labels=(), rug_signals=()) -> CoinRecord:
    return CoinRecord(
        token=TokenIdentity(chain="solana", address=address, symbol=address[:4]),
        detected_at=NOW,
        detection_price_usd=0.001,
        snapshots=tuple(snapshots),
        labels=tuple(labels),
        rug_signals=tuple(rug_signals),
    )


def winner(address, holders, liquidity, best_return=250.0) -> CoinRecord:
    return make_record(
        address,
        snapshots=[
            # Later snapshot listed FIRST to prove the report picks the
            # earliest by age, not by tuple order.
            CoinSnapshot(age_seconds=600, holder_count=holders * 10,
                         liquidity_usd=liquidity * 10),
            CoinSnapshot(age_seconds=30, holder_count=holders,
                         liquidity_usd=liquidity, volume_1h_usd=5_000.0,
                         buys=80, sells=20, top10_holder_percent=40.0,
                         dev_outflow_usd=0.0),
        ],
        labels=[OutcomeLabel(horizon_hours=6.0, bucket=OutcomeBucket.PUMP,
                             forward_return_percent=best_return)],
    )


def loser(address, holders=10, liquidity=800.0) -> CoinRecord:
    return make_record(
        address,
        snapshots=[CoinSnapshot(age_seconds=30, holder_count=holders,
                                liquidity_usd=liquidity, volume_1h_usd=300.0,
                                buys=30, sells=70, top10_holder_percent=85.0,
                                dev_outflow_usd=2_000.0)],
        labels=[OutcomeLabel(horizon_hours=6.0, bucket=OutcomeBucket.RUG,
                             forward_return_percent=-95.0)],
        rug_signals=[RugSignal(name="liquidity_removed", points=30.0)],
    )


def test_too_few_winners_reports_honestly_instead_of_rendering():
    winners = [winner(f"W{i}", 100, 20_000.0) for i in range(MIN_WINNERS - 1)]
    text = build_winners_report(winners, [loser("L1")])
    assert "Not enough winners" in text
    assert str(MIN_WINNERS - 1) in text


def test_report_compares_medians_from_earliest_snapshots():
    winners = [winner(f"W{i}", holders, 20_000.0)
               for i, holders in enumerate([100, 200, 300, 400, 500])]
    losers = [loser(f"L{i}") for i in range(5)]
    text = build_winners_report(winners, losers)
    # Median winner holders = 300 from the EARLIEST snapshot (age 30) — the
    # age-600 snapshot's 10x figures must not leak in.
    assert "Holders: 300 vs 10" in text
    assert "Liquidity: $20,000 vs $800" in text
    assert "Buy share of trades: 80% vs 30%" in text
    assert "Tripped a rug signal during life: 0% vs 100%" in text
    assert "peaked at +250%" in text
    assert "Descriptive, not predictive" in text


def test_unknown_metrics_say_not_enough_data_never_zero():
    bare = [make_record(f"W{i}",
                        snapshots=[CoinSnapshot(age_seconds=30)],
                        labels=[OutcomeLabel(horizon_hours=6.0,
                                             bucket=OutcomeBucket.PUMP,
                                             forward_return_percent=None)])
            for i in range(MIN_WINNERS)]
    text = build_winners_report(bare, [loser("L1"), loser("L2"), loser("L3")])
    assert "Holders: not enough data" in text
    # No fabricated numbers for the missing side.
    assert "Holders: 0" not in text
