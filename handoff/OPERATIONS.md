# Operations — Running, Updating, and Debugging the Live Deployment

> **Why this document exists.** Earlier handoffs described how to run the
> system *locally*; by the time of this handoff it runs 24/7 in production
> and the operator manages it from a phone. This is the live-system
> runbook: the exact update procedure, what healthy looks like, and what to
> do when it isn't. Keep the command blocks short and copy-pasteable — see
> OPERATOR.md for why.

## Where it runs

| Thing | Value |
|---|---|
| Host | DigitalOcean droplet, 1 GB RAM ($6/mo), Ubuntu |
| Repo path | `~/meme-intelligence` |
| Virtualenv | `~/meme-intelligence/.venv` |
| Service | systemd unit `meme-intelligence` (from `deploy/meme-intelligence.service`, `Restart=always`) |
| Cron | daily routine 05:05 UTC + `backtest --refresh` 6-hourly at 04:15/10:15/16:15/22:15 UTC + DB backup 05:45 UTC (`deploy/install-cron.sh` — Beirut-morning anchored since 2026-07-24) |
| Main DB | `data/meme_intelligence.sqlite3` (WAL mode — shared by daemon + cron) |
| Learning state | `learning_state/` (`learning.db` + model artifacts; gitignored) |
| Logs | `journalctl -u meme-intelligence` and `logs/meme_intelligence.log` (rotating) |
| Steady-state memory | ~440 MiB of 1 GB (verified 2026-07-09) — all caches bounded |

## The standard update procedure (give this to the operator verbatim)

```
cd ~/meme-intelligence
git pull
sudo systemctl restart meme-intelligence
```

Then verify:

```
sudo systemctl status meme-intelligence --no-pager
journalctl -u meme-intelligence -n 20 --no-pager
```

**What good looks like:** `Active: active (running)`, and within ~a minute a
log line like `cycle N: X pools, Y candidates, Z analyzed, ... alerts` with
no tracebacks. If he sends a screenshot of this, check those two things.

Optional deeper check (takes ~10s, safe while the service runs):

```
cd ~/meme-intelligence
.venv/bin/python -m pytest tests/ -q
```

Expect `766 passed` (count as of 2026-07-11; update this number when you
add tests).

## `.env` on the droplet (state as of 2026-07-11)

Set and working:
- `MEMEINTEL_HELIUS_API_KEY` — main Helius account (scanner). **Its monthly
  free credits are currently EXHAUSTED** (429 "max usage reached" on every
  call; resets monthly) — which is why wallet-intel is off, below. Also:
  this key appeared in a chat screenshot; recommend rotating it
  (helius.dev → regenerate → edit `.env` → restart).
- `MEMEINTEL_EXECUTION_HELIUS_API_KEY` — **second, separate Helius account
  used only by live trading** (balance reads, send, confirm). Healthy.
  Exists so the money path never competes with the scanner's credit burn
  (DECISIONS_LOG 2026-07-11). This key also transited chat; rotate at
  leisure.
- `MEMEINTEL_JUPITER_API_KEY` — Jupiter quotes/swaps (Project 1 sell-test
  AND a hard prerequisite for live trading).
- `MEMEINTEL_BIRDEYE_API_KEY` — Solana trades/overview.
- `MEMEINTEL_TELEGRAM_BOT_TOKEN` + `MEMEINTEL_TELEGRAM_CHAT_ID` — alert
  delivery to his phone (working; alerts arrive).
- `MEMEINTEL_TELEGRAM_COMMANDS_ENABLED=true` — two-way control (Project 2).
- `MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY` — **this folder has
  disagreed with itself about the live value** (this doc says `high` as of
  2026-07-11; `STATUS.md`'s own 2026-07-11 entry says he moved it to
  `medium` and back at least once while tuning alert volume). Don't trust
  either — run `grep MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY .env` on
  the droplet before assuming what his phone currently receives.
- Learning layer enabled in the monitor (`MEMEINTEL_LEARNING_*` — the mind
  layer accumulated 449 coins on day one and trains as outcomes resolve).
- **Live trading (Project 6) — ARMED, real money:**
  `MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=true`,
  `MEMEINTEL_EXECUTION_LIVE_ENABLED=true`,
  `MEMEINTEL_EXECUTION_PRIVATE_KEY` (dedicated ~$20 Phantom trading
  wallet — the funding IS the risk cap). **Updated 2026-07-20**: the buy
  buttons are no longer fixed-SOL presets — they're now percent-of-balance
  (`MEMEINTEL_EXECUTION_BUY_BUTTON_PERCENTS`, e.g. `20,50,75,100`), and the
  per-trade ceiling (`MEMEINTEL_EXECUTION_MAX_BUY_SOL`) was removed at his
  request — code default is `0.15` SOL but his `.env` sets it to `0`
  (= no ceiling). Don't describe either as still using fixed SOL amounts or
  a 0.15 cap; verify with `grep MEMEINTEL_EXECUTION .env` if in doubt.

- `MEMEINTEL_ANTHROPIC_API_KEY` — **re-enabled by the operator overnight
  2026-07-11** (was removed 2026-07-10 to conserve credits). The credit
  gate still applies: free deterministic screens run first, AI is a final
  second opinion on survivors only. Watch the Anthropic console spend for
  the first days. The system runs fully with OR without this key — a
  missing key is never a bug, and he may toggle it freely.

Deliberately OFF:
- `MEMEINTEL_WALLET_ENABLE_IN_MONITOR=false` — **wallet-intel / smart-money
  paused 2026-07-11** (exhausted the main Helius account's free-tier
  credits, produced only 429 noise). **Updated 2026-07-20**: it was
  rebuilt from scratch behind a credit gate and shipped as a one-command
  dormant kit — the old "re-enable only once profitable" criterion is
  retired. Current re-enable path: `bash deploy/enable-wallet-tracking.sh
  <PAID_HELIUS_KEY>` once he has a paid Helius plan. See
  `COMPLETE_SYSTEM_REFERENCE.md` §9 for the full dormant-kits table.
- `MEMEINTEL_SOCIAL_ENABLE_IN_MONITOR=false` — social/LunarCrush
  intelligence, built 2026-07-20 as a new dormant kit (not a restoration).
  Enable with `bash deploy/enable-x-community-tracking.sh` once he has a
  paid LunarCrush key.

Never in git: `.env` is gitignored; secrets exist only on the droplet.
Editing on the phone: `nano .env` trips him up (two incidents: a
paste-merge corrupted a line; an empty-file scare from the wrong cwd) —
prefer giving him exact one-liners (`printf '\nKEY=value\n' >> .env`,
`sed -i` edits) over interactive nano.

## Memory on the 1GB droplet (learned the hard way, 2026-07-11)

The learning layer's memory grows with every resolved coin (~440M at ~450
coins → ~740M at 6,362 coins). Two failure modes were hit live, one day
after arming:

1. **OOM crash-loop:** the unit's old `MemoryMax=512M` was outgrown — the
   process was killed every ~34s, and each restart's backlog-discard ate
   the operator's Telegram commands. Symptom: a NEW python PID in the
   journal every half-minute; `Failed with result 'oom-kill'`.
2. **Reclaim stall:** a `MemoryHigh=700M` soft limit with usage above it
   throttled the whole (single-event-loop) process into silence — running,
   zero errors, answering nothing. **Do not set MemoryHigh** on a no-swap
   droplet for this anon-heavy Python workload.

Current setup: `MemoryMax=880M` (OOM backstop only — in
`deploy/meme-intelligence.service`, overridable via
`/etc/systemd/system/meme-intelligence.service.d/memory.conf`) **plus a 1G
swapfile** (`/swapfile`, in `/etc/fstab`) so spikes degrade gracefully.

Watch it occasionally: `systemctl show meme-intelligence -p MemoryCurrent`.
When steady-state usage approaches ~800M, the honest options are bounding
the learning memory (cap/prune the analog index) or the $12/mo 2GB droplet
— raising the cap further on a 1GB box just starves the OS.

## Reading the system

- **Live tail:** `journalctl -u meme-intelligence -f`
- **Cycle line** (once per interval, default 45s):
  `cycle N: P pools, C candidates, A analyzed, L launches tracked, K learned, E alerts`
- **Mind layer report card:** `.venv/bin/python -m meme_intelligence mind metrics`
  — analog memory size, resolved outcomes, classifier trained/not, per-source
  and blended accuracy. Ask for this output before wiring the mind layer
  into alert vetoes (ROADMAP #3).
- **Alert history + usefulness:** `.venv/bin/python -m meme_intelligence alerts`
- **Watchlist:** `.venv/bin/python -m meme_intelligence watchlist --top`
- **Why did coin X alert / get rejected?** Every veto and downgrade logs its
  reason (`grep -i <address> logs/meme_intelligence.log`), and alert
  evidence lines name the veto (`deterministic risk veto: ...`,
  `duplicates established token ...`, `interest gate`).

## Telegram commands (Project 2 — control the bot from the phone)

**Enable once:** add `MEMEINTEL_TELEGRAM_COMMANDS_ENABLED=true` to `.env`
(the bot token + chat id from alert delivery are reused), then restart the
service. Only the configured chat id is answered — anyone else who finds
the bot gets silence. Note: exactly ONE process may consume `getUpdates`
per bot token; the monitor is that process (don't run a second listener
with the same token).

| Command | What it does |
|---|---|
| `/status` | scanner health, last cycle stats, which layers are on, DB totals |
| `/why <address>` | recent alerts with recorded evidence/veto reasons, latest score, flags |
| `/check <address> [chain]` | run the full analysis pipeline on a token right now (default chain solana; one at a time, 60s cache) |
| `/holding <address>` / `/unhold` | mark/unmark a coin actually bought — held coins keep full-priority protective alerts forever |
| `/holdings` | list active holdings with latest scores |
| `/watchlist` | top tracked coins by tier |
| `/mind` | learning report card + 👍/👎 feedback tallies |
| `/mute <address>` / `/unmute` | silence/restore ALL alert delivery for one token (analysis continues) |

Every Telegram alert carries 👍/👎 buttons (stored as **advisory**
operator feedback — never a training label) and a one-tap **📋 Copy
address** button. Buy/Dump buttons appear when
`MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=true`; whether they execute real
trades or dry-runs is governed by `MEMEINTEL_EXECUTION_LIVE_ENABLED` —
see the next section. (Historical note: Project 2 shipped this as a
dry-run-only scaffold; Project 6 added the live executor on 2026-07-10,
armed and validated live 2026-07-11.)

## Live buy / dump from Telegram (Project 6 — real money, arm carefully)

The bot can BUY and DUMP straight from Telegram. It never auto-trades — a
trade only ever happens when the operator taps a button or sends a command.
Setup is deliberately manual because it involves a hot wallet.

**One-time setup:**
1. In Phantom: ☰ → Add / Connect Wallet → **Create new account**. This is a
   DEDICATED trading wallet. Do NOT use your main account.
2. Fund it with only what you're willing to risk (agreed cap: ~$50 CAD).
   That amount is the real hard cap — a server compromise can only touch
   this wallet.
3. Export that account's **private key** (Phantom: account → Settings →
   Export Private Key). Copy it.
4. On the droplet, put it in `.env` — **never** paste it anywhere else, never
   into chat, never into git:
   ```
   nano ~/meme-intelligence/.env
   # add these lines:
   MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=true
   MEMEINTEL_EXECUTION_LIVE_ENABLED=true
   MEMEINTEL_EXECUTION_PRIVATE_KEY=<the base58 key you exported>
   MEMEINTEL_EXECUTION_MAX_BUY_SOL=0.15
   ```
5. **Give trading its own Helius account** (learned live 2026-07-11): the
   scanner can exhaust the main Helius account's monthly credits, and
   credits are per-ACCOUNT — a trade's balance read on a shared account
   then gets 429'd no matter what. Sign up for a SECOND free Helius
   account (different email), copy its API key, and add:
   ```
   MEMEINTEL_EXECUTION_HELIUS_API_KEY=<the second account's key>
   ```
   Empty = trading shares the main key (works, but a starved scanner
   account will block trades).
6. **Check the other prerequisites are present**: live trading also
   requires `MEMEINTEL_JUPITER_API_KEY` (quotes/swap building — without it
   `build_executor` silently falls back to dry-run) and a Helius key
   (dedicated or main). Both are already set on the droplet.
7. `sudo systemctl restart meme-intelligence`. The startup log prints
   `LIVE TRADING ARMED — trading wallet <address>`; `/status` shows
   `trading LIVE`. (`journalctl -u meme-intelligence | grep -i armed`
   to check.) If it silently stays dry-run, the causes seen live are:
   `solders` missing from the venv
   (`.venv/bin/pip install -r requirements.txt`), or a missing
   Jupiter/Helius/private key (the startup note names what's missing).

**Using it:**
- Every alert now carries one-tap **Buy 0.05◎ / Buy 0.1◎** buttons and a
  **💥 Dump all** button.
- Commands: `/buy <address> <sol>` (any amount up to the cap) and `/dump
  <address>` (sells your whole position in that token back to SOL).
- Each buy re-checks the wallet balance and the per-trade cap first; a buy
  over the cap or beyond the balance is refused, nothing spent. Replies carry
  a Solscan link to the transaction.

**First live test:** buy a tiny amount (e.g. `/buy <a well-known token> 0.01`)
and confirm it shows in the Phantom trading account before trusting it on a
fresh meme coin. **Status 2026-07-11: FULLY VALIDATED live** — a real
`/buy` and a real `/dump` both executed and confirmed on-chain (complete
round trip). To disable instantly: set `MEMEINTEL_EXECUTION_LIVE_ENABLED=false`
(or `BUY_BUTTON_ENABLED=false` to hide the buttons) and restart.

## Mind-layer P(rug) veto (Project 3 — enable only when EARNED)

The learning layer can block HIGH opportunity alerts with its learned
P(rug), but only once its measured rug precision has earned the vote. The
flag ships OFF. The procedure:

1. Send `/mind` to the bot and read the last lines. While it says
   `authority: not earned yet — …` leave the flag off; the layer keeps
   learning and being graded either way.
2. When it says `authority: EARNED — rug precision X over N graded rug
   calls`, add `MEMEINTEL_LEARNING_VETO_ENABLED=true` to `.env` and
   restart the service.
3. Vetoed alerts arrive downgraded to MEDIUM with the reason spelled out:
   `mind layer: p(rug) 90% >= 85% (authority earned: rug precision 0.80
   over 12 graded rug calls)`. If it ever misfires repeatedly, flip the
   flag back off and report — thresholds are tunable
   (`MEMEINTEL_LEARNING_VETO_MIN_*`).

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Service `failed`/restart loop | `journalctl -u meme-intelligence -n 50` — usually a bad `.env` edit (typo'd value fails validation loudly at startup, by design). Fix the line, restart. |
| No alerts for hours | Usually normal (HIGH-only filter + strict gates + screens = quiet by design). Verify cycle lines are advancing; check `alerts` history for MEDIUM/LOW activity to confirm the pipeline is alive. |
| Telegram alerts stopped | Delivery failures are logged and RETRIED after cooldown (never lost silently). Check log for `telegram delivery failed`; verify bot token/chat id; `alerts --test` sends a synthetic alert through every sink. |
| Provider outage (DexScreener/GoPlus/etc.) | Self-healing: failover pool + cooldowns + honest "unknown" scoring. No action needed; log shows recovery. |
| Out of memory | Shouldn't happen (bounded caches, ~440 MiB steady). If it does, `systemctl status` shows the kill; capture `journalctl` and investigate before upgrading the droplet. |
| Disk filling | DB + logs grow slowly; log rotation is on. Check `du -sh data/ logs/ learning_state/`. Backups from cron are the usual culprit — prune old ones. |
| After droplet reboot | systemd brings the service up automatically (`Restart=always`, enabled unit). A kernel update has been pending since ~2026-07-09; `sudo reboot` at a calm moment is fine. |

## Deploy checklist for every code change (for the assistant, not him)

1. Tests green locally (`python -m pytest tests/` → all pass) — Rule 3/14.
2. Docs updated when behavior/config changed (STATUS.md, DECISIONS_LOG.md,
   `.env.example`) — Rule 15/19.
3. Commit with a clear message; push to the designated `claude/...` branch
   (his droplet pulls that branch).
4. Send him the update block (above) + one sentence on what will change in
   what he sees.
5. If a new env var matters to him, give the exact line to add and the nano
   keystrokes.
