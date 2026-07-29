"""SQLite storage for tokens, assessment snapshots, watchlist, and journal.

Backs the daily research workflow (Part 11) and the future learning system
(Part 24): every assessment snapshot is kept so predictions can later be
compared against outcomes. SQLite keeps operations simple and reliable
(Rule 21); this module is the only place that knows the backend, so a
server database can replace it behind the same interface.

The API is synchronous — local SQLite operations are sub-millisecond and
the CLI flows call them between network awaits, all from the single
asyncio event loop thread. The connection is opened with the default
``check_same_thread=True``, so a ``Storage`` instance may only ever be
used from the thread that created it — do NOT wrap calls in
``run_in_executor``/a thread pool without first passing
``check_same_thread=False`` and adding your own serialization, or every
call from the executor thread raises immediately.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from meme_intelligence.analyzers.scoring_engine import MasterAssessment
from meme_intelligence.core.enums import Classification, WatchlistTier
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity

# SQL identifiers (table/column/type) cannot be bound as ``?`` parameters, so
# the few places that must interpolate them are restricted to this safe
# charset. Every value that reaches these paths is an internal constant
# (``_MIGRATIONS`` keys, module-literal query fragments), never user input —
# this guard makes that guarantee explicit and enforced (defense in depth).
_SAFE_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_ ]*$")


def _safe_identifier(value: str) -> str:
    """Return ``value`` if it is a safe SQL identifier/type, else raise."""
    if not _SAFE_SQL_IDENTIFIER.match(value):
        raise ValueError(f"unsafe SQL identifier: {value!r}")
    return value

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    first_seen TEXT NOT NULL,
    UNIQUE (chain, address)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    created_at TEXT NOT NULL,
    final_score REAL NOT NULL,
    classification TEXT NOT NULL,
    confidence TEXT NOT NULL,
    coverage REAL NOT NULL,
    category_scores TEXT NOT NULL,  -- JSON object
    overrides TEXT NOT NULL,        -- JSON array
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_token ON snapshots(token_id, created_at);

CREATE TABLE IF NOT EXISTS watchlist (
    token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
    tier TEXT NOT NULL,
    thesis TEXT,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_score REAL,
    last_classification TEXT
);

CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    token_id INTEGER REFERENCES tokens(id),
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,     -- discovery / thesis / decision / outcome / lesson
    content TEXT NOT NULL
);

-- Last-known security facts per token (Part 18 Section 10): the baseline
-- every new analysis is diffed against for contract-change monitoring.
CREATE TABLE IF NOT EXISTS security_facts (
    token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
    updated_at TEXT NOT NULL,
    facts TEXT NOT NULL  -- JSON object of FACT_FIELDS
);

-- Wallet sightings (Part 17 Sections 2-3): every observed wallet action,
-- the raw material for reputation once outcomes accumulate (Part 24).
CREATE TABLE IF NOT EXISTS wallet_sightings (
    id INTEGER PRIMARY KEY,
    wallet TEXT NOT NULL,
    chain TEXT NOT NULL,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    side TEXT NOT NULL,          -- buy / sell / hold_whale
    usd_value REAL,
    seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sightings_wallet ON wallet_sightings(wallet, seen_at);
CREATE INDEX IF NOT EXISTS idx_sightings_token ON wallet_sightings(token_id);

-- Alert history (Part 29 Section 11): every delivered alert, joinable
-- against later snapshots so Section 12 / Part 24 can measure which
-- alerts were useful and which were noise.
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    created_at TEXT NOT NULL,
    priority TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    title TEXT NOT NULL,
    reasons TEXT NOT NULL,       -- JSON array
    scores TEXT NOT NULL,        -- JSON object
    score_at_alert REAL,         -- master score when the alert fired
    source TEXT NOT NULL,
    outcome TEXT                 -- filled by performance analysis (Part 24)
);
CREATE INDEX IF NOT EXISTS idx_alerts_token ON alerts(token_id, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type, created_at);

-- Measured outcomes per prediction (Part 24, Sections 2-3): one row per
-- (prediction snapshot, time window), joining what the framework said
-- with what the market then did.
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    window_hours REAL NOT NULL,
    target_at TEXT NOT NULL,     -- prediction time + window
    measured_at TEXT NOT NULL,   -- when the measurement actually happened
    price_usd REAL,
    price_change_percent REAL,   -- vs price at prediction time
    liquidity_usd REAL,
    survived INTEGER,            -- liquidity above the survival floor (1/0), NULL unknown
    source TEXT NOT NULL,        -- snapshot / live_fetch
    UNIQUE (snapshot_id, window_hours)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_token ON outcomes(token_id, window_hours);

-- Operator holdings (Project 2, ROADMAP #2): coins the operator ACTUALLY
-- bought, marked via /holding on Telegram. A held coin is permanent
-- operator interest: protective alerts keep full priority for it.
CREATE TABLE IF NOT EXISTS holdings (
    id INTEGER PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_holdings_token ON holdings(token_id, active);

-- Muted tokens (Project 2): /mute suppresses ALL alert delivery for a
-- token; analysis continues normally, only delivery is silenced.
CREATE TABLE IF NOT EXISTS muted_tokens (
    token_id INTEGER PRIMARY KEY REFERENCES tokens(id),
    muted_at TEXT NOT NULL
);

-- Operator feedback (Project 2): thumbs up/down pressed on alert messages.
-- ADVISORY ONLY (Rule 8): operator opinion is not a measured outcome, so
-- this never touches alerts.outcome or the learning ground-truth labels.
CREATE TABLE IF NOT EXISTS operator_feedback (
    id INTEGER PRIMARY KEY,
    token_id INTEGER NOT NULL REFERENCES tokens(id),
    alert_id INTEGER,
    verdict TEXT NOT NULL CHECK (verdict IN ('up', 'down')),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_token ON operator_feedback(token_id);
"""

# Columns added to existing tables after their first release; applied by
# Storage._migrate() so databases created by earlier builds keep working
# (Rule 18 — extend, never break).
_MIGRATIONS = {
    "snapshots": (
        ("price_usd", "REAL"),       # market facts at prediction time (Part 24 S2)
        ("liquidity_usd", "REAL"),
        ("market_cap", "REAL"),
        ("regime", "TEXT"),          # market condition bucketing (Part 24 S9)
        ("opportunity_rank", "REAL"),  # Part 28 S5 watchlist opportunity ranking
    ),
}


@dataclass(frozen=True)
class WatchlistEntry:
    """One tracked token (Part 11, Section 5)."""

    token: TokenIdentity
    tier: WatchlistTier
    thesis: str | None
    added_at: datetime
    updated_at: datetime
    last_score: float | None
    last_classification: str | None


@dataclass(frozen=True)
class WatchlistChange:
    """A watchlist mutation, reported in the daily summary (Part 11, Section 14)."""

    token: TokenIdentity
    change: str  # "added" / "tier_changed" / "updated" / "archived"
    tier: WatchlistTier
    detail: str


class Storage:
    """SQLite-backed persistence for the research desk."""

    def __init__(
        self,
        path: str,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._now = now_func
        self._logger = get_logger("database.storage")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # timeout + WAL + busy_timeout: the 24/7 monitor and the scheduled
        # jobs (daily routine, backtest refresh) share this database file.
        # WAL lets readers and the writer coexist, and the busy timeout
        # makes a second writer wait politely instead of raising
        # "database is locked" (Rule 7). On :memory: databases WAL is a
        # harmless no-op (sqlite keeps "memory" journaling).
        self._conn = sqlite3.connect(path, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add post-release columns to tables from older databases (Rule 18)."""
        for table, columns in _MIGRATIONS.items():
            safe_table = _safe_identifier(table)
            # SQL identifiers (table/column/type) cannot be bound as ? params;
            # every value here is an internal _MIGRATIONS constant, additionally
            # charset-validated by _safe_identifier above — safe by construction.
            existing = {row["name"] for row in
                        # nosemgrep
                        self._conn.execute(f"PRAGMA table_info({safe_table})")}
            for name, sql_type in columns:
                if name not in existing:
                    safe_name = _safe_identifier(name)
                    safe_type = _safe_identifier(sql_type)
                    try:
                        # nosemgrep
                        self._conn.execute(
                            f"ALTER TABLE {safe_table} ADD COLUMN {safe_name} {safe_type}")
                        self._logger.info("migrated %s: added column %s", table, name)
                    except sqlite3.OperationalError as exc:
                        # Two processes sharing this database (the monitor
                        # and a cron job) can both start up against a
                        # pre-upgrade file at once, both see the column
                        # missing, and both attempt this ALTER TABLE — the
                        # loser hits "duplicate column name", not a real
                        # failure (Rule 7: the migration already happened).
                        if "duplicate column" not in str(exc).lower():
                            raise
                        self._logger.info(
                            "%s.%s already migrated by a concurrent process", table, name)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- Tokens ----

    def upsert_token(self, token: TokenIdentity) -> int:
        """Insert or refresh a token's identity; returns its row id."""
        now = self._now().isoformat()
        self._conn.execute(
            """INSERT INTO tokens (chain, address, symbol, name, first_seen)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (chain, address) DO UPDATE SET
                   symbol = COALESCE(excluded.symbol, tokens.symbol),
                   name = COALESCE(excluded.name, tokens.name)""",
            (token.chain, token.address, token.symbol, token.name, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM tokens WHERE chain = ? AND address = ?",
            (token.chain, token.address),
        ).fetchone()
        return int(row["id"])

    def find_token(self, address: str) -> TokenIdentity | None:
        """Look up a known token by address alone (Project 2: phone commands
        carry no chain). Exact match first (Solana base58 is case-sensitive);
        EVM 0x addresses also match case-insensitively. Newest row wins when
        the same address somehow exists on several chains."""
        row = self._conn.execute(
            "SELECT chain, address, symbol, name FROM tokens WHERE address = ? "
            "ORDER BY id DESC LIMIT 1",
            (address,),
        ).fetchone()
        if row is None and address.lower().startswith("0x"):
            row = self._conn.execute(
                "SELECT chain, address, symbol, name FROM tokens "
                "WHERE lower(address) = lower(?) ORDER BY id DESC LIMIT 1",
                (address,),
            ).fetchone()
        if row is None:
            return None
        return TokenIdentity(chain=row["chain"], address=row["address"],
                             symbol=row["symbol"], name=row["name"])

    def table_counts(self) -> dict:
        """Cheap DB totals for the /status command (Project 2)."""
        def count(sql: str) -> int:
            return int(self._conn.execute(sql).fetchone()[0])

        return {
            "tokens": count("SELECT COUNT(*) FROM tokens"),
            "alerts": count("SELECT COUNT(*) FROM alerts"),
            "watchlist": count("SELECT COUNT(*) FROM watchlist WHERE tier != 'archived'"),
            "holdings": count("SELECT COUNT(*) FROM holdings WHERE active = 1"),
        }

    # ---- Assessment snapshots (feeds Part 24 backtesting) ----

    def record_snapshot(
        self,
        assessment: MasterAssessment,
        source: str,
        *,
        pair=None,          # DexPair: market facts at prediction time (Part 24 S2)
        regime: str | None = None,  # market condition bucket (Part 24 S9)
        opportunity_rank: float | None = None,  # Part 28 S5 watchlist ranking
    ) -> int:
        token_id = self.upsert_token(assessment.token)
        cursor = self._conn.execute(
            """INSERT INTO snapshots
               (token_id, created_at, final_score, classification, confidence,
                coverage, category_scores, overrides, source,
                price_usd, liquidity_usd, market_cap, regime, opportunity_rank)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token_id,
                assessment.generated_at.isoformat(),
                assessment.final_score,
                assessment.classification.value,
                assessment.confidence.value,
                assessment.coverage,
                json.dumps(dataclasses.asdict(assessment.category_scores)),
                json.dumps(list(assessment.overrides)),
                source,
                pair.price_usd if pair is not None else None,
                pair.liquidity_usd if pair is not None else None,
                pair.market_cap if pair is not None else None,
                regime,
                opportunity_rank,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def score_history(self, token: TokenIdentity, limit: int = 30) -> list[dict]:
        """Recent snapshots for one token, newest first (Part 28 score tracking)."""
        rows = self._conn.execute(
            """SELECT s.created_at, s.final_score, s.classification, s.coverage, s.source
               FROM snapshots s JOIN tokens t ON t.id = s.token_id
               WHERE t.chain = ? AND t.address = ?
               ORDER BY s.created_at DESC LIMIT ?""",
            (token.chain, token.address, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- Watchlist (Part 11, Section 5) ----

    def get_watchlist(self, include_archived: bool = False) -> list[WatchlistEntry]:
        query = """SELECT t.chain, t.address, t.symbol, t.name,
                          w.tier, w.thesis, w.added_at, w.updated_at,
                          w.last_score, w.last_classification
                   FROM watchlist w JOIN tokens t ON t.id = w.token_id"""
        if not include_archived:
            query += " WHERE w.tier != 'archived'"
        # `ORDER BY w.tier` sorts the raw TEXT alphabetically, putting
        # 'archived' before every 'tier_N_*' value ('a' < 't') — with
        # include_archived=True, archived (lowest priority) entries sorted
        # ahead of tier_1 (highest priority). Rank explicitly instead.
        query += """ ORDER BY CASE w.tier
                         WHEN 'tier_1_high_priority' THEN 1
                         WHEN 'tier_2_developing' THEN 2
                         WHEN 'tier_3_research_only' THEN 3
                         WHEN 'archived' THEN 4
                         ELSE 5
                     END, w.last_score DESC"""
        rows = self._conn.execute(query).fetchall()
        return [
            WatchlistEntry(
                token=TokenIdentity(chain=row["chain"], address=row["address"],
                                    symbol=row["symbol"], name=row["name"]),
                tier=WatchlistTier(row["tier"]),
                thesis=row["thesis"],
                added_at=datetime.fromisoformat(row["added_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                last_score=row["last_score"],
                last_classification=row["last_classification"],
            )
            for row in rows
        ]

    def top_opportunities(self, limit: int = 10) -> list[dict]:
        """Active watchlist tokens ranked by their latest opportunity rank
        (Part 28 Sections 5-6 — 'which are the strongest available
        opportunities right now?'). Archived tokens are excluded; a token
        whose latest snapshot predates the opportunity-rank feature (NULL)
        sorts last rather than being dropped, so nothing silently vanishes.
        """
        rows = self._conn.execute(
            """SELECT t.chain, t.address, t.symbol, t.name,
                      w.tier, w.thesis, w.last_score, w.last_classification,
                      (SELECT s.opportunity_rank FROM snapshots s
                       WHERE s.token_id = w.token_id
                       ORDER BY s.created_at DESC LIMIT 1) AS opportunity_rank
               FROM watchlist w JOIN tokens t ON t.id = w.token_id
               WHERE w.tier != 'archived'
               ORDER BY opportunity_rank IS NULL, opportunity_rank DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_watchlist(
        self,
        token: TokenIdentity,
        tier: WatchlistTier,
        *,
        score: float | None = None,
        classification: Classification | None = None,
        thesis: str | None = None,
    ) -> WatchlistChange:
        """Insert or update a watchlist entry, reporting what changed."""
        token_id = self.upsert_token(token)
        now = self._now().isoformat()
        existing = self._conn.execute(
            "SELECT tier, thesis FROM watchlist WHERE token_id = ?", (token_id,)
        ).fetchone()

        classification_value = classification.value if classification else None
        # A separate SELECT-then-INSERT/UPDATE had a race window between
        # two processes sharing this database (the 24/7 monitor and the
        # daily/backtest cron jobs, now that Storage runs in WAL mode):
        # both could see "no existing row" for the same brand-new token
        # and both attempt INSERT, the second raising IntegrityError on
        # the token_id primary key. A single UPSERT is atomic — the write
        # itself can no longer race, even though the human-readable
        # change description below is best-effort (rare cosmetic staleness
        # under true concurrency, never a crash).
        self._conn.execute(
            """INSERT INTO watchlist
               (token_id, tier, thesis, added_at, updated_at, last_score, last_classification)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(token_id) DO UPDATE SET
                   tier = excluded.tier,
                   thesis = COALESCE(excluded.thesis, watchlist.thesis),
                   updated_at = excluded.updated_at,
                   last_score = COALESCE(excluded.last_score, watchlist.last_score),
                   last_classification =
                       COALESCE(excluded.last_classification, watchlist.last_classification)""",
            (token_id, tier.value, thesis, now, now, score, classification_value),
        )
        if existing is None:
            change = WatchlistChange(token, "added", tier,
                                     f"added at {tier.value}" + (f" (score {score:.0f})" if score is not None else ""))
        else:
            old_tier = WatchlistTier(existing["tier"])
            if old_tier is not tier:
                change = WatchlistChange(token, "tier_changed", tier,
                                         f"{old_tier.value} -> {tier.value}")
            else:
                change = WatchlistChange(token, "updated", tier, "score refreshed")
        self._conn.commit()
        self._logger.info("watchlist %s: %s (%s)", change.change,
                          token.symbol or token.address, change.detail)
        return change

    def archive(self, token: TokenIdentity, reason: str) -> WatchlistChange:
        """Move a token to the archived tier, keeping its history for learning."""
        self.update_watchlist(token, WatchlistTier.ARCHIVED)  # DB side effect only
        self.add_journal(token, "outcome", f"archived: {reason}")
        return WatchlistChange(token, "archived", WatchlistTier.ARCHIVED, reason)

    # ---- Security facts baseline (Part 18, Section 10) ----

    def latest_security_facts(self, token: TokenIdentity) -> dict | None:
        """The last-known security facts for a token, or None on first sighting."""
        row = self._conn.execute(
            """SELECT f.facts FROM security_facts f JOIN tokens t ON t.id = f.token_id
               WHERE t.chain = ? AND t.address = ?""",
            (token.chain, token.address),
        ).fetchone()
        return json.loads(row["facts"]) if row else None

    def record_security_facts(self, token: TokenIdentity, facts: dict) -> None:
        """Persist the merged security-fact baseline for future diffs."""
        token_id = self.upsert_token(token)
        self._conn.execute(
            """INSERT INTO security_facts (token_id, updated_at, facts)
               VALUES (?, ?, ?)
               ON CONFLICT (token_id) DO UPDATE SET
                   updated_at = excluded.updated_at, facts = excluded.facts""",
            (token_id, self._now().isoformat(), json.dumps(facts)),
        )
        self._conn.commit()

    # ---- Wallet sightings (Part 17, Sections 2-3) ----

    def record_wallet_sightings(
        self, token: TokenIdentity, sightings: list[tuple[str, str, float | None]],
    ) -> int:
        """Record observed wallet actions: (wallet, side, usd_value) tuples.

        This is the raw feed for wallet reputation: once Part 24's outcome
        tracking labels tokens as winners/losers, each wallet's recorded
        entries become a measurable track record.
        """
        token_id = self.upsert_token(token)
        now = self._now().isoformat()
        self._conn.executemany(
            """INSERT INTO wallet_sightings (wallet, chain, token_id, side, usd_value, seen_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(wallet, token.chain, token_id, side, usd, now)
             for wallet, side, usd in sightings],
        )
        self._conn.commit()
        return len(sightings)

    def wallet_history(self, wallet: str, limit: int = 100) -> list[dict]:
        """A wallet's recorded sightings across tokens, newest first."""
        rows = self._conn.execute(
            """SELECT t.chain, t.address, t.symbol, s.side, s.usd_value, s.seen_at
               FROM wallet_sightings s JOIN tokens t ON t.id = s.token_id
               WHERE s.wallet = ? ORDER BY s.seen_at DESC LIMIT ?""",
            (wallet, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def wallets_seen_on(self, token: TokenIdentity) -> list[str]:
        rows = self._conn.execute(
            """SELECT DISTINCT s.wallet
               FROM wallet_sightings s JOIN tokens t ON t.id = s.token_id
               WHERE t.chain = ? AND t.address = ?""",
            (token.chain, token.address),
        ).fetchall()
        return [row["wallet"] for row in rows]

    # ---- Research journal (Part 11, Section 8) ----

    def add_journal(self, token: TokenIdentity | None, kind: str, content: str) -> int:
        token_id = self.upsert_token(token) if token is not None else None
        cursor = self._conn.execute(
            "INSERT INTO journal (token_id, created_at, kind, content) VALUES (?, ?, ?, ?)",
            (token_id, self._now().isoformat(), kind, content),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def journal_entries(self, token: TokenIdentity | None = None, limit: int = 50) -> list[dict]:
        if token is None:
            rows = self._conn.execute(
                "SELECT created_at, kind, content FROM journal ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT j.created_at, j.kind, j.content
                   FROM journal j JOIN tokens t ON t.id = j.token_id
                   WHERE t.chain = ? AND t.address = ?
                   ORDER BY j.id DESC LIMIT ?""",
                (token.chain, token.address, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- Alert history & performance (Part 29, Sections 11-12) ----

    # Preference order for the one representative score stamped on an alert.
    # 'or' on the raw dict values would treat a legitimate 0.0 score as
    # missing (falsy); explicit None checks below avoid that, and checking
    # every key AutomationRules actually uses means risk-only alert types
    # (emergency_review, whale_exit, ...) get a real score instead of a
    # permanent NULL that silently excludes them from alert_performance()
    # and alerts_with_drift() (Rule 8).
    _SCORE_KEY_PREFERENCE = ("master", "overall", "security", "smart_money",
                             "community", "momentum")

    @classmethod
    def _score_at_alert(cls, scores: dict) -> float | None:
        for key in cls._SCORE_KEY_PREFERENCE:
            value = scores.get(key)
            if value is not None:
                return value
        return None

    def record_alert(self, event, source: str) -> int:
        """Persist one delivered alert (Part 29, Section 11).

        ``event`` is an :class:`~meme_intelligence.alerts.notification_engine.AlertEvent`
        (duck-typed to avoid an alerts->database->alerts import cycle).
        """
        token_id = self.upsert_token(event.token)
        cursor = self._conn.execute(
            """INSERT INTO alerts
               (token_id, created_at, priority, alert_type, title, reasons,
                scores, score_at_alert, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token_id, self._now().isoformat(), event.priority.value,
             event.alert_type, event.title, json.dumps(list(event.reasons)),
             json.dumps({k: v for k, v in event.scores.items()}),
             self._score_at_alert(event.scores),
             source),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def alert_history(self, token: TokenIdentity | None = None, limit: int = 50) -> list[dict]:
        """Recent alerts, newest first (Part 29, Section 11).

        Each row includes the recorded evidence ``reasons`` (parsed from
        JSON; veto/downgrade reasons live there), added for the /why
        Telegram command (Project 2) — purely additive for older callers.
        """
        if token is None:
            rows = self._conn.execute(
                """SELECT a.id, a.created_at, a.priority, a.alert_type, a.title,
                          a.reasons, a.score_at_alert, a.outcome,
                          t.chain, t.address, t.symbol
                   FROM alerts a JOIN tokens t ON t.id = a.token_id
                   ORDER BY a.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT a.id, a.created_at, a.priority, a.alert_type, a.title,
                          a.reasons, a.score_at_alert, a.outcome,
                          t.chain, t.address, t.symbol
                   FROM alerts a JOIN tokens t ON t.id = a.token_id
                   WHERE t.chain = ? AND t.address = ?
                   ORDER BY a.id DESC LIMIT ?""",
                (token.chain, token.address, limit),
            ).fetchall()
        history = []
        for row in rows:
            entry = dict(row)
            try:
                entry["reasons"] = list(json.loads(entry.get("reasons") or "[]"))
            except (TypeError, ValueError):
                entry["reasons"] = []
            history.append(entry)
        return history

    # ---- Operator holdings (Project 2, ROADMAP #2) ----

    def set_holding(self, token: TokenIdentity, note: str | None = None) -> bool:
        """Mark a coin the operator actually bought. Returns True when newly
        added, False when an active holding already exists (idempotent)."""
        token_id = self.upsert_token(token)
        existing = self._conn.execute(
            "SELECT id FROM holdings WHERE token_id = ? AND active = 1", (token_id,)
        ).fetchone()
        if existing is not None:
            return False
        self._conn.execute(
            "INSERT INTO holdings (token_id, acquired_at, active, note) VALUES (?, ?, 1, ?)",
            (token_id, self._now().isoformat(), note),
        )
        self._conn.commit()
        self._logger.info("holding recorded: %s (%s)", token.symbol or token.address,
                          token.chain)
        return True

    def release_holding(self, token: TokenIdentity) -> bool:
        """Unmark a holding (/unhold). Returns True when an active holding
        was found and released, False when there was nothing to release."""
        token_id = self.upsert_token(token)
        cursor = self._conn.execute(
            "UPDATE holdings SET active = 0, released_at = ? "
            "WHERE token_id = ? AND active = 1",
            (self._now().isoformat(), token_id),
        )
        self._conn.commit()
        released = cursor.rowcount > 0
        if released:
            self._logger.info("holding released: %s (%s)",
                              token.symbol or token.address, token.chain)
        return released

    _HOLDINGS_SELECT = (
        "SELECT t.chain, t.address, t.symbol, t.name, "
        "       h.acquired_at, h.released_at, h.active, h.note, "
        "       (SELECT s.final_score FROM snapshots s "
        "        WHERE s.token_id = h.token_id "
        "        ORDER BY s.created_at DESC LIMIT 1) AS last_score "
        "FROM holdings h JOIN tokens t ON t.id = h.token_id"
    )

    def get_holdings(self, active_only: bool = True) -> list[dict]:
        """Holdings joined with token identity + latest known master score."""
        if active_only:
            sql = self._HOLDINGS_SELECT + " WHERE h.active = 1 ORDER BY h.acquired_at DESC"
        else:
            sql = self._HOLDINGS_SELECT + " ORDER BY h.acquired_at DESC"
        rows = self._conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def is_holding(self, token: TokenIdentity) -> bool:
        """Held by ADDRESS, on any chain (Project 2 fix): the operator marks
        a holding by pasting an address, and an unscanned EVM address can't
        have its chain inferred reliably — so a hold set under a guessed
        chain must still be recognized when the scanner sees the real one.
        Address formats don't collide across Solana (base58) and EVM (0x)."""
        row = self._conn.execute(
            "SELECT 1 FROM holdings h JOIN tokens t ON t.id = h.token_id "
            "WHERE h.active = 1 AND t.address = ? LIMIT 1", (token.address,)
        ).fetchone()
        if row is None and token.address.lower().startswith("0x"):
            row = self._conn.execute(
                "SELECT 1 FROM holdings h JOIN tokens t ON t.id = h.token_id "
                "WHERE h.active = 1 AND lower(t.address) = lower(?) LIMIT 1",
                (token.address,)).fetchone()
        return row is not None

    # ---- Muted tokens (Project 2) ----

    def mute_token(self, token: TokenIdentity) -> bool:
        """Suppress all alert delivery for a token. True when newly muted."""
        token_id = self.upsert_token(token)
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO muted_tokens (token_id, muted_at) VALUES (?, ?)",
            (token_id, self._now().isoformat()),
        )
        self._conn.commit()
        muted = cursor.rowcount > 0
        if muted:
            self._logger.info("token muted: %s (%s)",
                              token.symbol or token.address, token.chain)
        return muted

    def unmute_token(self, token: TokenIdentity) -> bool:
        """Restore alert delivery. True when the token was actually muted."""
        token_id = self.upsert_token(token)
        cursor = self._conn.execute(
            "DELETE FROM muted_tokens WHERE token_id = ?", (token_id,))
        self._conn.commit()
        unmuted = cursor.rowcount > 0
        if unmuted:
            self._logger.info("token unmuted: %s (%s)",
                              token.symbol or token.address, token.chain)
        return unmuted

    def is_muted(self, token: TokenIdentity) -> bool:
        """Muted by ADDRESS, on any chain (Project 2 fix) — same reasoning as
        :meth:`is_holding`: a mute set under an inferred chain must still
        suppress alerts when the scanner analyzes the token on its real one."""
        row = self._conn.execute(
            "SELECT 1 FROM muted_tokens m JOIN tokens t ON t.id = m.token_id "
            "WHERE t.address = ? LIMIT 1", (token.address,)
        ).fetchone()
        if row is None and token.address.lower().startswith("0x"):
            row = self._conn.execute(
                "SELECT 1 FROM muted_tokens m JOIN tokens t ON t.id = m.token_id "
                "WHERE lower(t.address) = lower(?) LIMIT 1", (token.address,)).fetchone()
        return row is not None

    def muted_list(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT t.chain, t.address, t.symbol, m.muted_at
               FROM muted_tokens m JOIN tokens t ON t.id = m.token_id
               ORDER BY m.muted_at DESC""",
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- Operator feedback (Project 2; advisory only, Rule 8) ----

    def record_feedback(self, token: TokenIdentity, verdict: str,
                        alert_id: int | None = None) -> int:
        """Store one thumbs up/down. ADVISORY ONLY — never written into
        alerts.outcome and never fed into learning ground-truth labels
        (operator opinion is not a measured outcome, Rule 8)."""
        if verdict not in ("up", "down"):
            raise ValueError(f"feedback verdict must be 'up' or 'down', got {verdict!r}")
        token_id = self.upsert_token(token)
        cursor = self._conn.execute(
            """INSERT INTO operator_feedback (token_id, alert_id, verdict, created_at)
               VALUES (?, ?, ?, ?)""",
            (token_id, alert_id, verdict, self._now().isoformat()),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def feedback_summary(self) -> dict:
        """Total thumbs up/down counts, for the /mind report card."""
        summary = {"up": 0, "down": 0}
        for row in self._conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM operator_feedback GROUP BY verdict"):
            summary[row["verdict"]] = int(row["n"])
        return summary

    def feedback_for_token(self, token: TokenIdentity) -> list[dict]:
        rows = self._conn.execute(
            """SELECT f.verdict, f.created_at, f.alert_id
               FROM operator_feedback f JOIN tokens t ON t.id = f.token_id
               WHERE t.chain = ? AND t.address = ?
               ORDER BY f.id DESC""",
            (token.chain, token.address),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- Prediction outcomes (Part 24, Sections 2-3) ----

    def predictions(self, *, with_price_only: bool = True) -> list[dict]:
        """The FIRST snapshot per token — the moment the framework made its
        call (Part 24 S3). Later snapshots are re-assessments, not new
        predictions. ``with_price_only`` keeps rows measurable (a prediction
        without a stored price cannot have a price outcome — honest gap)."""
        base = (
            "SELECT s.id AS snapshot_id, s.token_id, s.created_at, s.final_score, "
            "       s.classification, s.confidence, s.coverage, s.category_scores, "
            "       s.price_usd, s.liquidity_usd, s.regime, "
            "       t.chain, t.address, t.symbol "
            "FROM snapshots s "
            "JOIN tokens t ON t.id = s.token_id "
            "WHERE s.id = (SELECT MIN(s2.id) FROM snapshots s2 "
            "              WHERE s2.token_id = s.token_id)"
        )
        if with_price_only:
            sql = base + " AND s.price_usd IS NOT NULL ORDER BY s.created_at"
        else:
            sql = base + " ORDER BY s.created_at"
        rows = self._conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def snapshots_for_token(self, token_id: int) -> list[dict]:
        rows = self._conn.execute(
            """SELECT id, created_at, final_score, price_usd, liquidity_usd
               FROM snapshots WHERE token_id = ? ORDER BY created_at""",
            (token_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_outcome(
        self, *, snapshot_id: int, token_id: int, window_hours: float,
        target_at: str, measured_at: str, price_usd: float | None,
        price_change_percent: float | None, liquidity_usd: float | None,
        survived: bool | None, source: str,
    ) -> None:
        """Insert one measured outcome.

        Re-measuring a window that already holds a real return is a no-op —
        the first honest measurement of a window stands (Part 24 S14). The
        one exception is a row whose ``price_change_percent`` is NULL: that
        is an UNMEASURABLE attempt (the implausible-return guard), and since
        the usual cause is a transient bad provider tick, a later good
        reading must be able to fill the hole. Without this a single glitch
        during one cron run would permanently delete that window's evidence
        — including a real winner's (review finding, 2026-07-29).
        """
        self._conn.execute(
            """INSERT INTO outcomes
               (snapshot_id, token_id, window_hours, target_at, measured_at,
                price_usd, price_change_percent, liquidity_usd, survived, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(snapshot_id, window_hours) DO UPDATE SET
                   measured_at = excluded.measured_at,
                   price_usd = excluded.price_usd,
                   price_change_percent = excluded.price_change_percent,
                   liquidity_usd = excluded.liquidity_usd,
                   survived = excluded.survived,
                   source = excluded.source
               WHERE outcomes.price_change_percent IS NULL
                 AND excluded.price_change_percent IS NOT NULL""",
            (snapshot_id, token_id, window_hours, target_at, measured_at,
             price_usd, price_change_percent, liquidity_usd,
             None if survived is None else int(survived), source),
        )
        self._conn.commit()

    def outcomes_for_snapshot(self, snapshot_id: int) -> dict[float, dict]:
        rows = self._conn.execute(
            "SELECT * FROM outcomes WHERE snapshot_id = ? ORDER BY window_hours",
            (snapshot_id,),
        ).fetchall()
        return {row["window_hours"]: dict(row) for row in rows}

    def alerts_with_drift(self) -> list[dict]:
        """Every alert with the master-score drift to the latest later
        snapshot (NULL drift when no re-assessment happened yet)."""
        rows = self._conn.execute(
            """SELECT a.id, a.alert_type, a.priority, a.outcome, a.created_at,
                      (SELECT s.final_score FROM snapshots s
                       WHERE s.token_id = a.token_id AND s.created_at > a.created_at
                       ORDER BY s.created_at DESC LIMIT 1) - a.score_at_alert AS drift
               FROM alerts a
               WHERE a.score_at_alert IS NOT NULL
               ORDER BY a.id""",
        ).fetchall()
        return [dict(row) for row in rows]

    def set_alert_outcome(self, alert_id: int, outcome: str) -> None:
        """Label an alert after measurement (Part 29 S11 / Part 24 S10)."""
        self._conn.execute("UPDATE alerts SET outcome = ? WHERE id = ?",
                           (outcome, alert_id))
        self._conn.commit()

    def alert_performance(self, *, min_followups: int = 1) -> list[dict]:
        """Per-alert-type outcome measurement (Part 29, Section 12).

        For every alert, the token's master score at alert time is compared
        with its latest snapshot afterwards. Positive average drift means
        the alert type tends to precede improvement (useful); near zero
        means noise; negative means it precedes deterioration — which for
        risk alerts is the alert WORKING. Interpretation stays with the
        reader; this reports the measurements (Rule 8).
        """
        rows = self._conn.execute(
            """SELECT a.alert_type,
                      COUNT(*) AS alerts_measured,
                      AVG(s.final_score - a.score_at_alert) AS avg_score_drift,
                      SUM(CASE WHEN s.final_score > a.score_at_alert THEN 1 ELSE 0 END)
                          AS improved_count
               FROM alerts a
               JOIN tokens t ON t.id = a.token_id
               JOIN snapshots s ON s.id = (
                   SELECT s2.id FROM snapshots s2
                   WHERE s2.token_id = a.token_id AND s2.created_at > a.created_at
                   ORDER BY s2.created_at DESC LIMIT 1
               )
               WHERE a.score_at_alert IS NOT NULL
               GROUP BY a.alert_type
               HAVING COUNT(*) >= ?
               ORDER BY alerts_measured DESC""",
            (min_followups,),
        ).fetchall()
        return [dict(row) for row in rows]
