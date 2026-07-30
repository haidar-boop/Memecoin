"""Guards on the alert-outcomes report.

It exists to answer one question with data: has the bot ever pitched a coin that
went on to multiply, or has it only ever failed to RECORD one? Those look
identical from outside and have opposite fixes, so the report must not be able to
confuse them — and it must not be able to write to the live database while
answering.
"""

import importlib.util
import pathlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
REPORT = REPO / "deploy" / "alert_outcomes_report.py"


def load():
    spec = importlib.util.spec_from_file_location("alert_outcomes_report", REPORT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_db(tmp_path, coins, stale_days=None, liquidity=5000.0):
    """A database shaped like the real one: alerts + snapshots + outcomes."""
    from meme_intelligence.core.models import TokenIdentity
    from meme_intelligence.database.storage import Storage

    path = str(tmp_path / "outcomes.sqlite3")
    storage = Storage(path)
    con = storage._conn
    now = datetime.now(timezone.utc)
    insert_alert = ("INSERT INTO alerts (token_id, created_at, priority, alert_type,"
                    " title, reasons, scores, source) VALUES (?,?,?,?,?,?,?,?)")
    for index, (symbol, pct) in enumerate(coins):
        token = TokenIdentity(chain="solana", address=f"Mint{symbol}{'x' * 30}",
                              symbol=symbol)
        token_id = storage.upsert_token(token)
        at = (now - timedelta(days=5 + index)).isoformat()
        con.execute(insert_alert, (token_id, at, "HIGH", "early_opportunity",
                                   "t", "[]", "{}", "scan"))
        con.execute(
            "INSERT INTO snapshots (token_id, created_at, final_score, classification,"
            " confidence, coverage, category_scores, overrides, source)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (token_id, at, 85.0, "Strong Candidate", "medium", 0.45, "{}", "[]", "scan"))
        snapshot_id = con.execute(
            "SELECT id FROM snapshots WHERE token_id = ?", (token_id,)).fetchone()[0]
        con.execute(
            "INSERT INTO outcomes (snapshot_id, token_id, window_hours, target_at,"
            " measured_at, price_usd, price_change_percent, liquidity_usd, survived,"
            " source) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, token_id, 720.0, at, now.isoformat(), 0.001, pct,
             liquidity, 1, "snapshot"))
    if stale_days is not None:
        token = TokenIdentity(chain="solana", address="MintSTALE" + "y" * 30,
                              symbol="STALE")
        token_id = storage.upsert_token(token)
        con.execute(insert_alert,
                    (token_id, (now - timedelta(days=stale_days)).isoformat(),
                     "HIGH", "momentum", "t", "[]", "{}", "scan"))
    con.commit()
    return path


def run(path, *extra):
    result = subprocess.run(
        [sys.executable, str(REPORT), "--db", path, *extra],
        capture_output=True, text=True, cwd=str(REPO), timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def test_a_realizable_moonshot_is_reported_as_sellable(tmp_path):
    """The distinction the whole report exists for. A 35x that had a pool to sell
    into is a real find; the same number against an empty pool is a screenshot."""
    path = build_db(tmp_path, [("MOON", 3400.0), ("DEAD", -95.0)])
    out = run(path)
    assert "+3400.0%" in out
    assert "SELLABLE" in out
    assert "could ACTUALLY have sold into: +3400.0%" in out
    assert "MOONSHOT" in out


def test_an_unrealizable_gain_is_not_credited_as_a_win(tmp_path):
    """The finding from the operator's real database: the headline returns sat on
    zero liquidity. A gain you cannot exit is not a gain, and the report must not
    present it as one."""
    path = build_db(tmp_path, [("PAPER", 5000.0)], liquidity=0.0)
    out = run(path)
    assert "unrealizable" in out
    assert "NONE of the top returns had" in out
    assert "sellable gains of +200% or better: 0" in out


def test_returns_above_the_plausibility_ceiling_are_excluded_as_artifacts(tmp_path):
    """Real finding: rows recorded before the 2026-07-29 guard keep fabricated
    values like +702,288,997%. Reporting those as achievements read as good news
    off the operator's own database."""
    path = build_db(tmp_path, [("BOGUS", 702_288_997.5), ("REAL", 150.0)])
    out = run(path)
    assert "exceed the plausibility ceiling" in out
    assert "EXCLUDED" in out
    assert "+702288997.5%" not in out and "+702,288,997.5%" not in out
    assert "Best plausible return: +150.0%" in out


def test_no_winner_reports_it_plainly(tmp_path):
    path = build_db(tmp_path, [("MEH", 12.0), ("DEAD", -95.0)])
    out = run(path)
    assert "Best plausible return: +12.0%" in out
    assert "MOONSHOT" in out  # the band label always prints; the count is what matters


def test_stale_unmeasured_alerts_are_called_out_as_the_finding(tmp_path):
    """A coin pitched 20 days ago with no result is a measurement bug, and must
    not be lumped in with one pitched an hour ago."""
    path = build_db(tmp_path, [("OK", 30.0)], stale_days=20)
    out = run(path)
    assert "alerted > 24h ago (should exist)" in out
    assert "20.0 days" in out


def test_low_coverage_says_the_question_is_unprovable(tmp_path):
    """If almost nothing was measured, 'it never found a winner' is not a fact
    about the bot — it is a fact about the database."""
    from meme_intelligence.core.models import TokenIdentity
    from meme_intelligence.database.storage import Storage

    path = str(tmp_path / "empty.sqlite3")
    storage = Storage(path)
    now = datetime.now(timezone.utc)
    for index in range(8):
        token = TokenIdentity(chain="solana", address=f"Mint{index}" + "z" * 34,
                              symbol=f"T{index}")
        token_id = storage.upsert_token(token)
        storage._conn.execute(
            "INSERT INTO alerts (token_id, created_at, priority, alert_type, title,"
            " reasons, scores, source) VALUES (?,?,?,?,?,?,?,?)",
            (token_id, (now - timedelta(days=3)).isoformat(), "HIGH",
             "early_opportunity", "t", "[]", "{}", "scan"))
    storage._conn.commit()
    out = run(path)
    assert "LOW COVERAGE" in out
    assert "UNPROVABLE" in out


def test_it_is_read_only_and_cannot_write(tmp_path):
    """It is run against the live monitor's database."""
    source = REPORT.read_text()
    assert "mode=ro" in source and "uri=True" in source
    upper = source.upper()
    for statement in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "CREATE TABLE"):
        assert statement not in upper, f"must not contain {statement!r}"


def test_it_loads_dotenv_like_every_other_deploy_script():
    """Settings.from_env() does not read .env — that mistake already made one
    probe report 'this adds nothing' on a correctly configured droplet."""
    source = REPORT.read_text()
    assert "get_settings()" in source
    assert "Settings.from_env()" not in source


def test_a_missing_database_exits_cleanly_rather_than_tracebacking(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPORT), "--db", str(tmp_path / "nope.sqlite3")],
        capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert result.returncode == 1
    assert "No database found" in result.stdout
    assert "Traceback" not in result.stderr


def test_band_edges_cover_every_return_without_gaps_or_overlap():
    """A coin landing exactly on a boundary must be counted once, not zero or
    twice — the distribution is the number he will read."""
    module = load()
    bands = module.BANDS
    for (_, hi, _), (lo_next, _, _) in zip(bands, bands[1:]):
        assert hi == lo_next, "bands must be contiguous"
    assert bands[-1][1] == float("inf")
    for value in (-100.0, -90.0, -50.0, -10.0, 0.0, 10.0, 50.0, 200.0, 1000.0, 1e9):
        hits = sum(1 for lo, hi, _ in bands if lo <= value < hi)
        assert hits == 1, f"{value} matched {hits} bands"


@pytest.mark.parametrize("stamp", ["not-a-date", None, "", "2026-13-45T99:99:99"])
def test_bad_timestamps_do_not_crash_the_age_split(stamp):
    module = load()
    assert module.hours_since(stamp, datetime.now(timezone.utc)) is None


def test_naive_timestamps_are_treated_as_utc():
    """Stored timestamps are ISO strings and some historical rows lack a zone."""
    module = load()
    now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    age = module.hours_since("2026-07-30T10:00:00", now)
    assert age == pytest.approx(2.0)
