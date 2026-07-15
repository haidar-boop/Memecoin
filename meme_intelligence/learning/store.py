"""SQLite persistence for the mind layer (Section 9).

Everything the learning layer accumulates survives restarts so learning
compounds across runs: coin lifecycle records, their full snapshot series,
resolved outcome labels, fired rug signals, the growing deployer blacklist,
and the self-evaluation metrics history. The FAISS index and the LightGBM /
scaler artifacts are persisted separately to disk (they are not SQL data);
this store is the labeled-record + metrics backbone they are rebuilt from.

Conventions mirror :mod:`meme_intelligence.database.storage` deliberately
(Rule 5 / Rule 18): WAL mode for safe concurrent monitor + cron access, a
single ``_SCHEMA`` script, and an injectable clock for deterministic tests.
This is a *separate* database file from the research desk's so the mind
layer stays modular (Rule 4) and its heavy write cadence never contends
with the main tables.

Threading: unlike the research desk's ``Storage`` (event-loop thread only),
this store is deliberately THREAD-SAFE. The scanner runs
``retrain_if_due`` in a worker thread via ``asyncio.to_thread`` — a
synchronous rebuild once stalled an emergency ``/dump`` — and that worker
reads this store while the event-loop thread keeps recording detections
and serving ``/mind``. The connection is opened with
``check_same_thread=False`` and every method holds ``self._lock`` (an
RLock, so composite methods like ``get_record`` can nest their helpers)
for its WHOLE body, keeping each method's multi-statement transaction
atomic rather than interleavable. Without this, every scheduled retrain
died with "SQLite objects created in a thread can only be used in that
same thread" — the classifier never trained once in production
(2026-07-15 droplet journal finding).
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
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


def _locked(method):
    """Hold ``self._lock`` for the method's whole body (see module docstring:
    the retrain worker thread and the event-loop thread share one
    connection; whole-method scope keeps multi-statement transactions
    atomic)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class LearningStore:
    """SQLite-backed persistence for the self-learning layer (thread-safe)."""

    def __init__(
        self,
        path: str,
        *,
        now_func: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._now = now_func
        self._logger = get_logger("learning.store")
        self._lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False is safe ONLY because every access is
        # serialized behind self._lock (@_locked on every method) — the
        # retrain worker thread must be able to read while the event-loop
        # thread owns the connection's birth thread.
        self._conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    @_locked
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

    @_locked
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LearningStore":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- Coin lifecycle ----

    @_locked
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

    @_locked
    def coin_id(self, token: TokenIdentity) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM learning_coins WHERE chain = ? AND address = ?",
            (token.chain, token.address),
        ).fetchone()
        return None if row is None else int(row["id"])

    # ---- Trajectory snapshots (Section 1/2) ----

    @_locked
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

    @_locked
    def snapshots_for(self, coin_id: int) -> list[CoinSnapshot]:
        rows = self._conn.execute(
            """SELECT data FROM learning_snapshots
               WHERE coin_id = ? ORDER BY age_seconds""",
            (coin_id,),
        ).fetchall()
        return [CoinSnapshot.from_dict(json.loads(row["data"])) for row in rows]

    # ---- Outcome labels (Section 1) ----

    @_locked
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

    @_locked
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

    @_locked
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

    @_locked
    def rug_signals_for(self, coin_id: int) -> list[RugSignal]:
        rows = self._conn.execute(
            "SELECT name, points, detail FROM learning_rug_signals WHERE coin_id = ?",
            (coin_id,),
        ).fetchall()
        return [RugSignal(name=r["name"], points=r["points"], detail=r["detail"])
                for r in rows]

    # ---- Deployer blacklist (Section 5a / Section 7) ----

    @_locked
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

    @_locked
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

    @_locked
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

    @_locked
    def resolved_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM learning_coins WHERE final_bucket IS NOT NULL",
        ).fetchone()
        return int(row["n"])

    @_locked
    def resolved_records(self, *, limit: int | None = None) -> list[CoinRecord]:
        """Every resolved coin as a full :class:`CoinRecord` (newest first).

        This is the labeled training set the analog index and the classifier
        are (re)built from. Snapshots, labels, and rug signals are loaded per
        coin so the caller gets complete records without extra queries.
        """
        query = ("SELECT id, chain, address, symbol, name, detected_at, "
                 "detection_price_usd, creator FROM learning_coins "
                 "WHERE final_bucket IS NOT NULL ORDER BY updated_at DESC")
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        rows = self._conn.execute(query).fetchall()
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

    @_locked
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

    @_locked
    def coin_final_bucket(self, coin_id: int) -> OutcomeBucket | None:
        """The coin's resolved outcome bucket, or None if still unresolved."""
        row = self._conn.execute(
            "SELECT final_bucket FROM learning_coins WHERE id = ?", (coin_id,),
        ).fetchone()
        if row is None or row["final_bucket"] is None:
            return None
        return OutcomeBucket(row["final_bucket"])

    @_locked
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

    @_locked
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

    @_locked
    def record_metrics(self, window: str, payload: dict) -> int:
        cursor = self._conn.execute(
            "INSERT INTO learning_metrics (created_at, window, payload) VALUES (?, ?, ?)",
            (self._now().isoformat(), window, json.dumps(payload)),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    # ---- Prediction records (Section 8 / Section 10) ----

    @_locked
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

    @_locked
    def get_prediction(self, coin_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM learning_predictions WHERE coin_id = ?", (coin_id,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    @_locked
    def update_prediction(self, coin_id: int, payload: dict) -> None:
        """Overwrite a stored prediction payload (e.g. to mark it graded)."""
        self._conn.execute(
            "UPDATE learning_predictions SET payload = ? WHERE coin_id = ?",
            (json.dumps(payload), coin_id),
        )
        self._conn.commit()

    @_locked
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
