import os,csv
import sys
import json
import time
import webbrowser
import traceback
from math import floor
from zoneinfo import ZoneInfo
from click import option
import pandas as pd
import pandas_ta as ta
from ip_update_windows import *
from datetime import datetime, timedelta, date
from kiteconnect import KiteConnect
from Auto_toptp_Engine import initialize_kite_session

LOG_DIR  = r"C:\Code\KiteConnect\Trade_logs_All_Bots"
LOG_FILE = os.path.join(LOG_DIR, "SENSEX_trades.csv")
HEADERS = [
    "date", "entry_time", "exit_time",
    "symbol", "option_type", "strike",
    "entry_price", "exit_price",
    "qty", "pnl", "pnl_pct",
    "entry_rsi_ema", "entry_rsi_wma",
    "exit_reason",                      # SIGNAL / EOD / MANUAL
    "regime_vwap",                      # price vs vwap at entry: ABOVE / BELOW
    "order_id_entry", "order_id_exit"
]
 

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE = os.path.join(BASE_DIR, "instruments_master.csv")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
TOKEN_FILE  = os.path.join(BASE_DIR, "access_token.json")

INDEX = "SENSEX"
INDEX_QUOTE = "BSE:SENSEX"
TIMEFRAME = "5minute"

start_time = datetime.strptime("09:30:00", "%H:%M:%S").time()
end_time = datetime.strptime("23:30:00", "%H:%M:%S").time()
exit_time = datetime.strptime("15:15:00", "%H:%M:%S").time()

RSI_LENGTH = 9
EMA_LENGTH = 9
WMA_LENGTH = 21
INDEX_TOKENS = {"SENSEX": 265}
IST = ZoneInfo("Asia/Kolkata")

Buy = False
Sell = False

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
        "date"           : now.strftime("%Y-%m-%d"),
        "entry_time"     : now.strftime("%H:%M:%S"),
        "symbol"         : symbol,
        "option_type"    : option_type,          # "CE" or "PE"
        "strike"         : strike,
        "entry_price"    : entry_price,
        "qty"            : qty,
        "entry_rsi_ema"  : round(df["EMA3"].iloc[-2], 4),
        "entry_rsi_wma"  : round(df["WMA21"].iloc[-2], 4),
        "regime_vwap"    : "ABOVE" if df["close"].iloc[-2] > df["VWAP"].iloc[-2] else "BELOW",
        "order_id_entry" : order_id,
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

def next_futures_expiry(master, index="SENSEX"):
    """
    Returns the nearest upcoming futures expiry date for the index.
    Looks in BFO segment for FUT instrument type.
    """
    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].eq("FUT") &
        master["segment"].astype(str).str.upper().eq("BFO-FUT")
    ].copy()
 
    sub["expiry"] = pd.to_datetime(sub["expiry"], errors="coerce").dt.date
    future = sub[sub["expiry"] >= date.today()]["expiry"].dropna().sort_values().unique()
 
    if len(future) == 0:
        raise Exception(f"No futures expiry found for {index} in master")
 
    exp = future[0]
    print(f"   Next futures expiry [{index}]: {exp}")
    return exp

def get_futures_token(master, index="SENSEX"):
    """
    Returns (tradingsymbol, instrument_token) for the front-month
    SENSEX futures contract from BFO.
    """
    exp = next_futures_expiry(master, index)
 
    sub = master[
        master["name"].str.upper().eq(index.upper()) &
        master["instrument_type"].eq("FUT") &
        master["segment"].astype(str).str.upper().eq("BFO-FUT") &
        (pd.to_datetime(master["expiry"], errors="coerce").dt.date == exp)
    ].copy()
 
    if sub.empty:
        raise Exception(f"Futures contract not found for {index} expiry {exp}")
 
    row = sub.iloc[0]
    symbol = str(row["tradingsymbol"])
    token  = int(row["instrument_token"])
    print(f"   Futures symbol [{index}]: {symbol} | Token: {token}")
    return symbol, token

def fetch_futures_ohlc(kite, index="SENSEX", interval="5minute", days=3):
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
        q = kite.quote([INDEX_QUOTE[index.upper()]])
        return float(q[INDEX_QUOTE[index.upper()]]["last_price"])
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

def is_expiry_today(master, index):
    """
    Returns True if today is the expiry day (DTE = 0) for `index`.
    Used to skip trading entirely on expiry day.
    """
    exp = next_expiry(master, index)
    return exp == date.today()

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
    df["RSI"] = ta.rsi(df["close"], length=RSI_LENGTH)
    df["EMA3"] = ta.ema(df["RSI"], length=EMA_LENGTH)
    df["WMA21"] = ta.wma(df["RSI"], length=WMA_LENGTH)
    df["VWAP"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    df["SMA50"] = ta.sma(df["close"], length=50)
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

def get_ltp_and_qty(kite, symbol, lot_size, use_margin_pct=0.90):
    """
    Get option LTP and calculate dynamic quantity based on available cash.
    """

    # Get available cash
    cash_available = kite.margins("equity")["available"]["live_balance"]

    # Get LTP
    ltp_data = kite.ltp(f"BFO:{symbol}")
    ltp = ltp_data[f"BFO:{symbol}"]["last_price"]

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
        ltp_data = kite.ltp(f"BFO:{symbol}")
        ltp = ltp_data[f"BFO:{symbol}"]["last_price"]

        # Buy slightly ABOVE market price for instant execution
        limit_price = round(ltp + 1, 1)

        order_id = kite.place_order(
            variety = kite.VARIETY_REGULAR,
            exchange = kite.EXCHANGE_BFO,
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

        ltp_data = kite.ltp(f"BFO:{symbol}")
        ltp = ltp_data[f"BFO:{symbol}"]["last_price"]

        # Sell slightly BELOW market for quick execution
        limit_price = round(ltp - 1, 1)

        order_id = kite.place_order(
            variety = kite.VARIETY_REGULAR,
            exchange = kite.EXCHANGE_BFO,
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

start_ip_watcher()

cprint("Kite and master loaded successfully! ",Fore.GREEN)
cprint("BOT STARTED", Fore.CYAN)
cash_available = kite.margins("equity")["available"]["live_balance"]
cprint(f"Available Cash: {cash_available}", Fore.GREEN)

while True:
    current_time = datetime.now().time()
    print("Running main loop... and current time is ",current_time)
    start_ip_watcher()

    if datetime.now().weekday() not in (1, 2):  # Tue=1, Wed=2
        cprint("Not Tuesday/Wednesday. Not trading today, skipping...", Fore.YELLOW)
        time.sleep(5)
        continue

    if current_time >= start_time and current_time <= end_time and is_expiry_today(master, INDEX):
        cprint("DTE = 0 (Expiry day). Not trading today, skipping...", Fore.YELLOW)
        time.sleep(5)
        continue

    if current_time >= start_time and current_time <= end_time:
            cprint("Market is open. Scanning for signals...", Fore.GREEN)
            
            # Place your scanning and trading logic here
            df = fetch_futures_ohlc(kite, INDEX, TIMEFRAME, days=3)
            df = calculate_indicators(df)
            df = df.dropna()
            #print(df)

            expiry = next_expiry(master, INDEX)
            spot = df["close"].iloc[-1]
            strike = atm_strike(spot, INDEX)
            cprint(f"Spot: {spot:.2f} | ATM Strike: {strike} | Expiry: {expiry}", Fore.BLUE)
            CE_symbol, CE_token = find_option_symbol(master, INDEX, expiry, strike, "CE")
            PE_symbol, PE_token = find_option_symbol(master, INDEX, expiry, strike, "PE")
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

            if df.EMA3.iloc[-3] < df.WMA21.iloc[-3] and df.EMA3.iloc[-2] > df.WMA21.iloc[-2] and Buy == False:
                    print("Buy Signal for CE")
                    cprint(f"Placing buy order for {CE_symbol}...", Fore.GREEN)

                    buy_order(CE_symbol, CE_qty)
                    Traded_symbol = CE_symbol
                    Buy = True
                    Traded_quanity = CE_qty

            elif df.EMA3.iloc[-3] > df.WMA21.iloc[-3] and df.EMA3.iloc[-2] < df.WMA21.iloc[-2] and Sell == False:
                    print("Buy Signal for PE")
                    cprint(f"Placing buy order for {PE_symbol}...", Fore.GREEN)

                    buy_order(PE_symbol, PE_qty)
                    Traded_symbol = PE_symbol
                    Sell = True
                    Traded_quanity = PE_qty
            elif df.EMA3.iloc[-3] > df.WMA21.iloc[-3] and df.EMA3.iloc[-2] < df.WMA21.iloc[-2] and Buy == True:
                    print("Sell Signal for CE")
                    cprint(f"Placing sell order for {Traded_symbol}...", Fore.GREEN)

                    sell_order(Traded_symbol, Traded_quanity)
                    Buy = False

            elif df.EMA3.iloc[-3] < df.WMA21.iloc[-3] and df.EMA3.iloc[-2] > df.WMA21.iloc[-2] and Sell == True:
                    print("Sell Signal for PE")
                    cprint(f"Placing sell order for {Traded_symbol}...", Fore.GREEN)

                    sell_order(Traded_symbol, Traded_quanity)
                    Sell = False
            elif df.RSI.iloc[-1] > 88 and df.RSI.iloc[-2] <= 88 and Buy[INDEX] == True:
                    print("RSI Exit Signal for CE")
                    cprint(f"Placing sell order for {Traded_symbol[INDEX]}...", Fore.GREEN)

                    sell_order(Traded_symbol[INDEX], Traded_quanity)
                    Buy[INDEX] = False
            
            elif df.RSI.iloc[-1] < 12 and df.RSI.iloc[-2] >= 12 and Sell[INDEX] == True:
                    print("RSI Exit Signal for PE")
                    cprint(f"Placing sell order for {Traded_symbol[INDEX]}...", Fore.GREEN)

                    sell_order(Traded_symbol[INDEX], Traded_quanity)
                    Sell[INDEX] = False

            elif current_time >= exit_time:
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