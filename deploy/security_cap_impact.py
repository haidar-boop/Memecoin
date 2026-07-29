"""Read-only: exactly how many of YOUR alerts the security coverage cap blocks.

The first probe (``security_score_probe.py``) reported the security SCORE of
delivered alerts. That was the wrong question: the cap keys on COVERAGE, and a
coin scoring 93 is unaffected if it was measured on full evidence but blocked
if it was measured on half. Score alone cannot tell those apart.

This reconstructs each alerted coin's SecurityProfile from the ``security_facts``
table — which stores the very fields the sub-scores are built from — then runs
the REAL SecurityAnalyzer twice, cap disabled and cap enabled, and counts how
many alerts actually change side of the 80.0 gate.

Caveat stated rather than buried: ``security_facts`` holds the LAST known facts
for a token, not the facts as they stood at alert time. For sizing a threshold
that is close enough; for anything else it is not.

Opens the database READ-ONLY. Safe to run against the live monitor.

    python3 deploy/security_cap_impact.py [path/to/meme_intelligence.sqlite3]
"""

import dataclasses
import json
import os
import sqlite3
import sys

# Running a script puts the SCRIPT's directory on sys.path, not the repo root,
# so `import meme_intelligence` fails when this is invoked as
# `python3 deploy/security_cap_impact.py` from the repo root — which is exactly
# how it is meant to be run. Put the repo root first.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.models import SecurityProfile, TokenIdentity

BUY = ("high_priority_opportunity", "strong_candidate", "early_opportunity",
       "momentum", "smart_money_accumulation")
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
print(f"alerted coins with stored security facts: {len(rows)}\n")
if not rows:
    print("No security_facts rows joined to buy-side alerts — cannot size the cap.")
    raise SystemExit

valid = {f.name for f in dataclasses.fields(SecurityProfile)}
uncapped = Settings.from_env(env={"MEMEINTEL_SECURITY_MIN_COVERAGE_FOR_FULL_SCORE": "0"})
capped = Settings.from_env(env={})
GATE = capped.alerts.security

def analyze(profile, settings):
    return SecurityAnalyzer(settings.security, settings.security_weights).assess(profile)

kept = blocked = errors = 0
cov_buckets = {}
examples = []
for r in rows:
    try:
        facts = json.loads(r["facts"] or "{}")
    except Exception:
        errors += 1
        continue
    kwargs = {k: v for k, v in facts.items() if k in valid}
    profile = SecurityProfile(
        token=TokenIdentity(chain=r["chain"], address=r["address"]),
        source="stored_facts", **kwargs)
    try:
        before = analyze(profile, uncapped)
        after = analyze(profile, capped)
    except Exception:
        errors += 1
        continue
    bucket = f"{int(before.coverage * 100) // 25 * 25}-{int(before.coverage * 100) // 25 * 25 + 24}%"
    cov_buckets[bucket] = cov_buckets.get(bucket, 0) + 1
    if before.overall_score >= GATE and after.overall_score < GATE:
        blocked += 1
        if len(examples) < 5:
            examples.append((r["address"][:12], before.overall_score,
                             after.overall_score, before.coverage))
    elif before.overall_score >= GATE:
        kept += 1

total_gated = kept + blocked
print("security COVERAGE of your alerted coins:")
for b in sorted(cov_buckets):
    print(f"  {b:>8} | {'#' * min(40, cov_buckets[b])} {cov_buckets[b]}")

print(f"\nof {total_gated} coins that cleared the {GATE:.0f} security gate before:")
print(f"  still alert : {kept}   ({100 * kept // max(total_gated, 1)}%)")
print(f"  now BLOCKED : {blocked}   ({100 * blocked // max(total_gated, 1)}%)")
if errors:
    print(f"  (skipped {errors} rows that could not be reconstructed)")
if examples:
    print("\nexamples of what gets blocked:")
    for addr, b, a, cov in examples:
        print(f"  {addr}… {b:.0f} -> {a:.1f} at {cov:.0%} evidence")
