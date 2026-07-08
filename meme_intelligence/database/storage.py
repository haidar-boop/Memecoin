"""SQLite storage for tokens, assessment snapshots, watchlist, and journal.

Backs the daily research workflow (Part 11) and the future learning system
(Part 24): every assessment snapshot is kept so predictions can later be
compared against outcomes. SQLite keeps operations simple and reliable
(Rule 21); this module is the only place that knows the backend, so a
server database can replace it behind the same interface.

The API is synchronous — local SQLite operations are sub-millisecond and
the CLI flows call them between network awaits. The continuous scanner
controller will wrap calls in a thread executor if profiling ever shows
contention.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from meme_intelligence.analyzers.scoring_engine import MasterAssessment
from meme_intelligence.core.enums import Classification, WatchlistTier
from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity

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
"""


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
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

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

    # ---- Assessment snapshots (feeds Part 24 backtesting) ----

    def record_snapshot(self, assessment: MasterAssessment, source: str) -> int:
        token_id = self.upsert_token(assessment.token)
        cursor = self._conn.execute(
            """INSERT INTO snapshots
               (token_id, created_at, final_score, classification, confidence,
                coverage, category_scores, overrides, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        query += " ORDER BY w.tier, w.last_score DESC"
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
        if existing is None:
            self._conn.execute(
                """INSERT INTO watchlist
                   (token_id, tier, thesis, added_at, updated_at, last_score, last_classification)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (token_id, tier.value, thesis, now, now, score, classification_value),
            )
            change = WatchlistChange(token, "added", tier,
                                     f"added at {tier.value}" + (f" (score {score:.0f})" if score is not None else ""))
        else:
            old_tier = WatchlistTier(existing["tier"])
            # COALESCE keeps the last known score/classification when an
            # update (e.g. archival) carries none — history feeds learning.
            self._conn.execute(
                """UPDATE watchlist SET tier = ?, thesis = COALESCE(?, thesis),
                       updated_at = ?,
                       last_score = COALESCE(?, last_score),
                       last_classification = COALESCE(?, last_classification)
                   WHERE token_id = ?""",
                (tier.value, thesis, now, score, classification_value, token_id),
            )
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
        change = self.update_watchlist(token, WatchlistTier.ARCHIVED)
        self.add_journal(token, "outcome", f"archived: {reason}")
        return WatchlistChange(token, "archived", WatchlistTier.ARCHIVED, reason)

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
