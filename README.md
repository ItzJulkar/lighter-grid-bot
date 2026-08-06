# Lighter Grid Bot + S/R Scalper

Trading bots for [Lighter](https://lighter.xyz/) (zkLighter perps — zero-fee orderbook DEX).

Two bots in this repo:

| Bot | File | What it does |
|-----|------|--------------|
| **Grid Bot v1** | `grid_bot.py` | Classic geometric grid — buy levels below, sell levels above, counter-orders on fills |
| **S/R Scalper v3** | `s_r_bot.py` | Candle-based structure scalper — 5m/24h S/R zones, resting limits at zone edges (buy-dip at support / sell-rally at resistance), SL = zone dist + 0.3%, TP at opposite level, persistent ledger + state, agent_zone.json override |

Both run in **paper mode by default** (real market prices, simulated fills, virtual margin). No keys needed to try them.

## Install (VPS / Windows / Linux — same steps)

```bash
# 1. Python 3.10+ and the official Lighter SDK (live mode only; paper needs just requests)
pip install lighter-sdk requests

# 2. Get the code
git clone https://github.com/ItzJulkar/lighter-grid-bot.git
cd lighter-grid-bot

# 3. Run (paper mode)
python3 s_r_bot.py        # S/R scalper (paper, $100 virtual)
python3 grid_bot.py       # classic grid (paper)
```

Windows: use `python s_r_bot.py`. No other OS-specific steps — pure Python + asyncio.

## Live mode (real trading)

1. Create your Lighter API key (Lighter app → API keys). You get:
   - **Account Index**
   - **API Key Index**
   - **Public / Private key**
2. Create `api_key_config.json` in this folder (**it is git-ignored — never commit it**):

```json
{
  "endpointProfile": "MAINNET",
  "accountIndex": 123456,
  "privateKeys": {
    "5": "your-private-key-hex"
  }
}
```

3. In `s_r_bot.py` / `grid_bot.py` set `LIVE_MODE = True` and restart.

> **WARNING:** live mode places REAL orders with REAL money. Start small. The S/R
> scalper uses 15x leverage / 80% margin — a 0.7% adverse move ≈ 8% of margin
> per trade. Trading perps can liquidate your position. Use at your own risk.

## How the S/R scalper works (v3 spec)

- **15x leverage, 80% margin** per position, $100 paper portfolio
- **Structure:** 5m candles from the Lighter candles API (24h lookback / 288 bars), swing pivots on candle highs/lows, pivot levels clustered and merged into zones
- **Zone trading (3 modes):**
  - price inside a zone → resting buy at support **and** resting sell at resistance
  - price below a zone → "buy the dip" limit at the zone support (reclaim play)
  - price above a zone → "sell the rally" limit at the zone resistance
- **Entry:** always LIMIT at the zone edge (fills simulated on price trading through the level)
- **Close:** SL/TP via mark **and** candle extremes (wick-safe)
- **Stop-loss:** zone distance + 0.3% buffer beyond the level; **take-profit:** opposite zone level
- **Pause:** only when no zone exists near price (trending with no structure)
- **Persistence:** `ledger.json` (realized PnL + trade history, survives restarts), `state.json` (open position resumes after restart)
- **Agent override:** `agent_zone.json` — write `{"support": S, "resistance": R}` to force a zone, or `{"side": "long", "entry": E, "sl": S, "tp": T}` for a fully-specified single trade; the bot polls the file every loop iteration
- S/R detection: swing highs/lows from 5m candle highs/lows (24h window)

## Files

- `s_r_bot.py` — S/R scalper (v2, default)
- `grid_bot.py` — geometric grid bot (v1)
- `api_key_config.json` — your keys (git-ignored, not in the repo)
- `srb.log` / `grid.log` — run logs (git-ignored)

## Disclaimer

Educational/experimental software. Not financial advice. Crypto perps trading
carries high risk of loss. The authors are not responsible for any losses.
