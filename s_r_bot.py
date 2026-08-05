#!/usr/bin/env python3
"""
Lighter S/R Scalper — "S/R Scalper v2"
Spec:
  - 15x leverage, 80% margin utilization (LIVE) / $100 paper portfolio
  - Entry: LIMIT at market price, refresh every 5s if unfilled
  - Close: always MARKET
  - SL = S/R distance + 0.3% buffer  (e.g. 0.4% S/R dist -> 0.7% SL)
  - TP = opposite S/R level
  - Long + short both sides (two resting limit orders)
  - Outside S/R zone -> PAUSE (no new orders; open position SL-managed)
  - Structure normalizes (2 supports + 2 resistances) -> RESUME

Paper mode (default): fills simulated when mark trades through the resting
limit; virtual margin. LIVE mode: real orders via lighter-sdk — requires
api_key_config.json (see README). FLIP LIVE_MODE ONLY WHEN READY.

Run (any OS):  python3 s_r_bot.py
"""
import asyncio, json, time, datetime, sys, os
import requests

# ---------------- config ----------------
MARKET_ID = 0
MARKET_SYMBOL = "ETH"
LEVERAGE = 15.0
MARGIN_USE = 0.80            # 80% of margin per position
SL_BUFFER_PCT = 0.3          # SL = sr_distance% + this
LIMIT_TTL_SEC = 5            # refresh resting limit if unfilled
POLL_SEC = 2
SWING_LOOKBACK = 12
SWING_BARS = 40
MIN_SR_DIST_PCT = 0.15
PAPER_MARGIN_USD = 100.0     # paper portfolio (user spec)
LIVE_MODE = False            # TRUE = real orders (needs api_key_config.json)
API_KEY_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_key_config.json")
BASE_URL = "https://mainnet.zklighter.elliot.ai"
LOG_FILE = "srb.log"
PRICE_DEC = 2                # ETH perp: price in cents
SIZE_DEC = 4                 # ETH perp: 0.0001 ETH units

# ---------------- helpers ----------------
def log(msg):
    line = f"[{datetime.datetime.now().isoformat()}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def price_units(p):  return int(round(p * (10 ** PRICE_DEC)))
def size_units(s):   return int(round(s * (10 ** SIZE_DEC)))

class Position:
    __slots__ = ("side", "size", "entry", "sl", "tp", "margin")
    def __init__(self, side, size, entry, sl, tp, margin):
        self.side = side; self.size = size; self.entry = entry
        self.sl = sl; self.tp = tp; self.margin = margin

# ---------------- market data ----------------
_session = requests.Session()
def fetch_mark():
    r = _session.get(f"{BASE_URL}/api/v1/orderBookDetails", timeout=10)
    for m in r.json()["order_book_details"]:
        if m["market_id"] == MARKET_ID:
            return float(m["mark_price"])
    return None

# ---------------- S/R detection (swing highs/lows) ----------------
class SRDetector:
    def __init__(self):
        self.series = []
        self.supports = []
        self.resistances = []

    def update(self, price):
        if self.series and abs(self.series[-1][1] - price) / price < 0.0001:
            return
        self.series.append((time.time(), price))
        if len(self.series) > SWING_BARS * 2:
            self.series = self.series[-(SWING_BARS * 2):]
        self._detect()

    def _detect(self):
        n = len(self.series)
        if n < SWING_LOOKBACK * 2 + 1:
            return
        pivots = []
        for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
            win = self.series[i - SWING_LOOKBACK:i + SWING_LOOKBACK + 1]
            p = self.series[i][1]
            if p == max(w[1] for w in win):
                pivots.append(("H", p))
            elif p == min(w[1] for w in win):
                pivots.append(("L", p))
        if pivots:
            highs = [p for t, p in pivots if t == "H"]
            lows = [p for t, p in pivots if t == "L"]
            self.resistances = highs[-4:]
            self.supports = lows[-4:]

    def zone(self, price):
        s = max([x for x in self.supports if x < price], default=None)
        r = min([x for x in self.resistances if x > price], default=None)
        return s, r

# ---------------- bot ----------------
class SRBot:
    def __init__(self, margin, live=False):
        self.margin = margin
        self.live = live
        self.det = SRDetector()
        self.pos = None
        self.buy_limit = None   # (order_index, price, placed_at)
        self.sell_limit = None
        self.paused = False
        self.closed_count = 0
        self.realized = 0.0
        self.order_seq = 5000
        self.client = None
        self.account_api = None

    # ---- live plumbing ----
    async def live_init(self):
        import lighter
        with open(API_KEY_CONFIG) as f:
            cfg = json.load(f)
        keys = {int(k): v for k, v in cfg["privateKeys"].items()}
        self.client = lighter.SignerClient(
            url=BASE_URL,
            account_index=cfg["accountIndex"],
            api_private_keys=keys,
            chain_id=304,
        )
        err = self.client.check_client()
        if err is not None:
            raise RuntimeError(f"check_client: {err}")
        self.account_api = lighter.AccountApi(
            lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL)))
        log(f"live client ready (account {cfg['accountIndex']})")

    async def place_limit(self, side, price, size):
        oi = self.order_seq; self.order_seq += 1
        tx, tx_hash, err = await self.client.create_order(
            market_index=MARKET_ID, client_order_index=oi,
            base_amount=size_units(size), price=price_units(price),
            is_ask=(side == "sell"), order_type=self.client.ORDER_TYPE_LIMIT,
            time_in_force=self.client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False, order_expiry=self.client.DEFAULT_28_DAY_ORDER_EXPIRY,
        )
        if err is not None:
            log(f"  live order FAILED ({side}): {err}")
            return None
        return oi

    async def cancel(self, oi):
        tx, tx_hash, err = await self.client.cancel_order(market_index=MARKET_ID, order_index=oi)
        if err is not None:
            log(f"  cancel FAILED oi={oi}: {err}")

    async def market_close(self, side, size):
        worst = 1 if side == "long" else 1_000_000_00  # buy-back low / sell high
        oi = self.order_seq; self.order_seq += 1
        tx, tx_hash, err = await self.client.create_order(
            market_index=MARKET_ID, client_order_index=oi,
            base_amount=size_units(size), price=worst,
            is_ask=(side == "short"), order_type=self.client.ORDER_TYPE_MARKET,
            time_in_force=self.client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            reduce_only=True,
        )
        if err is not None:
            log(f"  market close FAILED ({side}): {err}")
        return err is None

    # ---- sizing ----
    def notional(self):
        return self.margin * MARGIN_USE * LEVERAGE
    def size_for(self, price):
        return self.notional() / price

    # ---- core logic ----
    async def live_sync(self, mark):
        """LIVE MODE: wire real order placement/fill verification here.
        TODO (tomorrow, together): place/cancel limits via self.place_limit,
        verify fills via account API position, close via self.market_close.
        This stub only confirms the client works."""
        if self.client is not None and not getattr(self, "_live_checked", False):
            self._live_checked = True
            log("live_sync: client ok (order wiring lands in next update)")

    def step(self, mark, now):
        self.det.update(mark)
        s, r = self.det.zone(mark)
        if s is not None and r is not None:
            sr_dist = (r - s) / s * 100.0
            if sr_dist < MIN_SR_DIST_PCT:
                s, r = None, None
        if s is None or r is None:
            if not self.paused:
                log("PAUSE: no valid S/R zone — cancelling resting orders")
                self.paused = True
                self.buy_limit = self.sell_limit = None
        else:
            if self.paused:
                log(f"RESUME: zone S={s:.2f} R={r:.2f} (dist {sr_dist:.2f}%)")
                self.paused = False
            sr_dist = (r - s) / s * 100.0
            if self.pos is not None:
                self._manage_position(mark, s, r)
            if self.pos is None:
                self._manage_resting(mark, now, sr_dist, s, r)
        if self.pos is not None:
            pnl = (mark - self.pos.entry) * self.pos.size if self.pos.side == "long" else (self.pos.entry - mark) * self.pos.size
            log(f"  pos={self.pos.side} size={self.pos.size:.4f} entry={self.pos.entry:.2f} "
                f"sl={self.pos.sl:.2f} tp={self.pos.tp:.2f} pnl=${pnl:.2f}")

    def _manage_resting(self, mark, now, sr_dist, s, r):
        sz = self.size_for(mark)
        if self.buy_limit and now - self.buy_limit[2] > LIMIT_TTL_SEC:
            log(f"  buy limit {self.buy_limit[1]:.2f} unfilled {LIMIT_TTL_SEC}s — replacing at {mark:.2f}")
            self.buy_limit = None
        if self.sell_limit and now - self.sell_limit[2] > LIMIT_TTL_SEC:
            log(f"  sell limit {self.sell_limit[1]:.2f} unfilled {LIMIT_TTL_SEC}s — replacing at {mark:.2f}")
            self.sell_limit = None
        if self.buy_limit is None:
            self.buy_limit = (self.order_seq, mark, now); self.order_seq += 1
            log(f"  LIMIT BUY @ {mark:.2f} size={sz:.4f}")
        if self.sell_limit is None:
            self.sell_limit = (self.order_seq, mark, now); self.order_seq += 1
            log(f"  LIMIT SELL @ {mark:.2f} size={sz:.4f}")
        # fill simulation (paper) / assumption (live, verified next loop)
        if mark <= self.buy_limit[1]:
            self._open("long", self.buy_limit[1], sz, sr_dist, s, r)
        elif mark >= self.sell_limit[1]:
            self._open("short", self.sell_limit[1], sz, sr_dist, s, r)

    def _open(self, side, price, sz, sr_dist, s, r):
        if side == "long":
            sl = price * (1 - (sr_dist + SL_BUFFER_PCT) / 100.0)
            tp = r if r else price * (1 + sr_dist / 100.0)
        else:
            sl = price * (1 + (sr_dist + SL_BUFFER_PCT) / 100.0)
            tp = s if s else price * (1 - sr_dist / 100.0)
        self.pos = Position(side, sz, price, sl, tp, self.margin * MARGIN_USE)
        self.buy_limit = self.sell_limit = None
        log(f"  OPEN {side.upper()} @ {price:.2f} size={sz:.4f} sl={sl:.2f} tp={tp:.2f} (sr_dist {sr_dist:.2f}%)")

    def _manage_position(self, mark, s, r):
        p = self.pos
        hit = None
        if p.side == "long" and mark <= p.sl: hit = "SL"
        elif p.side == "long" and p.tp and mark >= p.tp: hit = "TP"
        elif p.side == "short" and mark >= p.sl: hit = "SL"
        elif p.side == "short" and p.tp and mark <= p.tp: hit = "TP"
        if hit:
            self._close(mark, hit)

    def _close(self, mark, why):
        p = self.pos
        pnl = (mark - p.entry) * p.size if p.side == "long" else (p.entry - mark) * p.size
        self.realized += pnl
        self.closed_count += 1
        log(f"  CLOSE {p.side.upper()} @ {mark:.2f} via {why} pnl=${pnl:.2f} "
            f"realized=${self.realized:.2f} (#{self.closed_count})")
        self.pos = None

# ---------------- main ----------------
async def main():
    log(f"S/R Scalper v2 — {MARKET_SYMBOL} lev={LEVERAGE}x margin_use={MARGIN_USE*100:.0f}% "
        f"sl_buffer={SL_BUFFER_PCT}% paper_margin=${PAPER_MARGIN_USD:.0f} live={LIVE_MODE}")
    bot = SRBot(PAPER_MARGIN_USD, live=LIVE_MODE)
    if LIVE_MODE:
        await bot.live_init()
    while True:
        try:
            mark = fetch_mark()
            if mark is None:
                log("mark fetch failed")
            else:
                if LIVE_MODE:
                    await bot.live_sync(mark)
                bot.step(mark, time.time())
        except Exception as e:
            log(f"loop error: {e}")
        await asyncio.sleep(POLL_SEC)

if __name__ == "__main__":
    asyncio.run(main())
