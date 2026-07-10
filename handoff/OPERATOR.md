# The Operator — Who Runs This System and How to Work With Him

> **Why this document exists.** Previous handoff folders described the code
> perfectly and the *operator* not at all — so every new session re-learned
> the same constraints by tripping over them (burning his Claude budget on
> agent swarms, sending walls of jargon, spending API credits he was trying
> to conserve). Read this before doing anything else. It is as binding as
> the rules doc: violating these constraints has repeatedly caused real
> problems mid-session.

## Profile

- **Non-technical.** He does not read code. Explain everything in plain
  language — what it does and why it matters, never how it's implemented,
  unless he asks. No jargon without a one-line translation.
- **Operates entirely from a phone.** He uses a phone SSH app to reach the
  droplet and Telegram to receive alerts. Consequences:
  - Give commands as small copy-paste blocks, never long one-liners that
    wrap badly on a phone screen.
  - After any deploy, give him a short verification block and tell him
    exactly what "good" output looks like.
  - He exits editors accidentally, pastes screenshots instead of text, and
    that's fine — read the screenshots carefully; they are ground truth
    about the live system.
- **Budget-conscious, in three separate currencies.** Never conflate them:
  1. **Droplet money** — $6/month DigitalOcean, 1 GB RAM. Don't suggest
     upgrades casually; the system is designed to fit (bounded caches,
     ~440 MiB in steady state).
  2. **Anthropic API credits** — for the bot's AI verification layer. He has
     run out twice. Doctrine (his words): *"If it's high risk already don't
     use my credits. If it's a rug pull don't use my credits. Only use my
     credits when everything else checks out."* This is implemented (the
     deterministic screens run before any paid call). **As of 2026-07-10 the
     API key is switched off by his choice** — the system is designed to run
     fully without it.
  3. **His Claude (chat) usage** — the sessions themselves. He has hit his
     monthly spend limit mid-task more than once. **Hard limits he has
     stated: never spawn more than 3–5 subagents, ever** ("last time u
     spawned 50"). Prefer solo, targeted work. When he says "don't use too
     much of my data," he means it literally — long agent fan-outs have
     killed work in progress.

## How he communicates

- Short messages, often voice-to-text with typos ("Mr bull shit" = "me
  bullshit"). Read for intent, not spelling.
- "Start" / "Sure start" / "Do your magic" = proceed with what was just
  discussed. He delegates readily once he trusts the plan.
- He asks "is it done?" when a session goes quiet — give him progress
  markers on long work.
- He will say clearly when something annoys him (junk alerts, credit burn).
  Treat those messages as the highest-priority requirements in the project.

## Decisions he has already made (do not re-litigate)

| Decision | His call | Where recorded |
|---|---|---|
| Trading | **Never.** Decision-support only; he makes every call. | Spec + every doc |
| Phone alerts | HIGH priority and above only (`external_min_priority=high`) | DECISIONS_LOG (strong-candidate tier) |
| Junk warnings | Coins never recommended to him must not buzz his phone | DECISIONS_LOG (interest gate) |
| AI credits | Free checks first, AI last; key currently off | DECISIONS_LOG (credit gate) |
| LunarCrush / paid social data | Deferred until the bot proves itself | DECISIONS_LOG, ROADMAP #5 |
| Web dashboard | Wanted, but deferred behind detection quality | ROADMAP #4 |
| Addressing him | Do **not** address him by name in replies (he retracted that) | session history |

## Standing expectations for every session

1. **The 21 Project Rules apply, always** — `PROJECT_RULES.md` (verbatim
   copy in this folder). The ones he personally enforces hardest: Rule 2
   (small steps — one thing per request), Rule 3 (never break working
   code), Rule 20 (ask before big changes).
2. **Keep the test suite green** and tell him the count. He can't read the
   tests, but "626 passing" is how he verifies work happened.
3. **Commit and push everything** to the designated branch — his droplet
   updates via `git pull`. Uncommitted work is invisible to him.
4. **End every change with the droplet update block** (see OPERATIONS.md).
   If he doesn't get the commands, the fix never reaches production.
5. **When he reports a problem, believe the screenshot over the design.**
   Several major fixes (interest gate, copycat veto) came from his
   screenshots showing behavior the design docs said couldn't happen.

## Security notes involving him

- His **Helius API key appeared on-screen in a chat screenshot**
  (2026-07-09). It still works. Recommend rotation at a calm moment —
  helius.dev dashboard → regenerate → update `.env` → restart service.
- His `.env` on the droplet holds: Helius key, Birdeye key, Telegram bot
  token + chat id. The Anthropic key line is currently removed/disabled.
- Never echo key values back in chat, even partially; he screenshots
  terminals and those screenshots persist.
