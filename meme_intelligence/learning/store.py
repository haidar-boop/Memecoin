"""SQLite persistence for the mind layer (Section 9).

Everything the learning layer accumulates survives restarts so learning
compounds across runs: coin lifecycle records, their full snapshot series,
resolved outcome labels, fired rug signals, the growing deployer blacklist,
and the self-evaluation metrics history. The FAISS index and the LightGBM /
scaler artifacts are persisted separately to disk (they are not SQL data);
this store is the labeled-record + metrics backbone they are rebuilt from.

Conventions mirror :mod:`meme_intelligence.database.storage` deliberately
(Rule 5 / Rule 18): WAL mode for safe concurrent monitor + cron access, a
single ``_SCHEMA`` script, synchronous calls from the event-loop thread, and
an injectable clock for deterministic tests. This is a *separate* database
file from the research desk's so the mind layer stays modular (Rule 4) and
its heavy write cadence never contends with the main tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from meme_intelligence.core.logging_setup import get_logger
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.learning.models import (
    CoinRecord,
    CoinSnapshot,
    OutcomeBucket,
    OutcomeLabel,
    RugSignal,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learning_coins (
    id INTEGER PRIMARY KEY,
    chain TEXT NOT NULL,
    address TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    detected_at TEXT NOT NULL,
    detection_price_usd REAL,
    creator TEXT,
    final_bucket TEXT,              -- NULL until resolved (Section 1)
    updated_at TEXT NOT NULL,
    deployer_counted INTEGER NOT NULL DEFAULT 0,  -- blacklist counted once per coin
    UNIQUE (chain, address)
);

CREATE TABLE IF NOT EXISTS learning_snapshots (
    id INTEGER PRIMARY KEY,
    coin_id INTEGER NOT NULL REFERENCES learning_coins(id),
    age_seconds REAL NOT NULL,
    captured_at TEXT,
    data TEXT NOT NULL              -- JSON of the CoinSnapshot fields
);
CREATE INDEX IF NOT EXISTS idx_learning_snap_coin
    ON learning_snapshots(coin_id, age_seconds);

CREATE TABLE IF NOT EXISTS learning_labels (
    id INTEGER PRIMARY KEY,
    coin_id INTEGER NOT NULL REFERENCES learning_coins(id),
    horizon_hours REAL NOT NULL,
    bucket TEXT NOT NULL,
    forward_return_percent REAL,
    resolved_at TEXT,
    UNIQUE (coin_id, horizon_hours)
);
CREATE INDEX IF NOT EXISTS idx_learning_labels_coin ON learning_labels(coin_id);

CREATE TABLE IF NOT EXISTS learning_rug_signals (
    id INTEGER PRIMARY KEY,
    coin_id INTEGER NOT NULL REFERENCES learning_coins(id),
    name TEXT NOT NULL,
    points REAL NOT NULL,
    detail TEXT,
    fired_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learning_rug_coin ON learning_rug_signals(coin_id);

-- Deployer reputation (Section 5a / Section 7): every confirmed rug's creator
-- feeds this, and it is checked on every new coin from the same wallet.
CREATE TABLE IF NOT EXISTS deployer_blacklist (
    creator TEXT NOT NULL,
    chain TEXT NOT NULL,
    rug_count INTEGER NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (creator, chain)
);

-- Self-evaluation metrics over rolling windows (Section 8), one row per
-- computed snapshot of the learning progress.
CREATE TABLE IF NOT EXISTS learning_metrics (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    window TEXT NOT NULL,          -- e.g. "last_200" / "all"
    payload TEXT NOT NULL          -- JSON metrics object
);
CREATE INDEX IF NOT EXISTS idx_learning_metrics_win
    ON learning_metrics(window, created_at);

-- The verdict recorded at a coin's FIRST evaluation (Section 8 / Section 10):
-- kept so that once the coin resolves, the prediction can be graded and the
-- ensemble's per-source accuracy updated. The first prediction is the one
-- graded (like the Part 24 backtester), so this is insert-once per coin.
CREATE TABLE IF NOT EXISTS learning_predictions (
    coin_id INTEGER PRIMARY KEY REFERENCES learning_coins(id),
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL          -- JSON: distribution, source labels, archetype, ...
);
"""


class LearningStore:
    """SQLite-backed persistence for the self-learning layer."""

    def __init__(
        self,
        path: str,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._now = now_func
        self._logger = get_logger("learning.store")
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: retrain_if_due (learning/service.py) is run
        # via asyncio.to_thread by the controller specifically so the CPU-bound
        # retrain never blocks the event loop -- but that moves it to a
        # different OS thread than the one that opened this connection.
        # sqlite3's default same-thread check has nothing to do with actual
        # safety here: the system SQLite library is built serialized
        # (thread-safe internally), which is exactly what this flag is for.
        # Without it, every retrain raised "SQLite objects created in a
        # thread can only be used in that same thread" and silently never
        # ran (bug-hunt finding, 2026-07-21 -- confirmed firing on every
        # monitor cycle in production logs).
        self._conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add post-release columns to databases from older builds (Rule 18)."""
        existing = {row["name"] for row in
                    self._conn.execute("PRAGMA table_info(learning_coins)")}
        if "deployer_counted" not in existing:
            try:
                self._conn.execute(
                    "ALTER TABLE learning_coins ADD COLUMN "
                    "deployer_counted INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError as exc:
                # Two processes migrating a pre-upgrade file at once: the
                # loser hits "duplicate column", which means it already
                # happened (same pattern as database/storage.py).
                if "duplicate column" not in str(exc).lower():
                    raise

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LearningStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- Coin lifecycle ----

    def record_detection(
        self,
        token: TokenIdentity,
        *,
        detected_at: datetime | None = None,
        detection_price_usd: float | None = None,
        creator: str | None = None,
    ) -> int:
        """Create (or refresh) a coin record; returns its row id (Section 1).

        Idempotent on (chain, address): a coin already being tracked keeps
        its original ``detected_at``/``detection_price`` (the anchor for
        forward returns) and only fills in newly-learned identity/creator.
        """
        now = self._now().isoformat()
        detected = (detected_at or self._now()).isoformat()
        self._conn.execute(
            """INSERT INTO learning_coins
               (chain, address, symbol, name, detected_at, detection_price_usd,
                creator, final_bucket, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
               ON CONFLICT (chain, address) DO UPDATE SET
                   symbol = COALESCE(excluded.symbol, learning_coins.symbol),
                   name = COALESCE(excluded.name, learning_coins.name),
                   creator = COALESCE(learning_coins.creator, excluded.creator),
                   detection_price_usd =
                       COALESCE(learning_coins.detection_price_usd,
                                excluded.detection_price_usd),
                   updated_at = excluded.updated_at""",
            (token.chain, token.address, token.symbol, token.name,
             detected, detection_price_usd, creator, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM learning_coins WHERE chain = ? AND address = ?",
            (token.chain, token.address),
        ).fetchone()
        return int(row["id"])

    def coin_id(self, token: TokenIdentity) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM learning_coins WHERE chain = ? AND address = ?",
            (token.chain, token.address),
        ).fetchone()
        return None if row is None else int(row["id"])

    # ---- Trajectory snapshots (Section 1/2) ----

    def append_snapshot(self, coin_id: int, snapshot: CoinSnapshot) -> int:
        captured = snapshot.captured_at.isoformat() if snapshot.captured_at else None
        data = {
            "age_seconds": snapshot.age_seconds,
            "price_usd": snapshot.price_usd,
            "liquidity_usd": snapshot.liquidity_usd,
            "market_cap_usd": snapshot.market_cap_usd,
            "volume_5m_usd": snapshot.volume_5m_usd,
            "volume_1h_usd": snapshot.volume_1h_usd,
            "holder_count": snapshot.holder_count,
            "buys": snapshot.buys,
            "sells": snapshot.sells,
            "top10_holder_percent": snapshot.top10_holder_percent,
            "dev_outflow_usd": snapshot.dev_outflow_usd,
            "liquidity_event_usd": snapshot.liquidity_event_usd,
        }
        cursor = self._conn.execute(
            """INSERT INTO learning_snapshots (coin_id, age_seconds, captured_at, data)
               VALUES (?, ?, ?, ?)""",
            (coin_id, snapshot.age_seconds, captured, json.dumps(data)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def snapshots_for(self, coin_id: int, *, limit: int | None = None) -> list[CoinSnapshot]:
        """The coin's trajectory, oldest first.

        ``limit`` keeps only the ``limit`` MOST RECENT snapshots (still
        returned oldest-first). Used by live evaluation to bound the series
        a long-tracked coin can accumulate — the recent arc is what carries
        slope/acceleration signal, and an unbounded read on a coin watched
        for days would grow without limit on a 1 GB droplet.
        """
        if limit is not None and limit <= 0:
            return []
        if limit is None:
            rows = self._conn.execute(
                """SELECT data FROM learning_snapshots
                   WHERE coin_id = ? ORDER BY age_seconds""",
                (coin_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT data FROM learning_snapshots
                   WHERE coin_id = ? ORDER BY age_seconds DESC LIMIT ?""",
                (coin_id, int(limit)),
            ).fetchall()
            rows = list(reversed(rows))
        return [CoinSnapshot.from_dict(json.loads(row["data"])) for row in rows]

    # ---- Outcome labels (Section 1) ----

    def record_label(self, coin_id: int, label: OutcomeLabel) -> None:
        """Persist a resolved horizon label and refresh the coin's final bucket.

        Re-resolving the same horizon overwrites it (a later, more complete
        measurement wins); the coin's ``final_bucket`` is recomputed from the
        full label set so RUG always dominates (Section 1).
        """
        resolved = label.resolved_at.isoformat() if label.resolved_at else self._now().isoformat()
        self._conn.execute(
            """INSERT INTO learning_labels
               (coin_id, horizon_hours, bucket, forward_return_percent, resolved_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (coin_id, horizon_hours) DO UPDATE SET
                   bucket = excluded.bucket,
                   forward_return_percent = excluded.forward_return_percent,
                   resolved_at = excluded.resolved_at""",
            (coin_id, label.horizon_hours, label.bucket.value,
             label.forward_return_percent, resolved),
        )
        self._refresh_final_bucket(coin_id)
        self._conn.commit()

    def _refresh_final_bucket(self, coin_id: int) -> None:
        rows = self._conn.execute(
            """SELECT horizon_hours, bucket FROM learning_labels
               WHERE coin_id = ? ORDER BY horizon_hours""",
            (coin_id,),
        ).fetchall()
        buckets = [(r["horizon_hours"], OutcomeBucket(r["bucket"])) for r in rows]
        resolved = [(h, b) for h, b in buckets if b.is_resolved]
        if not resolved:
            final: str | None = None
        elif any(b is OutcomeBucket.RUG for _, b in resolved):
            final = OutcomeBucket.RUG.value
        else:
            final = max(resolved, key=lambda hb: hb[0])[1].value
        self._conn.execute(
            "UPDATE learning_coins SET final_bucket = ?, updated_at = ? WHERE id = ?",
            (final, self._now().isoformat(), coin_id),
        )

    # ---- Rug signals (Section 5a) ----

    def record_rug_signals(self, coin_id: int, signals: list[RugSignal]) -> int:
        if not signals:
            return 0
        now = self._now().isoformat()
        self._conn.executemany(
            """INSERT INTO learning_rug_signals (coin_id, name, points, detail, fired_at)
               VALUES (?, ?, ?, ?, ?)""",
            [(coin_id, s.name, s.points, s.detail, now) for s in signals],
        )
        self._conn.commit()
        return len(signals)

    def rug_signals_for(self, coin_id: int) -> list[RugSignal]:
        rows = self._conn.execute(
            "SELECT name, points, detail FROM learning_rug_signals WHERE coin_id = ?",
            (coin_id,),
        ).fetchall()
        return [RugSignal(name=r["name"], points=r["points"], detail=r["detail"])
                for r in rows]

    # ---- Deployer blacklist (Section 5a / Section 7) ----

    def blacklist_deployer(self, creator: str, chain: str) -> int:
        """Record a confirmed rug for a creator wallet; returns new rug count."""
        now = self._now().isoformat()
        self._conn.execute(
            """INSERT INTO deployer_blacklist (creator, chain, rug_count, last_seen)
               VALUES (?, ?, 1, ?)
               ON CONFLICT (creator, chain) DO UPDATE SET
                   rug_count = deployer_blacklist.rug_count + 1,
                   last_seen = excluded.last_seen""",
            (creator, chain, now),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT rug_count FROM deployer_blacklist WHERE creator = ? AND chain = ?",
            (creator, chain),
        ).fetchone()
        return int(row["rug_count"])

    def mark_deployer_counted(self, coin_id: int) -> bool:
        """Atomically claim the one-time deployer-blacklist count for a coin.

        Returns True exactly once per coin even when two resolution passes
        race (an overlapping backtest cron + a manual `backtest` run both
        seeing the coin unresolved): the single UPDATE with the guard in its
        WHERE clause is atomic in SQLite, so exactly one caller wins — the
        blacklist can never durably record two rugs for one rug event.
        """
        cursor = self._conn.execute(
            "UPDATE learning_coins SET deployer_counted = 1 "
            "WHERE id = ? AND deployer_counted = 0", (coin_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def creator_of(self, token: TokenIdentity) -> str | None:
        """The recorded deployer wallet of a coin the bot has seen, if any.

        Read-only lookup for the /dev Telegram command (operator request,
        2026-07-28) — the creator is captured at detection time from the
        launch stream / security profile; a coin the bot never watched (or
        whose creator the providers did not expose) honestly returns None.
        """
        row = self._conn.execute(
            "SELECT creator FROM learning_coins WHERE chain = ? AND address = ?",
            (token.chain, token.address),
        ).fetchone()
        return row["creator"] if row is not None and row["creator"] else None

    def coins_by_creator(self, creator: str, chain: str,
                         *, limit: int = 30) -> list[dict]:
        """Every coin the bot watched from one deployer wallet, newest first.

        Lightweight rows (no snapshot loading) for the /dev rap-sheet card:
        symbol/address, resolved outcome bucket (None = still unresolved),
        and when the bot first saw it. Read-only.
        """
        rows = self._conn.execute(
            """SELECT address, symbol, final_bucket, detected_at
               FROM learning_coins WHERE creator = ? AND chain = ?
               ORDER BY detected_at DESC LIMIT ?""",
            (creator, chain, int(limit)),
        ).fetchall()
        return [{"address": r["address"], "symbol": r["symbol"],
                 "final_bucket": (OutcomeBucket(r["final_bucket"])
                                  if r["final_bucket"] else None),
                 "detected_at": r["detected_at"]} for r in rows]

    def blacklist_entry(self, creator: str, chain: str) -> tuple[int, str] | None:
        """(rug_count, last_seen) from the confirmed-rug blacklist, or None."""
        row = self._conn.execute(
            "SELECT rug_count, last_seen FROM deployer_blacklist "
            "WHERE creator = ? AND chain = ?",
            (creator, chain),
        ).fetchone()
        if row is None:
            return None
        return int(row["rug_count"]), str(row["last_seen"])

    def deployer_rug_count(self, creator: str | None, chain: str) -> int:
        """How many confirmed rugs this creator wallet is linked to (0 if clean)."""
        if not creator:
            return 0
        row = self._conn.execute(
            "SELECT rug_count FROM deployer_blacklist WHERE creator = ? AND chain = ?",
            (creator, chain),
        ).fetchone()
        return 0 if row is None else int(row["rug_count"])

    # ---- Labeled dataset (feeds FAISS + LightGBM) ----

    def resolved_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM learning_coins WHERE final_bucket IS NOT NULL",
        ).fetchone()
        return int(row["n"])

    def resolved_records(self, *, limit: int | None = None,
                         bucket: OutcomeBucket | None = None) -> list[CoinRecord]:
        """Every resolved coin as a full :class:`CoinRecord` (newest first).

        This is the labeled training set the analog index and the classifier
        are (re)built from. Snapshots, labels, and rug signals are loaded per
        coin so the caller gets complete records without extra queries.

        ``bucket`` optionally narrows to coins whose stored final outcome is
        that bucket (e.g. the last N PUMPs for the operator's /winners
        report) — a read-only filter, SQL-side so a rare bucket does not
        require walking the whole table.
        """
        query = ("SELECT id, chain, address, symbol, name, detected_at, "
                 "detection_price_usd, creator FROM learning_coins "
                 "WHERE final_bucket IS NOT NULL")
        params: tuple = ()
        if bucket is not None:
            query += " AND final_bucket = ?"
            params = (bucket.value,)
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = self._conn.execute(query, params).fetchall()
        records: list[CoinRecord] = []
        for row in rows:
            coin_id = int(row["id"])
            token = TokenIdentity(chain=row["chain"], address=row["address"],
                                  symbol=row["symbol"], name=row["name"])
            labels = self._labels_for(coin_id)
            records.append(CoinRecord(
                token=token,
                detected_at=datetime.fromisoformat(row["detected_at"]),
                detection_price_usd=row["detection_price_usd"],
                creator=row["creator"],
                snapshots=tuple(self.snapshots_for(coin_id)),
                labels=tuple(labels),
                rug_signals=tuple(self.rug_signals_for(coin_id)),
            ))
        return records

    def _labels_for(self, coin_id: int) -> list[OutcomeLabel]:
        rows = self._conn.execute(
            """SELECT horizon_hours, bucket, forward_return_percent, resolved_at
               FROM learning_labels WHERE coin_id = ? ORDER BY horizon_hours""",
            (coin_id,),
        ).fetchall()
        labels = []
        for r in rows:
            resolved_at = datetime.fromisoformat(r["resolved_at"]) if r["resolved_at"] else None
            labels.append(OutcomeLabel(
                horizon_hours=r["horizon_hours"],
                bucket=OutcomeBucket(r["bucket"]),
                forward_return_percent=r["forward_return_percent"],
                resolved_at=resolved_at,
            ))
        return labels

    def coin_final_bucket(self, coin_id: int) -> OutcomeBucket | None:
        """The coin's resolved outcome bucket, or None if still unresolved."""
        row = self._conn.execute(
            "SELECT final_bucket FROM learning_coins WHERE id = ?", (coin_id,),
        ).fetchone()
        if row is None or row["final_bucket"] is None:
            return None
        return OutcomeBucket(row["final_bucket"])

    def get_record(self, coin_id: int) -> CoinRecord | None:
        """Full lifecycle record for one coin (resolved or not)."""
        row = self._conn.execute(
            """SELECT id, chain, address, symbol, name, detected_at,
                      detection_price_usd, creator
               FROM learning_coins WHERE id = ?""",
            (coin_id,),
        ).fetchone()
        if row is None:
            return None
        token = TokenIdentity(chain=row["chain"], address=row["address"],
                              symbol=row["symbol"], name=row["name"])
        return CoinRecord(
            token=token,
            detected_at=datetime.fromisoformat(row["detected_at"]),
            detection_price_usd=row["detection_price_usd"],
            creator=row["creator"],
            snapshots=tuple(self.snapshots_for(coin_id)),
            labels=tuple(self._labels_for(coin_id)),
            rug_signals=tuple(self.rug_signals_for(coin_id)),
        )

    def unresolved_coins(self) -> list[tuple[int, TokenIdentity, datetime, float | None]]:
        """Coins still awaiting outcome resolution: (id, token, detected_at, price)."""
        rows = self._conn.execute(
            """SELECT id, chain, address, symbol, name, detected_at, detection_price_usd
               FROM learning_coins WHERE final_bucket IS NULL ORDER BY detected_at""",
        ).fetchall()
        return [
            (int(r["id"]),
             TokenIdentity(chain=r["chain"], address=r["address"],
                           symbol=r["symbol"], name=r["name"]),
             datetime.fromisoformat(r["detected_at"]),
             r["detection_price_usd"])
            for r in rows
        ]

    # ---- Self-evaluation metrics (Section 8) ----

    def record_metrics(self, window: str, payload: dict) -> int:
        cursor = self._conn.execute(
            "INSERT INTO learning_metrics (created_at, window, payload) VALUES (?, ?, ?)",
            (self._now().isoformat(), window, json.dumps(payload)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    # ---- Prediction records (Section 8 / Section 10) ----

    def record_prediction(self, coin_id: int, payload: dict) -> bool:
        """Persist the coin's FIRST evaluation verdict; returns True if stored.

        Insert-once: a re-evaluation of a coin does not overwrite the original
        prediction, because the first call is the one graded against the
        eventual outcome (matches the Part 24 backtester's prediction record).
        """
        cursor = self._conn.execute(
            """INSERT INTO learning_predictions (coin_id, created_at, payload)
               VALUES (?, ?, ?)
               ON CONFLICT (coin_id) DO NOTHING""",
            (coin_id, self._now().isoformat(), json.dumps(payload)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_prediction(self, coin_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM learning_predictions WHERE coin_id = ?", (coin_id,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def update_prediction(self, coin_id: int, payload: dict) -> None:
        """Overwrite a stored prediction payload (e.g. to mark it graded)."""
        self._conn.execute(
            "UPDATE learning_predictions SET payload = ? WHERE coin_id = ?",
            (json.dumps(payload), coin_id),
        )
        self._conn.commit()

    def metrics_history(self, window: str | None = None, limit: int = 50) -> list[dict]:
        if window is None:
            rows = self._conn.execute(
                "SELECT created_at, window, payload FROM learning_metrics "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT created_at, window, payload FROM learning_metrics "
                "WHERE window = ? ORDER BY id DESC LIMIT ?",
                (window, limit),
            ).fetchall()
        return [
            {"created_at": r["created_at"], "window": r["window"],
             "metrics": json.loads(r["payload"])}
            for r in rows
        ]
