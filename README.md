# Lighter Grid Bot + S/R Scalper

Trading bots for [Lighter](https://lighter.xyz/) (zkLighter perps — zero-fee orderbook DEX).

Two bots in this repo:

| Bot | File | What it does |
|-----|------|--------------|
| **Grid Bot v1** | `grid_bot.py` | Classic geometric grid — buy levels below, sell levels above, counter-orders on fills |
| **S/R Scalper v2** | `s_r_bot.py` | Support/Resistance scalper — 15x, 80% margin, limit entries at market (5s refresh), SL = S/R dist + 0.3%, TP at opposite S/R, market exits, pause outside S/R zone |

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

## How the S/R scalper works (v2 spec)

- **15x leverage, 80% margin** per position
- **Entry:** always LIMIT at market price; if unfilled after 5s → cancel & re-place at the new market price
- **Close:** always MARKET
- **Stop-loss:** S/R distance + 0.3% buffer (e.g. resistance 1900 with support 0.4% lower → SL 0.7%)
- **Take-profit:** opposite S/R level
- **Both sides:** a resting buy limit + a resting sell limit (long & short)
- **Pause/resume:** price outside the S/R zone → no new orders (open position stays SL-managed); when 2 supports + 2 resistances re-form → resume trading
- S/R detection: swing highs/lows from the live mark-price series

## Files

- `s_r_bot.py` — S/R scalper (v2, default)
- `grid_bot.py` — geometric grid bot (v1)
- `api_key_config.json` — your keys (git-ignored, not in the repo)
- `srb.log` / `grid.log` — run logs (git-ignored)

## Disclaimer

Educational/experimental software. Not financial advice. Crypto perps trading
carries high risk of loss. The authors are not responsible for any losses.
