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

## 5. Updating later

```bash
cd ~/meme-intelligence
git pull
source .venv/bin/activate
pip install -r requirements.txt   # only needed if deps changed
sudo systemctl restart meme-intelligence
```

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
