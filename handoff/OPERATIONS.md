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
| Cron | daily routine + 6-hourly `backtest --refresh` + DB backup (`deploy/install-cron.sh`) |
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

Expect `626 passed` (count as of 2026-07-10; update this number when you
add tests).

## `.env` on the droplet (state as of 2026-07-10)

Set and working:
- `MEMEINTEL_HELIUS_API_KEY` — Solana wallet/holder data. **Note: this key
  appeared in a chat screenshot; recommend rotating it** (helius.dev →
  regenerate → edit `.env` → restart).
- `MEMEINTEL_BIRDEYE_API_KEY` — Solana trades/overview.
- `MEMEINTEL_TELEGRAM_BOT_TOKEN` + `MEMEINTEL_TELEGRAM_CHAT_ID` — alert
  delivery to his phone (working; alerts arrive).
- `MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY=high` — his phone gets
  HIGH/CRITICAL only. **Do not lower this without asking him.**
- Learning layer enabled in the monitor (`MEMEINTEL_LEARNING_*` — the mind
  layer accumulated 449 coins on day one and trains as outcomes resolve).

Deliberately OFF:
- `MEMEINTEL_ANTHROPIC_API_KEY` — **removed by the operator (2026-07-10) to
  conserve credits.** The system is designed for this: all screens/gates are
  deterministic and free; AI is only ever a final second opinion. Do not
  treat the missing key as a bug. If he re-enables it, no other change is
  needed — the verify layer picks it up on restart.

Never in git: `.env` is gitignored; secrets exist only on the droplet.
Editing on the phone: `nano .env` trips him up — give exact keystrokes
(Ctrl+O, Enter, Ctrl+X) when asking him to edit it.

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
address** button. The `[Buy (dry run)]` button appears only when
`MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED=true` and does nothing but journal
the intent — there is no live trade executor, deliberately (DECISIONS_LOG
2026-07-10).

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
