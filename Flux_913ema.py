"""
Flux_913ema.py  —  OB Flux Triple-Confirmation Trading System
=============================================================
Version: 7.0  (3-Index Only · Smart Candle Cache · API Timeout Fix)

Changes from v6:
  1.  INSTRUMENTS TRIMMED TO 3 — NIFTY, BANKNIFTY, SENSEX only.
      All references to FINNIFTY, MIDCPNIFTY, CRUDEOIL have been removed
      from OPTIONS_CFG, INSTRUMENT_ALIAS, INDEX_KEY_MAP, INDEX_ALLOWED_DAYS,
      _MARGIN_FLOOR, and INDEX_POLL_CFG.

  2.  SMART /data ENDPOINT — eliminates API timeouts on 5-second polls.
      The UI polls /data every 5 s, but NIFTY/SENSEX candles are 10-minute
      and BANKNIFTY is 10-minute.  Previously this called download_candles()
      (2 × historical_data API round-trips) on EVERY poll, causing Kite
      rate-limit timeouts.  Now:
        • A per-(symbol,interval,candle_open) result cache is maintained.
        • Cache hit (same candle): zero Kite API calls.  Only in-memory
          exit/trail/E5 logic runs — returns in <1 ms.
        • Cache miss (new candle): full fetch + compute, stored in cache.
      Effective Kite API call rate drops from ~720/hour to ~4/hour per symbol.

  3.  HERMES ENDPOINT ALSO CACHE-AWARE — /hermes_brain uses
      _get_cached_candles instead of download_candles.

  4.  update_decision_outcome SIGNATURE FIX — accepts both the original
      positional form and the keyword-arg form used by exit_trade_fn:
        update_decision_outcome(decision_id=..., outcome=...,
                                pts_captured=..., actual_rr=..., exit_reason=...)

Previous v6 changes (all retained):
  • Background polling threads per index
  • Duplicate signal dedup guard
  • Fund sufficiency check
  • Per-index candle cache (_get_cached_candles)
  • NRML LIMIT orders, Hermes learning loop
"""

import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Imports ────────────────────────────────────────────────────────────────────
from hermes_brain import (
    startup, hermes_analyse, get_last_output, get_brain_stats,
    check_ollama_health, update_decision_outcome, build_market_prompt,
    call_hermes3, warmup, resolve_fut_token, fetch_futures_volume,
    enrich_obs_with_futures_vol, set_auto_cfg, get_auto_cfg, get_auto_log,
    get_portfolio, record_daily_profit, is_market_hours,
    hermes_mtf_scan, update_mtf_outcome, MTF_INTERVALS,
)

from flask import Flask, render_template, request, jsonify
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
import pytz, threading, time, logging
from kiteconnect import KiteConnect

log = logging.getLogger("flux_913ema")
from Auto_toptp_Engine import initialize_kite_session

app  = Flask(__name__, static_folder="static", template_folder="templates")
kite: KiteConnect = initialize_kite_session()
IST  = pytz.timezone("Asia/Kolkata")

# ── Constants ──────────────────────────────────────────────────────────────────
YF_TO_KITE = {
    "3m":"3minute","5m":"5minute","10m":"10minute",
    "15m":"15minute","30m":"30minute","60m":"60minute","1h":"60minute","1d":"day",
}
LOOKBACK = {
    "3minute":30,"5minute":30,"10minute":30,
    "15minute":30,"30minute":30,"60minute":60,"day":120,
}
# How many trailing days of candles are sent to the chart for display.
# Indicators/order-blocks/signals are still computed on the full LOOKBACK
# window above — only the chart payload is trimmed.
CHART_DAYS = 10
OPTIONS_CFG = {
    # lot_size intentionally removed — always fetched live from Kite master.
    # strike_gap is purely for ATM rounding and does NOT affect order quantity.
    "NIFTY":     {"exchange":"NFO", "strike_gap":50},
    "BANKNIFTY": {"exchange":"NFO", "strike_gap":100},
    "SENSEX":    {"exchange":"BFO", "strike_gap":100},
}
INSTRUMENT_ALIAS = {
    "^NSEI"     : ("NSE","NIFTY 50"),
    "NSEBANK"   : ("NSE","NIFTY BANK"),
    "^NSEBANK"  : ("NSE","NIFTY BANK"),
    "SENSEX"    : ("BSE","SENSEX"),
}
# Map symbol → index key used in OPTIONS_CFG / futures lookup
INDEX_KEY_MAP = {
    "^NSEI":"NIFTY", "NSEBANK":"BANKNIFTY", "^NSEBANK":"BANKNIFTY",
    "SENSEX":"SENSEX",
}

# ── Trading Calendar ───────────────────────────────────────────────────────────
# Each index is allowed ONLY on specific weekdays (0=Mon … 6=Sun).
# On expiry day, trading is permitted only up to 11:00 AM IST.
# BANKNIFTY is allowed for the last 10 calendar days before its expiry.
INDEX_ALLOWED_DAYS = {
    #              Mon Tue Wed Thu Fri
    "NIFTY":      {3, 4, 0},          # Thu, Fri, Mon
    "SENSEX":     {0, 1, 2},          # Mon, Tue, Wed
    "BANKNIFTY":  {0, 1, 2, 3, 4},   # all weekdays — filtered by 10-day expiry window
}
EXPIRY_CUTOFF_HOUR = 11   # On expiry day, stop trading at 11:00 AM IST


# ── Instruments TTL cache ──────────────────────────────────────────────────────
# kite.instruments() returns ~100k rows and counts as a heavy API call.
# Cache per exchange for _INSTRUMENTS_TTL seconds so that _nearest_expiry,
# get_atm_option, and /account never hit Kite more than once per 5 minutes.
_instruments_cache: dict = {}       # exchange → {"ts": float, "data": list}
_instruments_cache_lock = threading.Lock()
_INSTRUMENTS_TTL = 300              # 5 minutes


def _get_instruments(exchange: str) -> list:
    """Return instruments list for exchange from TTL cache or fresh Kite call."""
    now_ts = time.time()
    with _instruments_cache_lock:
        cached = _instruments_cache.get(exchange)
        if cached and (now_ts - cached["ts"]) < _INSTRUMENTS_TTL:
            return cached["data"]
    # Fetch outside the lock to avoid blocking other threads
    try:
        data = kite.instruments(exchange)
    except Exception as e:
        print(f"[INSTRUMENTS] kite.instruments({exchange}) failed: {e}")
        # Return stale cache if available rather than nothing
        with _instruments_cache_lock:
            cached = _instruments_cache.get(exchange)
        return cached["data"] if cached else []
    with _instruments_cache_lock:
        _instruments_cache[exchange] = {"ts": time.time(), "data": data}
    return data


def _nearest_expiry(cfg_key: str) -> date | None:
    """Return the nearest upcoming expiry date for cfg_key from Kite master."""
    cfg = OPTIONS_CFG.get(cfg_key)
    if not cfg:
        return None
    today = datetime.now(IST).date()
    insts = _get_instruments(cfg["exchange"])
    if not insts:
        print(f"[CALENDAR] No instruments data for {cfg_key} ({cfg['exchange']}) "
              f"— expiry unknown, proceeding with caution")
        return None
    expiries = sorted({
        i["expiry"] for i in insts
        if i.get("name", "").upper() == cfg_key
        and i.get("expiry") and i["expiry"] >= today
    })
    return expiries[0] if expiries else None


def is_index_tradeable(cfg_key: str) -> tuple[bool, str]:
    """
    Returns (allowed: bool, reason: str).

    Rules enforced:
    1. Weekday must be in INDEX_ALLOWED_DAYS for the index.
    2. On expiry day, current time must be < EXPIRY_CUTOFF_HOUR (11 AM IST).
    3. For BANKNIFTY: only the last 10 calendar days before expiry are allowed.
    """
    now      = datetime.now(IST)
    today    = now.date()
    weekday  = today.weekday()  # 0=Mon, …, 6=Sun

    allowed_days = INDEX_ALLOWED_DAYS.get(cfg_key, set())
    if weekday not in allowed_days:
        return False, f"{cfg_key} not traded on {today.strftime('%A')}"

    expiry = _nearest_expiry(cfg_key)
    if expiry is None:
        # Cannot confirm expiry → allow, but log warning
        print(f"[CALENDAR] Could not fetch expiry for {cfg_key} — proceeding with caution")
        return True, "expiry unknown"

    # On expiry day: block after 11 AM
    if today == expiry and now.hour >= EXPIRY_CUTOFF_HOUR:
        return False, (f"{cfg_key} expiry day cutoff: "
                       f"past {EXPIRY_CUTOFF_HOUR}:00 AM (expiry={expiry})")

    # BANKNIFTY: only last 10 calendar days before expiry (0 .. 9 DTE)
    if cfg_key == "BANKNIFTY":
        days_to_expiry = (expiry - today).days
        if days_to_expiry > 9:
            return False, (f"BANKNIFTY: {days_to_expiry}d to expiry — "
                           f"only last 10 days allowed (expiry={expiry})")

    return True, f"allowed (expiry={expiry}, dte={(expiry - today).days})"


# ── Quantity Calculator (funds-based) ─────────────────────────────────────────
# Sizes the trade directly off the live ATM option premium and the account's
# real-time available cash (margins("equity").available.live_balance):
#     usable_cash = live_balance * _USABLE_FUNDS_PCT
#     lots        = floor(usable_cash / (option_ltp * lot_size))
# Returns 0 if even 1 lot can't be afforded — caller must skip the trade.

_USABLE_FUNDS_PCT = 0.90  # allocate up to 90% of live available balance per trade
_MARGIN_FLOOR = {         # used only by _can_afford_all_today() below as a
    "NIFTY":      3_000,  # coarse worst-case per-lot estimate for the daily
    "BANKNIFTY":  4_000,  # multi-index affordability pre-check.
    "SENSEX":     3_500,
}


def calculate_lots(cfg_key: str, lot_size: int, option_ltp: float = 0.0) -> int:
    """
    Compute how many lots to trade based on live available funds and the
    actual ATM option premium. Returns 0 if even 1 lot can't be afforded.
    """
    try:
        cash = kite.margins("equity")["available"]["live_balance"]
    except Exception as e:
        print(f"[QTY CALC] Could not fetch margins: {e} — defaulting to 1 lot")
        return 1

    if option_ltp <= 0 or lot_size <= 0:
        print(f"[QTY CALC] {cfg_key}: missing ltp/lot_size — defaulting to 1 lot")
        return 1

    usable_cash = cash * _USABLE_FUNDS_PCT
    lots = int(usable_cash // (option_ltp * lot_size))
    print(f"[QTY CALC] {cfg_key}: cash=₹{cash:,.0f} ltp={option_ltp} lot_size={lot_size} "
          f"→ {lots} lot(s)")
    return lots



# ── Per-index preferred candle interval & poll frequency ──────────────────────
# interval_str  : passed to download_candles / generate_signals
# poll_minutes  : how often the poller wakes for this index
# The poller sleeps to the next candle boundary so it always processes a
# freshly closed candle, never a half-formed one.
INDEX_POLL_CFG = {
    # cfg_key       symbol        interval  poll_minutes
    # ── Moved to 10m candles for NIFTY & SENSEX per trading results ──
    "NIFTY":     {"symbol": "^NSEI",    "interval": "10m", "poll_minutes": 10},
    "SENSEX":    {"symbol": "SENSEX",   "interval": "10m", "poll_minutes": 10},
    "BANKNIFTY": {"symbol": "^NSEBANK", "interval": "10m", "poll_minutes": 10},
}

# ── Candle cache ───────────────────────────────────────────────────────────────
# Keyed by (symbol, interval).
# Two-layer protection against Kite rate-limit errors:
#
#  Layer 1 — candle-boundary cache:
#    Stores the fetched DataFrame until the candle closes.
#    NIFTY/SENSEX 15-min candles → one fetch per 15 min per symbol.
#    BANKNIFTY 10-min candles   → one fetch per 10 min per symbol.
#    All /data and /hermes_brain calls within the same candle share one df.
#
#  Layer 2 — per-key fetch lock + minimum re-fetch interval:
#    Even on a cache-miss, only ONE thread may call download_candles at a time
#    for a given (symbol, interval).  Other threads wait and then reuse the
#    result.  Additionally a hard 60-second minimum between fetches is enforced
#    regardless of candle boundary (protects against rapid interval switches
#    from the UI).
#
_candle_cache: dict = {}           # key → {"df": df, "candle_open": datetime, "fetched_at": float}
_cache_lock   = threading.Lock()
_fetch_locks: dict = {}            # key → threading.Lock  (one per symbol+interval)
_fetch_locks_lock = threading.Lock()

MIN_FETCH_INTERVAL = 60            # seconds — hard floor between any two fetches per key


def _get_fetch_lock(key) -> threading.Lock:
    with _fetch_locks_lock:
        if key not in _fetch_locks:
            _fetch_locks[key] = threading.Lock()
        return _fetch_locks[key]


def _candle_boundary(interval: str) -> datetime:
    """Floor now to the candle-open boundary for the given interval."""
    now = datetime.now(IST)
    pm  = next(
        (v["poll_minutes"] for v in INDEX_POLL_CFG.values()
         if v["interval"] == interval),
        15,   # default 15 min for unknown intervals
    )
    return now.replace(minute=(now.minute // pm) * pm, second=0, microsecond=0)


def _get_cached_candles(symbol: str, interval: str) -> pd.DataFrame:
    """
    Return a cached DataFrame for (symbol, interval).

    Guarantees at most ONE Kite historical_data call per candle period per
    symbol, regardless of how many threads or HTTP requests arrive concurrently.
    """
    key          = (symbol, interval)
    candle_open  = _candle_boundary(interval)
    now_ts       = time.time()

    # ── Fast path: cache hit (same candle boundary AND data is fresh) ─────────
    with _cache_lock:
        cached = _candle_cache.get(key)
        if (cached
                and cached["candle_open"] == candle_open
                and not cached["df"].empty
                and (now_ts - cached.get("fetched_at", 0)) < MIN_FETCH_INTERVAL * 20):
            return cached["df"]

    # ── Slow path: need a fresh fetch — acquire per-key lock ─────────────────
    fetch_lock = _get_fetch_lock(key)
    with fetch_lock:
        # Re-check under fetch_lock: another thread may have just fetched
        with _cache_lock:
            cached = _candle_cache.get(key)
            if (cached
                    and cached["candle_open"] == candle_open
                    and not cached["df"].empty
                    and (now_ts - cached.get("fetched_at", 0)) < MIN_FETCH_INTERVAL):
                return cached["df"]

        # Also enforce the hard minimum re-fetch interval (rate-limit guard)
        with _cache_lock:
            last_fetch = _candle_cache.get(key, {}).get("fetched_at", 0)
        elapsed = now_ts - last_fetch
        if elapsed < MIN_FETCH_INTERVAL:
            # Return stale df rather than hammering Kite
            with _cache_lock:
                cached = _candle_cache.get(key)
                if cached and not cached["df"].empty:
                    print(f"[CACHE] Rate-limit guard: returning stale df for {key} "
                          f"({MIN_FETCH_INTERVAL - elapsed:.0f}s until next fetch)")
                    return cached["df"]

        # Actually fetch
        df = download_candles(symbol, interval)
        with _cache_lock:
            _candle_cache[key] = {
                "df":          df,
                "candle_open": candle_open,
                "fetched_at":  time.time(),
            }
        return df


# ── Signal deduplication per index ────────────────────────────────────────────
# Stores the last signal key (bar_time + direction) that was ACTED ON.
# Format: { cfg_key: {"key": "2024-01-15T10:15:00+BUY", "ts": datetime} }
# A signal is suppressed if its key matches the last acted key AND less than
# 2 × poll_minutes have elapsed (prevents re-fire on same candle).
_last_acted: dict  = {}   # cfg_key → {"key": str, "ts": datetime}
_acted_lock = threading.Lock()


def _is_duplicate_signal(cfg_key: str, sig: dict) -> bool:
    """True if this signal was already acted on in the current candle window."""
    key = f"{sig.get('bar_time','?')}+{sig.get('direction','?')}"
    pm  = INDEX_POLL_CFG.get(cfg_key, {}).get("poll_minutes", 5)
    with _acted_lock:
        last = _last_acted.get(cfg_key)
        if not last:
            return False
        if last["key"] != key:
            return False
        # Same signal key — check age
        age_secs = (datetime.now(IST) - last["ts"]).total_seconds()
        return age_secs < pm * 60 * 2   # suppress for 2 candle lengths


def _mark_signal_acted(cfg_key: str, sig: dict):
    key = f"{sig.get('bar_time','?')}+{sig.get('direction','?')}"
    with _acted_lock:
        _last_acted[cfg_key] = {"key": key, "ts": datetime.now(IST)}


# ── Fund sufficiency check ─────────────────────────────────────────────────────
# If funds are enough to cover ALL tradeable indices simultaneously (worst-case
# sum of their floor margins × 1 lot), all are enabled.
# Otherwise, only those whose calendar allows them today are polled.

def _get_available_funds() -> float:
    try:
        m  = kite.margins()
        eq = m.get("equity", {})
        # "net" (== available.live_balance) is the real-time usable balance.
        return eq.get("net", 0)
    except Exception:
        return 0.0


def _can_afford_all_today() -> tuple[bool, float, float]:
    """
    Returns (can_afford_all, available_funds, required_total).
    required_total = sum of _MARGIN_FLOOR for every index tradeable today.
    """
    today   = datetime.now(IST).date()
    weekday = today.weekday()
    tradeable_today = [
        k for k, days in INDEX_ALLOWED_DAYS.items()
        if weekday in days
    ]
    required = sum(_MARGIN_FLOOR.get(k, 4_000) for k in tradeable_today)
    funds    = _get_available_funds()
    return funds >= required, funds, required


# ── Poller state ───────────────────────────────────────────────────────────────
# One entry per cfg_key updated by its polling thread.
_poller_state: dict = {}   # cfg_key → PollerEntry
_poller_lock  = threading.Lock()


def _update_poller_state(cfg_key: str, **kwargs):
    with _poller_lock:
        if cfg_key not in _poller_state:
            _poller_state[cfg_key] = {
                "cfg_key":     cfg_key,
                "status":      "idle",
                "last_run":    None,
                "next_run":    None,
                "last_signal": None,
                "last_action": None,
                "last_error":  None,
                "run_count":   0,
            }
        _poller_state[cfg_key].update(kwargs)


TRAIL_PCT   = 0.15
RSI_OB_EXIT = 88
RSI_OS_EXIT = 12
ST_PERIOD   = 7
ST_MULT     = 2.0

# ── Global state ───────────────────────────────────────────────────────────────
_active_trades: dict = {}
_closed_trades: list = []
_trade_lock = threading.Lock()

_day_targets = {
    "profit_target_rs": None, "max_loss_rs": None, "max_trades": None,
    "trading_halted": False,  "halt_reason": None,
}
_day_pnl = {"realised_rs": 0.0, "trade_count": 0, "date": date.today().isoformat()}
_day_lock = threading.Lock()

# Legacy in-memory Hermes state (still used by JS /hermes_state endpoint)
_hermes_state: dict = {"patterns": {}, "totalTrades": 0, "wins": 0, "seenSignalKeys": []}
_hermes_lock  = threading.Lock()

_poller_threads: dict = {}   # cfg_key → Thread


# ════════════════════════════════════════════════════════════════════════════════
# BACKGROUND POLLING ENGINE
# ════════════════════════════════════════════════════════════════════════════════

def _seconds_to_next_candle(poll_minutes: int) -> float:
    """
    Seconds until the next candle boundary + 5s warm-up buffer.
    e.g. poll_minutes=15: fires at :00, :15, :30, :45 + 5s
    """
    now  = datetime.now(IST)
    elapsed_in_period = (now.minute % poll_minutes) * 60 + now.second
    remaining = poll_minutes * 60 - elapsed_in_period
    return remaining + 5   # +5s so the candle is fully formed on the exchange


def _poll_index(cfg_key: str):
    """
    Daemon thread body for a single index.

    Lifecycle per wake:
    1. Check is_market_hours() — sleep if outside 09:15–15:30 IST.
    2. Check calendar (is_index_tradeable) — mark skipped if blocked.
    3. Check fund sufficiency:
       - If can_afford_all → proceed regardless of calendar (already checked above).
       - If not → only proceed if this index is calendar-allowed.
    4. Fetch candles (cache-aware), run signals, run backtest.
    5. Dedup guard — skip if signal already acted on this candle.
    6. Auto-trade if Hermes or signal confidence warrants it.
    7. Update poller state for /poller_status.
    8. Sleep until next candle boundary.
    """
    pcfg     = INDEX_POLL_CFG[cfg_key]
    symbol   = pcfg["symbol"]
    interval = pcfg["interval"]
    pm       = pcfg["poll_minutes"]

    print(f"[POLLER] {cfg_key} thread started  interval={interval}  poll={pm}m")
    _update_poller_state(cfg_key, status="started")

    while True:
        sleep_secs = _seconds_to_next_candle(pm)
        next_run   = datetime.now(IST) + timedelta(seconds=sleep_secs)
        _update_poller_state(cfg_key,
                             status="sleeping",
                             next_run=next_run.isoformat())
        time.sleep(sleep_secs)

        now = datetime.now(IST)
        _update_poller_state(cfg_key,
                             last_run=now.isoformat(),
                             status="running",
                             run_count=_poller_state.get(cfg_key, {}).get("run_count", 0) + 1)

        # ── 1. Market hours guard ─────────────────────────────────────────────
        if not is_market_hours():
            _update_poller_state(cfg_key, status="outside_market_hours")
            continue

        # ── 2. Calendar check ─────────────────────────────────────────────────
        tradeable, cal_reason = is_index_tradeable(cfg_key)
        if not tradeable:
            _update_poller_state(cfg_key,
                                 status="skipped",
                                 last_action=f"calendar_block: {cal_reason}")
            print(f"[POLLER] {cfg_key} skipped — {cal_reason}")
            continue

        # ── 3. Fund sufficiency ───────────────────────────────────────────────
        can_all, funds, required = _can_afford_all_today()
        # If we can't afford all, each index gets only its own share
        # (calculate_lots handles this per-trade; here we just log)
        fund_note = (f"funds=₹{funds:,.0f} {'≥' if can_all else '<'} "
                     f"required=₹{required:,.0f}")

        # ── 4. Fetch candles ──────────────────────────────────────────────────
        try:
            df = _get_cached_candles(symbol, interval)
        except Exception as e:
            _update_poller_state(cfg_key, status="error", last_error=str(e))
            print(f"[POLLER] {cfg_key} candle fetch error: {e}")
            continue

        if df.empty or len(df) < 20:
            _update_poller_state(cfg_key, status="error",
                                 last_error="insufficient candle data")
            continue

        # ── Futures vol enrichment ────────────────────────────────────────────
        try:
            fut_info = resolve_fut_token(cfg_key, kite)
            ki       = YF_TO_KITE.get(interval, "5minute")
            fut_vol  = fetch_futures_volume(fut_info["token"], kite, ki) if fut_info else {}
            session_vol = fut_vol.get("session_vol", 0)
        except Exception:
            session_vol = 0

        # ── 5. Signals ────────────────────────────────────────────────────────
        try:
            bulls, bears = detect_order_blocks(df)
            if session_vol > 0:
                bulls = enrich_obs_with_futures_vol(bulls, session_vol)
                bears = enrich_obs_with_futures_vol(bears, session_vol)
            sweeps  = detect_liquidity_sweeps(df)
            signals = generate_signals(df, bulls, bears, sweeps=sweeps)
            signals = backtest_signals(df, signals)
        except Exception as e:
            _update_poller_state(cfg_key, status="error", last_error=f"signal error: {e}")
            print(f"[POLLER] {cfg_key} signal error: {e}")
            continue

        live_sigs = [s for s in signals if s.get("exit_reason") in (None, "Open")]
        if not live_sigs:
            _update_poller_state(cfg_key, status="idle_no_signal",
                                 last_signal=None,
                                 last_action=f"no live signal | {fund_note}")
            continue

        sig = live_sigs[-1]   # most recent open signal
        sig_summary = f"{sig['direction']} bar={sig['bar_time']} entry={sig['entry']}"
        _update_poller_state(cfg_key, last_signal=sig_summary)

        # ── 6. Dedup guard ────────────────────────────────────────────────────
        if _is_duplicate_signal(cfg_key, sig):
            _update_poller_state(cfg_key, status="idle",
                                 last_action=f"dedup_skip: {sig_summary}")
            print(f"[POLLER] {cfg_key} dedup skip — already acted on {sig_summary}")
            continue

        # ── 7. Active-trade guard (no double entry) ───────────────────────────
        with _trade_lock:
            already_in = symbol in _active_trades
        if already_in:
            _update_poller_state(cfg_key, status="holding",
                                 last_action=f"active trade open — no new entry")
            continue

        # ── 8. Halted check ───────────────────────────────────────────────────
        halted, halt_reason = _check_limits()
        if halted:
            _update_poller_state(cfg_key, status="halted",
                                 last_action=f"day limit: {halt_reason}")
            continue

        # ── 9. Auto-trade execution ───────────────────────────────────────────
        auto_cfg = get_auto_cfg()
        if not auto_cfg.get("enabled"):
            _update_poller_state(cfg_key, status="signal_ready",
                                 last_action=f"auto-trade disabled | {sig_summary}")
            print(f"[POLLER] {cfg_key} signal ready but auto-trade disabled: {sig_summary}")
            continue

        # Optionally run Hermes LLM confirm (non-blocking fire-and-check)
        hermes_ok = True
        try:
            ema9  = calc_ema(df["Close"], 9)
            ema13 = calc_ema(df["Close"], 13)
            atr_s = compute_atr(df, 14)
            st_d, st_up, st_dn = compute_supertrend(df, ST_PERIOD, ST_MULT)
            avg_vol = float(df["Volume"].values[-20:].mean())
            _expiry = _nearest_expiry(cfg_key)
            _dte    = (_expiry - datetime.now(IST).date()).days if _expiry else None
            chart_state = {
                "symbol": symbol, "interval": interval,
                "last_close":  float(df["Close"].iloc[-1]),
                "last_ema9":   float(ema9.iloc[-1]),
                "last_ema13":  float(ema13.iloc[-1]),
                "prev_ema9":   float(ema9.iloc[-2]),
                "prev_ema13":  float(ema13.iloc[-2]),
                "last_st_dir": int(st_d[-1]),
                "last_st_line":float(st_dn[-1] if st_d[-1]==1 else st_up[-1]),
                "last_atr":    float(atr_s.iloc[-1]),
                "last_volume": int(df["Volume"].iloc[-1]),
                "avg_volume":  int(avg_vol),
                "bulls":       [{"top":o["top"],"bottom":o["bottom"],
                                 "vol_pct":o.get("vol_pct",0),
                                 "fut_vol_pct":o.get("fut_vol_pct",0)} for o in bulls],
                "bears":       [{"top":o["top"],"bottom":o["bottom"],
                                 "vol_pct":o.get("vol_pct",0),
                                 "fut_vol_pct":o.get("fut_vol_pct",0)} for o in bears],
                "sweeps":      [],
                "signals":     signals,
                "active_trade":None,
                "signal_stats":{"total":len(signals),
                                "wins": sum(1 for s in signals if (s.get("pts_captured") or 0)>0),
                                "win_rate": round(
                                    sum(1 for s in signals if (s.get("pts_captured") or 0)>0)
                                    / max(1,len(signals)) * 100, 1)},
                "calendar":    {"allowed": tradeable, "reason": cal_reason, "dte": _dte},
                "futures":     fut_vol if fut_vol else {},
            }
            result = hermes_analyse(chart_state)
            min_conf = auto_cfg.get("min_confidence", 70)
            if result["decision"] not in ("BUY","SELL"):
                hermes_ok = False
                _update_poller_state(cfg_key, status="hermes_hold",
                                     last_action=f"Hermes HOLD | {result.get('reason','')[:60]}")
            elif result["confidence"] < min_conf:
                hermes_ok = False
                _update_poller_state(cfg_key, status="hermes_low_conf",
                                     last_action=(f"Hermes conf={result['confidence']}% "
                                                  f"< min={min_conf}%"))
            elif result["decision"] != sig["direction"]:
                hermes_ok = False
                _update_poller_state(cfg_key, status="hermes_disagree",
                                     last_action=(f"Hermes says {result['decision']} "
                                                  f"but signal is {sig['direction']}"))
        except Exception as e:
            print(f"[POLLER] {cfg_key} Hermes error (proceeding): {e}")
            # Hermes failure is non-fatal — proceed if signal is strong

        if not hermes_ok:
            continue

        # ── 10. Place the trade ───────────────────────────────────────────────
        print(f"[POLLER] {cfg_key} FIRING {sig['direction']} — {sig_summary} | {fund_note}")
        _mark_signal_acted(cfg_key, sig)
        _update_poller_state(cfg_key, status="trading",
                             last_action=f"placed {sig['direction']} {sig_summary}")

        threading.Thread(
            target=_run_auto_trade,
            args=(symbol, interval, sig, auto_cfg.get("max_lots", 1)),
            daemon=True,
        ).start()


def start_pollers():
    """
    Launch one daemon polling thread per index.
    Threads are stored in _poller_threads so /poller_status can report them.
    Called once at startup after Hermes boot.
    """
    for cfg_key in INDEX_POLL_CFG:
        t = threading.Thread(
            target=_poll_index,
            args=(cfg_key,),
            name=f"poller_{cfg_key}",
            daemon=True,
        )
        t.start()
        _poller_threads[cfg_key] = t
        _update_poller_state(cfg_key, status="launched")
        print(f"[POLLER] {cfg_key} thread launched")


# ════════════════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════════════════
# INDICATORS
# ════════════════════════════════════════════════════════════════════════════════

def calc_ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"]  - df["Close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1).ewm(alpha=1/period, adjust=False).mean()


def compute_supertrend(df: pd.DataFrame, period=10, mult=3.0):
    atr = compute_atr(df, period)
    hl2 = (df["High"] + df["Low"]) / 2
    up  = (hl2 + mult * atr).values
    dn  = (hl2 - mult * atr).values
    cl  = df["Close"].values
    n   = len(df)
    fup = np.zeros(n); fdn = np.zeros(n); dire = np.ones(n, dtype=int)
    fup[0] = up[0]; fdn[0] = dn[0]
    for i in range(1, n):
        fup[i] = up[i] if up[i] < fup[i-1] or cl[i-1] > fup[i-1] else fup[i-1]
        fdn[i] = dn[i] if dn[i] > fdn[i-1] or cl[i-1] < fdn[i-1] else fdn[i-1]
        if   dire[i-1] == -1 and cl[i] > fup[i]: dire[i] =  1
        elif dire[i-1] ==  1 and cl[i] < fdn[i]: dire[i] = -1
        else: dire[i] = dire[i-1]
    return dire, fup, fdn


def compute_rsi(s: pd.Series, period=14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    return 100 - (100 / (1 + (g / l.replace(0, np.nan))))


# ════════════════════════════════════════════════════════════════════════════════
# INSTRUMENT HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def _index_key(symbol: str) -> str:
    return INDEX_KEY_MAP.get(symbol, "NIFTY")


def resolve_token(symbol: str) -> int:
    if symbol in INSTRUMENT_ALIAS:
        exchange, ts = INSTRUMENT_ALIAS[symbol]
    elif ":" in symbol:
        exchange, ts = symbol.split(":", 1)
    else:
        exchange, ts = "NSE", symbol
    for inst in _get_instruments(exchange):
        if inst["tradingsymbol"] == ts:
            return int(inst["instrument_token"])
    raise ValueError(f"Token not found: {symbol}")


def get_atm_option(underlying: str, spot: float, direction: str) -> dict | None:
    """
    Look up ATM option.
    lot_size is ALWAYS read from Kite master — NSE changes these periodically.
    OPTIONS_CFG carries only exchange + strike_gap.

    underlying is the RAW symbol (e.g. "^NSEBANK", "^NSEI", "SENSEX").
    We resolve it to an OPTIONS_CFG key via INDEX_KEY_MAP first, then fall back
    to the old substring search.  This fixes BANKNIFTY where "BANKNIFTY" is NOT
    a substring of "^NSEBANK".
    """
    # Primary: exact lookup via INDEX_KEY_MAP
    cfg_key = INDEX_KEY_MAP.get(underlying, INDEX_KEY_MAP.get(underlying.upper()))
    # cfg_key must also exist in OPTIONS_CFG
    if cfg_key and cfg_key not in OPTIONS_CFG:
        cfg_key = None
    # Fallback: substring search (works for plain "SENSEX", "NIFTY" etc.)
    if not cfg_key:
        cfg_key = next((k for k in OPTIONS_CFG if k in underlying.upper()), None)
    if not cfg_key:
        print(f"[ATM OPTION] Cannot resolve cfg_key for underlying={underlying!r}")
        return None
    cfg      = OPTIONS_CFG[cfg_key]
    atm      = round(spot / cfg["strike_gap"]) * cfg["strike_gap"]
    opt_type = "CE" if direction == "BUY" else "PE"
    today    = datetime.now(IST).date()
    insts = _get_instruments(cfg["exchange"])
    if not insts:
        print(f"[ATM OPTION] No instruments data for {cfg_key} — cannot look up option")
        return None
    cands = [
        i for i in insts
        if i.get("name", "").upper() == cfg_key
        and i.get("instrument_type") == opt_type
        and i.get("expiry") and i["expiry"] >= today
    ]
    if not cands:
        return None
    nearest_expiry = min(c["expiry"] for c in cands)
    cands = [c for c in cands if c["expiry"] == nearest_expiry]
    # Nearest available strike to the computed ATM — handles any gaps or
    # odd strike steps instead of requiring an exact match.
    inst = min(cands, key=lambda c: abs((c.get("strike") or 0) - atm))
    # Always use master lot_size — NEVER fall back to a hardcoded number
    lot_size = int(inst.get("lot_size") or 0)
    if lot_size <= 0:
        print(f"[LOT SIZE WARNING] Master returned 0 for {cfg_key} {opt_type} — skipping order")
        return None
    print(f"[LOT SIZE] {cfg_key} {opt_type}: master={lot_size}")
    return {
        "tradingsymbol":    inst["tradingsymbol"],
        "instrument_token": int(inst["instrument_token"]),
        "lot_size":         lot_size,
        "exchange":         cfg["exchange"],
        "strike":           inst.get("strike", atm),
        "opt_type":         opt_type,
        "expiry":           str(inst["expiry"]),
    }


# ════════════════════════════════════════════════════════════════════════════════
# DATA FETCH  — stitched for 30-day lookback
# ════════════════════════════════════════════════════════════════════════════════

def download_candles(symbol: str, interval: str) -> pd.DataFrame:
    ki  = YF_TO_KITE.get(interval)
    if not ki:
        return pd.DataFrame()
    lb  = LOOKBACK.get(ki, 30)
    now = datetime.now(IST)
    mid = now - timedelta(days=lb // 2)
    fdt = (now - timedelta(days=lb)).replace(hour=9, minute=0, second=0, microsecond=0)
    try:
        tok  = resolve_token(symbol)
        raw1 = kite.historical_data(tok, fdt, mid, ki, False, False)
        raw2 = kite.historical_data(tok, mid, now, ki, False, False)
        raw  = raw1 + [r for r in raw2 if r not in raw1]
    except Exception as e:
        print(f"[Kite candles] {e}")
        return pd.DataFrame()
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df.rename(columns={"date":"Date","open":"Open","high":"High",
                        "low":"Low","close":"Close","volume":"Volume"}, inplace=True)
    df["Date"] = pd.to_datetime(df["Date"])
    if df["Date"].dt.tz is None:
        df["Date"] = df["Date"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
    else:
        df["Date"] = df["Date"].dt.tz_convert("Asia/Kolkata")
    df.set_index("Date", inplace=True)
    df = df[["Open","High","Low","Close","Volume"]].copy()
    df["Volume"] = df["Volume"].fillna(0).astype(int)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


# ════════════════════════════════════════════════════════════════════════════════
# LIQUIDITY SWEEP DETECTION
# ════════════════════════════════════════════════════════════════════════════════

def detect_liquidity_sweeps(df: pd.DataFrame, lookback: int = 5) -> list:
    """
    A liquidity sweep is a wick that briefly pierces a recent swing high/low
    and then reverses — stop-hunting move that precedes institutional entry.

    Returns list of sweep events: {bar_idx, type:'sweep_high'/'sweep_low',
    swept_level, sweep_bar_idx}
    """
    if df.empty or len(df) < lookback + 2:
        return []
    H = df["High"].values; L = df["Low"].values; C = df["Close"].values
    n = len(df); sweeps = []
    for i in range(lookback, n - 1):
        # Swing high over previous `lookback` bars
        window_H = H[i - lookback:i]
        window_L = L[i - lookback:i]
        swing_high = float(window_H.max())
        swing_low  = float(window_L.min())
        # Sweep HIGH: wick above swing high but CLOSES back below it (rejection)
        if H[i] > swing_high and C[i] < swing_high:
            sweeps.append({
                "bar_idx":     i,
                "type":        "sweep_high",
                "swept_level": round(swing_high, 2),
            })
        # Sweep LOW: wick below swing low but CLOSES back above it (rejection)
        if L[i] < swing_low and C[i] > swing_low:
            sweeps.append({
                "bar_idx":     i,
                "type":        "sweep_low",
                "swept_level": round(swing_low, 2),
            })
    return sweeps


# ════════════════════════════════════════════════════════════════════════════════
# ORDER BLOCK DETECTION
# ════════════════════════════════════════════════════════════════════════════════

def detect_order_blocks(df, swing_length=10, atr_period=10, max_atr_mult=3.5, max_obs=30):
    if df.empty:
        return [], []
    H = df["High"].values; L = df["Low"].values
    C = df["Close"].values; O = df["Open"].values
    V = df["Volume"].values; n = len(df)
    atr = compute_atr(df, atr_period)
    total_vol = float(V.sum()) if V.sum() > 0 else 1.0
    bulls = []; bears = []; last_top = None; last_bot = None

    for i in range(swing_length, n):
        p = i - swing_length
        fh = H[p+1:i+1]; fl = L[p+1:i+1]
        if fh.size > 0 and H[p] > fh.max():
            if last_top is None or last_top["x"] != p:
                last_top = {"x": p, "y": float(H[p]), "crossed": False}
        if fl.size > 0 and L[p] < fl.min():
            if last_bot is None or last_bot["x"] != p:
                last_bot = {"x": p, "y": float(L[p]), "crossed": False}

        for ob in bulls[:]:
            if not ob.get("breaker"):
                if L[i] < ob["bottom"]: ob["breaker"] = True; ob["break_idx"] = i
            else:
                if H[i] > ob["top"]: bulls.remove(ob)
        for ob in bears[:]:
            if not ob.get("breaker"):
                if H[i] > ob["top"]: ob["breaker"] = True; ob["break_idx"] = i
            else:
                if L[i] < ob["bottom"]: bears.remove(ob)

        if last_top and C[i] > last_top["y"] and not last_top["crossed"]:
            last_top["crossed"] = True
            s, e = last_top["x"] + 1, i - 1
            if s <= e:
                sl = L[s:e+1]; sh = H[s:e+1]
                rp = int(np.argmin(sl))
                bb, bt, bl = float(sl.min()), float(sh[rp]), s + rp
            else:
                fi = min(last_top["x"] + 1, n - 1)
                bb, bt, bl = float(L[fi]), float(H[fi]), fi
            body_lo = min(float(O[bl]), float(C[bl]))
            body_hi = max(float(O[bl]), float(C[bl]))
            bb = min(bb, body_lo); bt = max(bt, body_hi)
            ov   = sum(int(V[max(0, i-k)]) for k in range(3))
            vpct = round(ov / total_vol * 100, 1)
            av   = float(atr.iloc[i]) if not np.isnan(atr.iloc[i]) else 0.0
            if av == 0.0 or abs(bt - bb) <= av * max_atr_mult:
                key = (round(bt, 1), round(bb, 1))
                bulls.insert(0, {"top":bt,"bottom":bb,"start_idx":bl,
                    "start_time":df.index[bl],"obVolume":ov,"vol_pct":vpct,
                    "fut_vol_pct":0.0,
                    "type":"Bull","breaker":False,"break_idx":None,"key":key})
                if len(bulls) > max_obs: bulls.pop()

        if last_bot and C[i] < last_bot["y"] and not last_bot["crossed"]:
            last_bot["crossed"] = True
            s, e = last_bot["x"] + 1, i - 1
            if s <= e:
                sh = H[s:e+1]; sl = L[s:e+1]
                rp = int(np.argmax(sh))
                bt, bb, bl = float(sh.max()), float(sl[rp]), s + rp
            else:
                fi = min(last_bot["x"] + 1, n - 1)
                bt, bb, bl = float(H[fi]), float(L[fi]), fi
            body_lo = min(float(O[bl]), float(C[bl]))
            body_hi = max(float(O[bl]), float(C[bl]))
            bb = min(bb, body_lo); bt = max(bt, body_hi)
            ov   = sum(int(V[max(0, i-k)]) for k in range(3))
            vpct = round(ov / total_vol * 100, 1)
            av   = float(atr.iloc[i]) if not np.isnan(atr.iloc[i]) else 0.0
            if av == 0.0 or abs(bt - bb) <= av * max_atr_mult:
                key = (round(bt, 1), round(bb, 1))
                bears.insert(0, {"top":bt,"bottom":bb,"start_idx":bl,
                    "start_time":df.index[bl],"obVolume":ov,"vol_pct":vpct,
                    "fut_vol_pct":0.0,
                    "type":"Bear","breaker":False,"break_idx":None,"key":key})
                if len(bears) > max_obs: bears.pop()

    return bulls, bears


# ════════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE  — triple confirmation + consolidation guard
# ════════════════════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# Entry sequence (all three must happen in order):
#   Step 1 — LIQUIDITY SWEEP: a wick swept a recent swing high/low within last 10 bars
#   Step 2 — ORDER BLOCK: price is touching or retesting a valid OB in the swept direction
#   Step 3 — TRIGGER: EMA 9/13 crossover OR SuperTrend direction flip (either qualifies)
#
# SELL signal = BUY PE option.  BUY signal = BUY CE option.
# ════════════════════════════════════════════════════════════════════════════════

def _is_ranging(C, H, L, i, lookback=20):
    """
    True when market is clearly sideways. Uses two checks:
      1. Total swing (hi-lo of window) < 1.5× avg bar range — extremely tight coil
      2. Close std < 0.4× avg bar range — closes bunching together
    Threshold deliberately loose — only filters dead consolidation, not normal trending.
    Returns (is_ranging, range_high, range_low)
    """
    s = max(0, i - lookback)
    wH = H[s:i+1]; wL = L[s:i+1]; wC = C[s:i+1]
    swing       = float(wH.max() - wL.min())
    bar_ranges  = wH - wL
    avg_bar_rng = float(bar_ranges.mean()) if bar_ranges.size > 0 else 1.0
    close_std   = float(wC.std()) if len(wC) > 1 else 0.0
    # Only call it ranging if swing is very tight (< 1.5× avg bar) AND closes bunching
    is_ranging  = (swing < avg_bar_rng * 1.5) and (close_std < avg_bar_rng * 0.4)
    return is_ranging, float(wH.max()), float(wL.min())


def generate_signals(df, bulls, bears, sweeps=None):
    """
    Entry sequence: Sweep → OB → Trigger (cross or ST flip).
    A signal fires only if all three steps are confirmed on the bar.
    """
    if df.empty or len(df) < 20:
        return []
    if sweeps is None:
        sweeps = detect_liquidity_sweeps(df)

    C   = df["Close"].values; H = df["High"].values
    L   = df["Low"].values;   ts = df.index
    e9  = calc_ema(df["Close"], 9).values
    e13 = calc_ema(df["Close"], 13).values
    st_dir, st_up, st_dn = compute_supertrend(df, ST_PERIOD, ST_MULT)
    atr_arr = compute_atr(df, 14).values
    signals = []; fired = {}

    # Index sweeps by bar for fast lookup
    sweep_by_bar = {}
    for sw in sweeps:
        sweep_by_bar.setdefault(sw["bar_idx"], []).append(sw)

    for i in range(4, len(df)):
        c = C[i]

        # ── Dead-bar guard (pre-market / zero-volume artifact) ────────────────
        atr_now = atr_arr[i] if not np.isnan(atr_arr[i]) else 0
        atr_avg = float(np.nanmean(atr_arr[max(0, i-20):i])) if i >= 5 else atr_now
        if atr_avg > 0 and (atr_now / atr_avg) < 0.20:
            continue

        # ── Ranging filter: skip signals in the middle of a tight box ─────────
        is_ranging, range_hi, range_lo = _is_ranging(C, H, L, i, lookback=20)
        range_span = range_hi - range_lo

        # ── Step 3: Detect trigger — EMA cross OR ST flip (within last 3 bars) ─
        # EMA cross
        cross_bar = None; cross_dir = None
        for k in range(3):
            j = i - k
            if j < 1: break
            if e9[j] > e13[j] and e9[j-1] <= e13[j-1]:
                cross_bar = j; cross_dir = "BUY"; break
            if e9[j] < e13[j] and e9[j-1] >= e13[j-1]:
                cross_bar = j; cross_dir = "SELL"; break

        # ST flip (current bar vs 2 bars back to allow 1-bar gap)
        st_flip_dir = None
        if i >= 2:
            if st_dir[i] == 1  and st_dir[i-2] == -1: st_flip_dir = "BUY"
            if st_dir[i] == -1 and st_dir[i-2] ==  1: st_flip_dir = "SELL"

        # At least one trigger must be present
        # Prefer EMA cross; fall back to ST flip
        trigger_dir  = cross_dir or st_flip_dir
        trigger_bar  = cross_bar if cross_dir else i
        if trigger_dir is None:
            continue

        # ST must AGREE with trigger direction (both must be aligned)
        if trigger_dir == "BUY"  and st_dir[i] != 1:  continue
        if trigger_dir == "SELL" and st_dir[i] != -1: continue

        # ── Ranging boundary check ────────────────────────────────────────────
        if is_ranging and range_span > 0:
            pos = (c - range_lo) / range_span   # 0=bottom, 1=top
            if trigger_dir == "BUY"  and pos > 0.40: continue
            if trigger_dir == "SELL" and pos < 0.60: continue

        # ── Step 1: Liquidity sweep must exist within last 10 bars ───────────
        # For BUY: need a sweep_low (swept below a swing low then reversed up)
        # For SELL: need a sweep_high (swept above a swing high then reversed down)
        sweep_window = range(max(0, i - 10), i + 1)
        required_sweep = "sweep_low" if trigger_dir == "BUY" else "sweep_high"
        sweep_found = any(
            sw["type"] == required_sweep
            for bi in sweep_window
            for sw in sweep_by_bar.get(bi, [])
        )
        if not sweep_found:
            continue   # no liquidity sweep → skip, not a valid SMC setup

        # ── Step 2: OB touch — price must have tested a valid OB ──────────────
        check_range = range(max(1, trigger_bar - 2), i + 1)

        if trigger_dir == "BUY":
            for ob in bulls:
                ob_key = ob["key"]
                if ob_key in fired and (i - fired[ob_key]) < 25: continue
                if ob.get("breaker"): continue
                if ob["start_idx"] >= trigger_bar: continue
                if (ob["top"] - ob["bottom"]) / c * 100 < 0.15: continue
                touched = any(L[j] <= ob["top"] and H[j] >= ob["bottom"]
                              for j in check_range if j < len(L))
                if not touched: continue
                if c <= ob["top"]: continue

                sl   = min(L[i], L[i-1], L[trigger_bar])
                risk = round(c - sl, 2)
                if risk <= 0: continue

                atr_val = float(atr_arr[i]) if not np.isnan(atr_arr[i]) else risk

                # T1–T5: walk up the bearish OB ladder; fall back to R multiples
                above = sorted([b for b in bears
                                if b["bottom"] > c and not b.get("breaker")],
                               key=lambda b: b["bottom"])
                def _tgt_b(n, mult):
                    return round(above[n]["bottom"], 2) if len(above) > n \
                           else round(c + risk * mult, 2)
                t1 = _tgt_b(0, 1.0)
                t2 = _tgt_b(1, 2.0)
                t3 = _tgt_b(2, 3.0)
                t4 = _tgt_b(3, 4.5)
                t5 = _tgt_b(4, 6.0)

                signals.append({
                    "bar_time": ts[i].isoformat(), "bar_idx": i,
                    "direction": "BUY", "entry": float(c), "sl": float(sl),
                    "t1": t1, "t2": t2, "t3": t3, "t4": t4, "t5": t5,
                    "risk_pts": risk, "atr": round(atr_val, 2),
                    "rr2": round((t2-c)/risk,2), "rr3": round((t3-c)/risk,2),
                    "rr4": round((t4-c)/risk,2), "rr5": round((t5-c)/risk,2),
                    "ob_top": ob["top"], "ob_bottom": ob["bottom"],
                    "ob_vol_pct": ob.get("vol_pct", 0),
                    "ob_fut_vol_pct": ob.get("fut_vol_pct", 0),
                    "trigger": "EMA_CROSS" if cross_dir else "ST_FLIP",
                    "sweep_confirmed": True,
                    "atr_ratio": round(atr_now/atr_avg,2) if atr_avg > 0 else 1.0,
                    "is_ranging": is_ranging, "st_line": float(st_dn[i]),
                    "exit_bar": None, "exit_time": None, "exit_price": None,
                    "exit_reason": None, "pts_captured": None, "actual_rr": None,
                })
                fired[ob_key] = i
                break

        elif trigger_dir == "SELL":
            for ob in bears:
                ob_key = ob["key"]
                if ob_key in fired and (i - fired[ob_key]) < 25: continue
                if ob.get("breaker"): continue
                if ob["start_idx"] >= trigger_bar: continue
                if (ob["top"] - ob["bottom"]) / c * 100 < 0.15: continue
                touched = any(H[j] >= ob["bottom"] and L[j] <= ob["top"]
                              for j in check_range if j < len(H))
                if not touched: continue
                if c >= ob["bottom"]: continue

                sl   = max(H[i], H[i-1], H[trigger_bar])
                risk = round(sl - c, 2)
                if risk <= 0: continue

                atr_val = float(atr_arr[i]) if not np.isnan(atr_arr[i]) else risk

                below = sorted([b for b in bulls
                                if b["top"] < c and not b.get("breaker")],
                               key=lambda b: b["top"], reverse=True)
                def _tgt_s(n, mult):
                    return round(below[n]["top"], 2) if len(below) > n \
                           else round(c - risk * mult, 2)
                t1 = _tgt_s(0, 1.0)
                t2 = _tgt_s(1, 2.0)
                t3 = _tgt_s(2, 3.0)
                t4 = _tgt_s(3, 4.5)
                t5 = _tgt_s(4, 6.0)

                signals.append({
                    "bar_time": ts[i].isoformat(), "bar_idx": i,
                    "direction": "SELL", "entry": float(c), "sl": float(sl),
                    "t1": t1, "t2": t2, "t3": t3, "t4": t4, "t5": t5,
                    "risk_pts": risk, "atr": round(atr_val, 2),
                    "rr2": round((c-t2)/risk,2), "rr3": round((c-t3)/risk,2),
                    "rr4": round((c-t4)/risk,2), "rr5": round((c-t5)/risk,2),
                    "ob_top": ob["top"], "ob_bottom": ob["bottom"],
                    "ob_vol_pct": ob.get("vol_pct", 0),
                    "ob_fut_vol_pct": ob.get("fut_vol_pct", 0),
                    "trigger": "EMA_CROSS" if cross_dir else "ST_FLIP",
                    "sweep_confirmed": True,
                    "atr_ratio": round(atr_now/atr_avg,2) if atr_avg > 0 else 1.0,
                    "is_ranging": is_ranging, "st_line": float(st_up[i]),
                    "exit_bar": None, "exit_time": None, "exit_price": None,
                    "exit_reason": None, "pts_captured": None, "actual_rr": None,
                })
                fired[ob_key] = i
                break

    return signals


def _debug_last_bar_signal(df, bulls, bears, sweeps):
    """
    Diagnostic helper — replays the generate_signals() gates for the most
    recent bar only and prints which gate (if any) blocked a signal.
    Use this to answer "everything looked right, why no entry?" questions.
    """
    if df.empty or len(df) < 20:
        print("[SIGDBG] BLOCKED: insufficient bars (<20)")
        return

    C = df["Close"].values; H = df["High"].values; L = df["Low"].values
    e9  = calc_ema(df["Close"], 9).values
    e13 = calc_ema(df["Close"], 13).values
    st_dir, st_up, st_dn = compute_supertrend(df, ST_PERIOD, ST_MULT)
    atr_arr = compute_atr(df, 14).values
    i = len(df) - 1
    c = C[i]

    sweep_by_bar = {}
    for sw in sweeps:
        sweep_by_bar.setdefault(sw["bar_idx"], []).append(sw)

    atr_now = atr_arr[i] if not np.isnan(atr_arr[i]) else 0
    atr_avg = float(np.nanmean(atr_arr[max(0, i-20):i])) if i >= 5 else atr_now
    ratio   = (atr_now/atr_avg) if atr_avg else 0
    print(f"[SIGDBG] bar={df.index[i]} close={c:.2f} st_dir={st_dir[i]} "
          f"atr_now={atr_now:.2f} atr_avg={atr_avg:.2f} ratio={ratio:.2f}")
    if atr_avg > 0 and ratio < 0.20:
        print("[SIGDBG] BLOCKED: dead-bar guard (low ATR vs avg)")
        return

    is_ranging, range_hi, range_lo = _is_ranging(C, H, L, i, lookback=20)
    range_span = range_hi - range_lo
    pos = (c - range_lo) / range_span if range_span else 0
    print(f"[SIGDBG] is_ranging={is_ranging} pos={pos:.2f} range=[{range_lo:.2f},{range_hi:.2f}]")

    cross_bar = None; cross_dir = None
    for k in range(3):
        j = i - k
        if j < 1: break
        if e9[j] > e13[j] and e9[j-1] <= e13[j-1]:
            cross_bar = j; cross_dir = "BUY"; break
        if e9[j] < e13[j] and e9[j-1] >= e13[j-1]:
            cross_bar = j; cross_dir = "SELL"; break

    st_flip_dir = None
    if i >= 2:
        if st_dir[i] == 1  and st_dir[i-2] == -1: st_flip_dir = "BUY"
        if st_dir[i] == -1 and st_dir[i-2] ==  1: st_flip_dir = "SELL"

    trigger_dir = cross_dir or st_flip_dir
    trigger_bar = cross_bar if cross_dir else i
    print(f"[SIGDBG] cross_dir={cross_dir}(bar={cross_bar}) st_flip_dir={st_flip_dir} "
          f"-> trigger_dir={trigger_dir} trigger_bar={trigger_bar}")
    if trigger_dir is None:
        print("[SIGDBG] BLOCKED: no trigger (no EMA cross / ST flip in last 3 bars)")
        return
    if trigger_dir == "BUY"  and st_dir[i] != 1:
        print(f"[SIGDBG] BLOCKED: ST disagrees with BUY trigger (st_dir={st_dir[i]})")
        return
    if trigger_dir == "SELL" and st_dir[i] != -1:
        print(f"[SIGDBG] BLOCKED: ST disagrees with SELL trigger (st_dir={st_dir[i]})")
        return

    if is_ranging and range_span > 0:
        if trigger_dir == "BUY" and pos > 0.40:
            print(f"[SIGDBG] BLOCKED: ranging + pos={pos:.2f}>0.40 (too high for BUY)")
            return
        if trigger_dir == "SELL" and pos < 0.60:
            print(f"[SIGDBG] BLOCKED: ranging + pos={pos:.2f}<0.60 (too low for SELL)")
            return

    sweep_window    = range(max(0, i - 10), i + 1)
    required_sweep  = "sweep_low" if trigger_dir == "BUY" else "sweep_high"
    found_sweeps    = [(bi, sw["type"]) for bi in sweep_window for sw in sweep_by_bar.get(bi, [])]
    sweep_found     = any(t == required_sweep for _, t in found_sweeps)
    print(f"[SIGDBG] need={required_sweep} window={list(sweep_window)} found={found_sweeps} -> sweep_found={sweep_found}")
    if not sweep_found:
        print("[SIGDBG] BLOCKED: no matching liquidity sweep in last 10 bars")
        return

    check_range = range(max(1, trigger_bar - 2), i + 1)
    obs = bears if trigger_dir == "SELL" else bulls
    ob_label = "bear" if trigger_dir == "SELL" else "bull"
    print(f"[SIGDBG] checking {len(obs)} {ob_label} OB(s), check_range={list(check_range)}")
    for ob in obs:
        reasons = []
        if ob.get("breaker"):
            reasons.append("breaker")
        if ob["start_idx"] >= trigger_bar:
            reasons.append(f"start_idx({ob['start_idx']})>=trigger_bar({trigger_bar})")
        size_pct = (ob["top"] - ob["bottom"]) / c * 100
        if size_pct < 0.15:
            reasons.append(f"too small ({size_pct:.3f}% < 0.15%)")
        if trigger_dir == "SELL":
            touched = any(H[j] >= ob["bottom"] and L[j] <= ob["top"] for j in check_range if j < len(H))
            if not touched:
                reasons.append("not touched")
            if c >= ob["bottom"]:
                reasons.append(f"close({c:.2f}) >= ob_bottom({ob['bottom']:.2f}) — no rejection close yet")
        else:
            touched = any(L[j] <= ob["top"] and H[j] >= ob["bottom"] for j in check_range if j < len(L))
            if not touched:
                reasons.append("not touched")
            if c <= ob["top"]:
                reasons.append(f"close({c:.2f}) <= ob_top({ob['top']:.2f}) — no breakout close yet")
        status = "OK -> WOULD FIRE" if not reasons else f"SKIP ({', '.join(reasons)})"
        print(f"[SIGDBG]   OB top={ob['top']:.2f} bottom={ob['bottom']:.2f} "
              f"start_idx={ob['start_idx']} breaker={ob.get('breaker')} -> {status}")
        if not reasons:
            return
    print("[SIGDBG] BLOCKED: no eligible OB passed all checks")


# ════════════════════════════════════════════════════════════════════════════════
# BACKTEST WALK-FORWARD
# ════════════════════════════════════════════════════════════════════════════════

def backtest_signals(df, signals):
    if not signals:
        return signals
    C = df["Close"].values; H = df["High"].values; L = df["Low"].values
    ts = df.index; n = len(df)
    rsi_arr = compute_rsi(df["Close"], 14).values

    for sig in signals:
        ei = sig["bar_idx"]
        if ei is None or ei >= n - 1:
            continue
        entry = sig["entry"]; sl = sig["sl"]
        t2 = sig["t2"]; t3 = sig["t3"]; d = sig["direction"]

        for j in range(ei + 1, n):
            lo, hi, cl, rsi_j = L[j], H[j], C[j], rsi_arr[j]
            if d == "BUY":
                if not np.isnan(rsi_j) and rsi_j >= RSI_OB_EXIT:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(cl),"exit_reason":"RSI_E1",
                        "pts_captured":round(cl - entry, 2)}); break
                if lo <= sl:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(sl),"exit_reason":"SL",
                        "pts_captured":round(sl - entry, 2)}); break
                if hi >= t3:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(t3),"exit_reason":"T3",
                        "pts_captured":round(t3 - entry, 2)}); break
                if hi >= t2:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(t2),"exit_reason":"T2",
                        "pts_captured":round(t2 - entry, 2)}); break
            else:   # SELL
                if not np.isnan(rsi_j) and rsi_j <= RSI_OS_EXIT:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(cl),"exit_reason":"RSI_E1",
                        "pts_captured":round(entry - cl, 2)}); break
                if hi >= sl:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(sl),"exit_reason":"SL",
                        "pts_captured":round(entry - sl, 2)}); break
                if lo <= t3:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(t3),"exit_reason":"T3",
                        "pts_captured":round(entry - t3, 2)}); break
                if lo <= t2:
                    sig.update({"exit_bar":j,"exit_time":ts[j].isoformat(),
                        "exit_price":float(t2),"exit_reason":"T2",
                        "pts_captured":round(entry - t2, 2)}); break
        else:
            last = float(C[-1])
            sig.update({"exit_bar":n-1,"exit_time":ts[n-1].isoformat(),
                "exit_price":last,"exit_reason":"Open",
                "pts_captured":round((last - entry) if d == "BUY" else (entry - last), 2)})

        r = sig["risk_pts"]
        if r and r > 0 and sig["pts_captured"] is not None:
            sig["actual_rr"] = round(sig["pts_captured"] / r, 2)

    return signals


# ════════════════════════════════════════════════════════════════════════════════
# ORDER PLACEMENT
# ════════════════════════════════════════════════════════════════════════════════

def place_order(opt: dict, lots: int, action: str = "BUY",
                limit_price: float = 0.0) -> str | None:
    """
    Place order with NRML product and LIMIT order type.
    limit_price: pass 0 (or omit) to auto-use the current LTP as limit price.
    A 0.5% slippage buffer is added:
      BUY  → limit = ltp * 1.005  (willing to pay slightly above)
      SELL → limit = ltp * 0.995  (willing to accept slightly below)
    """
    try:
        # Fetch LTP if no limit price provided
        if limit_price <= 0:
            try:
                ltp_data = kite.ltp([f"{opt['exchange']}:{opt['tradingsymbol']}"])
                raw_ltp  = float(list(ltp_data.values())[0]["last_price"])
            except Exception:
                raw_ltp  = 0.0
            if raw_ltp > 0:
                if action == "BUY":
                    limit_price = round(raw_ltp * 1.005, 1)
                else:
                    limit_price = round(raw_ltp * 0.995, 1)
            else:
                # If LTP fetch fails, fall back to MARKET order only for this case
                limit_price = 0.0

        order_kwargs = dict(
            tradingsymbol=opt["tradingsymbol"],
            exchange=opt["exchange"],
            transaction_type=(kite.TRANSACTION_TYPE_BUY
                              if action == "BUY" else kite.TRANSACTION_TYPE_SELL),
            quantity=opt["lot_size"] * lots,
            product=kite.PRODUCT_NRML,
            variety=kite.VARIETY_REGULAR,
        )
        if limit_price > 0:
            order_kwargs["order_type"] = kite.ORDER_TYPE_LIMIT
            order_kwargs["price"]      = limit_price
        else:
            # Fallback: market order when LTP unavailable
            order_kwargs["order_type"] = kite.ORDER_TYPE_MARKET
            print(f"[ORDER WARN] LTP unavailable for {opt['tradingsymbol']} — using MARKET")

        oid = kite.place_order(**order_kwargs)
        print(f"[ORDER] {action} {opt['tradingsymbol']} x{lots} lots "
              f"NRML LIMIT@{limit_price} → order_id={oid}")
        return str(oid)
    except Exception as e:
        print(f"[ORDER ERROR] {e}")
        return None


def exit_trade_fn(symbol: str, exit_price=None, reason: str = "signal"):
    """Place exit SELL order, update closed trades, day P&L, and Hermes memory."""
    with _trade_lock:
        t = _active_trades.get(symbol)
        if not t:
            return
        ep  = exit_price or t.get("ltp", t["entry_price"])
        d   = t["direction"]
        pts = (ep - t["entry_price"]) if d == "BUY" else (t["entry_price"] - ep)
        pnl_rs = round(pts * t["lot_size"] * t["lots"], 2)
        closed_record = {
            **t, "exit_price": ep, "exit_reason": reason,
            "exit_time": datetime.now(IST).isoformat(),
            "points": round(pts, 2), "pnl_rs": pnl_rs,
        }
        _closed_trades.insert(0, closed_record)
        if len(_closed_trades) > 50:
            _closed_trades.pop()
        place_order({
            "tradingsymbol": t["option_symbol"],
            "exchange":      t["option_exchange"],
            "lot_size":      t["lot_size"],
        }, t["lots"], "SELL", limit_price=ep)
        del _active_trades[symbol]
    with _day_lock:
        _day_pnl["realised_rs"]  += pnl_rs
        _day_pnl["trade_count"]  += 1
    print(f"[EXIT] {symbol} {reason} pts={round(pts,2)} pnl=₹{pnl_rs}")

    # ── Hermes learning: feed outcome back into brain memory ──────────────────
    # This is the core of Hermes' continuous learning loop.
    # We record whether this trade was a win/loss and the actual R:R achieved.
    try:
        risk = t.get("risk_pts") or abs(t["entry_price"] - t["sl"])
        actual_rr = round(pts / risk, 2) if risk and risk > 0 else 0.0
        outcome = "win" if pts > 0 else ("loss" if pts < 0 else "breakeven")
        order_id = t.get("order_id", "unknown")
        update_decision_outcome(
            decision_id=str(order_id),
            outcome=outcome,
            pts_captured=round(pts, 2),
            actual_rr=actual_rr,
            exit_reason=reason,
        )
        # ── MTF timeframe learning: correct the placeholder win flag ──────────
        # When hermes_mtf_scan fires, it records the chosen TF with win=False.
        # Here we correct it to the real outcome so the TF win-rate accumulates.
        idx_key  = _index_key(symbol)
        tf_key   = f"TF_{idx_key}_{t.get('interval','10m')}_{d}"
        update_mtf_outcome(tf_key, win=(pts > 0), rr=actual_rr)
        print(f"[Hermes LEARN] {symbol} {d} {outcome} pts={round(pts,2)} "
              f"rr={actual_rr} reason={reason} | TF={tf_key} → memory updated")
    except Exception as e:
        print(f"[Hermes LEARN] outcome update failed: {e}")


# ════════════════════════════════════════════════════════════════════════════════
# OPTION CHART & EXITS
# ════════════════════════════════════════════════════════════════════════════════

def fetch_option_ohlc(token: int, interval="3minute") -> pd.DataFrame:
    now = datetime.now(IST)
    fdt = (now - timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)
    try:
        raw = kite.historical_data(token, fdt, now, interval, False, False)
    except Exception as e:
        print(f"[OPT OHLC] {e}"); return pd.DataFrame()
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw)
    df.rename(columns={"date":"Date","open":"Open","high":"High",
                        "low":"Low","close":"Close","volume":"Volume"}, inplace=True)
    df["Date"] = pd.to_datetime(df["Date"])
    if df["Date"].dt.tz is None:
        df["Date"] = df["Date"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
    else:
        df["Date"] = df["Date"].dt.tz_convert("Asia/Kolkata")
    df.set_index("Date", inplace=True)
    return df[["Open","High","Low","Close","Volume"]].copy()


def check_option_exits(symbol: str, interval: str):
    """E1 (RSI ≥ 88) and E4 (15% trail on option premium) — runs on every poll."""
    with _trade_lock:
        t = dict(_active_trades.get(symbol, {}))
    if not t:
        return
    tok = t.get("option_token")
    if not tok:
        return
    ki  = YF_TO_KITE.get(interval, "3minute")
    odf = fetch_option_ohlc(tok, ki)
    if odf.empty or len(odf) < 5:
        return
    rsi      = compute_rsi(odf["Close"], 14)
    last_rsi = float(rsi.iloc[-1]) if not np.isnan(rsi.iloc[-1]) else 50.0
    ltp      = float(odf["Close"].iloc[-1])
    with _trade_lock:
        tr = _active_trades.get(symbol)
        if not tr:
            return
        tr["opt_ltp"] = ltp
        tr["opt_rsi"] = round(last_rsi, 1)
        peak = max(tr.get("opt_peak", ltp), ltp)
        tr["opt_peak"]     = peak
        tr["opt_trail_sl"] = round(peak * (1 - TRAIL_PCT), 2)
        reason = None
        if tr["direction"] == "BUY"  and last_rsi >= RSI_OB_EXIT:
            reason = f"RSI_OB_{last_rsi:.0f}"
        elif tr["direction"] == "SELL" and last_rsi <= RSI_OS_EXIT:
            reason = f"RSI_OS_{last_rsi:.0f}"
        elif ltp <= tr["opt_trail_sl"]:
            reason = "OPT_TRAIL_15pct"
    if reason:
        exit_trade_fn(symbol, reason=reason)


# ════════════════════════════════════════════════════════════════════════════════
# DAY HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def _reset_day():
    with _day_lock:
        today = date.today().isoformat()
        if _day_pnl["date"] != today:
            _day_pnl.update({"realised_rs": 0.0, "trade_count": 0, "date": today})
            _day_targets.update({"trading_halted": False, "halt_reason": None})


def _check_limits():
    _reset_day()
    with _day_lock:
        pt  = _day_targets["profit_target_rs"]
        ml  = _day_targets["max_loss_rs"]
        mt  = _day_targets["max_trades"]
        pnl = _day_pnl["realised_rs"]
        cnt = _day_pnl["trade_count"]
    if pt and pnl >= pt:  return True, f"Profit target ₹{pt:,.0f} reached"
    if ml and pnl <= ml:  return True, f"Max loss ₹{ml:,.0f} breached"
    if mt and cnt >= mt:  return True, f"Max {mt} trades reached"
    return False, None


# ════════════════════════════════════════════════════════════════════════════════
# SERIALISE OBs
# ════════════════════════════════════════════════════════════════════════════════

def ser_obs(obs, df):
    n = len(df)
    return [{
        "top":        o["top"],
        "bottom":     o["bottom"],
        "start_time": o["start_time"].isoformat(),
        "end_time":   (df.index[o["break_idx"]].isoformat()
                       if o.get("break_idx") is not None and o["break_idx"] < n
                       else df.index[-1].isoformat()),
        "type":       o["type"],
        "obVolume":   int(o.get("obVolume", 0)),
        "vol_pct":    o.get("vol_pct", 0),
        "fut_vol_pct":o.get("fut_vol_pct", 0),
        "breaker":    o.get("breaker", False),
    } for o in obs]


# ════════════════════════════════════════════════════════════════════════════════
# /data  — main chart data endpoint
# ════════════════════════════════════════════════════════════════════════════════


# ── Per-symbol computed-result cache ──────────────────────────────────────────
# Stores the last full compute result keyed by (symbol, interval, candle_open).
# If the same candle boundary is still current we return the cached result,
# only recomputing the live-exit/E5 portion (which needs no API call).
_result_cache: dict = {}   # key → {"payload": dict, "candle_open": datetime}
_result_lock  = threading.Lock()


def _current_candle_open(interval: str) -> datetime:
    """Alias for _candle_boundary — kept for readability at call sites."""
    return _candle_boundary(interval)


@app.route("/data")
def data_endpoint():
    symbol   = request.args.get("symbol",   "^NSEI")
    interval = request.args.get("interval", "15m")
    ki       = YF_TO_KITE.get(interval, "15minute")

    candle_open = _current_candle_open(interval)
    cache_key   = (symbol, interval)

    # ── Check result cache (same candle boundary) ─────────────────────────────
    with _result_lock:
        cached = _result_cache.get(cache_key)
        cache_hit = (cached is not None and cached["candle_open"] == candle_open)

    if cache_hit:
        # Candle hasn't closed — skip ALL Kite API calls.
        # Only refresh: active trade state, live exit checks, E5 cross.
        payload = dict(cached["payload"])  # shallow copy

        # Use last close from cached df for exit logic (no new fetch needed)
        lc  = payload["close"][-1]  if payload.get("close")  else 0.0
        lh  = payload["high"][-1]   if payload.get("high")   else 0.0
        ll  = payload["low"][-1]    if payload.get("low")    else 0.0
        e9v = payload["ema9"][-1]   if payload.get("ema9")   else 0.0
        e13v= payload["ema13"][-1]  if payload.get("ema13")  else 0.0
        pe9v= payload["ema9"][-2]   if len(payload.get("ema9",[]))>1   else e9v
        pe13v=payload["ema13"][-2]  if len(payload.get("ema13",[]))>1  else e13v

        # Trail stop ratchet + exit checks (stateful, no API)
        with _trade_lock:
            ts_ = dict(_active_trades.get(symbol, {}))
        if ts_:
            d = ts_["direction"]
            with _trade_lock:
                tr = _active_trades.get(symbol)
                if tr:
                    if d == "BUY":
                        new_trail = round(lc * (1 - TRAIL_PCT), 2)
                        if new_trail > tr["trail_stop"]:
                            tr["trail_stop"] = new_trail
                    else:
                        new_trail = round(lc * (1 + TRAIL_PCT), 2)
                        if new_trail < tr["trail_stop"]:
                            tr["trail_stop"] = new_trail
                    ts_ = dict(tr)
            if d == "BUY":
                if lc <= ts_["trail_stop"]:
                    exit_trade_fn(symbol, lc, "trail_stop")
                elif lh >= ts_["t3"]:
                    exit_trade_fn(symbol, ts_["t3"], "T3_target")
                elif lh >= ts_["t2"]:
                    exit_trade_fn(symbol, ts_["t2"], "T2_OB_target")
            else:
                if lc >= ts_["trail_stop"]:
                    exit_trade_fn(symbol, lc, "trail_stop")
                elif ll <= ts_["t3"]:
                    exit_trade_fn(symbol, ts_["t3"], "T3_target")
                elif ll <= ts_["t2"]:
                    exit_trade_fn(symbol, ts_["t2"], "T2_OB_target")

        # E5 check (no API)
        e5_reverse_cross = None
        if e9v < e13v and pe9v >= pe13v:
            e5_reverse_cross = "SELL"
        elif e9v > e13v and pe9v <= pe13v:
            e5_reverse_cross = "BUY"
        with _trade_lock:
            active_now = dict(_active_trades.get(symbol, {}))
        if e5_reverse_cross and active_now:
            active_dir = active_now.get("direction")
            if ((active_dir == "BUY"  and e5_reverse_cross == "SELL") or
                (active_dir == "SELL" and e5_reverse_cross == "BUY")):
                exit_trade_fn(symbol, lc, "E5_reverse_cross")
                print(f"[E5] Auto-exit {symbol} — reverse cross {e5_reverse_cross}")

        with _trade_lock:
            active = dict(_active_trades.get(symbol, {}))

        payload["active_trade"]      = active if active else None
        payload["recent_exits"]      = _closed_trades[:5]
        payload["e5_reverse_cross"]  = e5_reverse_cross
        return jsonify(payload)

    # ── Cache miss — new candle: full fetch + compute ─────────────────────────
    df = _get_cached_candles(symbol, interval)
    if df.empty:
        return jsonify({"error": "no data", "symbol": symbol})

    # Futures volume enrichment
    idx_key = _index_key(symbol)
    try:
        fut_info = resolve_fut_token(idx_key, kite)
        if fut_info:
            fut_vol     = fetch_futures_volume(fut_info["token"], kite, ki)
            session_vol = fut_vol.get("session_vol", 0)
        else:
            session_vol = 0
    except Exception:
        session_vol = 0

    bulls, bears = detect_order_blocks(df)
    if session_vol > 0:
        bulls = enrich_obs_with_futures_vol(bulls, session_vol)
        bears = enrich_obs_with_futures_vol(bears, session_vol)

    st_dir, st_up, st_dn = compute_supertrend(df, ST_PERIOD, ST_MULT)
    e9  = calc_ema(df["Close"], 9)
    e13 = calc_ema(df["Close"], 13)

    sweeps  = detect_liquidity_sweeps(df)
    signals = generate_signals(df, bulls, bears, sweeps=sweeps)
    signals = backtest_signals(df, signals)

    # ── Debug trace: explain why the latest bar produced no signal ───────────
    if not any(s.get("bar_idx") == len(df) - 1 for s in signals):
        _debug_last_bar_signal(df, bulls, bears, sweeps)

    # ── ATM option preview for the latest live (not-yet-exited) signal ───────
    atm_preview = None
    live_sigs = [s for s in signals if s.get("exit_reason") in (None, "Open")]
    if live_sigs:
        try:
            last_sig = live_sigs[-1]
            opt = get_atm_option(symbol, last_sig["entry"], last_sig["direction"])
            if opt:
                try:
                    ltp_raw = kite.ltp([f"{opt['exchange']}:{opt['tradingsymbol']}"])
                    opt_ltp = float(list(ltp_raw.values())[0]["last_price"])
                except Exception:
                    opt_ltp = 0.0
                atm_preview = {
                    "tradingsymbol": opt["tradingsymbol"],
                    "strike":        opt["strike"],
                    "opt_type":      opt["opt_type"],
                    "expiry":        opt["expiry"],
                    "lot_size":      opt["lot_size"],
                    "ltp":           opt_ltp,
                }
        except Exception as e:
            print(f"[ATM PREVIEW] {symbol}: {e}")

    # Live exit checks (uses just-fetched df)
    with _trade_lock:
        ts_ = dict(_active_trades.get(symbol, {}))

    if ts_:
        lc = float(df["Close"].iloc[-1])
        lh = float(df["High"].iloc[-1])
        ll = float(df["Low"].iloc[-1])
        d  = ts_["direction"]
        with _trade_lock:
            tr = _active_trades.get(symbol)
            if tr:
                if d == "BUY":
                    new_trail = round(lc * (1 - TRAIL_PCT), 2)
                    if new_trail > tr["trail_stop"]:
                        tr["trail_stop"] = new_trail
                else:
                    new_trail = round(lc * (1 + TRAIL_PCT), 2)
                    if new_trail < tr["trail_stop"]:
                        tr["trail_stop"] = new_trail
                ts_ = dict(tr)
        if d == "BUY":
            if lc <= ts_["trail_stop"]:
                exit_trade_fn(symbol, lc, "trail_stop")
            elif lh >= ts_["t3"]:
                exit_trade_fn(symbol, ts_["t3"], "T3_target")
            elif lh >= ts_["t2"]:
                exit_trade_fn(symbol, ts_["t2"], "T2_OB_target")
        else:
            if lc >= ts_["trail_stop"]:
                exit_trade_fn(symbol, lc, "trail_stop")
            elif ll <= ts_["t3"]:
                exit_trade_fn(symbol, ts_["t3"], "T3_target")
            elif ll <= ts_["t2"]:
                exit_trade_fn(symbol, ts_["t2"], "T2_OB_target")

    check_option_exits(symbol, interval)

    n_  = len(df)
    e5_reverse_cross = None
    if n_ >= 2:
        le9  = float(e9.iloc[-1]);  le13  = float(e13.iloc[-1])
        pe9  = float(e9.iloc[-2]);  pe13  = float(e13.iloc[-2])
        if le9 < le13 and pe9 >= pe13:
            e5_reverse_cross = "SELL"
        elif le9 > le13 and pe9 <= pe13:
            e5_reverse_cross = "BUY"

    with _trade_lock:
        active_now = dict(_active_trades.get(symbol, {}))
    if e5_reverse_cross and active_now:
        active_dir = active_now.get("direction")
        if ((active_dir == "BUY"  and e5_reverse_cross == "SELL") or
            (active_dir == "SELL" and e5_reverse_cross == "BUY")):
            exit_trade_fn(symbol, float(df["Close"].iloc[-1]), "E5_reverse_cross")
            print(f"[E5] Auto-exit {symbol} — reverse cross {e5_reverse_cross}")

    ts_out  = [t.isoformat() for t in df.index.to_pydatetime()]
    st_bull = [float(st_dn[i]) if st_dir[i] ==  1 else None for i in range(n_)]
    st_bear = [float(st_up[i]) if st_dir[i] == -1 else None for i in range(n_)]

    with _trade_lock:
        active = dict(_active_trades.get(symbol, {}))

    total = len(signals)
    wins  = sum(1 for s in signals if (s.get("pts_captured") or 0) > 0)
    loss  = sum(1 for s in signals if (s.get("pts_captured") or 0) < 0)
    open_ = sum(1 for s in signals if s.get("exit_reason") == "Open")
    tpts  = sum(s.get("pts_captured") or 0 for s in signals if s.get("exit_reason") != "Open")

    # ── Trim chart payload to the last CHART_DAYS days ────────────────────────
    # Indicators/OBs/signals above are computed on the FULL lookback window so
    # EMA/Supertrend/order-block warm-up stays accurate; only what's sent to
    # the chart is trimmed here.
    cutoff   = df.index[-1] - timedelta(days=CHART_DAYS)
    vis_idx0 = int((df.index >= cutoff).argmax())

    def _ob_visible(o):
        if o["start_idx"] >= vis_idx0: return True
        bi = o.get("break_idx")
        return bi is None or bi >= vis_idx0

    bulls_vis   = [o for o in bulls   if _ob_visible(o)]
    bears_vis   = [o for o in bears   if _ob_visible(o)]
    signals_vis = [s for s in signals if s.get("bar_idx", 0) >= vis_idx0]

    # Build the cacheable payload (everything except live state)
    static_payload = {
        "symbol": symbol, "interval": interval, "timestamps": ts_out[vis_idx0:],
        "open":   df["Open"].round(2).tolist()[vis_idx0:],
        "high":   df["High"].round(2).tolist()[vis_idx0:],
        "low":    df["Low"].round(2).tolist()[vis_idx0:],
        "close":  df["Close"].round(2).tolist()[vis_idx0:],
        "volume": df["Volume"].astype(int).tolist()[vis_idx0:],
        "ema9":   e9.round(2).tolist()[vis_idx0:],
        "ema13":  e13.round(2).tolist()[vis_idx0:],
        "st_bull": st_bull[vis_idx0:], "st_bear": st_bear[vis_idx0:], "st_dir": st_dir.tolist()[vis_idx0:],
        "bulls":   ser_obs(bulls_vis, df),
        "bears":   ser_obs(bears_vis, df),
        "signals": signals_vis,
        "atm_preview": atm_preview,
        "stats": {
            "total": total, "wins": wins, "losses": loss,
            "open": open_, "total_pts": round(tpts, 1),
        },
    }

    with _result_lock:
        _result_cache[cache_key] = {
            "payload":     static_payload,
            "candle_open": candle_open,
        }

    static_payload["active_trade"]     = active if active else None
    static_payload["recent_exits"]     = _closed_trades[:5]
    static_payload["e5_reverse_cross"] = e5_reverse_cross
    return jsonify(static_payload)




# ════════════════════════════════════════════════════════════════════════════════
# /replay_data
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/replay_data")
def replay_data():
    return data_endpoint()


# ════════════════════════════════════════════════════════════════════════════════
# /option_data
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/option_data")
def option_data_endpoint():
    symbol   = request.args.get("symbol",   "^NSEI")
    interval = request.args.get("interval", "3m")
    ki       = YF_TO_KITE.get(interval, "3minute")
    with _trade_lock:
        t = dict(_active_trades.get(symbol, {}))
    if not t:
        return jsonify({"error": "no active trade"})
    tok = t.get("option_token")
    if not tok:
        return jsonify({"error": "no token"})
    odf = fetch_option_ohlc(tok, ki)
    if odf.empty:
        return jsonify({"error": "no data"})
    rsi = compute_rsi(odf["Close"], 14).round(2).tolist()
    ts  = [t_.isoformat() for t_ in odf.index.to_pydatetime()]
    peak = 0.0; trail = []
    for c in odf["Close"]:
        if c > peak: peak = c
        trail.append(round(peak * (1 - TRAIL_PCT), 2))
    return jsonify({
        "option_symbol":  t.get("option_symbol"),
        "timestamps":     ts,
        "open":           odf["Open"].round(2).tolist(),
        "high":           odf["High"].round(2).tolist(),
        "low":            odf["Low"].round(2).tolist(),
        "close":          odf["Close"].round(2).tolist(),
        "volume":         odf["Volume"].astype(int).tolist(),
        "rsi":            rsi,
        "trail_sl_line":  trail,
        "opt_ltp":        t.get("opt_ltp", 0),
        "opt_rsi":        t.get("opt_rsi", 50),
        "opt_peak":       t.get("opt_peak", 0),
        "opt_trail_sl":   t.get("opt_trail_sl", 0),
    })


# ════════════════════════════════════════════════════════════════════════════════
# /trade
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/trade", methods=["POST"])
def trade_endpoint():
    halted, reason = _check_limits()
    if halted:
        return jsonify({"status": "error", "msg": f"Halted: {reason}"})
    b         = request.get_json(force=True)
    symbol    = b.get("symbol",    "^NSEI")
    direction = b.get("direction", "BUY")
    entry     = float(b.get("entry", 0))
    sl        = float(b.get("sl",    0))
    t2        = float(b.get("t2",    0))
    t3        = float(b.get("t3",    0))
    interval  = b.get("interval",  "3m")

    # ── Calendar check ────────────────────────────────────────────────────────
    cfg_key = _index_key(symbol)
    tradeable, cal_reason = is_index_tradeable(cfg_key)
    if not tradeable:
        print(f"[CALENDAR BLOCK] {symbol}: {cal_reason}")
        return jsonify({"status": "skipped", "msg": f"Calendar block: {cal_reason}"})
    print(f"[CALENDAR OK] {symbol}: {cal_reason}")

    with _trade_lock:
        if symbol in _active_trades:
            return jsonify({"status": "error", "msg": "Trade already open"})

    opt = get_atm_option(symbol, entry, direction)
    if not opt:
        return jsonify({"status": "error", "msg": "Option not found in master"})

    # ── Fund-based qty — ignore any manually-passed lots ─────────────────────
    # Fetch option LTP for a better margin estimate
    try:
        ltp_raw = kite.ltp([f"{opt['exchange']}:{opt['tradingsymbol']}"])
        opt_ltp = float(list(ltp_raw.values())[0]["last_price"])
    except Exception:
        opt_ltp = 0.0
    lots = calculate_lots(cfg_key, opt["lot_size"], opt_ltp)
    if lots <= 0:
        return jsonify({"status": "error",
                         "msg": f"Insufficient funds for 1 lot of {opt['tradingsymbol']} "
                                f"(premium={opt_ltp}, lot_size={opt['lot_size']})"})

    oid = place_order(opt, lots, "BUY", limit_price=round(opt_ltp * 1.005, 1) if opt_ltp > 0 else 0)
    if not oid:
        return jsonify({"status": "error", "msg": "Order placement failed"})

    with _trade_lock:
        initial_trail = (round(entry * (1 - TRAIL_PCT), 2) if direction == "BUY"
                         else round(entry * (1 + TRAIL_PCT), 2))
        _active_trades[symbol] = {
            "symbol":          symbol,
            "direction":       direction,
            "entry_price":     entry,
            "sl":              sl,
            "trail_stop":      initial_trail,
            "t2":              t2,
            "t3":              t3,
            "lots":            lots,
            "lot_size":        opt["lot_size"],
            "option_symbol":   opt["tradingsymbol"],
            "option_exchange": opt["exchange"],
            "option_token":    opt["instrument_token"],
            "order_id":        oid,
            "opened":          datetime.now(IST).isoformat(),
            "ltp":             entry,
            "running_pts":     0.0,
            "strike":          opt["strike"],
            "opt_type":        opt["opt_type"],
            "expiry":          opt["expiry"],
            "opt_ltp":         0.0,
            "opt_peak":        0.0,
            "opt_trail_sl":    0.0,
            "opt_rsi":         50.0,
            "interval":        interval,
        }

    return jsonify({
        "status":       "ok",
        "order_id":     oid,
        "option":       opt["tradingsymbol"],
        "strike":       opt["strike"],
        "expiry":       opt["expiry"],
        "option_token": opt["instrument_token"],
        "lot_size":     opt["lot_size"],
    })


# ════════════════════════════════════════════════════════════════════════════════
# /exit_trade  /emergency_exit
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/exit_trade", methods=["POST"])
def exit_trade_ep():
    b      = request.get_json(force=True)
    symbol = b.get("symbol", "^NSEI")
    with _trade_lock:
        t = _active_trades.get(symbol)
    exit_trade_fn(symbol, t["ltp"] if t else None, "manual")
    last = _closed_trades[0] if _closed_trades else {}
    return jsonify({"status": "ok", "points": last.get("points", 0), "pnl_rs": last.get("pnl_rs", 0)})


@app.route("/emergency_exit", methods=["POST"])
def emerg_exit():
    b = request.get_json(force=True)
    if b.get("confirm") != "CONFIRM_EXIT_ALL":
        return jsonify({"status": "error", "msg": "Wrong confirm string"})
    with _trade_lock:
        syms = list(_active_trades.keys())
    for s in syms:
        with _trade_lock:
            t = _active_trades.get(s)
        exit_trade_fn(s, t["ltp"] if t else None, "emergency")
    return jsonify({"status": "ok", "exited": syms})


# ════════════════════════════════════════════════════════════════════════════════
# /set_day_targets  /day_status
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/set_day_targets", methods=["POST"])
def set_day_targets():
    b = request.get_json(force=True)
    with _day_lock:
        if "profit_target_rs" in b:
            _day_targets["profit_target_rs"] = float(b["profit_target_rs"]) if b["profit_target_rs"] else None
        if "max_loss_rs" in b:
            _day_targets["max_loss_rs"] = float(b["max_loss_rs"]) if b["max_loss_rs"] else None
        if "max_trades" in b:
            _day_targets["max_trades"] = int(b["max_trades"]) if b["max_trades"] else None
        if b.get("resume"):
            _day_targets.update({"trading_halted": False, "halt_reason": None})
    return jsonify({"status": "ok"})


@app.route("/day_status")
def day_status():
    _reset_day()
    with _day_lock:
        pnl  = _day_pnl["realised_rs"]
        cnt  = _day_pnl["trade_count"]
        tgts = dict(_day_targets)
    unr = 0.0
    with _trade_lock:
        for t in _active_trades.values():
            ltp = t.get("ltp", t["entry_price"]); d = t["direction"]
            unr += (ltp - t["entry_price"] if d == "BUY" else t["entry_price"] - ltp) \
                   * t["lot_size"] * t["lots"]
    pt  = tgts.get("profit_target_rs")
    pct = round(min(100, max(0, pnl / pt * 100)), 1) if pt and pt > 0 else None
    return jsonify({
        "realised_pnl":    round(pnl, 2),
        "unrealised_pnl":  round(unr, 2),
        "total_pnl":       round(pnl + unr, 2),
        "trade_count":     cnt,
        "targets":         tgts,
        "progress_pct":    pct,
        "trading_halted":  tgts.get("trading_halted", False),
        "halt_reason":     tgts.get("halt_reason"),
    })


# ════════════════════════════════════════════════════════════════════════════════
# /account  /tradelog
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/account")
def account_ep():
    symbol = request.args.get("symbol", "^NSEI")
    try:
        m      = kite.margins()
        eq     = m.get("equity", {})
        av     = eq.get("available", {})
        cash   = av.get("cash", 0)
        collat = av.get("collateral", 0)
        # "net" (== available.live_balance) is the real-time usable balance —
        # cash/collateral above are start-of-day figures shown for reference.
        total  = eq.get("net", 0)
    except Exception:
        cash = collat = total = 0
    cfg_key = _index_key(symbol)
    spot    = 0.0
    try:
        ALIAS = {"^NSEI":"NSE:NIFTY 50","NSEBANK":"NSE:NIFTY BANK","SENSEX":"BSE:SENSEX"}
        spot  = float(list(kite.ltp([ALIAS.get(symbol, f"NSE:{symbol}")]).values())[0]["last_price"])
    except Exception:
        pass
    # ATM option (CE side) — correct strike + lot_size (from master) + live
    # premium, used for fund-based lot sizing below.
    lot_size_master = 0
    opt_ltp     = 0.0
    atm_strike  = None
    atm_symbol  = None
    try:
        opt = get_atm_option(symbol, spot, "BUY") if spot > 0 else None
        if opt:
            lot_size_master = opt["lot_size"]
            atm_strike      = opt["strike"]
            atm_symbol      = opt["tradingsymbol"]
            ltp_raw = kite.ltp([f"{opt['exchange']}:{opt['tradingsymbol']}"])
            opt_ltp = float(list(ltp_raw.values())[0]["last_price"])
    except Exception:
        pass

    # Suggested lots using fund-based calculation (uses live ATM premium)
    lots = calculate_lots(cfg_key, lot_size_master if lot_size_master > 0 else 1, opt_ltp)

    # Calendar status
    tradeable, cal_reason = is_index_tradeable(cfg_key)

    return jsonify({
        "available_cash":   round(cash, 2),
        "collateral":       round(collat, 2),
        "total_available":  round(total, 2),
        "suggested_lots":   lots,
        "spot":             round(spot, 2),
        "lot_size":         lot_size_master,
        "atm_strike":       atm_strike,
        "atm_symbol":       atm_symbol,
        "atm_premium":      round(opt_ltp, 2),
        "calendar_allowed": tradeable,
        "calendar_reason":  cal_reason,
    })


@app.route("/tradelog")
def tradelog_ep():
    """
    Returns only:
      orders    — broker orders with status COMPLETE for today (filled trades only)
      positions — broker positions with non-zero open qty (running positions)
      active    — internal _active_trades snapshot (bot's live positions)
    REJECTED, CANCELLED, AMO CANCELLED and any other failed statuses are excluded.
    """
    SHOW_STATUSES = {"COMPLETE"}          # only fully-filled broker orders
    orders    = []
    positions = []
    active    = []

    try:
        today = date.today().isoformat()
        orders = [
            {
                "order_id":  o.get("order_id"),
                "symbol":    o.get("tradingsymbol"),
                "type":      o.get("transaction_type"),
                "qty":       o.get("filled_quantity") or o.get("quantity"),
                "price":     round(float(o.get("average_price") or 0), 2),
                "status":    o.get("status"),
                "time":      str(o.get("order_timestamp", ""))[:19],
                "product":   o.get("product"),
                "exchange":  o.get("exchange"),
            }
            for o in kite.orders()
            if str(o.get("order_timestamp", "")).startswith(today)
            and (o.get("status") or "").upper() in SHOW_STATUSES
        ]
    except Exception as e:
        log.debug("[tradelog] orders fetch failed: %s", e)

    try:
        positions = [
            {
                "symbol": p.get("tradingsymbol"),
                "qty":    p.get("quantity"),
                "pnl":    round(float(p.get("pnl") or 0), 2),
                "ltp":    round(float(p.get("last_price") or 0), 2),
                "avg":    round(float(p.get("average_price") or 0), 2),
                "product":p.get("product"),
            }
            for p in kite.positions().get("day", [])
            if (p.get("quantity") or 0) != 0   # non-zero = still open
        ]
    except Exception as e:
        log.debug("[tradelog] positions fetch failed: %s", e)

    # Internal active trades — these are the bot's currently managed trades
    with _trade_lock:
        active = [
            {
                "symbol":       sym,
                "direction":    t.get("direction"),
                "option":       t.get("option_symbol"),
                "entry":        t.get("entry_price"),
                "trail_sl":     t.get("trail_stop"),
                "t2":           t.get("t2"),
                "t3":           t.get("t3"),
                "lots":         t.get("lots"),
                "lot_size":     t.get("lot_size"),
                "opt_ltp":      t.get("opt_ltp"),
                "opt_peak":     t.get("opt_peak"),
                "running_pts":  t.get("running_pts"),
                "opened":       t.get("opened"),
                "order_id":     t.get("order_id"),
            }
            for sym, t in _active_trades.items()
            if t.get("order_id")  # only trades that actually have a placed order
        ]

    return jsonify({
        "orders":           orders,
        "positions":        positions,
        "active":           active,
        "internal_exits":   _closed_trades[:20],
    })


# ════════════════════════════════════════════════════════════════════════════════
# /hermes_state  /hermes_reset  (legacy JS pattern memory endpoints)
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/hermes_state", methods=["GET", "POST"])
def hermes_state_ep():
    global _hermes_state
    if request.method == "GET":
        with _hermes_lock:
            state = dict(_hermes_state)
        state["winning_patterns"] = {
            k: v for k, v in state["patterns"].items()
            if v.get("count", 0) >= 2
            and v.get("wins", 0) / max(1, v["count"]) >= 0.50
        }
        return jsonify(state)

    b = request.get_json(force=True)
    with _hermes_lock:
        if "patterns" in b:
            for key, incoming in b["patterns"].items():
                ex = _hermes_state["patterns"].get(key, {"count": 0, "wins": 0, "totalRR": 0.0})
                ex["count"]   = ex.get("count", 0) + incoming.get("count", 0)
                ex["totalRR"] = ex.get("totalRR", 0.0) + incoming.get("totalRR", 0.0)
                if incoming.get("wins", 0) > 0:
                    ex["wins"] = ex.get("wins", 0) + incoming["wins"]
                _hermes_state["patterns"][key] = ex
        if "seenSignalKeys" in b:
            existing = set(_hermes_state["seenSignalKeys"])
            existing.update(b["seenSignalKeys"])
            _hermes_state["seenSignalKeys"] = list(existing)[-2000:]
        _hermes_state["totalTrades"] = sum(p["count"] for p in _hermes_state["patterns"].values())
        _hermes_state["wins"]        = sum(p["wins"]  for p in _hermes_state["patterns"].values())
    return jsonify({"status": "ok"})


@app.route("/hermes_reset", methods=["POST"])
def hermes_reset_ep():
    global _hermes_state
    with _hermes_lock:
        _hermes_state = {"patterns": {}, "totalTrades": 0, "wins": 0, "seenSignalKeys": []}
    return jsonify({"status": "ok"})


# ════════════════════════════════════════════════════════════════════════════════
# /hermes_brain  — FULL LLM analysis + auto-trade
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/hermes_brain")
def hermes_brain_endpoint():
    symbol   = request.args.get("symbol",   "^NSEI")
    interval = request.args.get("interval", "10m")

    df = _get_cached_candles(symbol, interval)
    if df.empty:
        return jsonify({"error": "no data"})

    idx_key = _index_key(symbol)
    try:
        fut_info    = resolve_fut_token(idx_key, kite)
        fut_vol_raw = fetch_futures_volume(fut_info["token"], kite, YF_TO_KITE.get(interval,"3minute")) if fut_info else {}
    except Exception:
        fut_info = {}; fut_vol_raw = {}

    # Calendar / DTE — based on the nearest WEEKLY option expiry for this index
    expiry = _nearest_expiry(idx_key)
    dte    = (expiry - datetime.now(IST).date()).days if expiry else None
    cal_ok_hb, cal_msg_hb = is_index_tradeable(idx_key)

    bulls, bears = detect_order_blocks(df)
    if fut_vol_raw.get("session_vol", 0):
        bulls = enrich_obs_with_futures_vol(bulls, fut_vol_raw["session_vol"])
        bears = enrich_obs_with_futures_vol(bears, fut_vol_raw["session_vol"])

    ema9  = calc_ema(df["Close"], 9)
    ema13 = calc_ema(df["Close"], 13)
    atr_s = compute_atr(df, 14)
    st_dir, st_up, st_dn = compute_supertrend(df, ST_PERIOD, ST_MULT)
    sweeps  = detect_liquidity_sweeps(df)
    signals = generate_signals(df, bulls, bears, sweeps=sweeps)
    signals = backtest_signals(df, signals)

    with _trade_lock:
        active = dict(_active_trades.get(symbol, {}))

    vols    = df["Volume"].values
    avg_vol = float(vols[-20:].mean()) if len(vols) >= 20 else float(vols.mean())

    def _ser(obs):
        return [{"top": o["top"], "bottom": o["bottom"],
                 "strength": o.get("vol_pct", 0),
                 "vol_pct": o.get("vol_pct", 0),
                 "fut_vol_pct": o.get("fut_vol_pct", 0)} for o in obs]

    chart_state = {
        "symbol":       symbol,
        "interval":     interval,
        "last_close":   float(df["Close"].iloc[-1]),
        "last_ema9":    float(ema9.iloc[-2]),
        "last_ema13":   float(ema13.iloc[-2]),
        "prev_ema9":    float(ema9.iloc[-3]) if len(ema9) > 1 else float(ema9.iloc[-2]),
        "prev_ema13":   float(ema13.iloc[-3]) if len(ema13) > 1 else float(ema13.iloc[-2]),
        "last_st_dir":  int(st_dir[-1]),
        "last_st_line": float(st_dn[-1] if st_dir[-1] == 1 else st_up[-1]),
        "last_atr":     float(atr_s.iloc[-1]),
        "last_volume":  int(df["Volume"].iloc[-1]),
        "avg_volume":   int(avg_vol),
        "bulls":        _ser(bulls),
        "bears":        _ser(bears),
        "sweeps":       [],
        "signals":      signals,
        "active_trade": active if active else None,
        "signal_stats": {
            "total":    len(signals),
            "wins":     sum(1 for s in signals if (s.get("pts_captured") or 0) > 0),
            "win_rate": round(
                sum(1 for s in signals if (s.get("pts_captured") or 0) > 0)
                / max(1, len(signals)) * 100, 1),
        },
        "calendar": {"allowed": cal_ok_hb, "reason": cal_msg_hb, "dte": dte},
        "futures":  fut_info or {},
    }

    result = hermes_analyse(chart_state)
    stats  = get_brain_stats()
    health = check_ollama_health()

    # ── Auto-trade execution ──────────────────────────────────────────────────
    auto_cfg = get_auto_cfg()
    halted, _ = _check_limits()

    if (auto_cfg.get("enabled")
            and result["decision"] in ("BUY", "SELL")
            and result["confidence"] >= auto_cfg.get("min_confidence", 70)
            and not active
            and is_market_hours()
            and not halted
            and cal_ok_hb):

        live_sigs = [s for s in signals if s.get("exit_reason") in (None, "Open")]
        if live_sigs:
            sig = live_sigs[-1]
            if sig["direction"] == result["decision"]:
                max_lots = auto_cfg.get("max_lots", 1)  # advisory only; fund-based qty used
                print(f"[Hermes AUTO-TRADE] {result['decision']} conf={result['confidence']}% "
                      f"| {result['reason'][:60]}")
                threading.Thread(
                    target=_run_auto_trade,
                    args=(symbol, interval, sig, max_lots),
                    daemon=True,
                ).start()
    elif not cal_ok_hb:
        print(f"[Hermes AUTO BLOCK] {symbol}: {cal_msg_hb}")

    # ── Auto-exit on Hermes EXIT_NOW advice ──────────────────────────────────
    if active and result.get("exit_advice") == "EXIT_NOW":
        print(f"[Hermes EXIT_NOW] {result['reason'][:60]}")
        exit_trade_fn(symbol, reason="hermes3_exit_now")

    return jsonify({**result, "stats": stats, "health": health,
                    "calendar": {"allowed": cal_ok_hb, "reason": cal_msg_hb}})


def _run_auto_trade(symbol: str, interval: str, sig: dict, lots: int):
    """Background thread: place order for Hermes auto-trade."""
    halted, _ = _check_limits()
    if halted:
        return
    with _trade_lock:
        if symbol in _active_trades:
            return

    # ── Calendar guard ────────────────────────────────────────────────────────
    cfg_key = _index_key(symbol)
    tradeable, cal_reason = is_index_tradeable(cfg_key)
    if not tradeable:
        print(f"[Hermes AUTO BLOCK] {symbol}: {cal_reason}")
        return
    print(f"[Hermes AUTO CALENDAR OK] {symbol}: {cal_reason}")

    opt = get_atm_option(symbol, sig["entry"], sig["direction"])
    if not opt:
        print(f"[Hermes AUTO] Option not found for {symbol}")
        return

    # ── Fund-based qty (ignore `lots` param — always recalculate) ─────────────
    try:
        ltp_raw = kite.ltp([f"{opt['exchange']}:{opt['tradingsymbol']}"])
        opt_ltp = float(list(ltp_raw.values())[0]["last_price"])
    except Exception:
        opt_ltp = 0.0
    computed_lots = calculate_lots(cfg_key, opt["lot_size"], opt_ltp)
    if computed_lots <= 0:
        print(f"[Hermes AUTO] Insufficient funds for {symbol} {opt['tradingsymbol']} "
              f"(premium={opt_ltp}, lot_size={opt['lot_size']}) — skipping trade")
        return

    limit_px = round(opt_ltp * 1.005, 1) if opt_ltp > 0 else 0.0
    oid = place_order(opt, computed_lots, "BUY", limit_price=limit_px)
    if not oid:
        print(f"[Hermes AUTO] Order failed for {symbol}")
        return

    with _trade_lock:
        direction    = sig["direction"]
        entry_price  = sig["entry"]
        risk_pts     = sig.get("risk_pts") or abs(entry_price - sig["sl"])
        initial_trail = (round(entry_price * (1 - TRAIL_PCT), 2) if direction == "BUY"
                         else round(entry_price * (1 + TRAIL_PCT), 2))
        _active_trades[symbol] = {
            "symbol":          symbol,
            "direction":       direction,
            "entry_price":     entry_price,
            "sl":              sig["sl"],
            "trail_stop":      initial_trail,
            "t2":              sig["t2"],
            "t3":              sig["t3"],
            "risk_pts":        round(risk_pts, 2),
            "lots":            computed_lots,
            "lot_size":        opt["lot_size"],
            "option_symbol":   opt["tradingsymbol"],
            "option_exchange": opt["exchange"],
            "option_token":    opt["instrument_token"],
            "order_id":        oid,
            "opened":          datetime.now(IST).isoformat(),
            "ltp":             entry_price,
            "running_pts":     0.0,
            "strike":          opt["strike"],
            "opt_type":        opt["opt_type"],
            "expiry":          opt["expiry"],
            "opt_ltp":         opt_ltp,
            "opt_peak":        opt_ltp,
            "opt_trail_sl":    round(opt_ltp * (1 - TRAIL_PCT), 2) if opt_ltp > 0 else 0.0,
            "opt_rsi":         50.0,
            "interval":        interval,
            "hermes_auto":     True,
        }
    print(f"[Hermes AUTO] Placed {sig['direction']} {opt['tradingsymbol']} "
          f"strike={opt['strike']} expiry={opt['expiry']} lot_size={opt['lot_size']} "
          f"x{computed_lots} lots NRML LIMIT@{limit_px} order_id={oid} "
          f"risk_pts={round(risk_pts,2)}")


# ════════════════════════════════════════════════════════════════════════════════
# /hermes_brain_status  — fast status (no LLM call)
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/hermes_brain_status")
def hermes_brain_status():
    return jsonify({
        "health": check_ollama_health(),
        "last":   get_last_output(),
        "stats":  get_brain_stats(),
    })


# ════════════════════════════════════════════════════════════════════════════════
# /hermes_mtf  — multi-timeframe cross-index scan
# ════════════════════════════════════════════════════════════════════════════════
# Fetches candles for NIFTY/BANKNIFTY/SENSEX across their primary+secondary
# timeframes, runs _compute_confidence on each, then sends ONE LLM call that
# sees all frames simultaneously and picks the best (symbol, interval) to trade.
#
# Called by the frontend every 30s (much less frequent than /data).
# All fetches go through _get_cached_candles — zero extra Kite API calls if
# the candle cache is warm.
# ════════════════════════════════════════════════════════════════════════════════

def _build_chart_state_for_mtf(symbol: str, interval: str) -> dict | None:
    """
    Build a minimal chart_state dict for hermes_mtf_scan from cached candles.
    Reuses the same computation pipeline as the main /data endpoint.
    Returns None if candles unavailable.
    """
    try:
        df = _get_cached_candles(symbol, interval)
        if df.empty or len(df) < 20:
            return None

        idx_key = _index_key(symbol)
        try:
            fut_info    = resolve_fut_token(idx_key, kite)
            fut_vol_raw = fetch_futures_volume(
                fut_info["token"], kite,
                YF_TO_KITE.get(interval, "15minute")
            ) if fut_info else {}
        except Exception:
            fut_vol_raw = {}

        bulls, bears = detect_order_blocks(df)
        if fut_vol_raw.get("session_vol", 0):
            bulls = enrich_obs_with_futures_vol(bulls, fut_vol_raw["session_vol"])
            bears = enrich_obs_with_futures_vol(bears, fut_vol_raw["session_vol"])

        ema9  = calc_ema(df["Close"], 9)
        ema13 = calc_ema(df["Close"], 13)
        atr_s = compute_atr(df, 14)
        st_dir, st_up, st_dn = compute_supertrend(df, ST_PERIOD, ST_MULT)
        sweeps  = detect_liquidity_sweeps(df)
        signals = generate_signals(df, bulls, bears, sweeps=sweeps)
        signals = backtest_signals(df, signals)

        vols    = df["Volume"].values
        avg_vol = float(vols[-20:].mean()) if len(vols) >= 20 else float(vols.mean())

        total = len(signals)
        wins  = sum(1 for s in signals if (s.get("pts_captured") or 0) > 0)

        expiry = _nearest_expiry(idx_key)
        dte    = (expiry - datetime.now(IST).date()).days if expiry else None
        cal_ok, cal_msg = is_index_tradeable(idx_key)

        return {
            "symbol":       symbol,
            "interval":     interval,
            "last_close":   float(df["Close"].iloc[-1]),
            "last_ema9":    float(ema9.iloc[-1]),
            "last_ema13":   float(ema13.iloc[-1]),
            "prev_ema9":    float(ema9.iloc[-2]) if len(ema9) > 1 else float(ema9.iloc[-1]),
            "prev_ema13":   float(ema13.iloc[-2]) if len(ema13) > 1 else float(ema13.iloc[-1]),
            "last_st_dir":  int(st_dir[-1]),
            "last_st_line": float(st_dn[-1] if st_dir[-1] == 1 else st_up[-1]),
            "last_atr":     float(atr_s.iloc[-1]),
            "last_volume":  int(df["Volume"].iloc[-1]),
            "avg_volume":   int(avg_vol),
            "bulls":        [{"top":o["top"],"bottom":o["bottom"],
                              "vol_pct":o.get("vol_pct",0),"breaker":o.get("breaker",False)}
                             for o in bulls],
            "bears":        [{"top":o["top"],"bottom":o["bottom"],
                              "vol_pct":o.get("vol_pct",0),"breaker":o.get("breaker",False)}
                             for o in bears],
            "signals":      signals,
            "signal_stats": {
                "total":    total,
                "wins":     wins,
                "win_rate": round(wins / max(1, total) * 100, 1),
            },
            "calendar":     {"allowed": cal_ok, "reason": cal_msg, "dte": dte},
            "futures":      fut_vol_raw,
        }
    except Exception as e:
        log.warning("[MTF] Frame build failed %s %s: %s", symbol, interval, e)
        return None


@app.route("/hermes_mtf")
def hermes_mtf_endpoint():
    """
    Returns Hermes' cross-index, cross-timeframe recommendation.
    Builds frames for NIFTY 15m+5m, BANKNIFTY 10m+5m, SENSEX 15m+5m
    then calls hermes_mtf_scan which runs one LLM call across all.
    """
    frames = []
    for symbol, intervals in MTF_INTERVALS.items():
        for ivl in intervals:
            cs = _build_chart_state_for_mtf(symbol, ivl)
            if cs:
                frames.append(cs)

    if not frames:
        return jsonify({"error": "no data available for any instrument"})

    with _trade_lock:
        active = dict(_active_trades)   # pass live trade state to scanner

    result = hermes_mtf_scan(frames, active_trades=active)
    result["health"] = check_ollama_health()
    return jsonify(result)


# ════════════════════════════════════════════════════════════════════════════════
# /hermes_keep_warm  — called by frontend every 2 min
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/hermes_keep_warm", methods=["POST"])
def hermes_keep_warm_route():
    threading.Thread(target=warmup, daemon=True).start()
    return jsonify({"status": "ok"})


# ════════════════════════════════════════════════════════════════════════════════
# /auto_trade_control  — enable/disable from UI toggle
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/auto_trade_control", methods=["POST"])
def auto_trade_control():
    b = request.get_json(force=True)
    # NOTE: enabled is always forced True — auto-trade must stay on at all times.
    # A UI toggle-off is accepted for config update purposes but immediately
    # re-enabled.  To pause trading use set_day_targets with max_trades=0.
    set_auto_cfg(
        enabled=True,                                    # always ON
        min_confidence=int(b.get("min_confidence", 70)),
        max_lots=int(b.get("max_lots", 1)),              # advisory; qty is fund-based
        product="NRML",                                  # always NRML
    )
    cfg = get_auto_cfg()
    print(f"[AutoTrade] ALWAYS ENABLED (forced) "
          f"min_conf={cfg['min_confidence']}% (qty=fund-based NRML LIMIT)")
    return jsonify({"status": "ok", "config": cfg})


# ════════════════════════════════════════════════════════════════════════════════
# /hermes_force_learn  — reset button in panel
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/hermes_force_learn", methods=["POST"])
def hermes_force_learn():
    """Reset SQLite brain memory and in-memory state."""
    global _hermes_state
    # Reset in-memory state
    with _hermes_lock:
        _hermes_state = {"patterns": {}, "totalTrades": 0, "wins": 0, "seenSignalKeys": []}
    # Reset SQLite memory via hermes_brain
    try:
        import hermes_brain as hb
        import sqlite3
        con = sqlite3.connect(hb.DB_PATH)
        cur = con.cursor()
        cur.execute("DELETE FROM patterns")
        cur.execute("DELETE FROM seen_signals")
        cur.execute("DELETE FROM decisions")
        con.commit()
        con.close()
        print("[Hermes] Memory reset via /hermes_force_learn")
    except Exception as e:
        print(f"[Hermes] Reset warning: {e}")
    return jsonify({"status": "ok", "msg": "Hermes memory cleared. Relearning from scratch."})


# ════════════════════════════════════════════════════════════════════════════════
# /hermes_log  — last 50 auto-trade log lines
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/hermes_log")
def hermes_log_ep():
    return jsonify({"log": get_auto_log(50)})


# ════════════════════════════════════════════════════════════════════════════════
# /portfolio  — auto-invest portfolio
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/portfolio")
def portfolio_ep():
    return jsonify({"portfolio": get_portfolio()})


# ════════════════════════════════════════════════════════════════════════════════
# /poller_status  — live state of all polling threads
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/poller_status")
def poller_status_ep():
    """
    Returns one entry per index with:
    - status        : sleeping | running | trading | skipped | halted | error | …
    - last_run      : ISO timestamp of last poll
    - next_run      : ISO timestamp of next scheduled poll
    - last_signal   : summary of last detected signal (or null)
    - last_action   : what the poller did last time
    - last_error    : last error string (or null)
    - run_count     : total polls since boot
    - calendar      : {allowed, reason, nearest_expiry, dte}
    - thread_alive  : whether the daemon thread is still running
    - fund_context  : available funds vs required for all-index coverage
    """
    can_all, funds, required = _can_afford_all_today()
    fund_ctx = {
        "available":      round(funds, 2),
        "required_all":   round(required, 2),
        "can_afford_all": can_all,
    }

    out = {}
    with _poller_lock:
        states = dict(_poller_state)

    for cfg_key, pcfg in INDEX_POLL_CFG.items():
        allowed, cal_reason = is_index_tradeable(cfg_key)
        expiry = _nearest_expiry(cfg_key)
        st     = states.get(cfg_key, {})
        thread = _poller_threads.get(cfg_key)
        out[cfg_key] = {
            **st,
            "symbol":       pcfg["symbol"],
            "interval":     pcfg["interval"],
            "poll_minutes": pcfg["poll_minutes"],
            "thread_alive": thread.is_alive() if thread else False,
            "calendar": {
                "allowed":        allowed,
                "reason":         cal_reason,
                "nearest_expiry": str(expiry) if expiry else None,
                "dte":            (expiry - datetime.now(IST).date()).days if expiry else None,
            },
            "fund_context": fund_ctx,
        }
    return jsonify(out)



@app.route("/calendar_status")
def calendar_status_ep():
    """Returns live tradability status for every configured index."""
    result = {}
    for cfg_key in OPTIONS_CFG:
        allowed, reason = is_index_tradeable(cfg_key)
        expiry = _nearest_expiry(cfg_key)
        result[cfg_key] = {
            "allowed":        allowed,
            "reason":         reason,
            "nearest_expiry": str(expiry) if expiry else None,
            "dte":            (expiry - datetime.now(IST).date()).days if expiry else None,
        }
    return jsonify(result)


# ════════════════════════════════════════════════════════════════════════════════
# /  — serve the dashboard
# ════════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    symbol   = request.args.get("symbol",   "^NSEI")
    interval = request.args.get("interval", "10m")
    return render_template("index.html", symbol=symbol, interval=interval)


# ════════════════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Full Hermes boot: DB init + LLM warmup + 9AM master validation
    startup(kite)
    # ── Auto-trade ON by default at every boot ────────────────────────────────
    # enabled=True means signals are acted on immediately without manual toggle.
    # Confidence threshold: 70 %.  Qty is always fund-based (calculate_lots).
    set_auto_cfg(
        enabled=True,
        min_confidence=70,
        max_lots=1,      # advisory only; actual qty computed from available funds
        product="NRML",
    )
    print("[AutoTrade] AUTO-TRADE ENABLED at boot  min_conf=70%  product=NRML  qty=fund-based")
    # Launch background polling threads (one per index)
    start_pollers()
    app.run(debug=True, use_reloader=False)