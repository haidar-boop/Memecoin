"""Read-only: what have the operator's alerts actually DONE?

The question this exists to answer, in his words: "why in its whole career has it
not found one coin that went to a million, even though many coins went to a
million?"

That premise has to be checked before it is explained, because outcome recording
in this project has been wrong three separate times — provider outages written as
-100% deaths, implausible forward returns taught as PUMPs, and a return guard
that deleted real rugs. "Never found one" and "never RECORDED one" look identical
from the outside and have completely different fixes.

So this reports, from his real database and nothing else:

  1. COVERAGE — how many buy-side alerts ever got an outcome measured at all.
     If this is low, the hit-rate question is a measurement problem and no amount
     of detection work will answer it.
  2. THE ACTUAL BEST RESULTS — the top recorded returns, per horizon. This is the
     direct answer: if the best thing it ever caught was +40%, that is the
     ceiling of what it has found. If something did 30x and nobody noticed, that
     changes the whole conversation.
  3. THE DISTRIBUTION — how the resolved alerts split across loss / flat / gain
     bands, so "win rate" means something specific rather than vibes.
  4. WHY THE UNRESOLVED ONES ARE UNRESOLVED — separated into "too recent to
     measure yet" and "old enough that it should have been measured and wasn't".
     Only the second kind is a bug.
  5. The mind layer's own labels, cross-checked against the main database, since
     the two are written by different code paths and disagreement is itself a
     finding.

Opens both databases READ-ONLY. Safe against the live monitor. Makes no network
calls and costs nothing.

    python3 deploy/alert_outcomes_report.py
    python3 deploy/alert_outcomes_report.py --db path/to/meme_intelligence.sqlite3
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meme_intelligence.config.settings import get_settings

BUY = ("high_priority_opportunity", "strong_candidate", "early_opportunity",
       "momentum", "smart_money_accumulation")

CANDIDATES = ("data/meme_intelligence.sqlite3",
              os.path.expanduser("~/meme-intelligence/data/meme_intelligence.sqlite3"))

#: Return bands. The top band is the question being asked: a coin alerted at a
#: $30k market cap that reaches $1M is roughly +3200%.
BANDS = ((-100.0, -90.0, "wiped out (<= -90%)"),
         (-90.0, -50.0, "heavy loss (-90 to -50%)"),
         (-50.0, -10.0, "loss (-50 to -10%)"),
         (-10.0, 10.0, "flat (-10 to +10%)"),
         (10.0, 50.0, "small gain (+10 to +50%)"),
         (50.0, 200.0, "good (+50 to +200%)"),
         (200.0, 1000.0, "big (+200 to +1000%, 3x-11x)"),
         (1000.0, float("inf"), "MOONSHOT (>= +1000%, 11x+)"))


def q(con, sql, params=()):
    try:
        return con.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        print(f"  (query unavailable: {exc})")
        return []


def hours_since(stamp: str, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() / 3600.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None)
    parser.add_argument("--top", type=int, default=15,
                        help="how many best-performing alerts to list")
    parser.add_argument("--min-liquidity", type=float, default=1000.0,
                        help="USD pool depth required to call a gain realizable")
    args = parser.parse_args()

    db = args.db or next((c for c in CANDIDATES if os.path.exists(c)), CANDIDATES[0])
    if not os.path.exists(db):
        print(f"No database found at {db}. Run from the repo root, or pass --db.")
        return 1

    settings = get_settings()
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    marks = ",".join("?" * len(BUY))

    print(f"reading {db} (read-only)")
    print("=" * 72)

    # ---- 1. Coverage -----------------------------------------------------
    print("\n1. COVERAGE — did anything get measured at all?\n")
    total_alerts = q(con, "SELECT COUNT(*) c FROM alerts")
    buy_alerts = q(con, f"SELECT COUNT(*) c FROM alerts WHERE alert_type IN ({marks})", BUY)
    tokens = q(con, "SELECT COUNT(*) c FROM tokens")
    snaps = q(con, "SELECT COUNT(*) c FROM snapshots")
    outcomes = q(con, "SELECT COUNT(*) c FROM outcomes")
    measured = q(con, "SELECT COUNT(*) c FROM outcomes WHERE price_change_percent IS NOT NULL")

    n_buy = buy_alerts[0]["c"] if buy_alerts else 0
    n_out = outcomes[0]["c"] if outcomes else 0
    n_meas = measured[0]["c"] if measured else 0
    print(f"  tokens ever analysed            {tokens[0]['c'] if tokens else 0:>8}")
    print(f"  snapshots (scored looks)        {snaps[0]['c'] if snaps else 0:>8}")
    print(f"  alerts of every kind           {total_alerts[0]['c'] if total_alerts else 0:>8}")
    print(f"  BUY-SIDE alerts (the pitches)  {n_buy:>8}")
    print(f"  outcome rows                   {n_out:>8}")
    print(f"  outcome rows with a real %     {n_meas:>8}")

    # How many DISTINCT buy-side-alerted tokens have any measured outcome. This
    # is the number that decides whether the hit-rate question is answerable.
    covered = q(con, f"""
        SELECT COUNT(DISTINCT t.id) c FROM tokens t
        JOIN alerts a ON a.token_id = t.id AND a.alert_type IN ({marks})
        JOIN outcomes o ON o.token_id = t.id
        WHERE o.price_change_percent IS NOT NULL""", BUY)
    alerted_tokens = q(con, f"""
        SELECT COUNT(DISTINCT token_id) c FROM alerts WHERE alert_type IN ({marks})""", BUY)
    n_cov = covered[0]["c"] if covered else 0
    n_alerted = alerted_tokens[0]["c"] if alerted_tokens else 0
    print(f"\n  distinct coins ever pitched    {n_alerted:>8}")
    print(f"  ...of those, with a result     {n_cov:>8}"
          f"   ({100.0 * n_cov / n_alerted:.1f}%)" if n_alerted else "")
    if n_alerted and n_cov / n_alerted < 0.25:
        print("\n  >> LOW COVERAGE. Most pitched coins have no recorded result, so")
        print("     'it never found a winner' is currently UNPROVABLE from this")
        print("     database. Fix measurement before touching detection.")

    # ---- 2. The best results ever --------------------------------------
    #
    # Two filters, and BOTH are load-bearing. Without them this section reported
    # "+702,288,997.5% — it HAS found a 7,022,891x" off the operator's real
    # database, which is nonsense and read as good news.
    #
    #   * PLAUSIBILITY. The backtester rejects a return above
    #     `backtest.max_measurable_return_percent` because a bad base price makes
    #     the ratio explode. That guard was only added 2026-07-29, so every row
    #     recorded before it keeps its fabricated value forever. Reporting those
    #     as achievements is the same Rule 8 failure the guard exists to stop.
    #   * REALIZABILITY. A gain you cannot sell into is not a gain. Almost every
    #     one of those fantasy returns sits on `liquidity_usd = 0` — there was no
    #     pool to exit through at any price.
    ceiling = settings.backtest.max_measurable_return_percent
    print("\n" + "=" * 72)
    print("\n2. THE BEST RESULTS EVER RECORDED — the direct answer\n")
    bogus = q(con, f"""
        SELECT COUNT(*) c FROM outcomes o
        WHERE o.price_change_percent IS NOT NULL AND ? > 0
          AND ABS(o.price_change_percent) > ?
          AND EXISTS (SELECT 1 FROM alerts a WHERE a.token_id = o.token_id
                      AND a.alert_type IN ({marks}))""", (ceiling, ceiling, *BUY))
    n_bogus = bogus[0]["c"] if bogus else 0
    if n_bogus:
        print(f"  !! {n_bogus} outcome row(s) exceed the plausibility ceiling of "
              f"{ceiling:,.0f}% and are EXCLUDED below.")
        print("     These predate the guard added 2026-07-29 and are measurement")
        print("     artifacts, not wins — a bad base price makes the ratio explode.")
        print("     They are still in the mind layer's training data.\n")

    best = q(con, f"""
        SELECT t.symbol, t.address, o.window_hours, o.price_change_percent pct,
               o.liquidity_usd liq, o.measured_at, o.source
        FROM outcomes o JOIN tokens t ON t.id = o.token_id
        WHERE o.price_change_percent IS NOT NULL
          AND (? <= 0 OR ABS(o.price_change_percent) <= ?)
          AND EXISTS (SELECT 1 FROM alerts a WHERE a.token_id = t.id
                      AND a.alert_type IN ({marks}))
        ORDER BY o.price_change_percent DESC LIMIT ?""",
             (ceiling, ceiling, *BUY, args.top))
    if not best:
        print("  NOTHING. No buy-side-alerted coin has a plausible measured return.")
    else:
        print(f"  {'symbol':<12} {'mint':<15} {'win':>6} {'return':>12} "
              f"{'liq at measure':>16}  exit?")
        for row in best:
            liq = row["liq"] or 0.0
            exitable = "SELLABLE" if liq >= args.min_liquidity else "no pool — unrealizable"
            print(f"  {(row['symbol'] or '?')[:12]:<12} {row['address'][:14]}… "
                  f"{row['window_hours']:>5.0f}h {row['pct']:>+11.1f}% "
                  f"${liq:>15,.0f}  {exitable}")
        real = [r for r in best if (r["liq"] or 0.0) >= args.min_liquidity]
        print(f"\n  Best plausible return: {best[0]['pct']:+.1f}%")
        if real:
            print(f"  Best return you could ACTUALLY have sold into: "
                  f"{real[0]['pct']:+.1f}% "
                  f"({1 + real[0]['pct'] / 100:.1f}x, "
                  f"${real[0]['liq']:,.0f} liquidity)")
        else:
            print(f"  >> NONE of the top returns had ${args.min_liquidity:,.0f} of")
            print("     liquidity to sell into. On paper wins you could not exit.")

    # ---- 3. Distribution ------------------------------------------------
    print("\n" + "=" * 72)
    print("\n3. DISTRIBUTION of every measured buy-side outcome\n")
    rows = q(con, f"""
        SELECT o.window_hours, o.price_change_percent pct, o.liquidity_usd liq
        FROM outcomes o
        WHERE o.price_change_percent IS NOT NULL
          AND (? <= 0 OR ABS(o.price_change_percent) <= ?)
          AND EXISTS (SELECT 1 FROM alerts a WHERE a.token_id = o.token_id
                      AND a.alert_type IN ({marks}))""", (ceiling, ceiling, *BUY))
    if rows:
        for lo, hi, label in BANDS:
            n = sum(1 for r in rows if lo <= r["pct"] < hi)
            bar = "#" * min(40, int(40.0 * n / max(1, len(rows))))
            print(f"  {label:<32} {n:>6}  ({100.0 * n / len(rows):>5.1f}%) {bar}")
        wins = sum(1 for r in rows if r["pct"] > 0)
        print(f"\n  measured outcomes {len(rows)}, of which above water: {wins} "
              f"({100.0 * wins / len(rows):.1f}%)")
        # The only win that pays is one you can sell. A +300% print against an
        # empty pool is a screenshot, not money.
        sellable = [r for r in rows
                    if r["pct"] > 0 and (r["liq"] or 0.0) >= args.min_liquidity]
        print(f"  ...and of which above water AND sellable "
              f"(>= ${args.min_liquidity:,.0f} pool): {len(sellable)} "
              f"({100.0 * len(sellable) / len(rows):.1f}%)")
        big_sellable = [r for r in sellable if r["pct"] >= 200.0]
        print(f"  ...sellable gains of +200% or better: {len(big_sellable)} "
              f"({100.0 * len(big_sellable) / len(rows):.2f}%)")
        print("  NOTE: one coin can appear several times (one row per horizon).")
        by_window = {}
        for r in rows:
            by_window.setdefault(r["window_hours"], []).append(r["pct"])
        print("\n  by horizon:")
        for window in sorted(by_window):
            vals = by_window[window]
            print(f"    {window:>6.0f}h  n={len(vals):<6} "
                  f"median {sorted(vals)[len(vals) // 2]:>+8.1f}%  "
                  f"best {max(vals):>+9.1f}%")
    else:
        print("  no measured outcomes to distribute")

    # ---- 4. Why the rest are unresolved ---------------------------------
    print("\n" + "=" * 72)
    print("\n4. THE UNMEASURED — bug, or simply too recent?\n")
    pending = q(con, f"""
        SELECT t.symbol, t.address, MAX(a.created_at) last_alert
        FROM alerts a JOIN tokens t ON t.id = a.token_id
        WHERE a.alert_type IN ({marks})
          AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.token_id = t.id
                          AND o.price_change_percent IS NOT NULL)
        GROUP BY t.id ORDER BY last_alert DESC""", BUY)
    if not pending:
        print("  none — every pitched coin has a result. Measurement is healthy.")
    else:
        fresh = stale = 0
        oldest = None
        for row in pending:
            age = hours_since(row["last_alert"], now)
            if age is None:
                continue
            if age < 24.0:
                fresh += 1
            else:
                stale += 1
                oldest = max(oldest or age, age)
        print(f"  pitched coins with NO result       {len(pending):>8}")
        print(f"    alerted < 24h ago (fair enough)  {fresh:>8}")
        print(f"    alerted > 24h ago (should exist) {stale:>8}")
        if oldest:
            print(f"    oldest unmeasured                {oldest / 24.0:>8.1f} days")
        if stale > fresh and stale > 10:
            print("\n  >> THIS IS THE FINDING. Coins pitched days ago still have no")
            print("     measured result, so the bot cannot learn from them and the")
            print("     hit rate cannot be computed. Look at the backtest cron")
            print("     (deploy/install-cron.sh) before anything else.")

    # ---- 5. The mind layer's own view -----------------------------------
    print("\n" + "=" * 72)
    print("\n5. THE MIND LAYER'S OWN LABELS (separate database, separate code)\n")
    learning_db = os.path.join(settings.learning.state_dir, "learning.db")
    if not os.path.exists(learning_db):
        print(f"  no learning database at {learning_db} — mind layer never ran here")
    else:
        lcon = sqlite3.connect(f"file:{learning_db}?mode=ro", uri=True)
        lcon.row_factory = sqlite3.Row
        print(f"  reading {learning_db} (read-only)")
        buckets = q(lcon, """SELECT bucket, horizon_hours, COUNT(*) n,
                                    MAX(forward_return_percent) best
                             FROM learning_labels
                             GROUP BY bucket, horizon_hours
                             ORDER BY horizon_hours, bucket""")
        if not buckets:
            print("  no resolved labels — the mind layer has learned from nothing")
        else:
            for row in buckets:
                best_txt = ("n/a" if row["best"] is None else f"{row['best']:+.1f}%")
                print(f"    {row['horizon_hours']:>6.0f}h  {row['bucket']:<12} "
                      f"n={row['n']:<6} best {best_txt}")
            top = q(lcon, """SELECT c.symbol, c.address, l.horizon_hours,
                                    l.forward_return_percent pct, l.bucket
                             FROM learning_labels l
                             JOIN learning_coins c ON c.id = l.coin_id
                             WHERE l.forward_return_percent IS NOT NULL
                             ORDER BY l.forward_return_percent DESC LIMIT ?""",
                     (args.top,))
            if top:
                print("\n  best forward returns the mind layer has ever seen:")
                for row in top:
                    print(f"    {(row['symbol'] or '?')[:12]:<12} "
                          f"{row['address'][:14]}…  {row['horizon_hours']:>6.0f}h  "
                          f"{row['pct']:>+10.1f}%  {row['bucket']}")
        lcon.close()

    con.close()
    print("\n" + "=" * 72)
    print("\nHOW TO READ THIS:")
    print("  * Section 1 low + section 4 large  -> a MEASUREMENT problem. The bot")
    print("    may well have pitched a winner and never recorded the outcome.")
    print("  * Section 2 best result modest + section 4 small -> a DETECTION or")
    print("    STRATEGY problem: it really has not found one, and the 1-hour")
    print("    freshness window is the first thing to question, because it forbids")
    print("    every buy-side alert on a coin older than 60 minutes -- which is")
    print("    exactly when a real runner becomes identifiable.")
    print("  * Sections 2 and 5 disagreeing -> the two outcome recorders do not")
    print("    agree with each other, which is its own bug and outranks both.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
