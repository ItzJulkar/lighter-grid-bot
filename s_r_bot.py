#!/usr/bin/env python3
"""
Lighter S/R Scalper — v3.1 (candle-based structure + zone trading + confluence filters)

v3.1 adds CONFLUENCE FILTERS (user spec: "ekadhik indicator select koro, 2-3
confirmation pailei trade execute"):
  - EMA 9/21      : trend direction (long only when price > EMA9 > EMA21, short mirrored)
  - VWAP (session): institutional bias, 00:00 UTC anchor (price > VWAP -> long bias)
  - RSI(7)        : at support RSI < 30 = oversold confirmation; at resistance RSI > 70
  - Volume spike  : zone-edge touch volume >= 1.5x avg(50) = strong level
  - ATR(14)       : SL floor (stop at least 1.5x ATR from entry; never inside noise)
  A side is ARMED only when >= CONFIRM_THRESHOLD (default 2) align.
  agent_zone.json overrides bypass confluence (explicit human/agent levels).

v3 base (unchanged):
  A. 5m/24h candle S/R zones from the Lighter candles API
  B. zone trading: inside -> buy@S + sell@R | below -> buy-dip@S | above -> sell-rally@R
  C. persistent ledger.json + state.json (position resumes after restart)

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

# ---- confluence filters (v3.1, research-adjusted) ----
# Sources: eplanetbrokers.com (RSI 5-7 / 80-20 or 75-25; 70/30 = false alarms),
#          sahi.com (VWAP direction gate; max 2-3 indicators agreeing),
#          mudrex.com (crypto session VWAP anchored 00:00 UTC),
#          quantvps.com / luxalgo.com (ATR stops 1.5x-2x for day trading)
CONFIRM_THRESHOLD = 2        # min aligned confirmations to arm a side (user: 2-3)
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 7
RSI_CONF_LONG = 30           # RSI below this at support = oversold confirmation (ranges: buy at 30)
RSI_CONF_SHORT = 70          # RSI above this at resistance = overbought confirmation (sell at 70)
VWAP_SESSION_UTC = True      # session VWAP anchored at 00:00 UTC (crypto 24/7 standard)
VWAP_MIN_BARS = 30           # fallback to rolling 288 if fewer session bars
VOL_SPIKE_RATIO = 1.5        # zone-edge touch volume >= 1.5x avg(50) = strong level
ATR_PERIOD = 14
ATR_SL_FLOOR = 1.5           # SL at least 1.5 x ATR(14) from entry (day-trading range 1.5-2x)

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

# ---------------- indicator math ----------------
def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes, period):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    g = sum(gains[-period:]) / period
    l = sum(losses[-period:]) / period
    if l == 0:
        return 100.0
    return 100.0 - 100.0 / (1 + g / l)

def vwap(candles):
    """Session VWAP anchored at 00:00 UTC (crypto 24/7 standard, per mudrex.com).
    Falls back to a rolling 288-bar VWAP if fewer than VWAP_MIN_BARS since midnight."""
    if not candles:
        return None
    now_t = candles[-1]["t"]
    day_start = int(now_t // 86400) * 86400
    seg = [c for c in candles if c["t"] >= day_start and c["v"] > 0]
    if len(seg) < VWAP_MIN_BARS:
        seg = [c for c in candles[-CANDLE_BARS:] if c["v"] > 0]
    if not seg:
        return None
    pv = sum(((c["h"] + c["l"] + c["c"]) / 3) * c["v"] for c in seg)
    vol = sum(c["v"] for c in seg)
    return pv / vol if vol > 0 else None

def atr(candles, period):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period

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
    """List of dicts {t(sec),o,h,l,c,v} or None. Zero-value candles are omitted by the API."""
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
    return [{"t": c["t"] // 1000, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v", 0.0)}
            for c in d.get("c", [])]

# ---------------- structure detection ----------------
class Structure:
    def __init__(self):
        self.candles = []       # list of {t,o,h,l,c,v} (t in seconds), last = forming candle
        self.zones = []         # list of dicts {s, r, vs, vr} merged boxes + volume ratios
        self.signals = {}       # confluence signals computed on refresh
        self.last_zone_sig = None

    def seed(self, candles):
        if candles:
            self.candles = candles

    def update_candle(self, mark, now):
        if self.candles and now < self.candles[-1]["t"] + res_ms(CANDLE_RES) / 1000:
            c = self.candles[-1]
            c["h"] = max(c["h"], mark); c["l"] = min(c["l"], mark); c["c"] = mark
        else:
            self.candles.append({"t": now, "o": mark, "h": mark, "l": mark, "c": mark, "v": 0.0})
            if len(self.candles) > CANDLE_BARS + 1:
                self.candles = self.candles[-(CANDLE_BARS + 1):]

    def refresh(self):
        """Re-fetch closed candles from the API and recompute structure + signals."""
        d = fetch_candles(CANDLE_RES, CANDLE_BARS)
        if not d:
            return False
        if self.candles and d and d[-1]["t"] == self.candles[-1]["t"]:
            d[-1] = self.candles[-1]    # keep the fresher locally-built forming candle
        self.candles = d
        self._recompute()
        self._compute_signals()
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
                highs.append((h, closed[i]["v"]))
            if l == min(c["l"] for c in closed[i - lb:i + lb + 1]):
                lows.append((l, closed[i]["v"]))
        def cluster(levels):
            if not levels:
                return []
            out = []
            for lv, vv in sorted(levels):
                if out and abs(lv - out[-1][0]) / out[-1][0] * 100 <= MERGE_PCT:
                    out[-1] = ((out[-1][0] + lv) / 2, max(out[-1][1], vv))
                else:
                    out.append((lv, vv))
            return out
        return cluster(highs), cluster(lows)

    def _touch_vol_ratio(self, level, side):
        """Max volume of candles rejecting AT the level / avg volume(50). 0 if none."""
        closed = self.candles[:-1]
        if len(closed) < 51:
            return 0.0
        avg = sum(c["v"] for c in closed[-50:]) / 50
        if avg <= 0:
            return 0.0
        if side == "s":
            touches = [c for c in closed[-CANDLE_BARS:]
                       if c["l"] <= level and c["l"] >= level * (1 - 0.003)]
        else:
            touches = [c for c in closed[-CANDLE_BARS:]
                       if c["h"] >= level and c["h"] <= level * (1 + 0.003)]
        if not touches:
            return 0.0
        return max(c["v"] for c in touches) / avg

    def _recompute(self):
        res, sup = self._pivot_levels()
        zones = []
        for r, _rv in res:
            cands = [(s, sv) for s, sv in sup if s < r and (r - s) / s * 100 <= MAX_ZONE_PCT]
            if cands:
                s, _sv = max(cands, key=lambda x: x[0])
                if (r - s) / s * 100 >= MIN_SR_DIST_PCT:
                    zones.append([s, r])
        zones.sort(key=lambda z: z[0])
        merged = []
        for z in zones:
            if merged and z[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], z[1])
            else:
                merged.append(list(z))
        self.zones = [{"s": round(s, 2), "r": round(r, 2),
                       "vs": round(self._touch_vol_ratio(s, "s"), 2),
                       "vr": round(self._touch_vol_ratio(r, "r"), 2)}
                      for s, r in merged]
        sig = tuple((z["s"], z["r"]) for z in self.zones)
        if sig != self.last_zone_sig:
            self.last_zone_sig = sig
            log(f"STRUCTURE: {len(self.zones)} zone(s) — "
                f"{[{'z': [z['s'], z['r']], 'vol': [z['vs'], z['vr']]} for z in self.zones] if self.zones else 'none'}")

    def _compute_signals(self):
        closed = self.candles[:-1]
        s = {"ema_bull": False, "ema_bear": False,
             "vwap_bull": False, "vwap_bear": False,
             "rsi": None, "atr_pct": None}
        if not closed:
            self.signals = s
            return
        closes = [c["c"] for c in closed]
        price = closes[-1]
        e9 = ema(closes, EMA_FAST); e21 = ema(closes, EMA_SLOW)
        if e9 is not None and e21 is not None:
            s["ema_bull"] = price > e9 > e21
            s["ema_bear"] = price < e9 < e21
        vw = vwap(closed)
        if vw is not None:
            s["vwap_bull"] = price > vw
            s["vwap_bear"] = price < vw
        s["rsi"] = rsi(closes, RSI_PERIOD)
        a = atr(closed, ATR_PERIOD)
        if a is not None:
            s["atr_pct"] = a / price * 100.0
        self.signals = s
        log(f"SIGNALS: ema={'BULL' if s['ema_bull'] else 'BEAR' if s['ema_bear'] else '-'} "
            f"vwap={'BULL' if s['vwap_bull'] else 'BEAR' if s['vwap_bear'] else '-'} "
            f"rsi={s['rsi'] if s['rsi'] is not None else '-'} "
            f"atr%={s['atr_pct'] if s['atr_pct'] is not None else '-'}")

    def select_zone(self, mark):
        """Return (mode, zone_dict) with mode in {'inside','buy','sell'} or (None, None)."""
        inside = [z for z in self.zones if z["s"] <= mark <= z["r"]]
        if inside:
            z = min(inside, key=lambda z: z["r"] - z["s"])
            return "inside", z
        above = [z for z in self.zones if z["s"] > mark]
        if above:
            z = min(above, key=lambda z: z["s"])
            if (z["s"] - mark) / mark * 100 <= RECLAIM_DIST_PCT:
                return "buy", z
        below = [z for z in self.zones if z["r"] < mark]
        if below:
            z = max(below, key=lambda z: z["r"])
            if (mark - z["r"]) / mark * 100 <= RECLAIM_DIST_PCT:
                return "sell", z
        return None, None

# ---------------- bot ----------------
class SRBot:
    def __init__(self, margin, live=False):
        self.margin = margin
        self.live = live
        self.struct = Structure()
        self.pos = None
        self.resting = []        # list of dicts: {side, price, size, sl, tp, place_mark, mark_min, mark_max}
        self.closed_count = 0
        self.realized = 0.0
        self.order_seq = 5000
        self.client = None
        self.account_api = None
        self.last_refresh = 0.0
        self.last_state_save = 0.0
        self.agent_file = None
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
                return {"s": s, "r": r, "vs": 0.0, "vr": 0.0}
        return None

    # ---- confluence ----
    def _side_conf(self, side, zone, mark):
        """Confirmations for one side. Returns (count, [names])."""
        sig = self.struct.signals
        confs = []
        if side == "long":
            if sig.get("ema_bull"): confs.append("ema")
            if sig.get("vwap_bull"): confs.append("vwap")
            if sig.get("rsi") is not None and sig["rsi"] < RSI_CONF_LONG: confs.append("rsi")
            if zone.get("vs", 0) >= VOL_SPIKE_RATIO: confs.append("vol")
        else:
            if sig.get("ema_bear"): confs.append("ema")
            if sig.get("vwap_bear"): confs.append("vwap")
            if sig.get("rsi") is not None and sig["rsi"] > RSI_CONF_SHORT: confs.append("rsi")
            if zone.get("vr", 0) >= VOL_SPIKE_RATIO: confs.append("vol")
        return len(confs), confs

    def _sl_dist_pct(self, sr_dist):
        atr_pct = self.struct.signals.get("atr_pct")
        base = sr_dist + SL_BUFFER_PCT
        if atr_pct is None:
            return base
        return max(base, ATR_SL_FLOOR * atr_pct)

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
        """Arm/cancel resting orders. Auto mode: per-side confluence (>= threshold).
        Agent mode: explicit override, no confluence.
        Logging is change-gated: decisions print only when the armed set changes."""
        agent_trade = self._agent_setup(mark)
        if agent_trade:
            side, price, sl, tp = agent_trade
            desired = [(side, round(price, 2), round(sl, 2), round(tp, 2))]
            notes = [f"AGENT LIMIT {side.upper()} @ {price:.2f} sl={sl:.2f} tp={tp:.2f}"]
        else:
            az = self._agent_zone(mark)
            if az:
                mode = "inside" if az["s"] <= mark <= az["r"] else ("buy" if mark < az["s"] else "sell")
                zone = az
            else:
                mode, zone = self.struct.select_zone(mark)
            desired, notes = [], []
            if zone is not None:
                sr_dist = (zone["r"] - zone["s"]) / zone["s"] * 100.0
                sl_dist = self._sl_dist_pct(sr_dist)
                if mode in ("inside", "buy"):
                    n, names = self._side_conf("long", zone, mark)
                    if n >= CONFIRM_THRESHOLD:
                        sl = zone["s"] * (1 - sl_dist / 100.0)
                        desired.append(("long", round(zone["s"], 2), round(sl, 2), round(zone["r"], 2)))
                        notes.append(f"ARM LONG @ {zone['s']:.2f} (conf {n}/{CONFIRM_THRESHOLD}: {','.join(names)})")
                    else:
                        notes.append(f"skip LONG @ {zone['s']:.2f} (conf {n}/{CONFIRM_THRESHOLD}: {','.join(names) or 'none'})")
                if mode in ("inside", "sell"):
                    n, names = self._side_conf("short", zone, mark)
                    if n >= CONFIRM_THRESHOLD:
                        sl = zone["r"] * (1 + sl_dist / 100.0)
                        desired.append(("short", round(zone["r"], 2), round(sl, 2), round(zone["s"], 2)))
                        notes.append(f"ARM SHORT @ {zone['r']:.2f} (conf {n}/{CONFIRM_THRESHOLD}: {','.join(names)})")
                    else:
                        notes.append(f"skip SHORT @ {zone['r']:.2f} (conf {n}/{CONFIRM_THRESHOLD}: {','.join(names) or 'none'})")
            elif mode is None:
                notes.append("PAUSE: no valid S/R zone")

        current = sorted((o["side"], round(o["price"], 2), round(o["sl"], 2), round(o["tp"], 2))
                         for o in self.resting)
        want = sorted(desired)
        if want != current:
            self.resting = []
            if notes:
                for note in notes:
                    log("  " + note)
            if want:
                self._arm(want, mark)
        if self.resting:
            self._check_fills(mark, now)

    def _arm(self, specs, mark):
        sz = self.size_for(mark)
        for side, price, sl, tp in specs:
            self.resting.append({"side": side, "price": price, "size": sz,
                                 "sl": sl, "tp": tp, "place_mark": mark,
                                 "mark_min": mark, "mark_max": mark})
            log(f"  LIMIT {side.upper()} @ {price:.2f} size={sz:.4f} sl={sl:.2f} tp={tp:.2f}")

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
    log(f"S/R Scalper v3.1 — {MARKET_SYMBOL} lev={LEVERAGE}x margin_use={MARGIN_USE*100:.0f}% "
        f"confluence={CONFIRM_THRESHOLD}/{EMA_FAST}-{EMA_SLOW}EMA/VWAP-sess/RSI{RSI_PERIOD}/vol{VOL_SPIKE_RATIO}/ATR{ATR_SL_FLOOR} "
        f"candles={CANDLE_RES}/{CANDLE_BARS} paper_margin=${PAPER_MARGIN_USD:.0f} live={LIVE_MODE}")
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
