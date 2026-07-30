# Deploying to a DigitalOcean droplet

Cheapest viable plan: **Basic droplet, $6/mo, 1 vCPU / 1GB RAM, Ubuntu
24.04 LTS**. This workload is outbound HTTP calls plus one WebSocket —
no GPU, minimal RAM, SQLite storage. 1GB is comfortable headroom.

## 1. Create the droplet

- DigitalOcean dashboard -> Create -> Droplets
- Image: **Ubuntu 24.04 (LTS) x64**
- Plan: **Basic**, Regular SSD, the $6/mo tier (1 vCPU, 1GB RAM, 25GB disk)
- Region: pick one close to you (latency to the data providers doesn't
  matter much here — nothing is latency-sensitive at this scale)
- Authentication: **SSH key** (recommended) or password
- Create the droplet, note its IP address

## 2. Connect and deploy

```bash
ssh root@<droplet-ip>

# Create a non-root user (good practice, not required)
adduser deploy
usermod -aG sudo deploy
su - deploy

git clone <your-repo-url> ~/meme-intelligence
cd ~/meme-intelligence
bash deploy/setup.sh
```

The script installs Python + deps into a virtualenv, copies
`.env.example` to `.env` if you don't have one yet, and installs (but
does not start) the systemd service.

## 3. Fill in your secrets

```bash
nano .env
```

Fill in (yours already has these locally — copy the values over):
- `MEMEINTEL_TELEGRAM_BOT_TOKEN` / `MEMEINTEL_TELEGRAM_CHAT_ID`
- `MEMEINTEL_ANTHROPIC_API_KEY` (if you want `--ai` judgments — note:
  the systemd unit runs plain `monitor --pumpfun`, not `--ai`; the AI
  layer's `enable_in_monitor` setting, `MEMEINTEL_AI_ENABLE_IN_MONITOR`,
  controls whether the continuous scanner uses it)
- Leave `MEMEINTEL_DISCORD_WEBHOOK_URL` blank — you said you don't need it

Never commit this file. `.gitignore` already excludes `.env`.

## 4. Start it

```bash
sudo systemctl start meme-intelligence
sudo systemctl status meme-intelligence   # should show "active (running)"
journalctl -u meme-intelligence -f        # live logs, Ctrl-C to stop watching
```

It's enabled to start automatically on reboot. `Restart=always` means
systemd restarts it if the process ever dies outright — on top of the
app's own internal exponential backoff for recoverable errors (Rule 7).

## 5. Scheduled jobs (daily routine, backtesting, backups)

The monitor runs 24/7 on its own, but three things run on a schedule:

```bash
cd ~/meme-intelligence
bash deploy/install-cron.sh
```

This installs three cron jobs (idempotent — safe to re-run):
- **05:05 UTC daily** (08:05 Beirut) — `daily` routine: market regime
  check, watchlist deep review, daily report (alerts go to your Telegram
  like the monitor's)
- **every 6 hours at 04:15/10:15/16:15/22:15 UTC** — `backtest --refresh`:
  measures prediction outcomes so the Part 24 self-improvement metrics
  accumulate; without this the system never learns (the 04:15 run feeds
  the daily report 50 minutes later)
- **05:45 UTC daily** (08:45 Beirut) — consistent online backup of the
  SQLite database to `data/backups/` (keeps the last 7 days; safe against
  the live writer)

Times are Beirut-morning anchored (operator moved to Lebanon 2026-07-24)
so the heavy cluster never squeezes the live monitor during his afternoon
— see the note in `install-cron.sh`.

Job output lands in `logs/cron-*.log`. The database runs in WAL mode
with a busy timeout, so the monitor and these jobs share it safely.

## 6. Updating later

```bash
cd ~/meme-intelligence
git pull
source .venv/bin/activate
pip install -r requirements.txt   # only needed if deps changed
sudo systemctl restart meme-intelligence
```

## 7. Read-only probes (run these BEFORE any change that tightens a gate)

Both open the database read-only and are safe against the live monitor. Paste as
one block each.

```bash
cd ~/meme-intelligence
source .venv/bin/activate
python3 deploy/security_evidence_report.py
```
Which rug-relevant facts the bot actually has today. This is the "before".

```bash
cd ~/meme-intelligence
source .venv/bin/activate
python3 deploy/onchain_facts_probe.py --limit 25
```
What the on-chain holder/LP layer *would* report on real alerted coins, and what
it would do to each score. **Run this before setting
`MEMEINTEL_ONCHAIN_SECURITY_ENABLED=true`.**

Good output looks like: a handful of coins in the "would now be BLOCKED" list,
each one you would agree is bad. If most coins flip to BLOCKED, stop — that is
what a wrong holder-exclusion rule looks like, and it would take the bot off the
air. Two earlier "make it stricter" changes would have silenced 99% of alerts and
were caught by exactly this kind of measurement.

Needs `MEMEINTEL_HELIUS_API_KEY` for the concentration column —
`getTokenLargestAccounts` is disabled on public RPC, so without a key the probe
honestly reports "unknown" instead of a number.

```bash
cd ~/meme-intelligence
source .venv/bin/activate
python3 deploy/alert_outcomes_report.py
```
What the bot's alerts have actually DONE — coverage, the best returns ever
recorded, the win/loss distribution, and whether unmeasured alerts are simply too
recent or were never measured at all. Costs nothing, makes no network calls.

Read it this way: **low coverage plus many stale unmeasured alerts** means the
hit-rate question is a measurement problem, and no detection work can answer it.
**A modest best-ever return plus few unmeasured** means it really has not found a
big winner, and the 1-hour freshness window
(`MEMEINTEL_ALERTS_OPPORTUNITY_MAX_AGE_HOURS`) is the first thing to question — it
suppresses every buy-side alert on a coin older than 60 minutes, which is exactly
when a real runner becomes identifiable.

## Sizing / cost check

- Droplet: $6 USD/mo (~$8 CAD) — well under your $50 CAD budget, leaves
  room to upgrade if you ever want it (you won't need to for this).
- No paid API keys are required for anything currently built:
  DexScreener, GeckoTerminal, GoPlus, CoinGecko (free tier),
  PumpPortal, and the pump.fun frontend API are all free/keyless.
  Anthropic (`--ai`) is the one paid cost, billed per API call, separate
  from hosting.

## Things to watch in the logs

- `pumpportal reconnecting in Ns` — normal; the WebSocket occasionally
  drops and reconnects with backoff. Frequent reconnects are fine.
- Repeated `pumpfun: unexpected status 403` — the unofficial frontend
  API may be rate-limiting or Cloudflare-challenging this IP. The
  scanner keeps running either way (launches just won't promote via
  that source); if this persists, it's worth revisiting the source
  choice (see `handoff/DECISIONS_LOG.md`).
- `cycle N failed: ... (backing off Ns)` occasionally is normal
  (a transient provider hiccup); constant back-to-back failures are not.
