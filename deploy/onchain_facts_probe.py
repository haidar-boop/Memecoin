"""Read-only: what WOULD the on-chain holder/LP collector report, on real coins?

This is the "measure it before you switch it on" step, and it is not optional.
The last two attempts to make the security score stricter were both shipped on
reasoning and both were wrong: a coverage cap measured AFTER the fact would have
blocked 338 of 339 alerted coins — 99% — and taking the bot off the air is the
single worst outcome in this project. `deploy/security_evidence_report.py` is the
BEFORE (which facts are missing today); this is the AFTER (what the new layer
would actually fill in, and what that does to the score).

It changes nothing. It reads the database read-only, replays the recorded GoPlus
facts through the real SecurityAnalyzer, then makes live RPC calls through the
real OnChainSecurityCollector to see what it would add — and prints both scores
side by side. No alerts, no writes, no settings changes.

What to look for before enabling the layer:

  * "would now be BLOCKED" — coins that pass today and would stop passing. A
    handful is the point of the exercise. A large fraction means STOP: read the
    per-coin lines and find out whether those coins are actually bad, or whether
    an exclusion rule is wrong. 99% is what a wrong exclusion set looks like.
  * "concentration unknown" — how often the census cannot answer. On the public
    RPC endpoint this will be ~100%, because getTokenLargestAccounts is disabled
    there (HTTP 429, x-ratelimit-method-limit: 0). Run it with a Helius key set
    or the concentration half tells you nothing.
  * "LP unknown" — expected to be high. The verified burn formula is Raydium AMM
    v4 only, and current pump.fun coins graduate to PumpSwap instead.

Usage (from the repo root, on the droplet):

    python3 deploy/onchain_facts_probe.py                    # 25 most recent alerted coins
    python3 deploy/onchain_facts_probe.py --limit 60
    python3 deploy/onchain_facts_probe.py --db path/to/meme_intelligence.sqlite3

Costs real RPC calls: up to six per coin, so --limit is the spend dial.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meme_intelligence.analyzers.security_analyzer import SecurityAnalyzer
from meme_intelligence.collectors.onchain_security import OnChainSecurityCollector
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.errors import MemeIntelError
from meme_intelligence.core.models import SecurityProfile, TokenIdentity
from meme_intelligence.core.rate_limiter import RateLimiter

BUY = ("high_priority_opportunity", "strong_candidate", "early_opportunity",
       "momentum", "smart_money_accumulation")

CANDIDATES = ("data/meme_intelligence.sqlite3",
              os.path.expanduser("~/meme-intelligence/data/meme_intelligence.sqlite3"))


def load_rows(db: str, limit: int) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT DISTINCT t.chain, t.address, t.symbol, f.facts "
            "FROM alerts a JOIN tokens t ON t.id = a.token_id "
            "JOIN security_facts f ON f.token_id = a.token_id "
            f"WHERE a.alert_type IN ({','.join('?' * len(BUY))}) "
            "AND t.chain IN ('solana', 'sol') "
            "ORDER BY a.id DESC LIMIT ?", (*BUY, limit)).fetchall()
    finally:
        con.close()


def profile_from_row(row: sqlite3.Row) -> SecurityProfile | None:
    """Rebuild the recorded GoPlus profile, ignoring fields the model dropped."""
    try:
        facts = json.loads(row["facts"])
    except (TypeError, ValueError):
        return None
    if not isinstance(facts, dict):
        return None
    known = {f.name for f in dataclasses.fields(SecurityProfile)}
    payload = {k: v for k, v in facts.items() if k in known}
    payload.pop("token", None)
    payload.setdefault("source", "goplus")
    try:
        return SecurityProfile(
            token=TokenIdentity(chain=row["chain"], address=row["address"],
                                symbol=row["symbol"]),
            **payload)
    except TypeError:
        return None


def fmt(value, suffix="%"):
    return "unknown" if value is None else f"{value:.2f}{suffix}"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None)
    parser.add_argument("--limit", type=int, default=25,
                        help="coins to probe (each costs up to 6 RPC calls)")
    args = parser.parse_args()

    db = args.db or next((c for c in CANDIDATES if os.path.exists(c)), CANDIDATES[0])
    if not os.path.exists(db):
        print(f"No database found at {db}. Run from the repo root, or pass --db.")
        return 1

    settings = Settings.from_env()
    gate = settings.alerts.security
    analyzer = SecurityAnalyzer(settings.security, settings.security_weights)

    print(f"reading {db} (read-only)")
    print(f"security gate: {gate:.0f}    "
          f"layer currently {'ENABLED' if settings.onchain_security.enabled else 'OFF'} "
          f"in this environment")
    if not settings.helius_api_key:
        print("!! no MEMEINTEL_HELIUS_API_KEY set — getTokenLargestAccounts is "
              "disabled on public RPC, so CONCENTRATION WILL READ 'unknown' for "
              "every coin. The LP column is still meaningful.")
    print()

    rows = load_rows(db, args.limit)
    if not rows:
        print("No Solana buy-side alerts with recorded security facts found.")
        return 0

    collector = OnChainSecurityCollector(
        settings.onchain_security,
        api_key=settings.helius_api_key,
        rpc_url=settings.providers.helius_rpc_url,
        rate_limiter=RateLimiter.per_minute(
            settings.onchain_security.requests_per_minute),
        cache=TTLCache(256, 300.0),
        timeout_seconds=settings.onchain_security.timeout_seconds,
    )

    newly_blocked: list[str] = []
    already_blocked = would_pass = 0
    conc_unknown = lp_unknown = 0
    filled = 0

    try:
        for row in rows:
            profile = profile_from_row(row)
            if profile is None:
                continue
            label = f"{row['symbol'] or '?':<10} {row['address'][:12]}…"
            before = analyzer.assess(profile)

            try:
                facts = await collector.collect(row["address"], None)
            except (MemeIntelError, OSError) as exc:
                print(f"{label}  RPC unavailable: {exc}")
                continue

            if facts.top_holder_percent is None:
                conc_unknown += 1
            if facts.lp_burned_percent is None:
                lp_unknown += 1

            updates = {}
            if facts.top_holder_percent is not None:
                updates["top_holder_percent"] = max(
                    facts.top_holder_percent, profile.top_holder_percent or 0.0)
            if facts.top10_holder_percent is not None:
                updates["top10_holder_percent"] = max(
                    facts.top10_holder_percent, profile.top10_holder_percent or 0.0)
            if facts.lp_burned_percent is not None:
                existing = profile.lp_locked_percent
                updates["lp_locked_percent"] = (
                    facts.lp_burned_percent if existing is None
                    else min(existing, facts.lp_burned_percent))

            if not updates:
                note = facts.notes[0] if facts.notes else "nothing established"
                print(f"{label}  {before.overall_score:6.1f} -> (unchanged)   {note}")
                continue

            filled += 1
            after = analyzer.assess(dataclasses.replace(profile, **updates))
            crossed = ""
            if before.overall_score >= gate > after.overall_score:
                crossed = "  <== would now be BLOCKED"
                newly_blocked.append(label)
            elif after.overall_score >= gate:
                would_pass += 1
            else:
                already_blocked += 1
            print(f"{label}  {before.overall_score:6.1f} -> {after.overall_score:6.1f}  "
                  f"top={fmt(facts.top_holder_percent)} "
                  f"top10={fmt(facts.top10_holder_percent)} "
                  f"lp_burned={fmt(facts.lp_burned_percent)} "
                  f"cov {before.coverage:.0%}->{after.coverage:.0%}{crossed}")
    finally:
        await collector.close()

    total = len(rows)
    print()
    print(f"probed                        {total}")
    print(f"facts filled in               {filled}")
    print(f"concentration unknown         {conc_unknown}")
    print(f"LP status unknown             {lp_unknown}")
    print(f"would now be BLOCKED          {len(newly_blocked)}"
          f"{'  (' + ', '.join(n.split()[0] for n in newly_blocked) + ')' if newly_blocked else ''}")
    print(f"still pass                    {would_pass}")
    print(f"were already below the gate   {already_blocked}")
    print()
    if filled and len(newly_blocked) / max(1, filled) > 0.5:
        print("STOP. More than half the coins with facts would stop passing. That is "
              "the shape of a wrong exclusion rule, not a strict gate — read the "
              "per-coin lines above and check whether an AMM vault or bonding "
              "curve is being counted as a holder before enabling anything.")
    elif not filled:
        print("Nothing was established on any coin. With no Helius key that is "
              "expected for concentration; if a key IS set, the layer is not "
              "adding anything and enabling it would be pointless.")
    else:
        print("Read the per-coin lines before enabling. A coin in the BLOCKED list "
              "should be one you would agree is bad.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
