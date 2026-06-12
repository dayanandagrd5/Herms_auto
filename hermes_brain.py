"""
hermes_brain.py — Hermes Autonomous Trading Intelligence
=========================================================
Full implementation of the HERMES SOUL specification.

Capabilities:
  • SQLite-persisted pattern memory (survives Flask restarts)
  • Ollama / hermes3 LLM reasoning with structured JSON output
  • Confidence scoring per SOUL spec (50-base, capped 20-95)
  • Auto-trade coordination with Flux_913ema.py
  • BTST gap prediction
  • E5 reverse crossover exit detection
  • Futures volume integration for OB strength
  • Daily master download trigger
  • Self-improvement milestone reporting
  • Crude oil & multi-instrument framework
  • 10% daily-profit auto-invest stub

Dependencies:
  pip install requests sqlite3(stdlib) schedule
"""

import sqlite3, json, time, threading, os, logging
from datetime import datetime, date, timedelta
from typing import Optional
import requests
import pytz

# ── Config ─────────────────────────────────────────────────────────────────────
OLLAMA_BASE   = os.getenv("OLLAMA_BASE", "http://localhost:11434")
HERMES_MODEL  = os.getenv("HERMES_MODEL", "hermes3")          # ollama model tag
DB_PATH       = os.getenv("HERMES_DB",   "hermes_memory.db")  # SQLite file
CONF_THRESHOLD     = 70    # minimum confidence to auto-trade
BTST_CONF_MIN      = 65    # minimum confidence for BTST
SELF_IMPROVE_EVERY = 10    # milestone: print report every N completed trades
LLM_TIMEOUT        = 25    # seconds to wait for Ollama response
IST = pytz.timezone("Asia/Kolkata")

logging.basicConfig(level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("hermes_brain")

# ── Thread safety ──────────────────────────────────────────────────────────────
_db_lock   = threading.Lock()
_llm_lock  = threading.Lock()
_state_lock = threading.Lock()

# ── In-memory state (also persisted to DB) ─────────────────────────────────────
_last_output: dict = {}
_warmup_done: bool  = False


# ════════════════════════════════════════════════════════════════════════════════
# DATABASE — SQLite schema & helpers
# ════════════════════════════════════════════════════════════════════════════════

def init_brain_db():
    """Create tables if they don't exist. Call once at startup."""
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()

        # Pattern memory — one row per pattern key
        cur.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            key        TEXT PRIMARY KEY,
            count      INTEGER DEFAULT 0,
            wins       INTEGER DEFAULT 0,
            total_rr   REAL    DEFAULT 0.0,
            updated_at TEXT
        )""")

        # Decision log — every hermes decision with outcome
        cur.execute("""
        CREATE TABLE IF NOT EXISTS decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            symbol      TEXT,
            interval    TEXT,
            decision    TEXT,
            confidence  INTEGER,
            reason      TEXT,
            exit_advice TEXT,
            btst        TEXT,
            pattern_note TEXT,
            latency_ms  INTEGER,
            outcome_pts REAL,
            correct     INTEGER,   -- 1=win, 0=loss, NULL=pending
            created_at  TEXT DEFAULT (datetime('now'))
        )""")

        # Seen signal keys — prevents double-learning
        cur.execute("""
        CREATE TABLE IF NOT EXISTS seen_signals (
            sig_key TEXT PRIMARY KEY,
            learned_at TEXT DEFAULT (datetime('now'))
        )""")

        # Daily stats — one row per date
        cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            trade_date  TEXT PRIMARY KEY,
            total_pnl   REAL DEFAULT 0.0,
            invest_amt  REAL DEFAULT 0.0,
            trades      INTEGER DEFAULT 0
        )""")

        # Master download log
        cur.execute("""
        CREATE TABLE IF NOT EXISTS master_log (
            dl_date TEXT PRIMARY KEY,
            status  TEXT,
            ts      TEXT
        )""")

        # Portfolio — auto-invest holdings (10% of daily profit)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            symbol      TEXT PRIMARY KEY,
            qty         REAL DEFAULT 0,
            avg_price   REAL DEFAULT 0.0,
            invested    REAL DEFAULT 0.0,
            last_updated TEXT
        )""")

        con.commit()
        con.close()
    log.info("[HermesBrain] DB initialised at %s", DB_PATH)


def _db():
    """Return a thread-local SQLite connection (check_same_thread=False safe with lock)."""
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def _upsert_pattern(key: str, win: bool, rr: float):
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO patterns(key,count,wins,total_rr,updated_at) VALUES(?,0,0,0.0,?)",
                    (key, datetime.now(IST).isoformat()))
        cur.execute("""UPDATE patterns SET
            count=count+1,
            wins=wins+?,
            total_rr=total_rr+?,
            updated_at=?
            WHERE key=?""",
            (1 if win else 0, rr, datetime.now(IST).isoformat(), key))
        con.commit()
        con.close()


def _get_all_patterns() -> dict:
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("SELECT key, count, wins, total_rr FROM patterns")
        rows = cur.fetchall()
        con.close()
    return {r[0]: {"count": r[1], "wins": r[2], "totalRR": r[3]} for r in rows}


def _is_signal_seen(sig_key: str) -> bool:
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("SELECT 1 FROM seen_signals WHERE sig_key=?", (sig_key,))
        found = cur.fetchone() is not None
        con.close()
    return found


def _mark_signal_seen(sig_key: str):
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("INSERT OR IGNORE INTO seen_signals(sig_key) VALUES(?)", (sig_key,))
        con.commit()
        con.close()


def _log_decision(record: dict) -> int:
    """Insert a decision row, return rowid."""
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("""INSERT INTO decisions
            (ts, symbol, interval, decision, confidence, reason,
             exit_advice, btst, pattern_note, latency_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (record.get("ts"), record.get("symbol"), record.get("interval"),
             record.get("decision"), record.get("confidence"), record.get("reason"),
             record.get("exit_advice"), record.get("btst"),
             record.get("pattern_note"), record.get("latency_ms")))
        rowid = cur.lastrowid
        con.commit()
        con.close()
    return rowid


def update_decision_outcome(rowid: int = None, pts: float = None, correct: int = None,
                            decision_id=None, outcome: str = None,
                            pts_captured: float = None, actual_rr: float = None,
                            exit_reason: str = None):
    """
    Called after a trade closes to record accuracy.
    Accepts both the original positional signature and the keyword-arg form
    used by Flux_913ema.py exit_trade_fn:
        update_decision_outcome(decision_id=..., outcome=..., pts_captured=..., actual_rr=..., exit_reason=...)
    """
    # Normalise keyword-arg form → positional form
    if decision_id is not None and rowid is None:
        rowid = decision_id
    if outcome is not None and correct is None:
        correct = 1 if outcome == "win" else (0 if outcome == "loss" else None)
    if pts_captured is not None and pts is None:
        pts = pts_captured

    if rowid is None:
        return  # nothing to update

    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("UPDATE decisions SET outcome_pts=?, correct=? WHERE id=?",
                    (pts, correct, rowid))
        con.commit()
        con.close()


def _get_recent_decisions(limit=20) -> list:
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("""SELECT decision, confidence, correct, outcome_pts
                       FROM decisions ORDER BY id DESC LIMIT ?""", (limit,))
        rows = cur.fetchall()
        con.close()
    return [{"decision": r[0], "conf": r[1], "correct": r[2], "pts": r[3]} for r in rows]


def get_brain_stats() -> dict:
    patterns = _get_all_patterns()
    total  = sum(p["count"] for p in patterns.values())
    wins   = sum(p["wins"]  for p in patterns.values())
    decided = sum(1 for p in patterns.values() if p["count"] > 0)
    accuracy = round(wins / max(1, total) * 100, 1) if total else 0
    recent = _get_recent_decisions(6)
    return {
        "total": total, "wins": wins, "decided": decided,
        "accuracy": accuracy, "patterns": patterns,
        "recent": recent,
    }


# ════════════════════════════════════════════════════════════════════════════════
# OLLAMA / LLM
# ════════════════════════════════════════════════════════════════════════════════

def check_ollama_health() -> dict:
    try:
        r = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        if r.status_code == 200:
            models = [m["name"] for m in r.json().get("models", [])]
            hermes_loaded = any(HERMES_MODEL in m for m in models)
            return {"ollama_live": True, "hermes_loaded": hermes_loaded, "models": models}
    except Exception as e:
        log.debug("Ollama health check failed: %s", e)
    return {"ollama_live": False, "hermes_loaded": False, "models": []}


def warmup():
    """
    Send a tiny prompt to load the model into memory and keep it there.
    Called once at startup, then re-called periodically via /hermes_keep_warm
    so Ollama's keep_alive timer never lapses and hermes3 stays resident.
    """
    global _warmup_done
    try:
        call_hermes3("Respond with exactly: READY", max_tokens=10, timeout=30)
        if not _warmup_done:
            _warmup_done = True
            log.info("[Hermes] Model warm.")
        else:
            log.debug("[Hermes] Keep-warm ping ok.")
    except Exception as e:
        log.warning("[Hermes] Warmup failed (will retry): %s", e)


def call_hermes3(prompt: str, system: str = "", max_tokens: int = 400,
                 timeout: int = LLM_TIMEOUT) -> str:
    """
    Call Ollama /api/generate. Returns raw text string.
    Raises requests.RequestException on network failure.
    Raises ValueError on empty response (caller must handle).
    """
    payload = {
        "model":  HERMES_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.1, "top_p": 0.9},
    }
    if system:
        payload["system"] = system

    with _llm_lock:
        r = requests.post(f"{OLLAMA_BASE}/api/generate",
                          json=payload, timeout=timeout)
    r.raise_for_status()

    # Guard 1: HTTP body must be non-empty
    raw_body = r.text.strip()
    if not raw_body:
        raise ValueError("Ollama returned empty HTTP body")

    # Guard 2: outer envelope must parse
    try:
        outer = r.json()
    except Exception:
        raise ValueError(f"Ollama non-JSON body: {raw_body[:120]}")

    # Guard 3: inner "response" field must be non-empty
    inner = outer.get("response", "").strip()
    if not inner:
        raise ValueError(f"Ollama empty 'response' field. done={outer.get('done')} "
                         f"eval_count={outer.get('eval_count')}")

    return inner


# ════════════════════════════════════════════════════════════════════════════════
# PROMPT BUILDER
# ════════════════════════════════════════════════════════════════════════════════

HERMES_SYSTEM = """You are Hermes — a professional Indian equity derivatives trader with 15 years experience.
You specialise in index options (NIFTY CE/PE, SENSEX CE/PE, BANKNIFTY CE/PE) on WEEKLY EXPIRIES.

CRITICAL CONTEXT — OPTIONS, NOT SPOT:
  - All trades are options (CE for BUY signals, PE for SELL signals)
  - SL is always managed on the OPTION PREMIUM, not the spot/index level
  - Trail stop = 15% below the peak option premium (E4 rule)
  - RSI ≥ 88 on the option chart triggers exit (E1 rule)
  - Reverse EMA cross on the index triggers exit (E5 rule)
  - T2/T3 are spot price levels — translate to option premium appreciation

WEEKLY EXPIRY RULES (non-negotiable):
  - NIFTY: weekly expiry every Thursday — avoid fresh entries after 11 AM on expiry day
  - SENSEX: weekly expiry every Friday — avoid fresh entries after 11 AM on expiry day
  - BANKNIFTY: weekly expiry every Wednesday — avoid fresh entries after 11 AM on expiry day
  - DTE ≤ 1: ONLY enter if signal is extremely strong (conf ≥ 80, fresh EMA cross + ST aligned + OB)
  - DTE = 0 (expiry day): NO new entries under any circumstances

STRATEGY — OB Flux Triple Confirmation (10m candles, all indices):
  - Triple confirmation: Order Block (OB) + 9/13 EMA crossover + SuperTrend direction
  - Entry: CE when EMA9 > EMA13 (fresh cross), ST=BULL, price retesting valid Bull OB
  - Entry: PE when EMA9 < EMA13 (fresh cross), ST=BEAR, price retesting valid Bear OB
  - Liquidity sweep must precede the OB retest (stop-hunt confirmation)
  - Exit CE/PE: option RSI≥88, option 15% trail breached, T3 spot reached, or E5 reverse cross
  - BTST: buy CE/PE overnight when close in top/bottom 25% of day range with directional bias

SL MANAGEMENT RULES:
  - Initial SL set at the OB low/high on the SPOT chart for reference
  - Operational SL is 15% below peak option premium (trail) — this is what gets hit/updated
  - As option premium rises, trail moves up automatically — NEVER move trail down
  - On T1 hit (50% of risk): raise trail to breakeven of option entry premium
  - On T2 hit: trail at 10% below peak premium (tighten)

You speak concisely. Numbers only. No fluff.
Always respond ONLY with valid JSON — no markdown, no prose outside JSON.
"""

def build_market_prompt(chart_state: dict) -> str:
    """Build the structured prompt Hermes reads each cycle."""
    cs = chart_state
    sig = (cs.get("signals") or [None])[-1]  # most recent signal
    active = cs.get("active_trade")
    patterns = _get_all_patterns()
    # Winning patterns for context
    win_pats = {k: v for k, v in patterns.items()
                if v["count"] >= 2 and v["wins"] / max(1, v["count"]) >= 0.50}

    # ── Weekly expiry / DTE context ──────────────────────────────────────────
    cal = cs.get("calendar", {})
    dte = cal.get("dte", None)
    expiry_warning = ""
    if dte is not None:
        if dte == 0:
            expiry_warning = "🔴 EXPIRY DAY — NO NEW ENTRIES. Exit any open positions before 11AM."
        elif dte == 1:
            expiry_warning = "🟡 DTE=1 — Only enter if confidence ≥ 80 AND all 3 confirmations fresh."
        elif dte <= 2:
            expiry_warning = f"🟡 DTE={dte} — Late week; prefer exits over new entries unless A+ setup."
        else:
            expiry_warning = f"🟢 DTE={dte} — Safe window for new entries."

    # ── Active option context ────────────────────────────────────────────────
    opt_ctx = ""
    if active:
        opt_ltp     = active.get("opt_ltp", 0)
        opt_peak    = active.get("opt_peak", 0)
        opt_trail   = active.get("opt_trail_sl", 0)
        opt_rsi     = active.get("opt_rsi", 50)
        opt_sym     = active.get("option_symbol", "?")
        trail_pct   = round((1 - opt_trail / opt_peak) * 100, 1) if opt_peak else 0
        opt_ctx = f"""
OPTION POSITION (what's actually trading):
  Symbol    : {opt_sym}
  Option LTP: {opt_ltp}   Peak: {opt_peak}   Trail SL: {opt_trail} ({trail_pct}% below peak)
  Option RSI: {opt_rsi}  ← EXIT if ≥ 88 (CE) or ≤ 12 (PE)
  Note: SL is managed on OPTION PREMIUM not spot index
"""

    prompt = f"""
MARKET STATE — {cs.get('symbol')} @ {cs.get('interval')} (10-minute candles)
==========================================
WEEKLY EXPIRY STATUS: {expiry_warning}

SPOT INDEX:
  Last close : {cs.get('last_close')}
  EMA9/EMA13 : {cs.get('last_ema9')} / {cs.get('last_ema13')}  (prev: {cs.get('prev_ema9')} / {cs.get('prev_ema13')})
  SuperTrend : {'BULL +1' if cs.get('last_st_dir')==1 else 'BEAR -1'}  line={cs.get('last_st_line')}
  ATR        : {cs.get('last_atr')}
  Volume     : {cs.get('last_volume')} (avg20={cs.get('avg_volume')}){opt_ctx}
BULL OBs (nearest 3):
{_fmt_obs(cs.get('bulls', [])[:3])}

BEAR OBs (nearest 3):
{_fmt_obs(cs.get('bears', [])[:3])}

RECENT SIGNALS (last 3):
{_fmt_signals(cs.get('signals', [])[-3:])}

SIGNAL STATS: total={cs['signal_stats']['total']} wins={cs['signal_stats']['wins']} WR={cs['signal_stats']['win_rate']}%

ACTIVE TRADE (SPOT reference): {json.dumps(active) if active else 'None'}

HERMES PATTERN MEMORY (winning patterns):
{json.dumps(win_pats, indent=2) if win_pats else 'No patterns yet'}

FUTURES INFO: {json.dumps(cs.get('futures', {}))}
CALENDAR: {json.dumps(cs.get('calendar', {}))}

TASK: Analyse as a professional options trader and respond with ONLY this JSON (no extra text):
{{
  "decision": "BUY" | "SELL" | "WAIT",
  "confidence": <0-100>,
  "reason": "<1-2 sentence concise reasoning including DTE context>",
  "exit_advice": "EXIT_NOW" | "HOLD" | "TIGHTEN_TRAIL" | null,
  "sl_update": "<new option trail SL level if TIGHTEN_TRAIL, else null>",
  "btst": "GAP_UP" | "GAP_DOWN" | "SKIP" | null,
  "pattern_note": "<short note on pattern memory match or null>"
}}

Professional rules (options, weekly expiry):
- BUY (CE): EMA9>EMA13 (fresh cross), ST=BULL, price at Bull OB, liquidity sweep confirmed, DTE > 1
- SELL (PE): EMA9<EMA13 (fresh cross), ST=BEAR, price at Bear OB, liquidity sweep confirmed, DTE > 1
- WAIT: DTE=0 (expiry day), OR no fresh cross, OR no OB touch, OR ST/EMA conflict
- EXIT_NOW: option RSI ≥ 88, or E5 reverse cross on spot, or DTE=0 with open position
- TIGHTEN_TRAIL: at T1 hit → trail to option entry premium; at T2 hit → trail 10% below peak
- BTST: GAP_UP if close top 25% range + bull bias + DTE ≥ 2, GAP_DOWN if bottom 25% + bear + DTE ≥ 2
- confidence reflects ALL factors: OB vol strength, EMA freshness, ST alignment, pattern WR, DTE risk
"""
    return prompt.strip()


def _fmt_obs(obs: list) -> str:
    if not obs: return "  None"
    lines = []
    for o in obs:
        lines.append(f"  top={o.get('top')} bot={o.get('bottom')} vol%={o.get('vol_pct',0)} fut_vol%={o.get('fut_vol_pct',0)}")
    return "\n".join(lines)


def _fmt_signals(sigs: list) -> str:
    if not sigs: return "  None"
    lines = []
    for s in sigs:
        lines.append(
            f"  {s.get('direction')} @ {s.get('entry')} | sl={s.get('sl')} t2={s.get('t2')} "
            f"| exit={s.get('exit_reason')} pts={s.get('pts_captured')} rr={s.get('actual_rr')}"
        )
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME SCANNER
# ════════════════════════════════════════════════════════════════════════════════
#
# hermes_mtf_scan(frames) is the entry point.
# `frames` is a list of chart_state dicts — one per (symbol × interval) combo.
# Each dict is the same structure already built by Flux_913ema for hermes_analyse.
#
# What it does:
#   1. Score every frame with _compute_confidence (fast, no LLM)
#   2. Rank by score — surface the top 3
#   3. Build a compact multi-frame comparison prompt
#   4. Send ONE LLM call that sees ALL frames simultaneously
#   5. LLM picks the best (symbol, interval) and explains why
#   6. Record the winning timeframe in pattern memory so Hermes learns
#      which combos historically produce the cleanest signals
#
# New pattern key format:  "TF_{symbol}_{interval}_{direction}"
#   e.g. "TF_BANKNIFTY_10m_BUY" — tracks win rate per instrument+timeframe+direction
#
# Called from Flux_913ema.py /hermes_mtf endpoint (new route, non-blocking).
# Also called internally by the background pollers when deciding which index to
# prioritise when funds can only cover one trade at a time.
# ════════════════════════════════════════════════════════════════════════════════

# Canonical timeframes each index is analysed on.
# The first entry is the primary; the second is the confirmation frame.
MTF_INTERVALS = {
    "^NSEI":    ["10m", "5m"],   # NIFTY  — primary 10m (proven best results), confirm 5m
    "NSEBANK":  ["10m", "5m"],   # BANKNIFTY — primary 10m, confirm 5m
    "SENSEX":   ["10m", "5m"],   # SENSEX — primary 10m (proven best results), confirm 5m
}

# Human-readable label for logging
_SYM_LABEL = {
    "^NSEI":   "NIFTY",
    "NSEBANK": "BANKNIFTY",
    "SENSEX":  "SENSEX",
}


def _score_frame(cs: dict) -> dict:
    """
    Lightweight scoring of one chart_state dict.
    Returns a summary dict used by the MTF prompt builder.
    """
    score, bias = _compute_confidence(cs)
    sigs   = cs.get("signals", [])
    live   = [s for s in sigs if s.get("exit_reason") in (None, "Open")]
    last   = live[-1] if live else None

    # ATR as % of close — measures volatility / opportunity size
    lc  = cs.get("last_close", 1) or 1
    atr = cs.get("last_atr", 0)
    atr_pct = round(atr / lc * 100, 2) if lc else 0

    # Volume health
    vol_ratio = (cs.get("last_volume", 0) / max(1, cs.get("avg_volume", 1)))

    # Backtest win rate on this frame
    ss  = cs.get("signal_stats", {})
    tot = ss.get("total", 0)
    wr  = round(ss.get("wins", 0) / tot * 100, 1) if tot >= 3 else None

    # Historical TF pattern win rate from memory
    tf_key   = f"TF_{_SYM_LABEL.get(cs.get('symbol'), cs.get('symbol'))}_{cs.get('interval')}_{bias}"
    patterns = _get_all_patterns()
    tf_pat   = patterns.get(tf_key)
    tf_wr    = round(tf_pat["wins"] / tf_pat["count"] * 100, 1) if tf_pat and tf_pat["count"] >= 3 else None

    return {
        "symbol":        cs.get("symbol"),
        "label":         _SYM_LABEL.get(cs.get("symbol"), cs.get("symbol")),
        "interval":      cs.get("interval"),
        "score":         score,
        "bias":          bias,
        "st_dir":        "BULL" if cs.get("last_st_dir") == 1 else "BEAR",
        "ema_cross":     bias if (
                             (bias == "BUY"  and cs.get("last_ema9",0) > cs.get("last_ema13",0) and cs.get("prev_ema9",0) <= cs.get("prev_ema13",1))
                          or (bias == "SELL" and cs.get("last_ema9",0) < cs.get("last_ema13",0) and cs.get("prev_ema9",0) >= cs.get("prev_ema13",0))
                         ) else None,
        "live_signal":   last is not None,
        "live_dir":      last.get("direction") if last else None,
        "live_entry":    last.get("entry") if last else None,
        "live_t2":       last.get("t2") if last else None,
        "live_sl":       last.get("sl") if last else None,
        "atr_pct":       atr_pct,
        "vol_ratio":     round(vol_ratio, 2),
        "bt_wr":         wr,
        "tf_wr":         tf_wr,          # Hermes memory: historical win rate on this TF
        "tf_key":        tf_key,
        "ob_count":      len([o for o in cs.get("bulls", []) if not o.get("breaker")])
                       + len([o for o in cs.get("bears", []) if not o.get("breaker")]),
        "close":         cs.get("last_close"),
    }


def _build_mtf_prompt(scored: list[dict], active_trades: dict) -> str:
    """
    Build the multi-frame comparison prompt.
    `scored` is sorted best-first by score.
    `active_trades` is {symbol: trade_dict} from Flux state.
    """
    patterns = _get_all_patterns()
    win_pats = {k: v for k, v in patterns.items()
                if v["count"] >= 3 and v["wins"] / max(1, v["count"]) >= 0.55
                and k.startswith("TF_")}

    lines = ["MULTI-TIMEFRAME SCAN — all instruments\n" + "=" * 45]
    for f in scored:
        active = active_trades.get(f["symbol"])
        sig_line = (f"  ↳ live {f['live_dir']} entry={f['live_entry']} t2={f['live_t2']} sl={f['live_sl']}"
                    if f["live_signal"] else "  ↳ no live signal")
        tf_mem   = f"tf_hist_WR={f['tf_wr']}%" if f["tf_wr"] is not None else "tf_hist=new"
        bt_mem   = f"bt_WR={f['bt_wr']}%" if f["bt_wr"] is not None else "bt=<3 signals"
        active_s = f"  ⚠ ACTIVE TRADE: {active['direction']} entered@{active['entry_price']}" if active else ""
        lines.append(
            f"\n[{f['label']} {f['interval']}]  score={f['score']}  bias={f['bias']}\n"
            f"  ST={f['st_dir']}  ema_cross={f['ema_cross'] or 'none'}  "
            f"atr%={f['atr_pct']}  vol={f['vol_ratio']}x  valid_OBs={f['ob_count']}\n"
            f"  {bt_mem}  {tf_mem}\n"
            f"{sig_line}{active_s}"
        )

    lines.append(f"\nHERMES TF MEMORY (winning timeframe patterns):\n"
                 f"{json.dumps(win_pats, indent=2) if win_pats else 'none yet'}")

    lines.append("""
TASK: Choose the SINGLE best (symbol, interval) setup to act on right now.
You are a professional options trader — entries are CE/PE on WEEKLY EXPIRIES.
Respond ONLY with this JSON:
{
  "best_symbol": "<^NSEI|NSEBANK|SENSEX>",
  "best_interval": "<10m|5m>",
  "best_label": "<NIFTY|BANKNIFTY|SENSEX>",
  "decision": "<BUY|SELL|WAIT>",
  "confidence": <0-100>,
  "reason": "<2-3 sentence explanation: why this instrument+timeframe is best right now, include DTE context>",
  "tf_ranking": [
    {"label":"...", "interval":"...", "score":..., "why_not":"<one reason skipped>"},
    ...
  ],
  "exit_advice": "<EXIT_NOW|HOLD|TIGHTEN_TRAIL|null>",
  "btst": "<GAP_UP|GAP_DOWN|SKIP|null>"
}

Selection rules (apply in order):
1. HARD BLOCK: DTE=0 (expiry day) — WAIT for ALL indices, EXIT any open position
2. DTE=1: only proceed if score ≥ 80 AND live_signal AND fresh EMA cross
3. Prefer frames with a LIVE signal (live_signal=true) that matches the bias
4. Among live signals, prefer fresh EMA cross (ema_cross not null) + ST aligned
5. Prefer higher atr% (more points available) IF vol_ratio > 0.8
6. Prefer frames where tf_hist_WR >= 55% (Hermes has seen this work before)
7. If an active trade already exists on a symbol, mark it HOLD and skip to next
8. WAIT if no frame scores >= 60 or all live signals conflict with ST direction
9. Remember: SL is on the OPTION PREMIUM (15% trail), not the spot level
""")
    return "\n".join(lines)


def hermes_mtf_scan(frames: list[dict], active_trades: dict = None) -> dict:
    """
    Multi-timeframe scan across all supplied chart_state frames.

    Args:
        frames:        List of chart_state dicts (one per symbol×interval).
        active_trades: {symbol: trade_dict} — passed from Flux _active_trades.

    Returns dict:
        best_symbol, best_interval, best_label, decision, confidence,
        reason, tf_ranking, scores (all frames), exit_advice, btst,
        latency_ms, error (if any)
    """
    if active_trades is None:
        active_trades = {}

    t0 = time.time()

    # 1. Score every frame
    scored = [_score_frame(cs) for cs in frames]
    scored.sort(key=lambda x: x["score"], reverse=True)

    # 2. Python fallback (used when Ollama is down)
    best     = scored[0]
    py_result = {
        "best_symbol":   best["symbol"],
        "best_interval": best["interval"],
        "best_label":    best["label"],
        "decision":      best["bias"] if best["score"] >= CONF_THRESHOLD and best["live_signal"] else "WAIT",
        "confidence":    best["score"],
        "reason":        (f"Python MTF: {best['label']} {best['interval']} scores highest "
                          f"({best['score']}%). "
                          f"{'Live signal present.' if best['live_signal'] else 'No live signal — WAIT.'}"),
        "tf_ranking":    [{"label": f["label"], "interval": f["interval"],
                           "score": f["score"], "why_not": "lower score"} for f in scored[1:]],
        "exit_advice":   None,
        "btst":          None,
        "scores":        scored,
        "ts":            datetime.now(IST).isoformat(),
    }

    # 3. Try LLM
    result = dict(py_result)
    health = check_ollama_health()
    if health.get("hermes_loaded") or health.get("ollama_live"):
        try:
            prompt = _build_mtf_prompt(scored, active_trades)
            raw    = call_hermes3(prompt, system=HERMES_SYSTEM, max_tokens=500)
            raw    = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            parsed = json.loads(raw)

            llm_conf    = max(20, min(95, int(parsed.get("confidence", best["score"]))))
            merged_conf = max(20, min(95, round((best["score"] + llm_conf) / 2)))

            result.update({
                "best_symbol":   parsed.get("best_symbol",   py_result["best_symbol"]),
                "best_interval": parsed.get("best_interval", py_result["best_interval"]),
                "best_label":    parsed.get("best_label",    py_result["best_label"]),
                "decision":      parsed.get("decision",      py_result["decision"]),
                "confidence":    merged_conf,
                "reason":        parsed.get("reason",        py_result["reason"]),
                "tf_ranking":    parsed.get("tf_ranking",    py_result["tf_ranking"]),
                "exit_advice":   parsed.get("exit_advice"),
                "btst":          parsed.get("btst"),
            })
        except json.JSONDecodeError as e:
            result["error"] = f"llm_parse_error: {e}"
        except Exception as e:
            result["error"] = f"llm_error: {type(e).__name__}: {e}"

    result["latency_ms"] = round((time.time() - t0) * 1000)
    result["scores"]     = scored   # always include raw scores for the UI

    # 4. Record winning TF in pattern memory (so Hermes learns over time)
    #    We only record when there's an actionable decision (not WAIT)
    if result["decision"] in ("BUY", "SELL"):
        win_sym   = result["best_symbol"]
        win_ivl   = result["best_interval"]
        win_label = result["best_label"]
        tf_key    = f"TF_{win_label}_{win_ivl}_{result['decision']}"
        # We mark it as "seen" — outcome recorded later via update_decision_outcome
        # For now just increment count; win/loss updated when trade closes
        _upsert_pattern(tf_key, win=False, rr=0.0)   # win=False → will be corrected on exit
        log.info("[HermesMTF] Selected %s %s %s conf=%d%% | %s",
                 win_label, win_ivl, result["decision"],
                 result["confidence"], result["reason"][:80])
    else:
        log.info("[HermesMTF] WAIT — best=%s %s score=%d | %s",
                 best["label"], best["interval"], best["score"], result["reason"][:80])

    with _state_lock:
        _last_output["mtf"] = result

    return result


def update_mtf_outcome(tf_key: str, win: bool, rr: float):
    """
    Called from exit_trade_fn to correct the TF pattern win flag set during scan.
    Fixes the initial win=False placeholder to the real outcome.
    """
    if not tf_key:
        return
    with _db_lock:
        con = _db()
        cur = con.cursor()
        # Correct: subtract the placeholder loss and add real outcome
        cur.execute("""UPDATE patterns SET
            wins = wins + ?,
            total_rr = total_rr + ?,
            updated_at = ?
            WHERE key = ?""",
            (1 if win else 0, rr, datetime.now(IST).isoformat(), tf_key))
        con.commit()
        con.close()


# ════════════════════════════════════════════════════════════════════════════════
# PATTERN LEARNING — auto-learn from backtest signals every cycle
# ════════════════════════════════════════════════════════════════════════════════

def _learn_from_signals(signals: list):
    """
    Scan closed backtest signals, update SQLite pattern memory.
    De-duplicates via seen_signals table — safe to call on every poll.
    """
    if not signals:
        return
    newly_learned = 0
    for sig in signals:
        if not sig.get("exit_reason") or sig["exit_reason"] == "Open":
            continue  # still open — skip
        sig_key = f"{sig.get('direction')}_{sig.get('bar_time')}_{sig.get('entry')}"
        if _is_signal_seen(sig_key):
            continue
        # Learn this signal
        vol_bracket = round((sig.get("ob_vol_pct") or 0) / 2) * 2
        pat_key = f"{sig['direction']}_vol{vol_bracket}"
        win = (sig.get("pts_captured") or 0) > 0
        rr  = sig.get("actual_rr") or 0.0
        _upsert_pattern(pat_key, win, rr)
        _mark_signal_seen(sig_key)
        newly_learned += 1

    if newly_learned:
        log.info("[Hermes] Learned %d new signal(s)", newly_learned)

    # Self-improvement milestone check
    stats = get_brain_stats()
    total = stats["total"]
    if total > 0 and total % SELF_IMPROVE_EVERY == 0:
        _self_improvement_report(stats)


def _self_improvement_report(stats: dict):
    patterns = stats["patterns"]
    entries = [(k, v) for k, v in patterns.items() if v["count"] >= 2]
    entries.sort(key=lambda x: x[1]["wins"] / max(1, x[1]["count"]), reverse=True)
    best  = entries[0] if entries else None
    worst = entries[-1] if len(entries) > 1 else None
    wr = stats["accuracy"]
    msg = f"[HermesSelf] {stats['total']}-trade milestone. Win rate: {wr}%."
    if best:
        bwr = round(best[1]["wins"] / max(1, best[1]["count"]) * 100)
        msg += f" Best: {best[0]} ({bwr}% WR, {best[1]['count']} trades)."
    if worst and worst != best:
        wwr = round(worst[1]["wins"] / max(1, worst[1]["count"]) * 100)
        msg += f" Weakest: {worst[0]} ({wwr}% WR — flag for review)."
    log.info(msg)


# ════════════════════════════════════════════════════════════════════════════════
# CONFIDENCE SCORING — pure Python, same formula as SOUL spec
# ════════════════════════════════════════════════════════════════════════════════

def _compute_confidence(cs: dict) -> tuple[int, str]:
    """
    Returns (score 20-95, direction_bias 'BUY'|'SELL'|'NEUTRAL').
    Mirrors the SOUL spec scoring table exactly.
    """
    score  = 50
    notes  = []

    e9, e13   = cs.get("last_ema9", 0),  cs.get("last_ema13", 0)
    pe9, pe13 = cs.get("prev_ema9", 0),  cs.get("prev_ema13", 0)
    st_dir    = cs.get("last_st_dir", 0)
    last_vol  = cs.get("last_volume", 0)
    avg_vol   = cs.get("avg_volume", 1) or 1
    last_close= cs.get("last_close", 0)
    signals   = cs.get("signals", [])

    bull_bias = e9 > e13
    st_bull   = st_dir == 1

    # Alignment
    if bull_bias == st_bull:
        score += 15; notes.append(f"ST+EMA {'bull' if bull_bias else 'bear'} align +15")
    else:
        score -= 10; notes.append("ST/EMA conflict -10")

    # Fresh EMA cross (within 2 bars)
    cross_up   = (e9 > e13 and pe9 <= pe13)
    cross_down = (e9 < e13 and pe9 >= pe13)
    if cross_up or cross_down:
        score += 12; notes.append(f"EMA cross {'UP' if cross_up else 'DN'} +12")

    # Price at OB
    bulls = cs.get("bulls", []); bears = cs.get("bears", [])
    near_bull = next((o for o in bulls if not o.get("breaker") and
                      o["bottom"] * 0.998 <= last_close <= o["top"] * 1.005), None)
    near_bear = next((o for o in bears if not o.get("breaker") and
                      o["bottom"] * 0.995 <= last_close <= o["top"] * 1.002), None)
    at_ob = (bull_bias and near_bull) or (not bull_bias and near_bear)
    if at_ob:
        score += 10; notes.append("Price at OB +10")
    else:
        score -= 5;  notes.append("No nearby OB -5")

    # Volume spike
    vol_ratio = last_vol / avg_vol
    if vol_ratio > 1.5:
        score += 8; notes.append(f"Vol spike {vol_ratio:.1f}x +8")
    elif vol_ratio < 0.7:
        score -= 5; notes.append("Low vol -5")

    # Signal on last bar
    last_sig = (signals or [None])[-1]
    if last_sig and last_sig.get("exit_reason") in (None, "Open"):
        score += 15; notes.append("Live signal on last bar +15")

    # Pattern win rate
    if last_sig:
        vol_bracket = round((last_sig.get("ob_vol_pct") or 0) / 2) * 2
        pat_key = f"{last_sig['direction']}_vol{vol_bracket}"
        patterns = _get_all_patterns()
        pat = patterns.get(pat_key)
        if pat and pat["count"] >= 2:
            wr = pat["wins"] / pat["count"]
            if wr > 0.65:
                score += 10; notes.append(f"Pattern {pat_key} WR={round(wr*100)}% +10")

    # Backtest win rate
    stats_inp = cs.get("signal_stats", {})
    if stats_inp.get("total", 0) > 3:
        bt_wr = stats_inp.get("wins", 0) / stats_inp["total"]
        if bt_wr > 0.55:
            score += 5; notes.append(f"BT WR={round(bt_wr*100)}% +5")

    score = max(20, min(95, score))
    bias = "BUY" if bull_bias else ("SELL" if not bull_bias else "NEUTRAL")
    return score, bias


# ════════════════════════════════════════════════════════════════════════════════
# MAIN ANALYSIS ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════

def hermes_analyse(chart_state: dict) -> dict:
    """
    Full analysis cycle:
      1. Learn from closed signals (pattern memory update)
      2. Compute confidence score (fast, Python)
      3. Call hermes3 LLM for structured reasoning (if Ollama live)
      4. Return merged result dict

    Returns dict with keys:
      decision, confidence, reason, exit_advice, btst, pattern_note,
      latency_ms, ts, error (if any)
    """
    global _last_output

    t0 = time.time()

    # 1. Learn
    _learn_from_signals(chart_state.get("signals", []))

    # 2. Python confidence (always available, fast)
    conf_py, bias = _compute_confidence(chart_state)

    # 3. Try LLM
    result = {
        "decision": bias if conf_py >= CONF_THRESHOLD else "WAIT",
        "confidence": conf_py,
        "reason": f"Python scoring only. Bias={bias} conf={conf_py}%.",
        "exit_advice": None,
        "btst": None,
        "pattern_note": None,
        "ts": datetime.now(IST).isoformat(),
        "symbol": chart_state.get("symbol"),
        "interval": chart_state.get("interval"),
    }

    health = check_ollama_health()
    if health.get("hermes_loaded") or health.get("ollama_live"):
        try:
            prompt = build_market_prompt(chart_state)
            raw = call_hermes3(prompt, system=HERMES_SYSTEM, max_tokens=350)

            # ── Normalise raw text → JSON string ─────────────────────────────
            raw = raw.strip()

            # Strip ```json ... ``` markdown fences if model ignored format hint
            if raw.startswith("```"):
                parts = raw.split("```")
                # parts[1] contains the fenced content (possibly "json\n{...}")
                raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw

            # If model returned prose before the JSON, extract first {...} block
            brace_start = raw.find("{")
            brace_end   = raw.rfind("}")
            if brace_start != -1 and brace_end > brace_start:
                raw = raw[brace_start:brace_end + 1]

            # Final guard: still empty after all stripping?
            if not raw:
                raise ValueError("Empty JSON after stripping prose/fences")

            parsed = json.loads(raw)

            # Validate mandatory field
            if "decision" not in parsed:
                raise ValueError(f"LLM JSON missing 'decision' key: {parsed}")

            # ── Merge LLM into result ────────────────────────────────────────
            llm_conf    = max(20, min(95, int(parsed.get("confidence", conf_py))))
            merged_conf = max(20, min(95, round((conf_py + llm_conf) / 2)))
            result.update({
                "decision":     parsed.get("decision", result["decision"]),
                "confidence":   merged_conf,
                "reason":       parsed.get("reason", result["reason"]),
                "exit_advice":  parsed.get("exit_advice"),
                "btst":         parsed.get("btst"),
                "pattern_note": parsed.get("pattern_note"),
            })
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("[Hermes] LLM parse error: %s | raw=%r",
                        e, (raw or "")[:120])
            result["reason"] = f"LLM parse error — {e}. Python fallback: conf={conf_py}%."
            result["error"]  = "llm_parse_error"
        except requests.exceptions.Timeout:
            log.warning("[Hermes] LLM timeout after %ds", LLM_TIMEOUT)
            result["reason"] = f"LLM timeout. Python fallback: conf={conf_py}%."
            result["error"]  = "llm_timeout"
        except Exception as e:
            log.warning("[Hermes] LLM error (%s): %s", type(e).__name__, e)
            result["reason"] = f"LLM unavailable ({type(e).__name__}). Python fallback active."
            result["error"]  = "llm_error"

    result["latency_ms"] = round((time.time() - t0) * 1000)

    # 4. Persist decision
    rowid = _log_decision(result)
    result["decision_id"] = rowid

    with _state_lock:
        _last_output = dict(result)

    log.info("[Hermes] %s %s conf=%d%% lat=%dms | %s",
             chart_state.get("symbol"), result["decision"],
             result["confidence"], result["latency_ms"],
             result["reason"][:60])

    return result


def get_last_output() -> dict:
    with _state_lock:
        return dict(_last_output)


# ════════════════════════════════════════════════════════════════════════════════
# MASTER DOWNLOAD — 9 AM daily trigger
# ════════════════════════════════════════════════════════════════════════════════

def should_download_master() -> bool:
    """True if master hasn't been downloaded today."""
    today = date.today().isoformat()
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("SELECT status FROM master_log WHERE dl_date=?", (today,))
        row = cur.fetchone()
        con.close()
    return row is None or row[0] != "ok"


def record_master_download(status: str = "ok"):
    today = date.today().isoformat()
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("INSERT OR REPLACE INTO master_log(dl_date,status,ts) VALUES(?,?,?)",
                    (today, status, datetime.now(IST).isoformat()))
        con.commit()
        con.close()
    log.info("[Hermes] Master download recorded: %s", status)


def validate_master_instruments(kite, symbols: list) -> dict:
    """
    Validate each symbol: lot_size, token, tradingsymbol, expiry.
    Returns {symbol: {valid, lot_size, token, error}}
    """
    results = {}
    try:
        nfo = kite.instruments("NFO")
        bfo = kite.instruments("BFO")
        nse = kite.instruments("NSE")
        bse = kite.instruments("BSE")
    except Exception as e:
        log.error("[Hermes] Master instrument fetch failed: %s", e)
        return {}

    for sym in symbols:
        ex = "NFO"; insts = nfo
        if "SENSEX" in sym.upper():
            ex = "BFO"; insts = bfo
        elif "NSE" in sym.upper():
            ex = "NSE"; insts = nse

        match = next((i for i in insts if sym in i.get("tradingsymbol", "")), None)
        if match:
            results[sym] = {
                "valid": True,
                "lot_size": match.get("lot_size", 0),
                "token": match.get("instrument_token"),
                "tradingsymbol": match.get("tradingsymbol"),
                "expiry": str(match.get("expiry", "")),
            }
        else:
            results[sym] = {"valid": False, "error": "not found in master"}
    return results


# ════════════════════════════════════════════════════════════════════════════════
# FUTURES VOLUME — fetch futures OI/volume for OB strength cross-check
# ════════════════════════════════════════════════════════════════════════════════

# Map index → nearest futures tradingsymbol prefix
FUTURES_PREFIX = {
    "NIFTY":     "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "SENSEX":    "SENSEX",
}

def resolve_fut_token(index_key: str, kite=None) -> dict:
    """
    Find the nearest-expiry futures token for the given index.
    Returns {token, tradingsymbol, expiry, lot_size} or {}
    """
    if kite is None:
        return {}
    prefix = FUTURES_PREFIX.get(index_key.upper(), index_key.upper())
    today  = date.today()

    try:
        exchange = "BFO" if "SENSEX" in prefix.upper() else "NFO"
        insts = kite.instruments(exchange)
    except Exception as e:
        log.debug("Futures token lookup failed: %s", e)
        return {}

    cands = [i for i in insts
             if i.get("instrument_type") in ("FUT",)
             and i.get("name", "").upper() == prefix.upper()
             and i.get("expiry") and i["expiry"] >= today]
    if not cands:
        return {}
    cands.sort(key=lambda x: x["expiry"])
    f = cands[0]
    return {
        "token":         int(f["instrument_token"]),
        "tradingsymbol": f["tradingsymbol"],
        "expiry":        str(f["expiry"]),
        "lot_size":      f.get("lot_size", 0),
    }


def fetch_futures_volume(token: int, kite, interval: str = "3minute") -> dict:
    """
    Fetch last 5 bars of futures and return avg volume + last volume.
    Used to cross-validate OB vol % with futures activity.
    """
    if not kite or not token:
        return {}
    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    fdt = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    try:
        raw = kite.historical_data(token, fdt, now, interval, False, False)
        if not raw:
            return {}
        vols = [r["volume"] for r in raw[-5:] if r.get("volume") is not None]
        total_session_vol = sum(r["volume"] for r in raw if r.get("volume"))
        if not vols:
            return {}
        return {
            "last_vol": vols[-1],
            "avg5_vol": round(sum(vols) / len(vols)),
            "session_vol": total_session_vol,
        }
    except Exception as e:
        log.debug("Futures volume fetch failed: %s", e)
        return {}


def enrich_obs_with_futures_vol(obs: list, fut_session_vol: int) -> list:
    """
    Add fut_vol_pct to each OB dict based on futures session volume.
    This gives a cross-market confirmation of OB strength.
    """
    if not fut_session_vol or fut_session_vol == 0:
        return obs
    for ob in obs:
        ob_vol = ob.get("obVolume", 0)
        ob["fut_vol_pct"] = round(ob_vol / fut_session_vol * 100, 2) if ob_vol else 0
    return obs


# ════════════════════════════════════════════════════════════════════════════════
# AUTO-INVEST — 10% of daily profit into stocks
# ════════════════════════════════════════════════════════════════════════════════

def record_daily_profit(pnl: float, trade_count: int):
    """Called once at end of session with the day's realised P&L."""
    today = date.today().isoformat()
    invest = round(max(0, pnl * 0.10), 2)   # 10% of profit only
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("""INSERT OR REPLACE INTO daily_stats(trade_date, total_pnl, invest_amt, trades)
                       VALUES(?,?,?,?)""", (today, round(pnl, 2), invest, trade_count))
        con.commit()
        con.close()
    log.info("[Hermes] Day P&L=%.2f | Auto-invest 10%%=%.2f pending.", pnl, invest)
    return invest


def add_to_portfolio(symbol: str, qty: float, price: float):
    """Record an auto-invest purchase into the portfolio table."""
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("SELECT qty, avg_price, invested FROM portfolio WHERE symbol=?", (symbol,))
        row = cur.fetchone()
        if row:
            old_qty, old_avg, old_invested = row
            new_qty = old_qty + qty
            new_invested = old_invested + qty * price
            new_avg = new_invested / new_qty if new_qty else price
            cur.execute("""UPDATE portfolio SET qty=?, avg_price=?, invested=?, last_updated=?
                           WHERE symbol=?""",
                        (new_qty, round(new_avg, 2), round(new_invested, 2),
                         datetime.now().isoformat(), symbol))
        else:
            cur.execute("""INSERT INTO portfolio(symbol, qty, avg_price, invested, last_updated)
                           VALUES(?,?,?,?,?)""",
                        (symbol, qty, price, round(qty * price, 2), datetime.now().isoformat()))
        con.commit()
        con.close()
    log.info("[Hermes] Portfolio += %s x%.2f @ %.2f", symbol, qty, price)


def get_portfolio() -> list:
    with _db_lock:
        con = _db()
        cur = con.cursor()
        cur.execute("SELECT symbol, qty, avg_price, invested, last_updated FROM portfolio")
        rows = cur.fetchall()
        con.close()
    return [{"symbol":r[0],"qty":r[1],"avg_price":r[2],"invested":r[3],"last_updated":r[4]}
            for r in rows]


# ════════════════════════════════════════════════════════════════════════════════
# LIVE WEBSOCKET FEED STUB — Kite WebSocket ticker
# ════════════════════════════════════════════════════════════════════════════════

_live_ticks: dict = {}   # {token: last_tick_dict}
_ticker_obj = None

def start_live_feed(kite, tokens: list):
    """
    Start KiteTicker websocket for live price feed.
    Stores ticks in _live_ticks dict keyed by instrument token.

    Usage in Flux_913ema.py:
        from hermes_brain import start_live_feed, get_live_tick
        start_live_feed(kite, [256265, 260105, ...])
    """
    global _ticker_obj
    try:
        from kiteconnect import KiteTicker
        kt = KiteTicker(kite.api_key, kite.access_token)

        def on_ticks(ws, ticks):
            for t in ticks:
                _live_ticks[t["instrument_token"]] = t

        def on_connect(ws, response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_QUOTE, tokens)
            log.info("[Hermes LiveFeed] Subscribed to %d tokens", len(tokens))

        def on_error(ws, code, reason):
            log.error("[Hermes LiveFeed] Error %s: %s", code, reason)

        def on_close(ws, code, reason):
            log.warning("[Hermes LiveFeed] Closed %s: %s", code, reason)

        kt.on_ticks   = on_ticks
        kt.on_connect = on_connect
        kt.on_error   = on_error
        kt.on_close   = on_close
        _ticker_obj = kt
        threading.Thread(target=kt.connect, kwargs={"threaded": True}, daemon=True).start()
        log.info("[Hermes LiveFeed] KiteTicker started for tokens: %s", tokens)
    except ImportError:
        log.warning("[Hermes LiveFeed] KiteTicker not available — install kiteconnect")
    except Exception as e:
        log.error("[Hermes LiveFeed] Failed to start: %s", e)


def get_live_tick(token: int) -> Optional[dict]:
    return _live_ticks.get(token)


def stop_live_feed():
    global _ticker_obj
    if _ticker_obj:
        try:
            _ticker_obj.close()
        except Exception:
            pass
        _ticker_obj = None


# ════════════════════════════════════════════════════════════════════════════════
# MARKET HOURS & EXPIRY HELPERS
# ════════════════════════════════════════════════════════════════════════════════

def is_market_hours() -> bool:
    now = datetime.now(IST)
    hm  = now.hour * 60 + now.minute
    # NSE: 9:15 AM to 3:20 PM IST
    return 555 <= hm < 920 and now.weekday() < 5


def is_btst_window() -> bool:
    now = datetime.now(IST)
    hm  = now.hour * 60 + now.minute
    return hm >= 900 and now.weekday() < 5   # after 3:00 PM


# ════════════════════════════════════════════════════════════════════════════════
# AUTO-TRADE HELPERS (called from Flux_913ema.py routes)
# ════════════════════════════════════════════════════════════════════════════════

_auto_cfg: dict = {"enabled": False, "min_confidence": CONF_THRESHOLD,
                   "product": "MIS", "order_type": "MARKET", "max_lots": 1}
_auto_lock = threading.Lock()
_auto_log_lines: list = []


def _auto_log(msg: str):
    ts = datetime.now(IST).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _auto_log_lines.insert(0, line)
    if len(_auto_log_lines) > 200:
        _auto_log_lines.pop()
    log.info(msg)


def set_auto_cfg(**kwargs):
    with _auto_lock:
        _auto_cfg.update(kwargs)


def get_auto_cfg() -> dict:
    with _auto_lock:
        return dict(_auto_cfg)


def get_auto_log(limit: int = 50) -> list:
    return _auto_log_lines[:limit]


# ════════════════════════════════════════════════════════════════════════════════
# STARTUP SEQUENCE (called from Flux_913ema.py __main__)
# ════════════════════════════════════════════════════════════════════════════════

def startup(kite=None):
    """
    Full startup:
      1. Init DB
      2. Warmup LLM
      3. If 9 AM window, trigger master download validation
    """
    init_brain_db()
    threading.Thread(target=warmup, daemon=True).start()

    now = datetime.now(IST)
    if 9 <= now.hour < 10 and should_download_master():
        log.info("[Hermes] 9 AM window — triggering master validation.")
        if kite:
            symbols_to_check = ["NIFTY", "BANKNIFTY", "SENSEX"]
            results = validate_master_instruments(kite, symbols_to_check)
            for sym, info in results.items():
                if info.get("valid"):
                    log.info("[Master] ✓ %s | lot=%s token=%s expiry=%s",
                             sym, info["lot_size"], info["token"], info["expiry"])
                else:
                    log.warning("[Master] ✗ %s — %s", sym, info.get("error"))
        record_master_download("ok")