# OB Flux — Autonomous Trading System: Project Context

> Feed this file to any new Claude session before asking for help.  
> It gives Claude the full picture of what is being built, why, and what has already been done.

---

## Who I Am and What I Am Building

I am building a **fully autonomous Indian equity derivatives trading system** that trades NIFTY 50, BANKNIFTY, and SENSEX index futures and options through the **Zerodha Kite API**. The system is called **OB Flux** and it is designed to eventually trade on its own — detecting setups, placing orders, managing trades, and learning from outcomes without me intervening on every trade.

The long-term vision is that an AI agent called **Hermes** (embedded in the dashboard) watches the charts continuously, understands the strategy deeply, makes trade decisions, and improves itself over time by learning from what worked and what did not.

---

## The Core Trading Strategy

The strategy is called **Triple-Confirmation Order Block (OB) Strategy** with **9/13 EMA crossover** as the entry trigger. Every entry needs three things to align:

### 1. Order Block (OB) Detection
- An **Order Block** is a specific supply/demand zone on the chart where institutional activity caused a significant move.
- **Bullish OB**: A down-candle zone (supply zone) that price broke above — now acts as support when price returns to it.
- **Bearish OB**: An up-candle zone (demand zone) that price broke below — now acts as resistance when price returns to it.
- OB strength is measured by **volume** — the percentage of total session volume that traded during the OB formation. Higher volume % = stronger OB.
- OBs can become **breaker blocks** if price re-enters them from the other side (invalidated).
- The code detects OBs using swing high/low logic and ATR filtering (OBs wider than 3.5× ATR are discarded as noise).

### 2. Liquidity Sweep Before Entry
- Before a valid entry, price must have **swept liquidity** — meaning it briefly dipped below a swing low (for buys) or above a swing high (for sells), triggering stop losses of retail traders, before reversing.
- This sweep is implicit in the OB detection: price must break the swing that created the OB, confirming that liquidity has been hunted.

### 3. EMA 9/13 Crossover
- **Bullish entry**: EMA9 crosses above EMA13 while price is at or above a Bullish OB, and SuperTrend is in bull mode (direction = +1).
- **Bearish entry**: EMA9 crosses below EMA13 while price is at or below a Bearish OB, and SuperTrend is in bear mode (direction = -1).
- The crossover is checked with a 2-bar lookback to catch slightly-delayed crosses.

### 4. SuperTrend Filter
- Period: 10, Multiplier: 3.0
- Acts as a trend filter — only take bull signals when SuperTrend is green (bull), only take bear signals when SuperTrend is red (bear).
- This prevents trading against the prevailing trend.

---

## Entry Rules (All Three Must Be True)

For a **BUY (CE) entry**:
1. Price is touching or inside a **Bullish OB** (not a breaker block)
2. **EMA9 crosses above EMA13** (current or within 2 bars)
3. **SuperTrend direction = +1** (bullish)
4. Close is above the OB top (confirmation of break)

For a **SELL (PE) entry**:
1. Price is touching or inside a **Bearish OB** (not a breaker block)
2. **EMA9 crosses below EMA13** (current or within 2 bars)
3. **SuperTrend direction = -1** (bearish)
4. Close is below the OB bottom (confirmation of break)

**One signal per OB** — once an OB fires a signal, it is locked (added to `fired` set) and cannot fire again.

---

## Target and Stop Loss Structure

On every signal, three targets are set dynamically:

| Level | Calculation |
|-------|-------------|
| **SL** | Lowest low of current + previous bar (BUY) / Highest high of current + previous bar (SELL) |
| **Risk (R)** | Entry price − SL (absolute points) |
| **T1** | Entry + 1×R (first profit take, 1:1 RR) |
| **T2** | Bottom of nearest Bearish OB above price (BUY) / Top of nearest Bullish OB below price (SELL). Falls back to 2×R if no OB found. |
| **T3** | Second nearest opposite OB / Falls back to 3×R |

The system tracks **RR2** (risk-reward to T2) and **RR3** (risk-reward to T3).

---

## Exit Rules (Four Simultaneous Watchers)

Once in a trade, four exit conditions are monitored **on the option chart**, not just the index:

| Exit | Trigger |
|------|---------|
| **E1 — RSI Exit** | Option RSI ≥ 88 (overbought, bull exit) or ≤ 12 (oversold, bear exit) |
| **E2 — T2 Target** | Index price reaches next opposite OB (T2 level) |
| **E3 — T3 Target** | Index price reaches second opposite OB (T3 level) |
| **E4 — Trail Stop** | Option premium drops 15% from its peak (peak × 0.85) |
| **E5 — Reverse Cross** | EMA9/13 crosses in the opposite direction (reversal signal) — exit immediately |

Exit E5 (reverse crossover) is not yet fully automated in the current code — it needs to be wired to trigger `exit_trade_fn()` when detected.

---

## Position Sizing and Options

- When a signal fires, the system looks up the **ATM (At-The-Money) option** for the active index.
- It selects the nearest expiry CE (for buys) or PE (for sells) at the ATM strike.
- Trade is placed as a **MIS (intraday)** market order on Zerodha.
- Lot sizes:  NIFTY = 65, BANKNIFTY = 30, SENSEX = 20
- Strike gaps:  NIFTY = 50pts, BANKNIFTY = 100pts, SENSEX = 100pts

---

## BTST (Buy Today Sell Tomorrow) Logic

When the session is closing:
- If price closes **near the High of Day** (top 25% of session range) with bullish bias → **Gap Up expected** → Buy CE overnight, exit at first candle of next session's open.
- If price closes **near the Low of Day** (bottom 25% of session range) with bearish bias → **Gap Down expected** → Buy PE overnight, exit at first candle of next session's open.
- BTST is only triggered when Hermes confidence ≥ 65%.
- Exit rule is strict: **market order at 9:15 AM candle close of next session**.

---

## ATR-Based Trailing (Planned Enhancement)

The current code uses a flat 15% trail on option premium. The planned upgrade is:
- Calculate ATR on the index chart.
- As the trend extends, widen the trail proportionally to ATR (so strong trends are not stopped out prematurely).
- Only tighten the trail when RSI approaches 88 (for buys) — signal that momentum is exhausting.

---

## Instruments Tracked

| Display Name | Kite Symbol | Exchange |
|---|---|---|
| NIFTY 50 | NIFTY 50 | NSE |
| BANKNIFTY | NIFTY BANK | NSE |
| SENSEX | SENSEX | BSE |

Volume data is fetched from the **Kite historical data API** for each instrument and used to score OB strength.

---

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Python 3.11 + Flask |
| Broker API | Zerodha KiteConnect |
| Session auth | `Auto_toptp_Engine.initialize_kite_session()` |
| Chart rendering | Plotly.js (candlestick + overlays) |
| Frontend | Single HTML file (no React, no build step) |
| Indicators | Pure NumPy/Pandas (no TA-Lib) |
| Data | Kite historical API, stitched in two calls for 30-day lookback |
| Deployment | Flask dev server, local machine |

---

## Files in the Project

| File | Purpose |
|---|---|
| `Flux_913ema.py` | Main Flask backend — all indicators, OB detection, signal engine, order placement, API endpoints |
| `templates/index.html` | Single-page dashboard — Plotly chart, left panel, Hermes AI, replay mode |
| `Auto_toptp_Engine.py` | Kite session manager (not shown — handles token/login) |

---

## Backend API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/data` | GET | Main data: OHLCV + OBs + signals + active trade |
| `/replay_data` | GET | Same as /data, used in replay mode |
| `/option_data` | GET | Option OHLC + RSI + trail SL line for active trade |
| `/trade` | POST | Place a new trade (signal → option order) |
| `/exit_trade` | POST | Manual exit of active trade |
| `/emergency_exit` | POST | Exit ALL active trades (requires confirmation string) |
| `/set_day_targets` | POST | Set daily profit/loss/trade count limits |
| `/day_status` | GET | Current day P&L, trade count, halt status |
| `/account` | GET | Margin, spot price, suggested lots |
| `/tradelog` | GET | Today's orders + positions + internal exit history |

---

## Dashboard Features

- **Index quick-select**: NIFTY 50 / BANKNIFTY / SENSEX buttons in top bar — no typing needed
- **Left panel** (all data on left): Account info, Hermes AI, backtest stats, signal card, active trade card, day targets, recent exits, live orders, emergency exit
- **Main chart**: Candlestick + Volume + EMA9/13 + SuperTrend + OB zones + signal markers + T1/T2/T3 lines
- **Option sub-chart**: Option OHLC + Trail SL line + RSI chart (shown on demand)
- **Replay mode**: Scrub through 30 days of data bar by bar — keyboard arrows, play/pause, speed control
- **OHLC info bar**: Shows OHLC, delta, EMA values, ST direction, volume for hovered candle
- **Hermes AI panel**: At top of left panel — see HERMES_SOUL.md for full Hermes spec

---

## What Is Working (as of latest version)

- OB detection with volume scoring
- Triple-confirmation signal generation
- Backtest walk-forward with RSI/SL/T2/T3 exits
- Option ATM lookup and order placement via Kite
- 15% trailing stop on option premium
- RSI ≥ 88 exit on option chart
- Day P&L tracking, halt on target/loss breach
- Replay mode with bar-by-bar playback
- Index quick-select buttons (NIFTY/BANKNIFTY/SENSEX)
- Hermes AI analysis panel with confidence scoring and BTST prediction

---

## Known Issues / Pending Work

1. **Reverse crossover exit (E5)** not yet wired to auto-exit — Hermes detects it but does not call `/exit_trade` automatically on a live cross.
2. **ATR-based trailing** — currently flat 15% on option premium; needs ATR-proportional widening.
3. **Multi-index simultaneous monitoring** — currently one index at a time on the chart; backend supports multiple active trades but UI only shows one.
4. **Hermes pattern memory is session-only** — not persisted to disk between Flask restarts. Needs a JSON/SQLite persistence layer.
5. **BTST exit automation** — BTST prediction shows in UI but does not place the overnight order automatically yet.
6. **Gap analysis using daily chart** — BTST prediction currently uses intraday candle position; should cross-check against daily chart gap levels.

---

## How to Continue Work in a New Session

1. Paste this entire file at the start of your message.
2. Attach `Flux_913ema.py` and `index.html` (or just paste relevant sections).
3. State specifically what you want to change or add.
4. Claude will have full context of the architecture, strategy, and current state.

If you also want Hermes AI context (its personality, duties, and learning behaviour), additionally provide `HERMES_SOUL.md`.
