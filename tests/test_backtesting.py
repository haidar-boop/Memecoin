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
from meme_intelligence.core.errors import AllProvidersFailedError, ConfigurationError
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
    """EVERY provider answered and none knows a pair: the token really died."""

    async def get_token_pairs_confirmed(self, address, chain=None):
        return []


class TotalOutageService:
    """Every provider down — a DNS blip on the droplet fails DexScreener and
    GeckoTerminal together. This is an absence of data, NOT a death."""

    async def get_token_pairs_confirmed(self, address, chain=None):
        raise AllProvidersFailedError("get_token_pairs", {
            "dexscreener": ConnectionResetError("connection reset by peer"),
            "geckoterminal": OSError("Temporary failure in name resolution"),
        })


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


async def test_total_provider_outage_is_never_recorded_as_death(tmp_path):
    """Bug hunt 2026-07-29 (verified by execution): ``get_best_pair`` returns
    ``None`` both when no provider knows a token AND when every provider
    failed, so a DNS blip during the 4x-daily backtest cron fabricated a
    measurement of price $0 / liquidity $0 for every prediction whose window
    was due. That became -100%, ``survived=False``, an ``OutcomeBucket.RUG``
    label and a PERMANENT deployer blacklisting — for healthy coins, written
    into the same memory the ARMED p(rug) veto draws its authority from.

    An outage must record NOTHING (Rule 8), leaving the window open for a
    later good reading."""
    with make_storage(tmp_path) as storage:
        storage.record_snapshot(
            master(9, 85.0, Classification.STRONG_CANDIDATE, T0),
            source="test", pair=pair(9, 1.0), regime="bull")
        recorded = await refresh_outcomes(storage, TotalOutageService(),
                                          settings=SETTINGS, now_func=lambda: NOW)
        assert recorded == 0, "an outage must not manufacture an outcome"
        outcomes = storage.outcomes_for_snapshot(storage.predictions()[0]["snapshot_id"])
        assert outcomes == {}


async def test_outage_never_reaches_the_mind_layer(tmp_path):
    """The poisoning path end to end: nothing may be taught during an outage."""
    class RecordingLearning:
        def __init__(self):
            self.calls = []

        def resolve_outcome(self, address, chain, horizon, ret, *, is_rug):
            self.calls.append((address, horizon, ret, is_rug))

    with make_storage(tmp_path) as storage:
        storage.record_snapshot(
            master(9, 85.0, Classification.STRONG_CANDIDATE, T0),
            source="test", pair=pair(9, 1.0), regime="bull")
        learning = RecordingLearning()
        await refresh_outcomes(storage, TotalOutageService(), settings=SETTINGS,
                               now_func=lambda: NOW, learning_service=learning)
        assert learning.calls == []

        # Control: a token that genuinely died still teaches the rug label,
        # so this guard cannot be mistaken for suppressing real rugs.
        await refresh_outcomes(storage, DeadTokenService(), settings=SETTINGS,
                               now_func=lambda: NOW, learning_service=learning)
        assert learning.calls, "a real death must still resolve as a rug"
        assert all(is_rug for *_, is_rug in learning.calls)


async def test_outage_window_reopens_for_a_later_good_reading(tmp_path):
    """The window stays open: once providers recover, the real value lands."""
    with make_storage(tmp_path) as storage:
        storage.record_snapshot(
            master(9, 85.0, Classification.STRONG_CANDIDATE, T0),
            source="test", pair=pair(9, 1.0), regime="bull")
        assert await refresh_outcomes(storage, TotalOutageService(),
                                      settings=SETTINGS, now_func=lambda: NOW) == 0

        class RecoveredService:
            async def get_token_pairs_confirmed(self, address, chain=None):
                return [pair(9, 2.50)]

        recorded = await refresh_outcomes(storage, RecoveredService(),
                                          settings=SETTINGS, now_func=lambda: NOW)
        assert recorded == 2
        outcomes = storage.outcomes_for_snapshot(storage.predictions()[0]["snapshot_id"])
        assert outcomes[24.0]["price_change_percent"] == pytest.approx(150.0)


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


# ---- Implausible-return guard (2026-07-29 live-data finding) ----


class _RecordingLearner:
    """Mind-layer stand-in: records what it was asked to learn from."""

    def __init__(self):
        self.calls = []

    def resolve_outcome(self, address, chain, horizon_hours, forward_return_percent,
                        *, is_rug=False):
        self.calls.append((address, horizon_hours, forward_return_percent, is_rug))


async def test_implausible_return_is_unmeasurable_and_never_taught(tmp_path):
    """Live data (2026-07-29) showed returns of +1.7e11% — a baseline price of
    3.7e-11 against a later reading of 0.063. Anything past the pump threshold
    becomes a PUMP label, so these fantasies were entering the mind layer's
    permanent memory as winners. The row must still be recorded (the attempt
    is auditable) but with no percentage, and learning must not see it."""
    with make_storage(tmp_path) as storage:
        seed(storage, 1, Classification.STRONG_CANDIDATE,
             start_price=3.70077676159037e-11, later_price=0.06342)
        learner = _RecordingLearner()
        recorded = await refresh_outcomes(storage, settings=SETTINGS,
                                          learning_service=learner,
                                          now_func=lambda: NOW)
        assert recorded >= 1
        rows = storage.outcomes_for_snapshot(
            storage.predictions()[0]["snapshot_id"])
        assert rows, "the measurement attempt must still be recorded"
        assert all(r["price_change_percent"] is None for r in rows.values())
        assert learner.calls == [], "a fantasy return must never become a label"


@pytest.mark.parametrize("multiple,label", [
    (20, "20x"),
    (1_000, "1000x — happens in this market (operator, 2026-07-29)"),
    (50_000, "50,000x — a $20k detection reaching a $1B market cap"),
])
async def test_genuine_moonshots_are_still_measured_and_taught(tmp_path, multiple, label):
    """The guard must never eat a real run. The rare monster is the single
    most valuable record this system can hold — filtering it would delete
    exactly the evidence the operator is hunting. An earlier 1000x ceiling
    sat right where his best outcomes live; these cases pin the floor."""
    with make_storage(tmp_path) as storage:
        seed(storage, 2, Classification.STRONG_CANDIDATE,
             start_price=0.000001, later_price=0.000001 * multiple)
        learner = _RecordingLearner()
        await refresh_outcomes(storage, settings=SETTINGS,
                               learning_service=learner, now_func=lambda: NOW)
        rows = storage.outcomes_for_snapshot(
            storage.predictions()[0]["snapshot_id"])
        assert any(r["price_change_percent"] is not None
                   for r in rows.values()), f"{label} must stay measurable"
        assert learner.calls, f"{label} must still be learned from"
        assert any(c[2] > 100.0 * (multiple - 1) * 0.99 for c in learner.calls)


async def test_guard_can_be_disabled(tmp_path):
    settings = BacktestSettings(max_measurable_return_percent=0.0)
    with make_storage(tmp_path) as storage:
        seed(storage, 3, Classification.STRONG_CANDIDATE,
             start_price=3.7e-11, later_price=0.06342)
        learner = _RecordingLearner()
        await refresh_outcomes(storage, settings=settings,
                               learning_service=learner, now_func=lambda: NOW)
        assert learner.calls, "0 disables the ceiling entirely"


def test_ceiling_must_exceed_the_success_threshold():
    with pytest.raises(ConfigurationError, match="max_measurable_return_percent"):
        BacktestSettings(max_measurable_return_percent=10.0)   # below success 50
    with pytest.raises(ConfigurationError, match="max_measurable_return_percent"):
        BacktestSettings(max_measurable_return_percent=-1.0)


# ---- Review findings on the guard itself (2026-07-29) ----


async def test_drained_pool_still_resolves_as_rug_despite_unmeasurable_return(tmp_path):
    """CRITICAL review finding: _bucket_for_return checks is_rug FIRST, so a
    corrupt return on a drained pool was still producing a CORRECT rug label.
    Nulling the change and requiring `change is not None` deleted it — no rug
    fingerprint in the analog index, no deployer blacklisting, no graded rug
    call for the ARMED veto's authority. Losing real rugs to avoid fake pumps
    is a strictly worse trade for a rug-veto system."""
    with make_storage(tmp_path) as storage:
        seed(storage, 1, Classification.STRONG_CANDIDATE,
             start_price=3.70077676159037e-11, later_price=0.06342,
             later_liquidity=0.0)                      # pool drained = real rug
        learner = _RecordingLearner()
        await refresh_outcomes(storage, settings=SETTINGS,
                               learning_service=learner, now_func=lambda: NOW)
        assert learner.calls, "a confirmed rug must still resolve"
        assert all(c[3] is True for c in learner.calls), "must be flagged is_rug"
        assert all(c[2] is None for c in learner.calls), "magnitude stays honest"


async def test_corrupt_return_on_a_live_pool_is_still_dropped(tmp_path):
    """The original point of the guard survives the rug fix."""
    with make_storage(tmp_path) as storage:
        seed(storage, 2, Classification.STRONG_CANDIDATE,
             start_price=3.70077676159037e-11, later_price=0.06342,
             later_liquidity=50_000.0)                 # alive: no rug to record
        learner = _RecordingLearner()
        await refresh_outcomes(storage, settings=SETTINGS,
                               learning_service=learner, now_func=lambda: NOW)
        assert learner.calls == []


async def test_nan_return_never_reaches_learning(tmp_path):
    """HIGH review finding: abs(nan) > ceiling is False, so NaN slipped past
    the ceiling and was taught as FLAT (nan >= 50 and nan <= -50 are both
    False) while SQLite stored NULL — audit trail and training set
    disagreeing. Non-finite must be rejected unconditionally."""
    for ceiling in (SETTINGS.max_measurable_return_percent, 0.0):   # 0 = guard off
        settings = BacktestSettings(max_measurable_return_percent=ceiling)
        with make_storage(tmp_path / f"nan{ceiling}") as storage:
            seed(storage, 3, Classification.STRONG_CANDIDATE,
                 start_price=0.000001, later_price=float("nan"))
            learner = _RecordingLearner()
            await refresh_outcomes(storage, settings=settings,
                                   learning_service=learner, now_func=lambda: NOW)
            assert learner.calls == [], f"NaN leaked with ceiling={ceiling}"


async def test_unmeasurable_window_is_retried_and_filled_by_a_good_reading(tmp_path):
    """MEDIUM review finding: an unmeasurable row must not burn the window
    forever. The usual cause is a transient bad tick, and permanently losing
    that window could delete a real winner's evidence."""
    with make_storage(tmp_path) as storage:
        seed(storage, 4, Classification.STRONG_CANDIDATE,
             start_price=3.70077676159037e-11, later_price=0.06342)
        await refresh_outcomes(storage, settings=SETTINGS, now_func=lambda: NOW)
        snapshot_id = storage.predictions()[0]["snapshot_id"]
        assert all(r["price_change_percent"] is None
                   for r in storage.outcomes_for_snapshot(snapshot_id).values())

        # A later, sane reading for the same windows must fill the hole.
        storage.record_outcome(
            snapshot_id=snapshot_id, token_id=storage.predictions()[0]["token_id"],
            window_hours=24.0, target_at=(T0 + timedelta(hours=24)).isoformat(),
            measured_at=NOW.isoformat(), price_usd=0.0000012,
            price_change_percent=20.0, liquidity_usd=50_000.0,
            survived=True, source="snapshot")
        assert storage.outcomes_for_snapshot(snapshot_id)[24.0][
            "price_change_percent"] == 20.0


async def test_a_settled_window_is_never_overwritten(tmp_path):
    """The flip side: a window holding a REAL return stays settled — the
    first honest measurement of a window stands (Part 24 S14)."""
    with make_storage(tmp_path) as storage:
        seed(storage, 5, Classification.STRONG_CANDIDATE,
             start_price=0.000001, later_price=0.000002)      # +100%
        await refresh_outcomes(storage, settings=SETTINGS, now_func=lambda: NOW)
        snapshot_id = storage.predictions()[0]["snapshot_id"]
        before = storage.outcomes_for_snapshot(snapshot_id)[24.0]["price_change_percent"]
        storage.record_outcome(
            snapshot_id=snapshot_id, token_id=storage.predictions()[0]["token_id"],
            window_hours=24.0, target_at=(T0 + timedelta(hours=24)).isoformat(),
            measured_at=NOW.isoformat(), price_usd=9.99,
            price_change_percent=999.0, liquidity_usd=1.0,
            survived=False, source="live_fetch")
        assert storage.outcomes_for_snapshot(snapshot_id)[24.0][
            "price_change_percent"] == before


class SplitCoverageService:
    """DexScreener has not indexed the token and answers HTTP 200 with an empty
    list; GeckoTerminal is holding the live pool. Review finding 2026-07-29."""

    def __init__(self, live_pair):
        self._live = live_pair
        self.confirm_sweeps = 0

    async def get_token_pairs(self, address, chain=None):
        return []          # what the pool's first responder says

    async def get_token_pairs_confirmed(self, address, chain=None):
        self.confirm_sweeps += 1
        return [self._live]  # what a second provider actually knows


async def test_one_providers_silence_is_not_a_token_death(tmp_path):
    """A Gecko-discovered coin that DexScreener never indexed must not be
    recorded as dead just because DexScreener replied first. Before the fix
    this wrote -100%/RUG and PERMANENTLY blacklisted an innocent deployer."""
    with make_storage(tmp_path) as storage:
        storage.record_snapshot(
            master(9, 85.0, Classification.STRONG_CANDIDATE, T0),
            source="test", pair=pair(9, 1.0), regime="bull")
        service = SplitCoverageService(pair(9, 2.50))
        recorded = await refresh_outcomes(storage, service, settings=SETTINGS,
                                          now_func=lambda: NOW)
        assert service.confirm_sweeps > 0, "must use the confirming lookup"
        assert recorded == 2
        outcomes = storage.outcomes_for_snapshot(storage.predictions()[0]["snapshot_id"])
        # The real +150% move, not a fabricated -100% death.
        assert outcomes[24.0]["price_change_percent"] == pytest.approx(150.0)
        assert outcomes[24.0]["survived"] == 1


async def test_non_finite_liquidity_is_unknown_not_a_confirmed_rug(tmp_path):
    """`nan >= floor` is False, so a NaN liquidity reading produced
    survived=False — which alone is enough to write a permanent RUG label and
    blacklist the deployer. The same NaN hole was closed for `change` but
    missed for `liquidity` (review finding, 2026-07-29)."""
    class NanLiquidityService:
        async def get_token_pairs_confirmed(self, address, chain=None):
            return [pair(9, 2.0, liquidity=float("nan"))]

    calls = []

    class RecordingLearning:
        def resolve_outcome(self, address, chain, horizon, ret, *, is_rug):
            calls.append(is_rug)

    with make_storage(tmp_path) as storage:
        storage.record_snapshot(
            master(9, 85.0, Classification.STRONG_CANDIDATE, T0),
            source="test", pair=pair(9, 1.0), regime="bull")
        await refresh_outcomes(storage, NanLiquidityService(), settings=SETTINGS,
                               now_func=lambda: NOW, learning_service=RecordingLearning())
        outcomes = storage.outcomes_for_snapshot(storage.predictions()[0]["snapshot_id"])
        assert outcomes[24.0]["survived"] is None      # unknown, not "drained"
        assert not any(calls), "a NaN liquidity must never resolve as a rug"


async def test_an_unsettled_row_accepts_a_later_confirmed_drain(tmp_path):
    """An UNMEASURABLE row (NULL return, e.g. the implausible-return guard
    fired) must accept a later reading that proves the pool drained. Refusing
    it left the audit row asserting nothing was observed while the mind layer
    held a RUG label from that very reading (bug hunt, 2026-07-29)."""
    with make_storage(tmp_path) as storage:
        seed(storage, 1, Classification.STRONG_CANDIDATE, 1.00, 2.00)
        prediction = storage.predictions()[0]
        storage.record_outcome(
            snapshot_id=prediction["snapshot_id"], token_id=prediction["token_id"],
            window_hours=24.0, target_at=NOW.isoformat(), measured_at=NOW.isoformat(),
            price_usd=None, price_change_percent=None, liquidity_usd=None,
            survived=None, source="live_fetch")
        assert storage.outcomes_for_snapshot(
            prediction["snapshot_id"])[24.0]["survived"] is None

        storage.record_outcome(
            snapshot_id=prediction["snapshot_id"], token_id=prediction["token_id"],
            window_hours=24.0, target_at=NOW.isoformat(), measured_at=NOW.isoformat(),
            price_usd=None, price_change_percent=None, liquidity_usd=0.0,
            survived=False, source="live_fetch")
        assert storage.outcomes_for_snapshot(
            prediction["snapshot_id"])[24.0]["survived"] == 0


async def test_a_settled_window_is_not_overwritten_even_by_a_drain(tmp_path):
    """Part 24 S14 stands: what happened at 24h is what happened at 24h, and a
    later drain belongs to a later window — not to this row."""
    with make_storage(tmp_path) as storage:
        seed(storage, 1, Classification.STRONG_CANDIDATE, 1.00, 2.00)
        await refresh_outcomes(storage, None, settings=SETTINGS, now_func=lambda: NOW)
        prediction = storage.predictions()[0]
        row = storage.outcomes_for_snapshot(prediction["snapshot_id"])[24.0]
        assert row["price_change_percent"] == pytest.approx(100.0)

        storage.record_outcome(
            snapshot_id=prediction["snapshot_id"], token_id=prediction["token_id"],
            window_hours=24.0, target_at=NOW.isoformat(), measured_at=NOW.isoformat(),
            price_usd=0.0, price_change_percent=None, liquidity_usd=0.0,
            survived=False, source="live_fetch")
        after = storage.outcomes_for_snapshot(prediction["snapshot_id"])[24.0]
        assert after["price_change_percent"] == pytest.approx(100.0)
        assert after["survived"] == 1
