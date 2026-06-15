import os,csv
import sys
import json
import time
import webbrowser
import traceback
from math import floor, ceil
from zoneinfo import ZoneInfo
from click import option
import pandas as pd
import pandas_ta as ta
from ip_update_windows import *
from datetime import datetime, timedelta, date
from kiteconnect import KiteConnect
from Auto_toptp_Engine import initialize_kite_session

LOG_DIR  = r"C:\Code\KiteConnect\Trade_logs_All_Bots"
LOG_FILE = os.path.join(LOG_DIR, "NIFTY_ST_trades.csv")
HEADERS = [
    "date", "entry_time", "exit_time",
    "symbol", "option_type", "strike",
    "entry_price", "exit_price",
    "qty", "pnl", "pnl_pct",
    "entry_supertrend", "entry_st_direction",
    "exit_reason",                      # SIGNAL / EOD / MANUAL
    "regime_trend",                      # trend direction at entry: BULLISH / BEARISH
    "order_id_entry", "order_id_exit"
]


BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE = os.path.join(BASE_DIR, "instruments_master.csv")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TOKEN_FILE  = os.path.join(BASE_DIR, "access_token.json")

INDEX = "NIFTY"
INDEX_QUOTE = "NSE:NIFTY 50"
TIMEFRAME = "5minute"

start_time = datetime.strptime("09:30:00", "%H:%M:%S").time()
end_time = datetime.strptime("23:30:00", "%H:%M:%S").time()
exit_time = datetime.strptime("15:15:00", "%H:%M:%S").time()

ST_LENGTH = 7
ST_MULTIPLIER = 2.1
INDEX_TOKENS = {"NIFTY": 256265}
IST = ZoneInfo("Asia/Kolkata")

Buy = False
Sell = False

USE_RETEST = True                  # require price to pull back and touch the ST line before entering
pending_direction = None           # "BULLISH"/"BEARISH" -- flip seen, waiting for retest
retest_confirmed_direction = None  # retest seen on the previous bar; enter now if regime holds
last_processed_bar_ts = None       # timestamp of the last iloc[-2] bar already reacted to

try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    COLOR_ENABLED = True
except Exception:
    COLOR_ENABLED = False

    class Fore:
        RED = ""
        GREEN = ""
        YELLOW = ""
        BLUE = ""
        CYAN = ""
        MAGENTA = ""
        WHITE = ""

    class Style:
        RESET_ALL = ""


# =========================================================
# LOGGING
# =========================================================
def init_log():
    """Creates the log file with headers if it doesn't exist."""
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            writer.writeheader()
        print(f"   ✓ Trade log created: {LOG_FILE}")
    else:
        print(f"   ✓ Trade log found  : {LOG_FILE}")


# Holds the open trade state between entry and exit
_open_trade = {}

def log_entry(symbol, option_type, strike, entry_price, qty,
              df, order_id=""):
    """
    Call this immediately after a successful buy_order().
    Stores entry details in memory until log_exit() is called.
    """
    now = datetime.now(IST)
    _open_trade.clear()
    _open_trade.update({
        "date"               : now.strftime("%Y-%m-%d"),
        "entry_time"         : now.strftime("%H:%M:%S"),
        "symbol"             : symbol,
        "option_type"        : option_type,          # "CE" or "PE"
        "strike"             : strike,
        "entry_price"        : entry_price,
        "qty"                : qty,
        "entry_supertrend"   : round(df["SUPERT"].iloc[-2], 4),
        "entry_st_direction" : int(df["SUPERTd"].iloc[-2]),
        "regime_trend"       : "BULLISH" if df["SUPERTd"].iloc[-2] == 1 else "BEARISH",
        "order_id_entry"     : order_id,
    })
    print(f"   ✓ Entry logged: {symbol} @ {entry_price}")


def log_exit(exit_price, exit_reason="SIGNAL", order_id=""):
    """
    Call this immediately after a successful sell_order().
    Writes the complete trade row to CSV.
    exit_reason: 'SIGNAL' | 'EOD' | 'MANUAL'
    """
    if not _open_trade:
        print("   ⚠ log_exit called but no open trade in memory")
        return

    now        = datetime.now(IST)
    entry_px   = _open_trade["entry_price"]
    qty        = _open_trade["qty"]
    pnl        = round((exit_price - entry_px) * qty, 2)
    pnl_pct    = round((exit_price - entry_px) / entry_px * 100, 2)

    row = {**_open_trade,
           "exit_time"     : now.strftime("%H:%M:%S"),
           "exit_price"    : exit_price,
           "pnl"           : pnl,
           "pnl_pct"       : pnl_pct,
           "exit_reason"   : exit_reason,
           "order_id_exit" : order_id,
    }

    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writerow(row)

    color = "\033[92m" if pnl >= 0 else "\033[91m"
    reset = "\033[0m"
    print(f"   {color}✓ Trade logged | PnL: {pnl:+.2f} ({pnl_pct:+.2f}%){reset}")
    _open_trade.clear()

# =========================================================
# PRINT HELPERS
# =========================================================

def cprint(text, color=Fore.WHITE):
    print(f"{color}{text}{Style.RESET_ALL}")


def print_separator():
    cprint("\n" + "=" * 70, Fore.WHITE)


# =========================================================
# CONFIG
# =========================================================

def load_config():
    default = {
        "api_key":     "YOUR_API_KEY",
        "api_secret":  "YOUR_API_SECRET",
        "user_id":     "YOUR_USER_ID",
        "password":    "YOUR_PASSWORD",
        "totp_secret": "YOUR_TOTP_SECRET"
    }

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        cprint("config.json created", Fore.YELLOW)
        cprint("Fill api_key & api_secret and run again", Fore.YELLOW)
        sys.exit()

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    return cfg


# =========================================================
# AUTHENTICATION
# =========================================================

def get_kite(cfg):
    """
    Fully automated login using Auto_toptp_Engine.
    initialize_kite_session() reads the same config.json,
    handles Selenium TOTP login, caches the token, and
    returns a ready-to-use KiteConnect instance.
    """
    kite = initialize_kite_session()
    profile = kite.profile()
    cprint(f"Logged In: {profile['user_name']}", Fore.GREEN)
    return kite

# =========================================================
# MASTER DOWNLOAD / LOAD
# =========================================================

def get_master(kite, force_refresh=False):
    if force_refresh or not os.path.exists(MASTER_FILE):
        cprint("Downloading instruments master...", Fore.CYAN)
        df = pd.DataFrame(kite.instruments())
        df.columns = [c.lower().strip() for c in df.columns]
        df.to_csv(MASTER_FILE, index=False)
    else:
        df = pd.read_csv(MASTER_FILE, low_memory=False)
        df.columns = [c.lower().strip() for c in df.columns]

    if "expiry" in df.columns:
        df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce").dt.date

    if "strike" in df.columns:
        df["strike"] = pd.to_numeric(df["strike"], errors="coerce")

    if "lot_size" in df.columns:
        df["lot_size"] = pd.to_numeric(df["lot_size"], errors="coerce")

    return df

def next_futures_expiry(master, index="NIFTY"):
    """
    Returns the nearest upcoming futures expiry date for the index.
    Looks in NFO segment for FUT instrument type.
    """
    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].eq("FUT") &
        master["segment"].astype(str).str.upper().eq("NFO-FUT")
    ].copy()

    sub["expiry"] = pd.to_datetime(sub["expiry"], errors="coerce").dt.date
    future = sub[sub["expiry"] >= date.today()]["expiry"].dropna().sort_values().unique()

    if len(future) == 0:
        raise Exception(f"No futures expiry found for {index} in master")

    exp = future[0]
    print(f"   Next futures expiry [{index}]: {exp}")
    return exp

def get_futures_token(master, index="NIFTY"):
    """
    Returns (tradingsymbol, instrument_token) for the front-month
    NIFTY futures contract from NFO.
    """
    exp = next_futures_expiry(master, index)

    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].eq("FUT") &
        master["segment"].astype(str).str.upper().eq("NFO-FUT") &
        (pd.to_datetime(master["expiry"], errors="coerce").dt.date == exp)
    ].copy()

    if sub.empty:
        raise Exception(f"Futures contract not found for {index} expiry {exp}")

    row = sub.iloc[0]
    symbol = str(row["tradingsymbol"])
    token  = int(row["instrument_token"])
    print(f"   Futures symbol [{index}]: {symbol} | Token: {token}")
    return symbol, token

def fetch_futures_ohlc(kite, index="NIFTY", interval="5minute", days=15):
    """
    Fetches OHLCV from the front-month futures contract.
    This gives real volume — needed for VWAP calculation.
    Use this df for calculate_indicators() instead of spot df.
    Prices track spot very closely (basis < 0.1% intraday).
    """
    _, token = get_futures_token(master, index)

    to_dt = datetime.now(IST)
    fr_dt = to_dt - timedelta(days=days)

    rows = kite.historical_data(
        token,
        fr_dt.strftime("%Y-%m-%d %H:%M:%S"),
        to_dt.strftime("%Y-%m-%d %H:%M:%S"),
        interval, False, False
    )

    if not rows:
        raise Exception(f"No futures OHLCV returned for {index}")

    df = pd.DataFrame(rows).rename(columns={"date": "dt"})
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.set_index("dt").sort_index()

    print(f"   ✓ Futures OHLCV [{index}]: {len(df)} x {interval} "
          f"| Close: {df['close'].iloc[-1]:.2f} "
          f"@ {df.index[-1].strftime('%H:%M')} "
          f"| Volume: {df['volume'].iloc[-1]}")
    return df

def get_spot(kite, index):
    try:
        q = kite.quote([INDEX_QUOTE])
        return float(q[INDEX_QUOTE]["last_price"])
    except Exception as e:
        print(f"   ✗ Spot fetch error: {e}")
        return 0.0

def next_expiry(master, index):
    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].isin(["CE", "PE"])
    ].copy()
    sub["expiry"] = pd.to_datetime(sub["expiry"], errors="coerce").dt.date
    future = sub[sub["expiry"] >= date.today()]["expiry"].dropna().sort_values().unique()
    exp = future[0]
    print(f"   Next expiry [{index}]: {exp}")
    return exp

def lot_size(master, index):
    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].isin(["CE", "PE"])
    ]
    lot = int(sub.iloc[0]["lot_size"])
    print(f"   Lot size    [{index}]: {lot}")
    return lot

def atm_strike(spot, index):
    # NIFTY: 50-point strikes | BANKNIFTY & SENSEX: 100-point strikes
    step = {"BANKNIFTY": 100, "SENSEX": 100}.get(index.upper(), 50)
    return round(spot / step) * step

def itm_strikes(spot, index):
    """
    Returns (CE_strike, PE_strike) one strike ITM on each side of spot:
      CE_strike = nearest strike at/below spot -> ITM call
      PE_strike = nearest strike at/above spot -> ITM put
    """
    step = {"BANKNIFTY": 100, "SENSEX": 100}.get(index.upper(), 50)
    ce_strike = floor(spot / step) * step
    pe_strike = ceil(spot / step) * step
    if ce_strike == pe_strike:
        pe_strike += step
    return ce_strike, pe_strike

def find_option_symbol(master, index, expiry, strike, opt_type):
    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].eq(opt_type.upper()) &
        (pd.to_datetime(master["expiry"], errors="coerce").dt.date == expiry)
    ].copy()
    sub["strike"] = pd.to_numeric(sub["strike"], errors="coerce")
    sub["diff"]   = (sub["strike"] - strike).abs()
    row = sub.nsmallest(1, "diff").iloc[0]
    return str(row["tradingsymbol"]), int(row["instrument_token"])

def calculate_indicators(df):
    st = ta.supertrend(df["high"], df["low"], df["close"], length=ST_LENGTH, multiplier=ST_MULTIPLIER)
    suffix = f"{ST_LENGTH}_{float(ST_MULTIPLIER)}"
    df["SUPERT"] = st[f"SUPERT_{suffix}"]
    df["SUPERTd"] = st[f"SUPERTd_{suffix}"]
    return df

def find_option_symbol(master, index, expiry, strike, opt_type):
    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].eq(opt_type.upper()) &
        (pd.to_datetime(master["expiry"], errors="coerce").dt.date == expiry)
    ].copy()
    sub["strike"] = pd.to_numeric(sub["strike"], errors="coerce")
    sub["diff"]   = (sub["strike"] - strike).abs()
    row = sub.nsmallest(1, "diff").iloc[0]
    return str(row["tradingsymbol"]), int(row["instrument_token"])

def get_ltp_and_qty(kite, symbol, lot_size, use_margin_pct=0.95):
    """
    Get option LTP and calculate dynamic quantity based on available cash.
    """

    # Get available cash
    cash_available = kite.margins("equity")["available"]["live_balance"]

    # Get LTP
    ltp_data = kite.ltp(f"NFO:{symbol}")
    ltp = ltp_data[f"NFO:{symbol}"]["last_price"]

    # Capital to use
    usable_cash = cash_available * use_margin_pct

    # Qty calculation
    qty = floor(usable_cash / (ltp * lot_size)) * lot_size

    print(f"Symbol      : {symbol}")
    print(f"LTP         : {ltp}")
    print(f"Cash        : {cash_available}")
    print(f"Final Qty   : {qty}")

    return ltp, qty

def buy_order(symbol, qty):

    try:

        # Fetch latest price
        ltp_data = kite.ltp(f"NFO:{symbol}")
        ltp = ltp_data[f"NFO:{symbol}"]["last_price"]

        # Buy slightly ABOVE market price for instant execution
        limit_price = round(ltp + 1, 1)

        order_id = kite.place_order(
            variety = kite.VARIETY_REGULAR,
            exchange = kite.EXCHANGE_NFO,
            tradingsymbol = symbol,
            transaction_type = kite.TRANSACTION_TYPE_BUY,
            quantity = qty,
            product = kite.PRODUCT_NRML,
            order_type = kite.ORDER_TYPE_LIMIT,
            price = limit_price,
            validity = kite.VALIDITY_DAY
        )

        print(f"BUY Order Placed : {symbol}")
        print(f"LTP       : {ltp}")
        print(f"Limit     : {limit_price}")
        print(f"Order ID  : {order_id}")

        return order_id

    except Exception as e:

        print(f"BUY Order Failed : {e}")

def sell_order(symbol, qty):

    try:

        ltp_data = kite.ltp(f"NFO:{symbol}")
        ltp = ltp_data[f"NFO:{symbol}"]["last_price"]

        # Sell slightly BELOW market for quick execution
        limit_price = round(ltp - 1, 1)

        order_id = kite.place_order(
            variety = kite.VARIETY_REGULAR,
            exchange = kite.EXCHANGE_NFO,
            tradingsymbol = symbol,
            transaction_type = kite.TRANSACTION_TYPE_SELL,
            quantity = qty,
            product = kite.PRODUCT_NRML,
            order_type = kite.ORDER_TYPE_LIMIT,
            price = limit_price,
            validity = kite.VALIDITY_DAY
        )

        print(f"SELL Order Placed : {symbol}")
        print(f"LTP       : {ltp}")
        print(f"Limit     : {limit_price}")
        print(f"Order ID  : {order_id}")

        return order_id

    except Exception as e:

        print(f"SELL Order Failed : {e}")
# =========================================================
# MAIN
# =========================================================

cfg = load_config()
kite = get_kite(cfg)
master = get_master(kite)



cprint("Kite and master loaded successfully! ",Fore.GREEN)
cprint("BOT STARTED", Fore.CYAN)


while True:

    current_time = datetime.now().time()
    print("Running main loop... and current time is ",current_time)
    cash_available = kite.margins("equity")["available"]["live_balance"]
    cprint("" + "=" * 70, Fore.YELLOW)
    cprint(f"Available Cash: {cash_available}", Fore.GREEN)
    cprint("" + "=" * 70, Fore.YELLOW)
    start_ip_watcher()

    if datetime.now().weekday() not in (0, 3, 4):  # Mon=0, Thu=3, Fri=4
        cprint("Not Monday/Thursday/Friday. Not trading today, skipping...", Fore.YELLOW)
        time.sleep(5)
        continue

    if current_time >= start_time and current_time <= end_time:
            cprint("Market is open. Scanning for signals...", Fore.GREEN)

            # Place your scanning and trading logic here
            df = fetch_futures_ohlc(kite, INDEX, TIMEFRAME, days=15)
            df = calculate_indicators(df)
            df = df.dropna()
            #print(df)

            expiry = next_expiry(master, INDEX)
            spot = get_spot(kite, INDEX)
            CE_strike, PE_strike = itm_strikes(spot, INDEX)
            cprint(f"Spot: {spot:.2f} | CE Strike (ITM): {CE_strike} | PE Strike (ITM): {PE_strike} | Expiry: {expiry}", Fore.BLUE)
            CE_symbol, CE_token = find_option_symbol(master, INDEX, expiry, CE_strike, "CE")
            PE_symbol, PE_token = find_option_symbol(master, INDEX, expiry, PE_strike, "PE")
            cprint(f"ATM CE: {CE_symbol} (Token: {CE_token})", Fore.YELLOW)
            cprint(f"ATM PE: {PE_symbol} (Token: {PE_token})", Fore.YELLOW)

            CE_ltp, CE_qty = get_ltp_and_qty(
                kite,
                CE_symbol,
                lot_size(master, INDEX)
            )

            PE_ltp, PE_qty = get_ltp_and_qty(
                kite,
                PE_symbol,
                lot_size(master, INDEX)
            )

            cprint(f"CE LTP: {CE_ltp} | Qty: {CE_qty}", Fore.GREEN)
            cprint(f"PE LTP: {PE_ltp} | Qty: {PE_qty}", Fore.RED)

            cprint(f"Supertrend: {df.SUPERT.iloc[-2]:.2f} | Trend: {'BULLISH' if df.SUPERTd.iloc[-2] == 1 else 'BEARISH'}", Fore.MAGENTA)

            st_direction = "BULLISH" if df.SUPERTd.iloc[-2] == 1 else "BEARISH"
            cprint(f"NIFTY: {spot:.2f} | ST Direction: {st_direction} | CE: {CE_symbol} @ {CE_ltp} (Qty: {CE_qty})", Fore.CYAN)

            cur_d  = df.SUPERTd.iloc[-2]
            prev_d = df.SUPERTd.iloc[-3]
            last_bar_ts = df.index[-2]
            new_bar = last_processed_bar_ts is None or last_bar_ts > last_processed_bar_ts

            entered_direction = None

            if USE_RETEST:
                if new_bar:
                    # Resolve a retest that was confirmed on the previous bar
                    if retest_confirmed_direction is not None:
                        if cur_d == prev_d:
                            entered_direction = retest_confirmed_direction
                        retest_confirmed_direction = None

                    # Detect a new flip, or watch for the retest of a pending flip
                    if cur_d != prev_d:
                        direction = "BULLISH" if cur_d == 1 else "BEARISH"
                        st_val  = df.SUPERT.iloc[-2]
                        touched = (df.low.iloc[-2] <= st_val) if direction == "BULLISH" else (df.high.iloc[-2] >= st_val)
                        if touched:
                            cprint(f"Supertrend flipped {direction} (retest already satisfied @ {st_val:.2f}) - entering next candle", Fore.MAGENTA)
                            retest_confirmed_direction = direction
                            pending_direction = None
                        else:
                            cprint(f"Supertrend flipped {direction} - waiting for retest of ST line ({st_val:.2f})", Fore.YELLOW)
                            pending_direction = direction
                    elif pending_direction is not None:
                        st_val  = df.SUPERT.iloc[-2]
                        touched = (df.low.iloc[-2] <= st_val) if pending_direction == "BULLISH" else (df.high.iloc[-2] >= st_val)
                        if touched:
                            cprint(f"Retest confirmed for {pending_direction} @ {st_val:.2f} - entering next candle", Fore.MAGENTA)
                            retest_confirmed_direction = pending_direction
                            pending_direction = None

                    last_processed_bar_ts = last_bar_ts
            else:
                if cur_d == 1 and prev_d == -1:
                    entered_direction = "BULLISH"
                elif cur_d == -1 and prev_d == 1:
                    entered_direction = "BEARISH"

            if entered_direction == "BULLISH" and Buy == False:
                    print("Supertrend flip confirmed - Buy Signal for CALL")

                    if Sell == True:
                        cprint(f"Trailing exit (ST flip) - placing sell order for {Traded_symbol}...", Fore.GREEN)
                        sell_order(Traded_symbol, Traded_quanity)
                        Sell = False

                    cprint(f"Placing buy order for {CE_symbol}...", Fore.GREEN)
                    buy_order(CE_symbol, CE_qty)
                    Traded_symbol = CE_symbol
                    Buy = True
                    Traded_quanity = CE_qty

            elif entered_direction == "BEARISH" and Sell == False:
                    print("Supertrend flip confirmed - Buy Signal for PUT")

                    if Buy == True:
                        cprint(f"Trailing exit (ST flip) - placing sell order for {Traded_symbol}...", Fore.GREEN)
                        sell_order(Traded_symbol, Traded_quanity)
                        Buy = False

                    cprint(f"Placing buy order for {PE_symbol}...", Fore.GREEN)
                    buy_order(PE_symbol, PE_qty)
                    Traded_symbol = PE_symbol
                    Sell = True
                    Traded_quanity = PE_qty

            if current_time >= exit_time:
                    cprint("Market is about to close. Exiting any open positions...", Fore.RED)
                    if Buy == True:
                        sell_order(Traded_symbol, Traded_quanity)
                        Buy = False
                    if Sell == True:
                        sell_order(Traded_symbol, Traded_quanity)
                        Sell = False


            if Buy== True:
                cprint(f"Currently in a BUY position for {Traded_symbol}", Fore.GREEN)
            elif Sell == True:
                cprint(f"Currently in a SELL position for {Traded_symbol}", Fore.RED)

            if current_time >= end_time:
                cprint("Market is closed. Waiting for next trading day...", Fore.YELLOW)
    time.sleep(5)
