"""Read-only: which rug-relevant security facts does the bot actually HAVE?

Replaces deploy/security_cap_impact.py, which measured a coverage cap that has
since been removed. Left in place it became a trap: it compared
Settings.from_env({"MEMEINTEL_SECURITY_MIN_COVERAGE_FOR_FULL_SCORE": "0"})
against Settings.from_env({}), and once the setting was deleted from_env
silently ignored the unknown variable, so both configs were identical and the
script always printed "now BLOCKED: 0 (0%)" — the exact inverse of the 99% it
had measured on the same database. Anyone re-running it would have read that as
empirical clearance to ship the cap back on (adversarial review, 2026-07-29).

What this measures instead is the actual open problem. On 2026-07-29, 499 of the
operator's 500 alerted coins had only 25-49% security coverage: GoPlus returns
no holder or LP data for fresh pump.fun mints, so the two facts that decide a
rug are unknown on essentially every coin, the security score is always just
the contract sub-score, and it passed 100% of buy-side alerts.

This counts, per rug-relevant field, how often the bot actually knows it. That
is the BEFORE for on-chain holder/LP collection; re-run it afterwards and the
"missing" counts should collapse. It is also the honest answer to "is the bot
blind?" — a question no threshold can settle.

Opens the database READ-ONLY. Safe to run against the live monitor.

    python3 deploy/security_evidence_report.py [path/to/meme_intelligence.sqlite3]
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.models import SecurityProfile, TokenIdentity

import dataclasses

BUY = ("high_priority_opportunity", "strong_candidate", "early_opportunity",
       "momentum", "smart_money_accumulation")

#: The facts that actually decide a rug, and which sub-score each one feeds.
DECISIVE = {
    "top_holder_percent": "distribution — one wallet holding the float",
    "top10_holder_percent": "distribution — a coordinated cluster",
    "holder_count": "distribution — is anyone else even here",
    "creator_percent": "developer — how much the deployer kept",
    "lp_locked_percent": "liquidity — can the pool be pulled",
}

CANDIDATES = ("data/meme_intelligence.sqlite3",
              os.path.expanduser("~/meme-intelligence/data/meme_intelligence.sqlite3"))
db = sys.argv[1] if len(sys.argv) > 1 else next(
    (c for c in CANDIDATES if os.path.exists(c)), CANDIDATES[0])
if not os.path.exists(db):
    print(f"No database found at {db}. Run from the repo root, or pass the path.")
    raise SystemExit(1)

con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT DISTINCT t.chain, t.address, f.facts "
    "FROM alerts a JOIN tokens t ON t.id = a.token_id "
    "JOIN security_facts f ON f.token_id = a.token_id "
    f"WHERE a.alert_type IN ({','.join('?' * len(BUY))}) "
    "ORDER BY a.id DESC LIMIT 500", BUY).fetchall()

print(f"reading {db} (read-only)")
print(f"coins you were alerted about, with stored security facts: {len(rows)}\n")
if not rows:
    print("No security_facts rows joined to buy-side alerts — nothing to measure.")
    raise SystemExit

settings = Settings.from_env(env={})
valid = {f.name for f in dataclasses.fields(SecurityProfile)}
known = {k: 0 for k in DECISIVE}
scores, coverages, gated = [], [], 0
GATE = settings.alerts.security

for r in rows:
    try:
        facts = json.loads(r["facts"] or "{}")
    except Exception:
        continue
    for field in DECISIVE:
        if facts.get(field) is not None:
            known[field] += 1
    profile = SecurityProfile(
        token=TokenIdentity(chain=r["chain"], address=r["address"]),
        source="stored_facts",
        **{k: v for k, v in facts.items() if k in valid})
    try:
        sec = SecurityAnalyzer(settings.security, settings.security_weights).assess(profile)
    except Exception:
        continue
    scores.append(sec.overall_score)
    coverages.append(sec.coverage)
    gated += sec.overall_score >= GATE

total = len(rows)
print("does the bot know the facts that decide a rug?\n")
for field, why in DECISIVE.items():
    n = known[field]
    pct = 100 * n / max(total, 1)
    bar = "#" * int(pct / 2.5)
    print(f"  {field:22} {n:>4}/{total} ({pct:5.1f}%) {bar}")
    print(f"  {'':22} {why}")

if scores:
    scores.sort()
    print(f"\nsecurity score : median {scores[len(scores)//2]:.0f}, "
          f"min {scores[0]:.0f}, max {scores[-1]:.0f}")
    print(f"coverage       : median {coverages[len(coverages)//2]:.0%}")
    print(f"cleared the {GATE:.0f} security gate: {gated}/{len(scores)} "
          f"({100 * gated // max(len(scores), 1)}%)")
    print("\nA gate that passes ~everything is not filtering; it is a constant.")
    print("Re-run this after on-chain holder/LP collection lands — the 'missing'")
    print("counts above should collapse and this pass rate should drop.")
