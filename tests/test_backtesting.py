"""Tests for the backtesting & self-improvement system (Spec Part 24)."""

from datetime import datetime, timedelta, timezone

import pytest

from meme_intelligence.analytics.backtesting import (
    evaluate_predictions,
    failure_success_patterns,
    label_alert_outcomes,
    performance_metrics,
    record_strategy_change,
    refresh_outcomes,
    render_backtest_report,
    signal_performance,
    weight_experiments,
)
from meme_intelligence.analyzers.scoring_engine import MasterAssessment
from meme_intelligence.config.settings import BacktestSettings
from meme_intelligence.core.enums import Classification, ConfidenceLevel
from meme_intelligence.core.errors import ConfigurationError
from meme_intelligence.core.models import CategoryScores, DexPair, TokenIdentity
from meme_intelligence.database.storage import Storage

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
SETTINGS = BacktestSettings()


def token(n: int) -> TokenIdentity:
    return TokenIdentity(chain="solana", address=f"Token{n}", symbol=f"T{n}")


def master(n: int, score: float, classification: Classification,
           at: datetime, categories: CategoryScores | None = None) -> MasterAssessment:
    return MasterAssessment(
        token=token(n), generated_at=at,
        category_scores=categories or CategoryScores(security=score),
        final_score=score, coverage=0.5, classification=classification,
        overrides=(), decision_trace=(), confidence=ConfidenceLevel.MEDIUM,
    )


def pair(n: int, price: float, liquidity: float = 50_000.0) -> DexPair:
    return DexPair(chain="solana", pair_address=f"Pool{n}", base_token=token(n),
                   price_usd=price, liquidity_usd=liquidity)


def seed(storage: Storage, n: int, classification: Classification,
         start_price: float, later_price: float, *, later_liquidity: float = 50_000.0,
         categories: CategoryScores | None = None,
         score: float = 85.0) -> None:
    """One prediction at T0 plus a follow-up snapshot ~24h later."""
    storage.record_snapshot(master(n, score, classification, T0, categories),
                            source="test", pair=pair(n, start_price), regime="bull")
    storage.record_snapshot(
        master(n, score, classification, T0 + timedelta(hours=24)),
        source="test", pair=pair(n, later_price, later_liquidity), regime="bull")


NOW = T0 + timedelta(hours=30)  # 1h and 24h windows due; 7d/30d not yet


def make_storage(tmp_path) -> Storage:
    return Storage(str(tmp_path / "bt.sqlite3"), now_func=lambda: NOW)


# ---- Outcome measurement (Sections 2-3) ----

async def test_outcomes_measured_from_snapshots(tmp_path):
    with make_storage(tmp_path) as storage:
        seed(storage, 1, Classification.STRONG_CANDIDATE, 1.00, 2.00)  # +100%
        recorded = await refresh_outcomes(storage, None, settings=SETTINGS,
                                          now_func=lambda: NOW)
        # 24h window measured from the stored follow-up; 1h has no nearby
        # snapshot; 7d/30d are not due yet.
        assert recorded == 1
        prediction = storage.predictions()[0]
        outcomes = storage.outcomes_for_snapshot(prediction["snapshot_id"])
        assert list(outcomes) == [24.0]
        assert outcomes[24.0]["price_change_percent"] == pytest.approx(100.0)
        assert outcomes[24.0]["survived"] == 1
        assert outcomes[24.0]["source"] == "snapshot"


async def test_windows_never_measured_early_and_never_twice(tmp_path):
    with make_storage(tmp_path) as storage:
        seed(storage, 1, Classification.STRONG_CANDIDATE, 1.00, 2.00)
        first = await refresh_outcomes(storage, None, settings=SETTINGS,
                                       now_func=lambda: NOW)
        again = await refresh_outcomes(storage, None, settings=SETTINGS,
                                       now_func=lambda: NOW)
        assert first == 1 and again == 0  # idempotent (Section 14: measured once)


class DeadTokenService:
    async def get_best_pair(self, address, chain=None):
        return None  # no tradable pair left


async def test_live_fetch_records_token_death(tmp_path):
    with make_storage(tmp_path) as storage:
        # Prediction only — no follow-up snapshot exists.
        storage.record_snapshot(
            master(9, 85.0, Classification.STRONG_CANDIDATE, T0),
            source="test", pair=pair(9, 1.0), regime="bull")
        recorded = await refresh_outcomes(storage, DeadTokenService(),
                                          settings=SETTINGS, now_func=lambda: NOW)
        assert recorded == 2  # 1h + 24h windows, both from the live fetch
        outcomes = storage.outcomes_for_snapshot(storage.predictions()[0]["snapshot_id"])
        assert outcomes[24.0]["survived"] == 0
        assert outcomes[24.0]["price_change_percent"] == pytest.approx(-100.0)
        assert outcomes[24.0]["source"] == "live_fetch"


# ---- Grading & Section 4 metrics ----

async def graded_verdicts(tmp_path):
    with make_storage(tmp_path) as storage:
        # Winner called Strong Candidate (correct positive)
        seed(storage, 1, Classification.STRONG_CANDIDATE, 1.00, 2.00,
             categories=CategoryScores(security=90, community=80, narrative=85))
        # Collapse called Strong Candidate (false positive)
        seed(storage, 2, Classification.STRONG_CANDIDATE, 1.00, 0.20,
             categories=CategoryScores(security=88, community=30, narrative=75))
        # Collapse called Avoid (correct risk detection)
        seed(storage, 3, Classification.AVOID, 1.00, 0.10, score=25.0,
             categories=CategoryScores(security=20))
        # Winner called Avoid (missed opportunity)
        seed(storage, 4, Classification.AVOID, 1.00, 3.00, score=30.0,
             categories=CategoryScores(security=35))
        # Sideways watchlist call (ungraded middle class)
        seed(storage, 5, Classification.WATCHLIST, 1.00, 1.05, score=72.0)
        await refresh_outcomes(storage, None, settings=SETTINGS, now_func=lambda: NOW)
        return evaluate_predictions(storage, SETTINGS)


async def test_grading_matrix(tmp_path):
    verdicts = {v.token_symbol: v for v in await graded_verdicts(tmp_path)}
    assert verdicts["T1"].verdict == "correct"
    assert verdicts["T2"].verdict == "incorrect"
    assert verdicts["T3"].verdict == "correct"
    assert verdicts["T4"].verdict == "incorrect"
    assert verdicts["T5"].verdict == "ungraded"


async def test_section4_metrics(tmp_path):
    verdicts = await graded_verdicts(tmp_path)
    metrics = performance_metrics(verdicts, SETTINGS)
    assert metrics.predictions_graded == 4
    assert metrics.accuracy_percent == pytest.approx(50.0)
    # Winners: T1 (+100%) and T4 (+200%); T4 was called Avoid -> 50% detected.
    assert metrics.opportunity_detection_percent == pytest.approx(50.0)
    # Positive calls: T1, T2 -> one failed.
    assert metrics.false_positive_percent == pytest.approx(50.0)
    # Failures: T2, T3 -> T3 was called Avoid.
    assert metrics.risk_detection_percent == pytest.approx(50.0)
    assert metrics.by_regime["bull"]["graded"] == 4          # Section 9
    assert metrics.by_confidence["medium"]["graded"] == 4    # Section 12


# ---- Sections 5-8 ----

async def test_signal_performance_separates_high_and_low(tmp_path):
    verdicts = await graded_verdicts(tmp_path)
    signals = signal_performance(verdicts, SETTINGS)
    security = signals["security"]
    # High-security bucket: T1 (+100), T2 (-80), T5 (+5); low: T3 (-90), T4 (+200).
    assert security["high_n"] == 3 and security["low_n"] == 2
    assert security["high_avg_change"] == pytest.approx(25.0 / 3)
    assert security["low_avg_change"] == pytest.approx(55.0)


async def test_weight_experiments_require_sample_then_report(tmp_path):
    verdicts = await graded_verdicts(tmp_path)
    assert weight_experiments(verdicts, SETTINGS) is None  # n=5 < 10 (Section 1)
    small = BacktestSettings(min_predictions_for_weights=3)
    experiments = weight_experiments(verdicts, small)
    assert set(experiments) == {"locked_baseline", "security_heavy",
                                "community_heavy", "narrative_heavy"}
    for row in experiments.values():
        assert row["n"] == 5
        assert "discrimination" in row


async def test_failure_and_success_patterns(tmp_path):
    verdicts = await graded_verdicts(tmp_path)
    patterns = failure_success_patterns(verdicts, SETTINGS)
    # T2 failed with confidently-high security + narrative -> named as suspects.
    assert patterns["failure_signals"]["security"] == 1
    assert patterns["failure_signals"]["narrative"] == 1
    # T1 won with high security/community/narrative.
    assert patterns["success_signals"]["community"] == 1


# ---- Alert labeling (Part 29 -> 24 bridge) ----

def test_alert_outcomes_labeled(tmp_path):
    from tests.test_alert_delivery import make_event

    with make_storage(tmp_path) as storage:
        storage.record_snapshot(master(1, 70.0, Classification.WATCHLIST, T0),
                                source="test", pair=pair(1, 1.0))
        storage.record_alert(make_event(scores={"master": 70.0}, token=token(1)),
                             source="test")
        # Follow-up snapshot AFTER the alert (which is stamped at NOW = T0+30h).
        storage.record_snapshot(
            master(1, 85.0, Classification.STRONG_CANDIDATE, NOW + timedelta(hours=24)),
            source="test", pair=pair(1, 1.5))
        labeled = label_alert_outcomes(storage, SETTINGS)
        assert labeled == 1
        assert storage.alert_history()[0]["outcome"] == "useful"  # +15 drift


def test_young_alert_not_labeled_until_mature(tmp_path):
    """Bug-hunt: alert outcomes are PERMANENT, but were assigned from whatever
    drift existed at the first backtest run — an alert fired minutes before
    the cron got a permanent verdict from minutes of noise. The maturity gate
    holds the label until alert_outcome_min_hours has passed."""
    from tests.test_alert_delivery import make_event

    with make_storage(tmp_path) as storage:  # storage now_func = NOW (T0+30h)
        storage.record_snapshot(master(1, 70.0, Classification.WATCHLIST, T0),
                                source="test", pair=pair(1, 1.0))
        storage.record_alert(make_event(scores={"master": 70.0}, token=token(1)),
                             source="test")  # stamped at NOW
        storage.record_snapshot(
            master(1, 85.0, Classification.STRONG_CANDIDATE, NOW + timedelta(hours=1)),
            source="test", pair=pair(1, 1.5))
        # Backtest runs 1h after the alert — too young for a permanent verdict.
        labeled = label_alert_outcomes(storage, SETTINGS,
                                       now_func=lambda: NOW + timedelta(hours=1))
        assert labeled == 0
        assert storage.alert_history()[0]["outcome"] is None
        # A day later the same alert matures and is labeled.
        labeled = label_alert_outcomes(storage, SETTINGS,
                                       now_func=lambda: NOW + timedelta(hours=25))
        assert labeled == 1


# ---- Section 11 strategy journal, settings, rendering ----

def test_strategy_change_recorded(tmp_path):
    with make_storage(tmp_path) as storage:
        record_strategy_change(storage, what="raised security weight to 0.20",
                               why="experiments showed +30pt discrimination",
                               results="pending re-measurement")
        entries = storage.journal_entries()
        assert entries[0]["kind"] == "strategy_change"
        assert "WHAT:" in entries[0]["content"]


def test_backtest_settings_validated():
    with pytest.raises(ConfigurationError):
        BacktestSettings(windows_hours="24,1")          # not ascending
    with pytest.raises(ConfigurationError):
        BacktestSettings(failure_price_change_percent=10)  # must be negative
    assert BacktestSettings(windows_hours="1, 24").window_list == [1.0, 24.0]


async def test_report_renders_every_section(tmp_path):
    verdicts = await graded_verdicts(tmp_path)
    text = render_backtest_report(
        performance_metrics(verdicts, SETTINGS),
        signal_performance(verdicts, SETTINGS),
        weight_experiments(verdicts, BacktestSettings(min_predictions_for_weights=3)),
        failure_success_patterns(verdicts, SETTINGS),
        alerts_labeled=2, outcomes_recorded=5,
    )
    for expected in (
        "BACKTESTING & SELF-IMPROVEMENT REPORT",
        "PERFORMANCE (Section 4)", "Accuracy:",
        "BY MARKET REGIME (Section 9)",
        "CONFIDENCE CALIBRATION (Section 12)",
        "SIGNAL PERFORMANCE (Section 6)",
        "WEIGHT EXPERIMENTS (Section 5",
        "locked per Part 31",
        "FAILURE SIGNALS (Section 7",
        "SUCCESS PATTERNS (Section 8",
    ):
        assert expected in text, f"missing: {expected}"

    from meme_intelligence.ai.prompts import check_language
    assert check_language(text) == []


# ---- Migration (Rule 18) ----

def test_old_database_migrates_new_columns(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY, token_id INTEGER, created_at TEXT,
        final_score REAL, classification TEXT, confidence TEXT,
        coverage REAL, category_scores TEXT, overrides TEXT, source TEXT)""")
    conn.commit()
    conn.close()
    with Storage(path) as storage:  # must not raise; columns get added
        storage.record_snapshot(master(1, 80.0, Classification.WATCHLIST, T0),
                                source="test", pair=pair(1, 1.0), regime="bull")
        assert storage.predictions()[0]["price_usd"] == pytest.approx(1.0)
