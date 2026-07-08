# Handoff Folder — Meme Coin Intelligence System

This folder is a complete snapshot of project state as of **2026-07-08**,
written so that any developer (or a fresh AI session with no memory of
prior conversations) can pick up exactly where this session left off.

## Read these in order

1. **[STATUS.md](./STATUS.md)** — what has been built (Parts 1–18), file
   by file, with test counts and what's been live-verified against real
   APIs. This is the ground truth for "is X done?"
2. **[SETUP.md](./SETUP.md)** — how to run the system locally: Python
   setup, API keys, `.env`, the full CLI command list.
3. **[DECISIONS_LOG.md](./DECISIONS_LOG.md)** — architectural decisions,
   places where the spec was ambiguous or self-contradictory and how it
   was resolved, and things intentionally deferred with the reason why.
4. **[next_steps/](./next_steps/)** — the original project specification
   text for every part **not yet built** (Parts 19–33), copied verbatim
   from the source prompt, unmodified. See `next_steps/INDEX.md` for the
   list. **These files are the literal spec, not a summary** — build
   against them directly the same way Parts 1–18 were built.

## The one-paragraph summary

This is a meme coin research/intelligence platform — **never an
auto-trader** — built in Python 3.11 (async), SQLite, and a set of free
and low-cost APIs (DexScreener, GeckoTerminal, GoPlus Security, CoinGecko,
Helius, Birdeye). It discovers new token launches, screens them for
security risk, scores them across seven weighted categories (security,
community, on-chain/wallet behavior, foundation, momentum, narrative,
timing), generates trade plans and full intelligence reports, tracks a
tiered watchlist, runs as either a one-shot daily routine or a continuous
24/7 scanner, and dispatches alerts (console today, Telegram/Discord
planned). 18 of the ~33 specification parts are built, tested (274
passing tests), and have been verified against live data.

## Where the code lives

Repository: `haidar-boop/memecoin`
Branch: `claude/large-prompt-review-l49wp1`
Root: `/home/user/Memecoin` (or wherever this repo is cloned)

## The 21 project rules (still in force)

The original `PROJECT_RULES.md` governs how this codebase is built and
must continue to be followed for all future work:

1. Follow the specification; explain assumptions on ambiguity
2. Build in small steps — one logical section per response
3. Never break working code
4. Modular design — one purpose per module
5. Prioritize readability
6. Production quality — handle errors, missing data, API failures, timeouts
7. Build for reliability — crash recovery, retry logic, graceful shutdown
8. Data before assumptions — no fabricated data, flag uncertainty
9. Multi-source intelligence — never rely on one source when avoidable
10. Efficient data collection — WebSockets/caching over polling, filter before expensive analysis
11. Avoid API abuse — rate limiting, backoff, caching
12. Keep performance high
13. Log every important action
14. Test every new feature; verify no regressions
15. Document new modules, APIs, config, DB changes, architecture decisions
16. Security — never expose API keys/secrets; env vars only
17. Configuration — no hardcoding; config files/env vars
18. Backward compatibility — extend, don't rewrite, unless there's a clear benefit
19. Explain major architectural decisions (why, benefits, trade-offs)
20. Ask before major changes when the spec is ambiguous or two valid paths exist
21. Development mindset — reliable, maintainable, extensible; simple over clever
