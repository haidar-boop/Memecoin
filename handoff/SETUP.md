# Setup & Running Instructions

## Prerequisites

- Python 3.11+
- Git

## First-time setup

```bash
git clone https://github.com/haidar-boop/Memecoin.git
cd Memecoin
git checkout claude/memecoin-onboarding-yrvjbg   # the authoritative branch

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## API keys — current status

Create a file named `.env` in the project root (it is gitignored — never
committed). The app loads it automatically on startup
(`config/settings.load_dotenv()`), no manual `export` needed.

| Provider | Status | Env var | Used by |
|---|---|---|---|
| DexScreener | ✅ no key needed | — | market data |
| GeckoTerminal | ✅ no key needed | — | discovery, market data |
| GoPlus Security | ✅ no key needed | — | contract security |
| CoinGecko | ✅ no key needed | — | market-environment check |
| **Helius** | ✅ **live, verified** | `MEMEINTEL_HELIUS_API_KEY` | Solana wallet/holder data |
| **Birdeye** | ✅ **live, verified** | `MEMEINTEL_BIRDEYE_API_KEY` | Solana trades/overview |
| **Jupiter** | ⏳ **free plan, needs a key** | `MEMEINTEL_JUPITER_API_KEY` | live buy/sell round-trip test (Project 1, Solana only) |
| **Anthropic (LLM layer)** | 🟡 optional — **operator keeps it OFF to save credits** (system fully functional without) | `MEMEINTEL_ANTHROPIC_API_KEY` | AI reasoning layer (Part 23) + opportunity verification |
| Telegram bot | 🟡 code ready — create via @BotFather | `MEMEINTEL_TELEGRAM_BOT_TOKEN` + `MEMEINTEL_TELEGRAM_CHAT_ID` | alert delivery (Part 29); test: `alerts --test` |
| Discord webhook | 🟡 code ready, optional | `MEMEINTEL_DISCORD_WEBHOOK_URL` | alert delivery (Part 29); test: `alerts --test` |
| CoinGecko community data | ✅ **live** (free; optional demo key raises limits) | `MEMEINTEL_COINGECKO_API_KEY` (optional) | community/narrative scoring (Parts 5, 19) |
| LunarCrush (upgrade path) | ✅ built as a dormant kit (2026-07-20), off by default | `MEMEINTEL_LUNARCRUSH_API_KEY` + `bash deploy/enable-x-community-tracking.sh` | Twitter engagement depth |
| Alchemy (EVM wallets) | ⏳ **not set up, optional** | (not yet defined) | EVM wallet intelligence (Part 17 extension) |

Your current working `.env` should contain at minimum:

```
MEMEINTEL_BIRDEYE_API_KEY=<your birdeye key>
MEMEINTEL_HELIUS_API_KEY=<your helius key>
```

Get free keys at:
- Helius: https://helius.dev (dashboard auto-creates a key)
- Birdeye: https://bds.birdeye.so/auth/sign-up (Security tab → Generate Key)
- Jupiter: https://developers.jup.ag/portal (the $0/month "Free" plan still
  requires signup — Jupiter deprecated its old fully-keyless "Lite" tier;
  1 request/second, no monthly usage cap). Without this key, the live
  round-trip sell test (Project 1) silently stays off — everything else
  keeps working exactly as before, same "no key = feature off" pattern as
  Helius/Birdeye.

## Verify the install

```bash
python -m pytest              # all tests should pass (698 as of 2026-07-10)
```

## Full CLI command reference

```bash
python -m meme_intelligence search <query>                     # search DexScreener by name/symbol
python -m meme_intelligence token <address> [--chain X]         # pairs for one token
python -m meme_intelligence discover --network solana           # scan for new launches
python -m meme_intelligence security <address> --chain X        # standalone security check
python -m meme_intelligence scan --network solana --top 5       # discovery -> security -> on-chain
python -m meme_intelligence plan <address> --chain X [--regime bull|neutral|bear|unknown]
                                                                  # full research pass + trade plan
python -m meme_intelligence report <address> --chain X          # canonical intelligence report
python -m meme_intelligence quick <address> --chain X           # Level 1 fast scan
python -m meme_intelligence compare X:0xabc Y:solAddr           # compare 2+ tokens (chain:address)
python -m meme_intelligence watchlist [--refresh] [--include-archived]
                                                                  # view / re-score tracked tokens
python -m meme_intelligence wallets <address> [--chain solana]  # smart money & whale analysis
python -m meme_intelligence daily [--network solana]            # full daily research routine
python -m meme_intelligence alerts [--test]                      # alert history/performance; --test sends via every sink
python -m meme_intelligence backtest [--refresh]                 # grade predictions against measured outcomes
python -m meme_intelligence monitor [--network solana] [--cycles N] [--interval SECONDS]
                                                                  # continuous 24/7 scanner (Ctrl-C to stop gracefully)
python -m meme_intelligence mind evaluate <address> [--chain X]  # mind-layer verdict for one token
python -m meme_intelligence mind metrics                         # learning report card (memory size, accuracy)
```

## Configuration reference

Every tunable value (scoring weights, thresholds, intervals, rate limits)
is listed with its default in `.env.example` at the repo root. Override
any of them by adding the line (uncommented, with your value) to `.env`.

## Data & logs

- SQLite database: `data/meme_intelligence.sqlite3` (created on first run,
  gitignored)
- Logs: `logs/meme_intelligence.log` (rotating, gitignored)

## Deploying for 24/7 operation — DONE (live in production)

The system runs 24/7 on the operator's $6/mo DigitalOcean droplet via the
kit in `deploy/`: `meme-intelligence.service` (systemd, `Restart=always`),
`setup.sh` (one-shot bootstrap), `install-cron.sh` (daily routine +
backtest refresh + DB backup). **For the live system's update procedure,
`.env` state, and troubleshooting, see [OPERATIONS.md](./OPERATIONS.md)** —
this file covers fresh local installs only.
