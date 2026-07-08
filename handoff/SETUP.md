# Setup & Running Instructions

## Prerequisites

- Python 3.11+
- Git

## First-time setup

```bash
git clone https://github.com/haidar-boop/Memecoin.git
cd Memecoin
git checkout claude/large-prompt-review-l49wp1

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
| **Anthropic (LLM layer)** | ✅ **live, verified** | `MEMEINTEL_ANTHROPIC_API_KEY` | AI reasoning layer (Part 23): `report --ai` / `plan --ai` |
| Telegram bot | ⏳ **not set up** | (not yet defined in settings) | alert delivery (Part 29) |
| Discord webhook | ⏳ **not set up, optional** | (not yet defined in settings) | alert delivery (Part 29) |
| CoinGecko community data | ✅ **live** (free; optional demo key raises limits) | `MEMEINTEL_COINGECKO_API_KEY` (optional) | community/narrative scoring (Parts 5, 19) |
| LunarCrush (upgrade path) | ⏳ deferred — revisit if the bot proves itself (~$5/day) | (not yet defined) | Twitter engagement depth |
| Alchemy (EVM wallets) | ⏳ **not set up, optional** | (not yet defined) | EVM wallet intelligence (Part 17 extension) |

Your current working `.env` should contain at minimum:

```
MEMEINTEL_BIRDEYE_API_KEY=<your birdeye key>
MEMEINTEL_HELIUS_API_KEY=<your helius key>
```

Get free keys at:
- Helius: https://helius.dev (dashboard auto-creates a key)
- Birdeye: https://bds.birdeye.so/auth/sign-up (Security tab → Generate Key)

## Verify the install

```bash
python -m pytest              # should print "274 passed"
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
python -m meme_intelligence monitor [--network solana] [--cycles N] [--interval SECONDS]
                                                                  # continuous 24/7 scanner (Ctrl-C to stop gracefully)
```

## Configuration reference

Every tunable value (scoring weights, thresholds, intervals, rate limits)
is listed with its default in `.env.example` at the repo root. Override
any of them by adding the line (uncommented, with your value) to `.env`.

## Data & logs

- SQLite database: `data/meme_intelligence.sqlite3` (created on first run,
  gitignored)
- Logs: `logs/meme_intelligence.log` (rotating, gitignored)

## Deploying for 24/7 operation (not done yet)

Not required for local testing. When ready to run `monitor` continuously:
a small VPS (Hetzner CX22 or similar, ~€4/mo) is more than sufficient —
the system is I/O-bound and rate-limited by design. A systemd service
file to keep it running and auto-restart on reboot has not been written
yet; this belongs to Part 21/22's deployment step.
