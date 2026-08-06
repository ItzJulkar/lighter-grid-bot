#!/usr/bin/env python3
"""
Lighter S/R Scalper — v3 (candle-based structure + zone trading + persistent ledger)

v3 changes (from v2):
  A. Candle-based S/R detection — 5m candles from the Lighter candles API
     (24h lookback, 288 bars), swing pivots on candle highs/lows.
     v2 sampled mark price every 2s and only saw ~6.7 minutes of history, so it
     could never see the multi-hour boxes a human sees on the chart.
  B. Zone trading — trades the actual support/resistance structure:
       - price inside a zone  -> resting buy at support + resting sell at resistance
       - price below a zone   -> "buy the dip" limit at the zone support (reclaim play)
       - price above a zone   -> "sell the rally" limit at the zone resistance
     v2 required a support below AND resistance above price (straddle), so it
     PAUSED whenever price sat outside every detected box.
  C. Persistent ledger (ledger.json) + state (state.json) — realized PnL and
     trade history survive restarts; an open position resumes after restart.
  D. agent_zone.json override — an external agent (e.g. Hermes cron) can write
       {"support": S, "resistance": R}            -> force a zone (auto mode logic)
       {"side":"long","entry":E,"sl":S,"tp":T}    -> fully-specified single trade
     The bot polls the file every loop iteration.

Paper mode default (simulated fills, $100 virtual). LIVE mode needs
api_key_config.json + live order wiring (live_sync stub — NOT production ready).
"""
import asyncio, json, time, datetime, os
import requests

# ---------------- config ----------------
MARKET_ID = 0
MARKET_SYMBOL = "ETH"
LEVERAGE = 15.0
MARGIN_USE = 0.80            # 80% of margin per position
SL_BUFFER_PCT = 0.3          # SL = zone_dist% + this (beyond the level)
POLL_SEC = 2
CANDLE_RES = "5m"
CANDLE_BARS = 288            # 24h of 5m candles
CANDLE_REFRESH_SEC = 120     # re-fetch candles every 2 min (self-healing)
SWING_LOOKBACK = 10          # pivot = high/low of +/- this many closed bars
MERGE_PCT = 0.12             # cluster pivot levels within 0.12% into one level
MIN_SR_DIST_PCT = 0.15       # discard zones tighter than this (noise)
MAX_ZONE_PCT = 4.0           # don't trade >4% ranges as mean-reversion zones
RECLAIM_DIST_PCT = 1.5       # max price->zone-edge distance for reclaim/pullback setup
PAPER_MARGIN_USD = 100.0
LIVE_MODE = False
API_KEY_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_key_config.json")
BASE_URL = "https://mainnet.zklighter.elliot.ai"
LOG_FILE = "srb.log"
LEDGER_FILE = "ledger.json"
STATE_FILE = "state.json"
AGENT_ZONE_FILE = "agent_zone.json"
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

def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)

def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

class Position:
    __slots__ = ("side", "size", "entry", "sl", "tp", "margin", "opened_at")
    def __init__(self, side, size, entry, sl, tp, margin, opened_at=None):
        self.side = side; self.size = size; self.entry = entry
        self.sl = sl; self.tp = tp; self.margin = margin
        self.opened_at = opened_at or time.time()
    def to_dict(self):
        return {"side": self.side, "size": self.size, "entry": self.entry,
                "sl": self.sl, "tp": self.tp, "margin": self.margin, "opened_at": self.opened_at}
    @staticmethod
    def from_dict(d):
        if not d: return None
        return Position(d["side"], d["size"], d["entry"], d["sl"], d["tp"], d["margin"], d.get("opened_at"))

# ---------------- market data ----------------
_session = requests.Session()
RES_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}

def res_ms(res):
    n = int(res[:-1]); u = res[-1]
    return n * RES_MS[u]

def fetch_mark():
    r = _session.get(f"{BASE_URL}/api/v1/orderBookDetails", timeout=10)
    for m in r.json()["order_book_details"]:
        if m["market_id"] == MARKET_ID:
            return float(m["mark_price"])
    return None

def fetch_candles(res, count_back):
    """Return list of dicts {t(sec),o,h,l,c} or None. Zero-value candles are omitted by the API."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - res_ms(res) * count_back
    try:
        r = _session.get(f"{BASE_URL}/api/v1/candles", params={
            "market_id": MARKET_ID, "resolution": res,
            "start_timestamp": start_ms, "end_timestamp": now_ms,
            "count_back": count_back}, timeout=10)
        d = r.json()
    except Exception as e:
        log(f"candles fetch error: {e}")
        return None
    if d.get("code") != 200:
        log(f"candles API error: {d}")
        return None
    return [{"t": c["t"] // 1000, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"]} for c in d.get("c", [])]

# ---------------- structure detection ----------------
class Structure:
    def __init__(self):
        self.candles = []       # list of {t,o,h,l,c} (t in seconds), last = forming candle
        self.zones = []         # list of (support, resistance) merged boxes
        self.last_zone_sig = None

    def seed(self, candles):
        if candles:
            self.candles = candles

    def update_candle(self, mark, now):
        if self.candles and now < self.candles[-1]["t"] + res_ms(CANDLE_RES) / 1000:
            c = self.candles[-1]
            c["h"] = max(c["h"], mark); c["l"] = min(c["l"], mark); c["c"] = mark
        else:
            self.candles.append({"t": now, "o": mark, "h": mark, "l": mark, "c": mark})
            if len(self.candles) > CANDLE_BARS + 1:
                self.candles = self.candles[-(CANDLE_BARS + 1):]

    def refresh(self):
        """Re-fetch closed candles from the API and recompute structure."""
        d = fetch_candles(CANDLE_RES, CANDLE_BARS)
        if not d:
            return False
        if self.candles and d and d[-1]["t"] == self.candles[-1]["t"]:
            d[-1] = self.candles[-1]    # keep the fresher locally-built forming candle
        self.candles = d
        self._recompute()
        return True

    def _pivot_levels(self):
        """Pivots from CLOSED candles only (no repainting on the forming bar)."""
        closed = self.candles[:-1]
        n = len(closed)
        lb = SWING_LOOKBACK
        if n < lb * 2 + 1:
            return [], []
        highs, lows = [], []
        for i in range(lb, n - lb):
            h = closed[i]["h"]; l = closed[i]["l"]
            if h == max(c["h"] for c in closed[i - lb:i + lb + 1]):
                highs.append(h)
            if l == min(c["l"] for c in closed[i - lb:i + lb + 1]):
                lows.append(l)
        def cluster(levels):
            if not levels:
                return []
            out = []
            for lv in sorted(levels):
                if out and abs(lv - out[-1]) / out[-1] * 100 <= MERGE_PCT:
                    out[-1] = (out[-1] + lv) / 2
                else:
                    out.append(lv)
            return out
        return cluster(highs), cluster(lows)

    def _recompute(self):
        res, sup = self._pivot_levels()
        zones = []
        for r in res:
            cands = [s for s in sup if s < r and (r - s) / s * 100 <= MAX_ZONE_PCT]
            if cands:
                s = max(cands)
                if (r - s) / s * 100 >= MIN_SR_DIST_PCT:
                    zones.append([s, r])
        zones.sort(key=lambda z: z[0])
        merged = []
        for z in zones:
            if merged and z[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], z[1])
            else:
                merged.append(list(z))
        self.zones = [[round(s, 2), round(r, 2)] for s, r in merged]
        sig = tuple(tuple(z) for z in self.zones)
        if sig != self.last_zone_sig:
            self.last_zone_sig = sig
            log(f"STRUCTURE: {len(self.zones)} zone(s) — {self.zones if self.zones else 'none'}")

    def select_zone(self, mark):
        """Return (mode, s, r): mode in {'inside','buy','sell'} or (None,None,None)."""
        inside = [z for z in self.zones if z[0] <= mark <= z[1]]
        if inside:
            z = min(inside, key=lambda z: z[1] - z[0])
            return "inside", z[0], z[1]
        above = [z for z in self.zones if z[0] > mark]
        if above:
            z = min(above, key=lambda z: z[0])
            if (z[0] - mark) / mark * 100 <= RECLAIM_DIST_PCT:
                return "buy", z[0], z[1]
        below = [z for z in self.zones if z[1] < mark]
        if below:
            z = max(below, key=lambda z: z[1])
            if (mark - z[1]) / mark * 100 <= RECLAIM_DIST_PCT:
                return "sell", z[0], z[1]
        return None, None, None

# ---------------- bot ----------------
class SRBot:
    def __init__(self, margin, live=False):
        self.margin = margin
        self.live = live
        self.struct = Structure()
        self.pos = None
        self.resting = []        # list of dicts: {side, price, size, place_mark, mark_min, mark_max}
        self.setup = None        # (mode, s, r) currently armed, or ("agent", side, price, sl, tp)
        self.closed_count = 0
        self.realized = 0.0
        self.order_seq = 5000
        self.client = None
        self.account_api = None
        self.last_refresh = 0.0
        self.last_state_save = 0.0
        self.agent_file = None   # parsed agent_zone.json content
        self._load_ledger()
        self._load_state()

    # ---- persistence ----
    def _load_ledger(self):
        led = _load_json(LEDGER_FILE, None)
        if led:
            self.realized = float(led.get("realized", 0.0))
            self.closed_count = int(led.get("closed_count", 0))
            log(f"LEDGER loaded: realized=${self.realized:.2f} trades={self.closed_count}")

    def _load_state(self):
        st = _load_json(STATE_FILE, None)
        if st:
            self.pos = Position.from_dict(st.get("pos"))
            self.order_seq = int(st.get("order_seq", self.order_seq))
            if self.pos:
                log(f"STATE: resumed open {self.pos.side.upper()} @ {self.pos.entry:.2f} "
                    f"sl={self.pos.sl:.2f} tp={self.pos.tp:.2f}")

    def _save_state(self):
        _save_json(STATE_FILE, {"pos": self.pos.to_dict() if self.pos else None,
                                "order_seq": self.order_seq})

    def _save_ledger(self):
        _save_json(LEDGER_FILE, {"realized": round(self.realized, 4),
                                 "closed_count": self.closed_count})

    # ---- live plumbing (NOT production-ready; stub confirms client only) ----
    async def live_init(self):
        import lighter
        with open(API_KEY_CONFIG) as f:
            cfg = json.load(f)
        keys = {int(k): v for k, v in cfg["privateKeys"].items()}
        self.client = lighter.SignerClient(url=BASE_URL, account_index=cfg["accountIndex"],
                                           api_private_keys=keys, chain_id=304)
        err = self.client.check_client()
        if err is not None:
            raise RuntimeError(f"check_client: {err}")
        self.account_api = lighter.AccountApi(lighter.ApiClient(configuration=lighter.Configuration(host=BASE_URL)))
        log(f"live client ready (account {cfg['accountIndex']})")

    async def place_limit(self, side, price, size):
        oi = self.order_seq; self.order_seq += 1
        tx, tx_hash, err = await self.client.create_order(
            market_index=MARKET_ID, client_order_index=oi,
            base_amount=size_units(size), price=price_units(price),
            is_ask=(side == "sell"), order_type=self.client.ORDER_TYPE_LIMIT,
            time_in_force=self.client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=False, order_expiry=self.client.DEFAULT_28_DAY_ORDER_EXPIRY)
        if err is not None:
            log(f"  live order FAILED ({side}): {err}")
            return None
        return oi

    async def cancel(self, oi):
        tx, tx_hash, err = await self.client.cancel_order(market_index=MARKET_ID, order_index=oi)
        if err is not None:
            log(f"  cancel FAILED oi={oi}: {err}")

    async def market_close(self, side, size):
        worst = 1 if side == "long" else 1_000_000_00
        oi = self.order_seq; self.order_seq += 1
        tx, tx_hash, err = await self.client.create_order(
            market_index=MARKET_ID, client_order_index=oi,
            base_amount=size_units(size), price=worst,
            is_ask=(side == "short"), order_type=self.client.ORDER_TYPE_MARKET,
            time_in_force=self.client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            reduce_only=True)
        if err is not None:
            log(f"  market close FAILED ({side}): {err}")
        return err is None

    async def live_sync(self, mark):
        if self.client is not None and not getattr(self, "_live_checked", False):
            self._live_checked = True
            log("live_sync: client ok — real order wiring NOT implemented (paper only)")

    # ---- sizing ----
    def notional(self):
        return self.margin * MARGIN_USE * LEVERAGE
    def size_for(self, price):
        return self.notional() / price

    # ---- agent override ----
    def _load_agent(self):
        try:
            st = os.stat(AGENT_ZONE_FILE)
            if st.st_mtime > getattr(self, "_agent_mtime", 0):
                self._agent_mtime = st.st_mtime
                try:
                    with open(AGENT_ZONE_FILE) as f:
                        d = json.load(f)
                except Exception:
                    d = None
                self.agent_file = d if isinstance(d, dict) else None
                log(f"AGENT FILE: {self.agent_file}")
        except FileNotFoundError:
            if self.agent_file is not None:
                self.agent_file = None
                log("AGENT FILE: removed — reverting to auto structure")
        except Exception:
            pass

    def _agent_setup(self, mark):
        """If agent file gives a full trade spec, return its (side, price, sl, tp)."""
        d = self.agent_file or {}
        if all(k in d for k in ("side", "entry", "sl", "tp")):
            side = str(d["side"]).lower()
            if side in ("long", "short"):
                return side, float(d["entry"]), float(d["sl"]), float(d["tp"])
        return None

    def _agent_zone(self, mark):
        d = self.agent_file or {}
        if "support" in d and "resistance" in d:
            s, r = float(d["support"]), float(d["resistance"])
            if 0 < s < r:
                if s <= mark <= r:
                    return "inside", s, r
                if mark < s:
                    return "buy", s, r
                return "sell", s, r
        return None

    # ---- core ----
    def step(self, mark, now):
        self.struct.update_candle(mark, now)
        if now - self.last_refresh > CANDLE_REFRESH_SEC:
            self.struct.refresh()
            self.last_refresh = now
        self._load_agent()
        last = self.struct.candles[-1] if self.struct.candles else None
        candle_low = last["l"] if last else mark
        candle_high = last["h"] if last else mark

        if self.pos is not None:
            self._manage_position(mark, candle_low, candle_high)
        if self.pos is None:
            self._manage_entries(mark, now)

        if self.pos is not None:
            p = self.pos
            pnl = (mark - p.entry) * p.size if p.side == "long" else (p.entry - mark) * p.size
            log(f"  pos={p.side} size={p.size:.4f} entry={p.entry:.2f} sl={p.sl:.2f} "
                f"tp={p.tp:.2f} pnl=${pnl:.2f}")

        if now - self.last_state_save > 30:
            self._save_state()
            self.last_state_save = now

    def _manage_entries(self, mark, now):
        """Arm/cancel resting limit orders from the operative zone (auto or agent)."""
        # decide desired setup
        agent_trade = self._agent_setup(mark)
        if agent_trade:
            side, price, sl, tp = agent_trade
            desired = ("agent", side, price, sl, tp)
        else:
            az = self._agent_zone(mark)
            mode, s, r = az if az else self.struct.select_zone(mark)
            if mode is None:
                desired = None
            elif mode == "inside":
                desired = ("inside", s, r)
            elif mode == "buy":
                desired = ("buy", s, r)
            else:
                desired = ("sell", s, r)

        if desired != self.setup:
            old = self.setup
            self.resting = []
            self.setup = desired
            if desired is None:
                log("PAUSE: no valid S/R zone — cancelling resting orders")
            else:
                log(f"SETUP: {desired} (was {old})")
            if desired is not None:
                self._arm(desired, mark)

        if self.resting:
            self._check_fills(mark, now)

    def _arm(self, setup, mark):
        sz = self.size_for(mark)
        if setup[0] == "agent":
            _, side, price, sl, tp = setup
            self.resting.append({"side": side, "price": price, "size": sz,
                                 "sl": sl, "tp": tp, "place_mark": mark,
                                 "mark_min": mark, "mark_max": mark})
            log(f"  AGENT LIMIT {side.upper()} @ {price:.2f} size={sz:.4f} sl={sl:.2f} tp={tp:.2f}")
            return
        mode, s, r = setup
        sr_dist = (r - s) / s * 100.0
        if mode in ("inside", "buy"):
            sl = s * (1 - (sr_dist + SL_BUFFER_PCT) / 100.0)
            tp = r
            self.resting.append({"side": "long", "price": s, "size": sz,
                                 "sl": sl, "tp": tp, "place_mark": mark,
                                 "mark_min": mark, "mark_max": mark})
            log(f"  LIMIT BUY @ {s:.2f} size={sz:.4f} sl={sl:.2f} tp={tp:.2f} (zone {s:.2f}-{r:.2f})")
        if mode in ("inside", "sell"):
            sl = r * (1 + (sr_dist + SL_BUFFER_PCT) / 100.0)
            tp = s
            self.resting.append({"side": "short", "price": r, "size": sz,
                                 "sl": sl, "tp": tp, "place_mark": mark,
                                 "mark_min": mark, "mark_max": mark})
            log(f"  LIMIT SELL @ {r:.2f} size={sz:.4f} sl={sl:.2f} tp={tp:.2f} (zone {s:.2f}-{r:.2f})")

    def _check_fills(self, mark, now):
        for o in self.resting:
            o["mark_min"] = min(o["mark_min"], mark)
            o["mark_max"] = max(o["mark_max"], mark)
            L = o["price"]
            fills = (L <= o["place_mark"] and o["mark_min"] <= L) or \
                    (L >= o["place_mark"] and o["mark_max"] >= L)
            if fills:
                self._open(o["side"], L, o["size"], o["sl"], o["tp"])
                return

    def _open(self, side, price, sz, sl, tp):
        self.pos = Position(side, sz, price, sl, tp, self.margin * MARGIN_USE)
        self.resting = []
        self.setup = None
        log(f"  OPEN {side.upper()} @ {price:.2f} size={sz:.4f} sl={sl:.2f} tp={tp:.2f}")

    def _manage_position(self, mark, candle_low, candle_high):
        p = self.pos
        hit = None
        if p.side == "long":
            if mark <= p.sl or candle_low <= p.sl: hit = "SL"
            elif p.tp and (mark >= p.tp or candle_high >= p.tp): hit = "TP"
        else:
            if mark >= p.sl or candle_high >= p.sl: hit = "SL"
            elif p.tp and (mark <= p.tp or candle_low <= p.tp): hit = "TP"
        if hit:
            self._close(mark, hit)

    def _close(self, mark, why):
        p = self.pos
        pnl = (mark - p.entry) * p.size if p.side == "long" else (p.entry - mark) * p.size
        self.realized += pnl
        self.closed_count += 1
        log(f"  CLOSE {p.side.upper()} @ {mark:.2f} via {why} pnl=${pnl:.2f} "
            f"realized=${self.realized:.2f} (#{self.closed_count})")
        led = _load_json(LEDGER_FILE, {"trades": []})
        trades = led.get("trades", [])
        trades.append({"ts": time.time(), "side": p.side, "size": round(p.size, 4),
                       "entry": round(p.entry, 2), "exit": round(mark, 2),
                       "pnl": round(pnl, 4), "why": why,
                       "sl": round(p.sl, 2), "tp": round(p.tp, 2)})
        _save_json(LEDGER_FILE, {"realized": round(self.realized, 4),
                                 "closed_count": self.closed_count,
                                 "trades": trades[-500:]})
        self.pos = None
        self._save_state()

# ---------------- main ----------------
async def main():
    log(f"S/R Scalper v3 — {MARKET_SYMBOL} lev={LEVERAGE}x margin_use={MARGIN_USE*100:.0f}% "
        f"sl_buffer={SL_BUFFER_PCT}% candles={CANDLE_RES}/{CANDLE_BARS} paper_margin=${PAPER_MARGIN_USD:.0f} live={LIVE_MODE}")
    bot = SRBot(PAPER_MARGIN_USD, live=LIVE_MODE)
    if LIVE_MODE:
        await bot.live_init()
    ok = bot.struct.refresh()
    if not ok:
        log("WARN: initial candle fetch failed — will retry in the loop")
    bot.last_refresh = time.time()
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
