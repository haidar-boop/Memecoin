"""Read-only: what did SECURITY score on the coins the bot actually sent?

Answers one question with the operator's own data instead of a guess: of the
coins that actually reached his phone, how many scored a PERFECT 100 on
security? A cluster at exactly 100 is the signature of the 2026-07-29 coverage
bug — a fresh mint whose holder/LP facts GoPlus cannot supply is scored on
whichever sub-score did resolve, so `contract` alone becomes the whole score.

Used to size `MEMEINTEL_SECURITY_MIN_COVERAGE_FOR_FULL_SCORE` against reality
rather than judgement. Opens the database READ-ONLY; it cannot alter anything,
and is safe to run while the monitor is live.

    python3 deploy/security_score_probe.py [path/to/meme_intelligence.sqlite3]
"""
import json, sqlite3, sys, collections

db = sys.argv[1] if len(sys.argv) > 1 else "data/meme_intelligence.sqlite3"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

BUY = ("high_priority_opportunity", "strong_candidate", "early_opportunity",
       "momentum", "smart_money_accumulation")
rows = con.execute(
    "SELECT created_at, alert_type, scores FROM alerts "
    f"WHERE alert_type IN ({','.join('?' * len(BUY))}) "
    "ORDER BY id DESC LIMIT 400", BUY).fetchall()

sec, buckets, perfect = [], collections.Counter(), 0
for r in rows:
    try:
        s = json.loads(r["scores"] or "{}")
    except Exception:
        continue
    v = s.get("security")
    if isinstance(v, (int, float)):
        sec.append(v)
        if v >= 99.5:
            perfect += 1
        buckets[min(int(v // 10) * 10, 90)] += 1

print(f"buy-side alerts examined : {len(rows)}")
print(f"with a security score    : {len(sec)}")
if not sec:
    print("\nNo security scores recorded yet — nothing to judge from.")
    raise SystemExit
sec.sort()
print(f"median security score    : {sec[len(sec)//2]:.0f}")
print(f"scored 100 (perfect)     : {perfect}  ({100*perfect/len(sec):.0f}% of them)")
print("\nsecurity score distribution:")
for lo in sorted(buckets):
    n = buckets[lo]
    print(f"  {lo:3d}-{lo+9:3d} | {'#' * min(40, n)} {n}")

# What the new cap would do, using the same formula the code uses.
print("\nIf a coin's security was 100 on 25% coverage it becomes 62.5 (blocked at 80).")
print("Coins already scoring <80 were never gated on security anyway.")
above = sum(1 for v in sec if v >= 80)
print(f"alerts whose security cleared the 80 gate: {above} ({100*above/len(sec):.0f}%)")
