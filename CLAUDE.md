# Meme Coin Intelligence System — Session Instructions

## The 21 Project Rules are mandatory

Read `PROJECT_RULES.md` (repo root) before doing any work in this repository.
It contains the 21 Project Rules, extracted verbatim from the original
PROJECT_RULES.md PDF. They define HOW this codebase must be engineered and
they are never optional, in every session, for every change:

- **Rule 1** — the specification is the source of truth for features
- **Rule 2** — build in small steps; verify, explain, list remaining work
- **Rule 3** — never break working code
- **Rule 4** — modular design (scanner / security / community / on-chain /
  scoring / alerts / database / config / logging / testing stay separated)
- **Rule 5** — prioritize readability
- **Rule 6** — production quality (handle errors, missing data, API
  failures, invalid responses, timeouts)
- **Rule 7** — build for reliability (recovery, retries, graceful shutdown)
- **Rule 8** — data before assumptions; reduce confidence when data is
  missing; never fabricate data
- **Rule 9** — multi-source intelligence; degrade gracefully when a
  provider is unavailable
- **Rule 10** — efficient data collection (WebSockets, events, caching)
- **Rule 11** — avoid API abuse (rate limiting, backoff, queues)
- **Rule 12** — keep performance high
- **Rule 13** — log every important action
- **Rule 14** — test new features; keep existing tests green
- **Rule 15** — document new modules, config, and major decisions
- **Rule 16** — never expose API keys or secrets; use environment variables
- **Rule 17** — no hardcoded values; settings live in configuration
- **Rule 18** — backward compatibility; extend rather than rewrite
- **Rule 19** — explain major architectural decisions
- **Rule 20** — ask before major changes when the spec is ambiguous
- **Rule 21** — accuracy, stability, scalability, clear architecture;
  simple reliable solutions over clever fragile ones

The summaries above are for orientation only — the verbatim text in
`PROJECT_RULES.md` is authoritative.

## Where everything lives

- **Specification (source of truth):** `handoff/FULL_PROJECT_HANDOFF.md` —
  the 21 rules plus the complete original spec, Parts 1–32.5, verbatim.
  Individual unbuilt parts are also split out under `handoff/next_steps/`
  (see `handoff/next_steps/INDEX.md` for the build order).
- **Implementation:** `meme_intelligence/` (modular packages: `ai`,
  `alerts`, `analyzers`, `collectors`, `config`, `core`, `database`,
  `scanners`, `trading`, `workflow`).
- **Tests:** `tests/` — run with `python -m pytest tests/ -q`. All existing
  tests must stay green (Rule 3, Rule 14).
- **Build status and prior decisions:** `handoff/STATUS.md` and
  `handoff/DECISIONS_LOG.md`. When you implement a new part or resolve a
  spec ambiguity, record it in `DECISIONS_LOG.md` and update `STATUS.md`
  (Rule 15, Rule 19).

## Working conventions

- Build one spec part (or one logical section) per request — Rule 2.
- Implement against the **literal spec text**; do not paraphrase or invent
  features — Rule 1. If two valid readings exist, present the options
  before proceeding — Rule 20.
- New analyzers/modules must follow the existing conventions in
  `meme_intelligence/analyzers/` (input dataclasses, confidence handling
  for missing data, logging, settings in `meme_intelligence/config/`).
- Secrets come only from environment variables / `.env` (see
  `.env.example`); never commit keys — Rule 16.
