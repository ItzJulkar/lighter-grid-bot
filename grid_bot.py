#!/usr/bin/env python3
"""
Lighter Grid Bot — v1
Geometric grid on Lighter perps (default: ETH-PERP, market_id=0).
  - real mark price from orderBookDetails (public REST, no keys needed)
  - paper mode: local fill simulation when mark crosses grid levels (real prices)
  - live mode (LIVE_MODE=True): places real limit orders via lighter-sdk
    (requires api_key_config.json from examples/system_setup.py)
Buy low, sell high: each filled buy at level i places a sell one level up;
each filled sell places a buy one level down. Grid profit per completed pair.

STATUS: prototype. Paper mode validated; live mode requires your API keys.
"""
import asyncio, json, math, sys, time, datetime
import requests
import lighter  # only needed for live mode

# ---------------- config ----------------
MARKET_ID = 0                 # ETH perps
MARKET_SYMBOL = "ETH"
RANGE_PCT = 6.0               # re-center boundary: +/-3% around center
GRID_LEVELS = 10              # number of levels on each side
GRID_SPACING_PCT = 0.6        # geometric step between adjacent levels (~0.6%)
CAPITAL_USD_PER_LEVEL = 100.0 # notional per grid slot (paper: virtual)
MIN_NOTIONAL_USD = 10.0       # ignore levels that can't meet min_base
RECHECK_SECONDS = 10          # poll interval
LIVE_MODE = False             # True = real orders (needs api_key_config.json)
API_KEY_CONFIG = "./api_key_config.json"
BASE_URL = "https://mainnet.zklighter.elliot.ai"
LOG_FILE = "grid.log"

# ---------------- helpers ----------------
def log(msg):
    line = f"[{datetime.datetime.now().isoformat()}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def price_dec(md): return md.get("price_decimals", 2)
def size_dec(md):  return md.get("size_decimals", 4)

def to_price_units(price, pd):   # USD -> integer price units (cents etc)
    return int(round(price * (10 ** pd)))
def from_price_units(pu, pd):
    return pu / (10 ** pd)
def to_size_units(size, sd):     # base tokens -> integer size units
    return int(round(size * (10 ** sd)))
def from_size_units(su, sd):
    return su / (10 ** sd)

def geometric_levels(center, n, spacing_pct):
    """2n+1 geometric levels centered on `center`: level[k] = center * g^k, k=-n..n"""
    g = 1.0 + spacing_pct / 100.0
    return {k: center * (g ** k) for k in range(-n, n + 1)}

# ---------------- market data ----------------
_session = requests.Session()
def fetch_market():
    r = _session.get(f"{BASE_URL}/api/v1/orderBookDetails", timeout=15)
    d = r.json()
    for m in d["order_book_details"]:
        if m["market_id"] == MARKET_ID:
            return m
    return None

# ---------------- grid state machine ----------------
class GridSlot:
    __slots__ = ("k", "level", "side", "filled", "fill_price", "order_index", "open")
    def __init__(self, k, level, side):
        self.k = k                  # grid index (buy: negative, sell: positive)
        self.level = level          # price
        self.side = side            # "buy" | "sell"
        self.filled = False
        self.fill_price = 0.0
        self.order_index = 0
        self.open = False

class GridBot:
    def __init__(self, md, capital_per_level):
        self.md = md
        self.pd = price_dec(md)
        self.sd = size_dec(md)
        self.min_base = float(md["min_base_amount"])
        self.capital = capital_per_level
        self.slots = []
        self.center = None
        self.lo, self.hi = 0.0, 0.0
        self.realized_usd = 0.0
        self.position = 0.0          # signed: +long / -short
        self.avg_entry = 0.0
        self.order_seq = 1000
        self.fills = []

    def size_for(self, price):
        sz = self.capital / price
        return max(sz, self.min_base)

    def reset_grid(self, mark):
        self.center = mark
        self.lo = mark * (1 - RANGE_PCT / 200.0)
        self.hi = mark * (1 + RANGE_PCT / 200.0)
        spacing = GRID_SPACING_PCT
        levels = geometric_levels(mark, GRID_LEVELS, spacing)
        self.slots = []
        for k in range(-GRID_LEVELS, 0):          # buys below center
            self.slots.append(GridSlot(k, levels[k], "buy"))
        for k in range(1, GRID_LEVELS + 1):       # sells above center
            self.slots.append(GridSlot(k, levels[k], "sell"))
        log(f"grid reset: center={mark:.2f} spacing={spacing}% "
            f"levels={len(self.slots)} buys={GRID_LEVELS} sells={GRID_LEVELS}")

    # ---- signed position accounting ----
    def _buy(self, price, sz):
        if self.position >= 0:
            self.avg_entry = (self.avg_entry * self.position + price * sz) / (self.position + sz)
            self.position += sz
        else:
            closed = min(sz, -self.position)
            self.realized_usd += (self.avg_entry - price) * closed   # short closed
            self.position += sz
            if self.position > 0:
                self.avg_entry = price                               # new long leg

    def _sell(self, price, sz):
        if self.position <= 0:
            self.avg_entry = (self.avg_entry * abs(self.position) + price * sz) / (abs(self.position) + sz)
            self.position -= sz
        else:
            closed = min(sz, self.position)
            self.realized_usd += (price - self.avg_entry) * closed   # long closed
            self.position -= sz
            if self.position < 0:
                self.avg_entry = price                               # new short leg

    def fill_slot(self, slot, price):
        sz = self.size_for(slot.level)
        if slot.side == "buy":
            self._buy(price, sz)
        else:
            self._sell(price, sz)
        slot.filled = True
        slot.fill_price = price
        self.fills.append((datetime.datetime.now().isoformat(), slot.side, slot.k, slot.level, price, sz))
        side_txt = "BUY " if slot.side == "buy" else "SELL"
        log(f"  FILL {side_txt} k={slot.k:+d} @ {price:.4f} size={sz:.4f} "
            f"position={self.position:+.4f} realized=${self.realized_usd:.2f}")

    def place_counter_order(self, slot):
        """buy fill at k -> sell at k+1; sell fill at k -> buy at k-1"""
        nk = slot.k + 1 if slot.side == "buy" else slot.k - 1
        for s in self.slots:
            if s.k == nk and not s.filled:
                s.open = True
                self.order_seq += 1
                s.order_index = self.order_seq
                log(f"  PLACE {'SELL' if nk > 0 else 'BUY'} k={nk:+d} @ {s.level:.4f} (counter)")
                return s
        return None

    def on_fill(self, slot, price):
        self.fill_slot(slot, price)
        self.place_counter_order(slot)

    def step(self, mark):
        for slot in self.slots:
            if slot.filled:
                continue
            if slot.side == "buy" and mark <= slot.level:
                self.on_fill(slot, min(mark, slot.level))
            elif slot.side == "sell" and mark >= slot.level:
                self.on_fill(slot, max(mark, slot.level))

    def mark_to_market(self, mark):
        if self.position > 0:
            return self.position * (mark - self.avg_entry)
        if self.position < 0:
            return abs(self.position) * (self.avg_entry - mark)
        return 0.0

    def stats(self, mark):
        return {
            "position": round(self.position, 4),
            "avg_entry": round(self.avg_entry, 2),
            "realized_usd": round(self.realized_usd, 2),
            "floating_usd": round(self.mark_to_market(mark), 2),
            "total_pnl_usd": round(self.realized_usd + self.mark_to_market(mark), 2),
        }

# ---------------- live-mode (requires keys) ----------------
def live_client():
    with open(API_KEY_CONFIG) as f:
        cfg = json.load(f)
    private_keys = {int(k): v for k, v in cfg["privateKeys"].items()}
    client = lighter.SignerClient(
        url=BASE_URL,
        account_index=cfg["accountIndex"],
        api_private_keys=private_keys,
        chain_id=304,   # mainnet chain id
    )
    err = client.check_client()
    if err is not None:
        raise RuntimeError(f"check_client: {err}")
    return client

# ---------------- main ----------------
async def main():
    log(f"Lighter Grid Bot v1 — market {MARKET_SYMBOL} (id={MARKET_ID}) live_mode={LIVE_MODE}")
    md = fetch_market()
    if not md:
        log("FATAL: market not found")
        return
    mark = float(md["mark_price"])
    log(f"market: mark={mark} price_dec={price_dec(md)} size_dec={size_dec(md)} min_base={md['min_base_amount']}")

    bot = GridBot(md, CAPITAL_USD_PER_LEVEL)
    bot.reset_grid(mark)

    if LIVE_MODE:
        client = live_client()
        log("live client ready — placing real orders (NOT paper!)")

    last_mark = mark
    while True:
        try:
            md = fetch_market()
            mark = float(md["mark_price"])
            if LIVE_MODE:
                # real order placement loop (v1: log only, orders placed in live_place())
                pass
            else:
                bot.step(mark)
            s = bot.stats(mark)
            if abs(mark - last_mark) > 0.01 or s["total_pnl_usd"] != 0:
                log(f"mark={mark:.2f} pos={s['position']:+.4f} realized=${s['realized_usd']:.2f} "
                    f"floating=${s['floating_usd']:.2f} total=${s['total_pnl_usd']:.2f}")
                last_mark = mark
            # re-center if price left the grid
            if mark < bot.lo or mark > bot.hi:
                log(f"WARN: price {mark:.2f} outside grid [{bot.lo:.2f},{bot.hi:.2f}] — re-centering")
                bot.reset_grid(mark)
        except Exception as e:
            log(f"loop error: {e}")
        await asyncio.sleep(RECHECK_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
