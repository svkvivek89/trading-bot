import pandas as pd
import numpy as np
import datetime, time, schedule, requests, os, json
import logging
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException, NetworkException, InputException, OrderException

# === Setup Logging ===
logging.basicConfig(level=logging.INFO, # Changed to INFO for production-ready logs
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("bot.log"), # Log to file
                        logging.StreamHandler()         # Log to console
                    ])
logger = logging.getLogger(__name__)

# === Credentials ===
# IMPORTANT: Replace with your actual API Key, Secret, and a live Access Token.
api_key             = "xdvu36rtx33987xc"
api_secret          = "qOmM2PijRwX54a5EVyXW6g0NTz19A2Hm"
access_token        = "qOmM2PijRwX54a5EVyXW6g0NTz19A2Hm" # <<< CRITICAL: Your access token will expire daily.
                                                    # You need a mechanism to refresh this daily.
                                                    # This hardcoded token will fail tomorrow!
telegram_bot_token = "7973791702:AAH17vg_izSUMKbJvgqc1ICZtE_ueX4iUZE" # Replace with your bot token
telegram_chat_id    = "6544776630" # Replace with your chat ID

# === Parameters ===
RISK_PERCENTAGE_PER_TRADE = 0.01 # Risk 1% of equity per trade. (Adjust based on your risk appetite)
MAX_TRADES = 3               # Max number of *new* entries per index per day

# Option premium based SL/Target percentages
SL_PERCENTAGE_INITIAL = 0.05    # 5% Stop Loss on option premium (initial static SL)
TARGET_PERCENTAGE_INITIAL = 0.10 # 10% Target Profit on option premium

# Trailing SL Parameters
TRAIL_SL_ATR_MULTIPLIER = 1.5 # Trail SL at 1.5 * ATR from highest/lowest point
TRAIL_SL_PERCENTAGE_GAIN = 0.02 # Start trailing SL after 2% gain (example threshold)

# Daily PnL Limits (in INR) - IMPORTANT for risk management
MAX_DAILY_LOSS_PER_INDEX = 5000  # Max loss per Nifty/BankNifty trades per day
MAX_DAILY_PROFIT_PER_INDEX = 10000 # Max profit per Nifty/BankNifty trades per day
MAX_DAILY_DRAWDOWN_PERCENT = 0.05 # Stop trading if current PnL drops 5% from daily PnL peak

# Indicator Parameters
EMA_FAST = 14
EMA_SLOW = 50
CANDLE_INTERVAL = "5minute"
LOOKBACK_MINUTES = 300 # 5 hours of 5-minute candles for indicator calculation
ADX_THRESHOLD = 25
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
BREAKOUT_LOOKBACK_CANDLES = 10 # Number of previous candles to define High/Low for breakouts

# Volume Confirmation for Breakouts
VOLUME_MULTIPLIER = 1.5 # Current candle volume must be X times avg volume for a valid breakout

# === Kite Connect Setup ===
kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

# === Global Trade State Variables ===
STATE_FILE = "bot_state.json"
trade_cnt = {"NIFTY": 0, "BANKNIFTY": 0}
active_trades = {
    "NIFTY": {"tradingsymbol": None, "instrument_token": None, "instrument_type": None,
              "entry_price": None, "sl_price": None, "target_price": None, "quantity": None, "side": None,
              "initial_sl_order_id": None, "current_pnl": 0.0, "total_pnl": 0.0,
              "status": "CLOSED", "pnl_peak": 0.0},
    "BANKNIFTY": {"tradingsymbol": None, "instrument_token": None, "instrument_type": None,
                  "entry_price": None, "sl_price": None, "target_price": None, "quantity": None, "side": None,
                  "initial_sl_order_id": None, "current_pnl": 0.0, "total_pnl": 0.0,
                  "status": "CLOSED", "pnl_peak": 0.0}
}
daily_pnl = {"NIFTY": 0.0, "BANKNIFTY": 0.0}
trading_active = {"NIFTY": True, "BANKNIFTY": True}
user_equity = 0.0 # Will be fetched at startup

# Global definitions for instrument data
instruments_df = pd.DataFrame()
nifty_options = pd.DataFrame()
banknifty_options = pd.DataFrame()
nifty_futures_token = None
banknifty_futures_token = None
index_map = {} # This will be populated in main execution block

# --- Telegram Helper ---
def tg(msg):
    """Sends a message to the configured Telegram chat."""
    try:
        # Filter out non-printable characters and surrogates
        safe_msg = ''.join(c for c in msg if c.isprintable() and not (0xD800 <= ord(c) <= 0xDFFF))
        response = requests.get(f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage",
                                params={"chat_id": telegram_chat_id, "text": safe_msg}, timeout=5)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
    except requests.exceptions.Timeout:
        logger.error("[Telegram] Request timed out while sending message.")
    except requests.exceptions.RequestException as e:
        logger.error(f"[Telegram] Error sending message: {e}")
    except Exception as e:
        logger.error(f"[Telegram] Unexpected error: {e}")

# --- Test Kite Connection ---
def test_connection_and_get_equity():
    """Tests Kite Connect API and fetches user equity."""
    global user_equity
    try:
        profile = kite.profile()
        name = profile.get("user_name", "Unknown User")
        margins = kite.margins() # Fetch equity margins
        equity_margin = margins.get('equity', {}).get('net', 0.0)
        user_equity = equity_margin # Store for position sizing
        
        tg(f"\u2705 Zerodha API Connected! User: {name}. Equity: \u20b9{user_equity:,.2f}")
        logger.info(f"Zerodha API Connected. User: {name}. Equity: ₹{user_equity:,.2f}")
        return True
    except TokenException:
        tg(f"\u274C Zerodha Connection Error: Invalid/Expired Access Token. Please regenerate.")
        logger.critical(f"Zerodha Connection Error: Invalid/Expired Access Token. Exiting.")
        return False
    except NetworkException as ex:
        tg(f"\u274C Zerodha Connection Error: Network issue - {ex}.")
        logger.critical(f"Zerodha Connection Error: Network issue - {ex}. Exiting.")
        return False
    except Exception as ex:
        tg(f"\u274C Zerodha Connection Error: {ex}. Please check API keys/token.")
        logger.critical(f"Zerodha Connection Error: {ex}. Exiting.")
        return False

def load_initial_instruments():
    """Loads instruments and populates global DFs for Nifty/BankNifty options and futures tokens."""
    global instruments_df, nifty_options, banknifty_options, nifty_futures_token, banknifty_futures_token
    try:
        instruments_df = pd.DataFrame(kite.instruments("NFO"))
        
        # Filter for relevant index options
        nifty_options = instruments_df[(instruments_df['name'] == 'NIFTY') & (instruments_df['segment'] == 'NFO-OPT')]
        banknifty_options = instruments_df[(instruments_df['name'] == 'BANKNIFTY') & (instruments_df['segment'] == 'NFO-OPT')]

        # Find nearest futures token for volume
        current_date = datetime.date.today()
        
        # NIFTY Futures
        nifty_fut = instruments_df[(instruments_df['name'] == 'NIFTY') & (instruments_df['segment'] == 'NFO-FUT')]
        if not nifty_fut.empty:
            nifty_fut_exp = sorted([exp for exp in nifty_fut['expiry'].unique() if exp >= current_date])
            if nifty_fut_exp:
                nifty_futures_token = nifty_fut[nifty_fut['expiry'] == nifty_fut_exp[0]]['instrument_token'].iloc[0]
                logger.info(f"NIFTY Futures Token: {nifty_futures_token} (Expiry: {nifty_fut_exp[0]})")
            else:
                logger.warning("No NIFTY Futures found for volume tracking.")

        # BANKNIFTY Futures
        banknifty_fut = instruments_df[(instruments_df['name'] == 'BANKNIFTY') & (instruments_df['segment'] == 'NFO-FUT')]
        if not banknifty_fut.empty:
            banknifty_fut_exp = sorted([exp for exp in banknifty_fut['expiry'].unique() if exp >= current_date])
            if banknifty_fut_exp:
                banknifty_futures_token = banknifty_fut[banknifty_fut['expiry'] == banknifty_fut_exp[0]]['instrument_token'].iloc[0]
                logger.info(f"BANKNIFTY Futures Token: {banknifty_futures_token} (Expiry: {banknifty_fut_exp[0]})")
            else:
                logger.warning("No BANKNIFTY Futures found for volume tracking.")

    except Exception as e:
        logger.critical(f"Failed to load initial instruments: {e}")
        tg(f"\u274C Critical: Failed to load initial instruments. Bot may not function. Error: {e}")
        raise SystemExit("Failed to load initial instruments. Exiting.")

# === State Management ===
def save_state():
    """Saves the bot's current trading state to a JSON file."""
    state = {
        "trade_cnt": trade_cnt,
        "active_trades": active_trades,
        "daily_pnl": daily_pnl,
        "trading_active": trading_active,
        "user_equity": user_equity # Save current equity as well
    }
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f, indent=4) # Pretty print JSON
        # No INFO log for save_state, to reduce verbosity on frequent saves
    except Exception as e:
        logger.error(f"Error saving bot state: {e}")
        tg(f"⚠️ Error saving bot state: {e}")

def load_state():
    """Loads the bot's trading state from a JSON file."""
    global trade_cnt, active_trades, daily_pnl, trading_active, user_equity
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                trade_cnt = state.get("trade_cnt", {"NIFTY": 0, "BANKNIFTY": 0})
                active_trades = state.get("active_trades", {
                    "NIFTY": {"tradingsymbol": None, "status": "CLOSED", "current_pnl": 0.0, "total_pnl": 0.0, "pnl_peak": 0.0, "instrument_type": None},
                    "BANKNIFTY": {"tradingsymbol": None, "status": "CLOSED", "current_pnl": 0.0, "total_pnl": 0.0, "pnl_peak": 0.0, "instrument_type": None}
                })
                daily_pnl = state.get("daily_pnl", {"NIFTY": 0.0, "BANKNIFTY": 0.0})
                trading_active = state.get("trading_active", {"NIFTY": True, "BANKNIFTY": True})
                user_equity = state.get("user_equity", 0.0) # Load last known equity

            logger.info("Bot state loaded.")
            tg("✅ Bot state loaded.")
        except json.JSONDecodeError as e:
            logger.error(f"Error decoding JSON state file: {e}. Starting fresh.")
            tg(f"⚠️ Error loading state file: {e}. Starting fresh.")
            reset_daily_state() # If file is corrupt, reset
        except Exception as e:
            logger.error(f"Error loading bot state: {e}. Starting fresh.")
            tg(f"⚠️ Error loading state: {e}. Starting fresh.")
            reset_daily_state()
    else:
        logger.info("No existing state file found. Starting fresh.")
        reset_daily_state() # Initialize if no file

def reset_daily_state():
    """Resets trade counters, active positions, and daily PnL for the new trading day."""
    global trade_cnt, active_trades, daily_pnl, trading_active, user_equity
    trade_cnt["NIFTY"] = trade_cnt["BANKNIFTY"] = 0
    for symbol in active_trades:
        active_trades[symbol] = {"tradingsymbol": None, "instrument_token": None, "instrument_type": None,
                                 "entry_price": None, "sl_price": None, "target_price": None, "quantity": None, "side": None,
                                 "initial_sl_order_id": None, "current_pnl": 0.0, "total_pnl": 0.0,
                                 "status": "CLOSED", "pnl_peak": 0.0}
    daily_pnl["NIFTY"] = daily_pnl["BANKNIFTY"] = 0.0
    trading_active["NIFTY"] = trading_active["BANKNIFTY"] = True
    
    # Re-fetch equity at the start of a new day
    if not test_connection_and_get_equity():
        logger.critical("Failed to re-fetch equity on daily reset. Exiting.")
        tg("\u274C Failed to re-fetch equity on daily reset. Exiting.")
        raise SystemExit("Equity fetch failed at daily reset.")

    tg("\ud83d\udd04 Trade counters, active positions, and daily PnL reset for new day.")
    logger.info("Daily state reset for new trading day.")
    save_state()

# === Indicator Calculation ===
def fetch_candles(instrument_token, futures_token_for_volume=None):
    """
    Fetches historical candles for index token and volume data from futures token if provided.
    Calculates EMA, ATR, ADX, and RSI.
    Includes robust checks for sufficient data.
    """
    try:
        min_candles_for_indicators = max(EMA_SLOW, 14) + 20
        required_minutes_for_data = min_candles_for_indicators * 5

        from_dt = datetime.datetime.now() - datetime.timedelta(minutes=max(LOOKBACK_MINUTES, required_minutes_for_data) + 15)
        to_dt = datetime.datetime.now()

        index_data = kite.historical_data(instrument_token, from_dt, to_dt, CANDLE_INTERVAL)
        df = pd.DataFrame(index_data)
        df.set_index('date', inplace=True)
        df.index = pd.to_datetime(df.index)

        if futures_token_for_volume:
            try:
                volume_data = kite.historical_data(futures_token_for_volume, from_dt, to_dt, CANDLE_INTERVAL)
                df_vol = pd.DataFrame(volume_data)
                df_vol.set_index('date', inplace=True)
                df_vol.index = pd.to_datetime(df_vol.index)

                if 'volume' in df_vol.columns:
                    df = df.merge(df_vol['volume'], left_index=True, right_index=True, how='left', suffixes=('_index', '_futures'))
                    df['volume'].fillna(0, inplace=True)
                else:
                    logger.warning(f"[Candle Fetch] Futures volume data for {futures_token_for_volume} does not contain a 'volume' column. Using 0 volume.")
                    df['volume'] = 0
            except Exception as e:
                logger.warning(f"[Candle Fetch] Error processing futures volume for {futures_token_for_volume}: {e}. Using 0 volume.")
                df['volume'] = 0
        else:
            df['volume'] = 0

        if len(df) < min_candles_for_indicators:
            logger.warning(f"[Candle Fetch] Not enough historical data for {instrument_token}. Need {min_candles_for_indicators} candles, got {len(df)}. Skipping indicators.")
            return pd.DataFrame()

        # EMA
        df['EMA_FAST'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
        df['EMA_SLOW'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

        # ATR calculation (standard 14-period)
        high_low = df['high'] - df['low']
        high_prev_close = abs(df['high'] - df['close'].shift(1))
        low_prev_close = abs(df['low'] - df['close'].shift(1))
        df['TR'] = pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)
        df['ATR'] = df['TR'].ewm(span=14, adjust=False).mean()

        # ADX Calculation (using Wilder's Smoothing)
        df['UpMove'] = df['high'] - df['high'].shift(1)
        df['DownMove'] = df['low'].shift(1) - df['low']

        df['plus_dm'] = np.where((df['UpMove'] > df['DownMove']) & (df['UpMove'] > 0), df['UpMove'], 0)
        df['minus_dm'] = np.where((df['DownMove'] > df['UpMove']) & (df['DownMove'] > 0), df['DownMove'], 0)

        alpha_adx = 1/14
        df['plus_di'] = df['plus_dm'].ewm(alpha=alpha_adx, adjust=False).mean()
        df['minus_di'] = df['minus_dm'].ewm(alpha=alpha_adx, adjust=False).mean()
        df['ATR_ADX_Smooth'] = df['TR'].ewm(alpha=alpha_adx, adjust=False).mean()

        dx_denominator = (df['plus_di'] + df['minus_di'])
        df['DX'] = 100 * (abs(df['plus_di'] - df['minus_di']) / dx_denominator).replace([np.inf, -np.inf], np.nan).fillna(0)
        df['ADX'] = df['DX'].ewm(alpha=alpha_adx, adjust=False).mean()

        # RSI Calculation
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(span=14, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(span=14, adjust=False).mean()
        
        rs = (gain / loss).replace([np.inf, -np.inf], np.nan).fillna(0)
        df['RSI'] = 100 - (100 / (1 + rs))

        return df.dropna()
    except NetworkException as e:
        logger.error(f"[Candle Fetch] Network error for {instrument_token}: {e}")
        tg(f"\u26a0\ufe0f Network error fetching candles for {instrument_token}.")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"[Candle Fetch] Failed for {instrument_token}: {e}")
        tg(f"\u26a0\ufe0f Candle Fetch Error for {instrument_token}: {e}")
        return pd.DataFrame()

# === Option Chain Fetch & Selection ===
# Cache for option chain (structure: {expiry_date: {strike: {type: row_data}}})
option_chain_cache = {"NIFTY": {}, "BANKNIFTY": {}}

def fetch_and_cache_option_chain_symbols(symbol_name, symbol_df):
    """
    Fetches and caches option chain symbols (instrument_token, tradingsymbol) for relevant expiries.
    This runs once at startup or daily.
    """
    try:
        current_date = datetime.date.today()
        relevant_expiries = sorted([exp for exp in symbol_df['expiry'].unique() if exp >= current_date])
        
        expiries_to_cache = []
        if relevant_expiries:
            nearest_weekly = None
            for exp in relevant_expiries:
                if exp.weekday() == 3 and (exp - current_date).days >= 0 and (exp - current_date).days <= 7:
                    nearest_weekly = exp
                    break
            if nearest_weekly:
                expiries_to_cache.append(nearest_weekly)
            
            nearest_monthly = None
            for exp in relevant_expiries:
                if exp.weekday() == 3 and (exp + pd.DateOffset(weeks=1)).month != exp.month:
                    if exp not in expiries_to_cache:
                        nearest_monthly = exp
                        break
            if nearest_monthly:
                expiries_to_cache.append(nearest_monthly)
            
            if not expiries_to_cache and relevant_expiries:
                expiries_to_cache.append(relevant_expiries[0])

        option_chain_cache[symbol_name] = {}
        for expiry in expiries_to_cache:
            filtered_df = symbol_df[symbol_df['expiry'] == expiry].copy()
            for _, row in filtered_df.iterrows():
                strike = row['strike']
                op_type = row['instrument_type']
                if strike not in option_chain_cache[symbol_name]:
                    option_chain_cache[symbol_name][strike] = {}
                option_chain_cache[symbol_name][strike][op_type] = row.to_dict()
            logger.info(f"Cached {symbol_name} options for expiry: {expiry.strftime('%Y-%m-%d')}")

    except Exception as e:
        logger.error(f"Error caching option chain for {symbol_name}: {e}")
        tg(f"\u26a0\ufe0f Error caching {symbol_name} options: {e}")


def get_atm_option_details(symbol_name, current_index_price, option_type):
    """
    Finds the At-The-Money (ATM) option (or closest to ATM) for a given type.
    Uses cached option chain and fetches live LTP for the selected option.
    """
    cached_chain = option_chain_cache.get(symbol_name)
    if not cached_chain:
        logger.warning(f"No cached option chain for {symbol_name}. Cannot find ATM option.")
        return None

    all_strikes_for_type = []
    for strike, types in cached_chain.items():
        if option_type in types:
            all_strikes_for_type.append(strike)
    
    if not all_strikes_for_type:
        logger.warning(f"No {option_type} options found in cached chain for {symbol_name}. Cannot find ATM.")
        return None

    closest_strike = min(all_strikes_for_type, key=lambda x: abs(x - current_index_price))
    
    selected_option_details = cached_chain[closest_strike].get(option_type)
    if selected_option_details:
        instrument_token = selected_option_details['instrument_token']
        tradingsymbol = selected_option_details['tradingsymbol']
        
        try:
            ltp_data = kite.ltp([f"NFO:{tradingsymbol}"])
            if f"NFO:{tradingsymbol}" in ltp_data and 'last_price' in ltp_data[f"NFO:{tradingsymbol}"]:
                selected_option_details['ltp'] = ltp_data[f"NFO:{tradingsymbol}"]['last_price']
                selected_option_details['instrument_token'] = instrument_token
                return selected_option_details
            else:
                logger.warning(f"LTP not found for {tradingsymbol}. Skipping option selection.")
                return None
        except NetworkException as e:
            logger.error(f"Network error fetching LTP for {tradingsymbol}: {e}")
            tg(f"\u26a0\ufe0f Network error fetching LTP for {tradingsymbol}.")
            return None
        except Exception as e:
            logger.error(f"Error fetching LTP for {tradingsymbol}: {e}")
            tg(f"\u26a0\ufe0f Error fetching LTP for {tradingsymbol}.")
            return None
    
    return None

# === Order Placement ===
def place_order(ts, instrument_token, instrument_type, side, qty, entry_price_from_ltp, symbol_name):
    """
    Places a market order for an option and immediately places a corresponding SL-M order.
    Updates the global active_trades dictionary with actual fill details.
    """
    global user_equity
    
    try:
        order_params = {
            "variety": kite.VARIETY_REGULAR, "exchange": "NFO", "tradingsymbol": ts,
            "transaction_type": side, "quantity": qty, "order_type": kite.ORDER_TYPE_MARKET,
            "product": kite.PRODUCT_NRML, "tag": "BREAKOUT_BOT_ENTRY"
        }
        buy_order_response = kite.place_order(**order_params)
        entry_order_id = buy_order_response.get('order_id')
        logger.info(f"Market Order placed for {ts} (ID: {entry_order_id}). Qty: {qty}. Awaiting fill...")
        
        time.sleep(3)

        actual_entry_price = 0.0
        filled_quantity = 0
        order_status_msg = "N/A"
        try:
            order_history = kite.get_order_history(order_id=entry_order_id)
            if order_history:
                latest_order_info = order_history[0]
                order_status_msg = latest_order_info['status']

                if latest_order_info['status'] == 'COMPLETE':
                    actual_entry_price = latest_order_info['average_price']
                    filled_quantity = latest_order_info['filled_quantity']
                elif latest_order_info['status'] in ['OPEN', 'PARTIALLY_FILLED', 'PENDING']:
                    logger.warning(f"Order {entry_order_id} for {ts} is {order_status_msg}. Attempting to cancel and abort trade.")
                    kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=entry_order_id)
                    tg(f"\u26a0\ufe0f {ts} Order {entry_order_id} was {order_status_msg}. Cancelled. No trade opened.")
                    active_trades[symbol_name]["status"] = "CANCELLED"
                    return
                else:
                    logger.warning(f"Order {entry_order_id} for {ts} failed. Status: {order_status_msg}. Skipping SL placement.")
                    tg(f"\u274c {ts} Order {entry_order_id} failed. Status: {order_status_msg}. No trade opened.")
                    active_trades[symbol_name]["status"] = "FAILED"
                    return

            if filled_quantity == 0:
                logger.warning(f"Order {entry_order_id} for {ts} reported 0 filled quantity. Skipping SL.")
                tg(f"\u26a0\ufe0f {ts} Order {entry_order_id} filled 0 qty. No trade opened.")
                active_trades[symbol_name]["status"] = "FAILED"
                return
            
            logger.info(f"Order {entry_order_id} for {ts} filled. Avg Price: ₹{actual_entry_price:.2f}, Qty: {filled_quantity}")

        except (OrderException, NetworkException) as e:
            logger.error(f"Error fetching order history for {entry_order_id}: {e}. Trade status uncertain. Skipping SL placement.")
            tg(f"\u26a0\ufe0f Error fetching fill for {entry_order_id}. Trade status uncertain. Skipping SL.")
            active_trades[symbol_name]["status"] = "FAILED"
            return
        except Exception as e:
            logger.error(f"Unexpected error fetching order history for {entry_order_id}: {e}. Skipping SL placement.")
            tg(f"\u26a0\ufe0f Unexpected error fetching order history. Skipping SL for {ts}.")
            active_trades[symbol_name]["status"] = "FAILED"
            return

        calculated_sl = round(actual_entry_price * (1 - SL_PERCENTAGE_INITIAL), 1)
        
        # --- Correct Target Price Calculation based on instrument_type ---
        if instrument_type == 'CE': # Call Option: Profit when price goes up
            calculated_tgt = round(actual_entry_price * (1 + TARGET_PERCENTAGE_INITIAL), 1)
        else: # PE (Put Option): Profit when price goes down
            calculated_tgt = round(actual_entry_price * (1 - TARGET_PERCENTAGE_INITIAL), 1)

        sl_order_params = {
            "variety": kite.VARIETY_REGULAR, "exchange": "NFO", "tradingsymbol": ts,
            "transaction_type": kite.TRANSACTION_TYPE_SELL if side == kite.TRANSACTION_TYPE_BUY else kite.TRANSACTION_TYPE_BUY,
            "quantity": filled_quantity, "order_type": kite.ORDER_TYPE_SLM,
            "trigger_price": calculated_sl, "price": 0.0, "product": kite.PRODUCT_NRML,
            "tag": "BREAKOUT_BOT_SL"
        }
        sl_order_response = kite.place_order(**sl_order_params)
        sl_order_id = sl_order_response.get('order_id')

        # --- Store instrument_type in active_trades ---
        active_trades[symbol_name] = {
            "tradingsymbol": ts, "instrument_token": instrument_token, "instrument_type": instrument_type,
            "entry_price": actual_entry_price, "sl_price": calculated_sl, "target_price": calculated_tgt,
            "quantity": filled_quantity, "side": side, "initial_sl_order_id": sl_order_id,
            "current_pnl": 0.0, "total_pnl": 0.0, "status": "OPEN", "pnl_peak": 0.0
        }
        
        logger.info(f"Trade OPEN for {symbol_name}: {ts} | Entry: ₹{actual_entry_price:.2f}, SL: ₹{calculated_sl}, TGT: ₹{calculated_tgt}")
        tg(f"\u2705 Trade OPEN for {symbol_name}: {ts}\nEntry: \u20b9{actual_entry_price:.2f}, SL: \u20b9{calculated_sl:.2f}, TGT: \u20b9{calculated_tgt:.2f}")
        trade_cnt[symbol_name] += 1
        save_state()

    except (OrderException, NetworkException, InputException) as e:
        logger.error(f"Order Placement/API Error for {ts}: {e}")
        tg(f"\u274c Order Placement/API Error for {ts}: {e}")
        active_trades[symbol_name]["status"] = "FAILED"
    except Exception as e:
        logger.error(f"Unexpected Error in place_order for {ts}: {e}")
        tg(f"\u274c Unexpected Error in place_order for {ts}: {e}")
        active_trades[symbol_name]["status"] = "FAILED"

def my_monitor_and_manage_positions(): # Renamed function as discussed
    """
    Monitors all open positions, checks for target hits, SL hits (from Kite),
    and implements trailing stop loss logic.
    Also checks daily PnL limits.
    """
    # No 'global index_map' needed here as we are only reading from the global.

    # --- Fetch candles ONCE for efficiency ---
    nifty_candles_df = fetch_candles(index_map["NIFTY"]["token"], index_map["NIFTY"]["futures_token"])
    banknifty_candles_df = fetch_candles(index_map["BANKNIFTY"]["token"], index_map["BANKNIFTY"]["futures_token"])
    
    nifty_atr = nifty_candles_df['ATR'].iloc[-1] if not nifty_candles_df.empty and 'ATR' in nifty_candles_df.columns else None
    banknifty_atr = banknifty_candles_df['ATR'].iloc[-1] if not banknifty_candles_df.empty and 'ATR' in banknifty_candles_df.columns else None

    current_atr_for_symbol = {"NIFTY": nifty_atr, "BANKNIFTY": banknifty_atr}
    # --- END: Fetch candles ONCE ---

    for symbol_name in ["NIFTY", "BANKNIFTY"]:
        current_trade = active_trades[symbol_name]
        
        pnl_current_trade = 0.0
        if current_trade["status"] == "OPEN" and current_trade["tradingsymbol"]:
            try:
                ltp_data = kite.ltp([f"NFO:{current_trade['tradingsymbol']}"])
                if f"NFO:{current_trade['tradingsymbol']}" in ltp_data and 'last_price' in ltp_data[f"NFO:{current_trade['tradingsymbol']}"]:
                    current_ltp = ltp_data[f"NFO:{current_trade['tradingsymbol']}"]['last_price']
                    
                    pnl_current_trade = (current_ltp - current_trade["entry_price"]) * current_trade["quantity"] \
                                        if current_trade["side"] == kite.TRANSACTION_TYPE_BUY else \
                                        (current_trade["entry_price"] - current_ltp) * current_trade["quantity"]
                    
                    current_trade["current_pnl"] = pnl_current_trade
                    current_trade["pnl_peak"] = max(current_trade["pnl_peak"], pnl_current_trade)
                else:
                    logger.warning(f"LTP not found for {current_trade['tradingsymbol']} for PnL. Skipping.")

            except NetworkException as e:
                logger.error(f"Network error getting LTP for PnL for {current_trade['tradingsymbol']}: {e}")
                tg(f"\u26a0\ufe0f Network error getting LTP for {current_trade['tradingsymbol']}.")
            except Exception as e:
                logger.error(f"Error updating PnL for {current_trade['tradingsymbol']}: {e}")

        effective_daily_pnl = daily_pnl[symbol_name] + pnl_current_trade
        
        if trading_active[symbol_name]:
            if effective_daily_pnl <= -MAX_DAILY_LOSS_PER_INDEX:
                tg(f"\u274c {symbol_name} Max Daily Loss hit! (\u20b9{effective_daily_pnl:.2f}). Stopping new trades for today.")
                logger.warning(f"{symbol_name} Max Daily Loss hit! (\u20b9{effective_daily_pnl:.2f}). Stopping new trades.")
                trading_active[symbol_name] = False
                if active_trades[symbol_name]["status"] == "OPEN":
                    square_off_single_position(symbol_name, "Daily Loss Limit Reached")

            elif effective_daily_pnl >= MAX_DAILY_PROFIT_PER_INDEX:
                tg(f"\u2705 {symbol_name} Max Daily Profit hit! (\u20b9{effective_daily_pnl:.2f}). Stopping new trades for today.")
                logger.info(f"{symbol_name} Max Daily Profit hit! (\u20b9{effective_daily_pnl:.2f}). Stopping new trades.")
                trading_active[symbol_name] = False
                if active_trades[symbol_name]["status"] == "OPEN":
                    square_off_single_position(symbol_name, "Daily Profit Limit Reached")
            
            if active_trades[symbol_name]["status"] == "OPEN" and active_trades[symbol_name]["pnl_peak"] > 0:
                if pnl_current_trade < active_trades[symbol_name]["pnl_peak"]:
                    drawdown = (active_trades[symbol_name]["pnl_peak"] - pnl_current_trade) / active_trades[symbol_name]["pnl_peak"]
                    if drawdown >= MAX_DAILY_DRAWDOWN_PERCENT:
                        tg(f"\u274c {symbol_name} Max Daily Drawdown ({drawdown:.2%}) from peak PnL (\u20b9{active_trades[symbol_name]['pnl_peak']:.2f}) hit! Squaring off.")
                        logger.warning(f"{symbol_name} Max Daily Drawdown ({drawdown:.2%}) hit! Squaring off.")
                        trading_active[symbol_name] = False
                        square_off_single_position(symbol_name, "Max Daily Drawdown Hit")
        
    for symbol_name, trade_info in active_trades.items():
        if trade_info["status"] == "OPEN" and trade_info["tradingsymbol"]:
            ts = trade_info["tradingsymbol"]
            instrument_type = trade_info["instrument_type"]
            
            entry_price = trade_info["entry_price"]
            target_price = trade_info["target_price"]
            sl_price = trade_info["sl_price"]
            qty = trade_info["quantity"]
            side = trade_info["side"]
            initial_sl_order_id = trade_info["initial_sl_order_id"]

            try:
                ltp_data = kite.ltp([f"NFO:{ts}"])
                if f"NFO:{ts}" not in ltp_data or 'last_price' not in ltp_data[f"NFO:{ts}"]:
                    logger.warning(f"Could not get LTP for {ts} during monitoring. Skipping this cycle.")
                    continue
                current_ltp = ltp_data[f"NFO:{ts}"]['last_price']
                
                # --- Correct Target Hit Logic based on instrument_type ---
                target_hit = False
                if instrument_type == 'CE' and current_ltp >= target_price:
                    target_hit = True
                elif instrument_type == 'PE' and current_ltp <= target_price:
                    target_hit = True

                if target_hit:
                    logger.info(f"{symbol_name} Target Hit for {ts}! Exiting at {current_ltp:.2f}.")
                    tg(f"\u2705 {symbol_name} Target Hit for {ts}! Exiting at \u20b9{current_ltp:.2f}.")
                    square_off_single_position(symbol_name, "Target Hit")
                    continue

                try:
                    order_status_info = kite.get_order_history(order_id=initial_sl_order_id)
                    if order_status_info and order_status_info[0]['status'] == 'COMPLETE':
                        exit_price = order_status_info[0]['average_price']
                        logger.warning(f"{symbol_name} SL Hit by Exchange for {ts}! Exited at {exit_price:.2f}.")
                        tg(f"\u274c {symbol_name} SL Hit by Exchange for {ts}! Exited at \u20b9{exit_price:.2f}.")
                        
                        pnl_on_exit = (exit_price - entry_price) * qty if side == kite.TRANSACTION_TYPE_BUY else (entry_price - exit_price) * qty
                        daily_pnl[symbol_name] += pnl_on_exit
                        active_trades[symbol_name]["total_pnl"] += pnl_on_exit
                        active_trades[symbol_name]["current_pnl"] = 0.0
                        active_trades[symbol_name]["status"] = "CLOSED"
                        active_trades[symbol_name]["tradingsymbol"] = None
                        active_trades[symbol_name]["instrument_token"] = None
                        active_trades[symbol_name]["instrument_type"] = None # Reset
                        logger.info(f"{symbol_name} trade closed. Daily PnL: \u20b9{daily_pnl[symbol_name]:.2f}")
                        tg(f"\u2139\ufe0f {symbol_name} trade closed. Daily PnL: \u20b9{daily_pnl[symbol_name]:.2f}")
                        save_state()
                        continue

                except (OrderException, NetworkException) as e:
                    logger.error(f"Error checking SL order status for {initial_sl_order_id}: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error checking SL order status for {initial_sl_order_id}: {e}")

                # Trailing SL Logic
                if side == kite.TRANSACTION_TYPE_BUY:
                    pnl_percent_current_trade = (current_ltp - entry_price) / entry_price
                    
                    if pnl_percent_current_trade >= TRAIL_SL_PERCENTAGE_GAIN:
                        # --- Use pre-fetched ATR ---
                        current_atr = current_atr_for_symbol.get(symbol_name)

                        if current_atr is not None and current_atr > 0:
                            new_trailing_sl = round(current_ltp - (TRAIL_SL_ATR_MULTIPLIER * current_atr), 1)
                            
                            current_trigger_price = 0.0
                            try:
                                sl_order_info = kite.get_order_history(order_id=initial_sl_order_id)
                                if sl_order_info and sl_order_info[0]['status'] in ['OPEN', 'AMO', 'TRIGGER_PENDING']:
                                    current_trigger_price = sl_order_info[0]['trigger_price']
                                else:
                                    logger.warning(f"SL order {initial_sl_order_id} for {ts} not modifiable. Status: {sl_order_info[0]['status'] if sl_order_info else 'N/A'}. Skipping TSL.")
                                    continue
                            except Exception as e:
                                logger.error(f"Could not fetch current trigger price for SL order {initial_sl_order_id}: {e}. Skipping TSL.")
                                continue
                            
                            if new_trailing_sl > current_trigger_price and new_trailing_sl > entry_price:
                                try:
                                    kite.modify_order(
                                        variety=kite.VARIETY_REGULAR, order_id=initial_sl_order_id,
                                        trigger_price=new_trailing_sl, order_type=kite.ORDER_TYPE_SLM,
                                        price=0.0
                                    )
                                    active_trades[symbol_name]["sl_price"] = new_trailing_sl
                                    logger.info(f"{symbol_name} TSL updated for {ts} to \u20b9{new_trailing_sl:.2f} (from \u20b9{current_trigger_price:.2f}).")
                                    tg(f"\u2139\ufe0f {symbol_name} TSL for {ts} \u2191 to \u20b9{new_trailing_sl:.2f}.")
                                    save_state()
                                except OrderException as e:
                                    logger.error(f"Failed to modify SL order {initial_sl_order_id} for {ts}: {e}")
                                    tg(f"\u26a0\ufe0f Failed to modify SL for {ts}: {e}")
                                except Exception as e:
                                    logger.error(f"Unexpected error modifying SL order {initial_sl_order_id}: {e}")
                                    tg(f"\u26a0\ufe0f Unexpected error modifying SL for {ts}: {e}")
                        else:
                            logger.warning(f"Invalid ATR for {symbol_name} TSL. Skipping. (ATR: {current_atr})")

            except NetworkException as e:
                logger.error(f"Network error monitoring position for {ts}: {e}")
                tg(f"\u26a0\ufe0f Network error monitoring {ts}.")
            except Exception as e:
                logger.error(f"Error monitoring position for {ts}: {e}")
                tg(f"\u26a0\ufe0f Error monitoring position for {ts}: {e}")
    save_state()

def square_off_single_position(symbol_name, reason="Unknown"):
    """Squares off a single active position for the given symbol."""
    trade_info = active_trades[symbol_name]
    if trade_info["status"] != "OPEN" or not trade_info["tradingsymbol"]:
        logger.info(f"No open position to square off for {symbol_name}.")
        return

    ts = trade_info["tradingsymbol"]
    qty = trade_info["quantity"]
    side = trade_info["side"]
    initial_sl_order_id = trade_info["initial_sl_order_id"]
    entry_price = trade_info["entry_price"]

    try:
        try:
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=initial_sl_order_id)
            logger.info(f"SL Order {initial_sl_order_id} cancelled for {ts}. Reason: {reason}.")
        except OrderException as cancel_ex:
            logger.warning(f"Failed to cancel SL order {initial_sl_order_id} for {ts}: {cancel_ex}. Already executed/cancelled?")
        except Exception as cancel_ex:
            logger.error(f"Error cancelling SL order {initial_sl_order_id} for {ts}: {cancel_ex}")

        exit_transaction_type = kite.TRANSACTION_TYPE_SELL if side == kite.TRANSACTION_TYPE_BUY else kite.TRANSACTION_TYPE_BUY
        exit_order_response = kite.place_order(
            variety=kite.VARIETY_REGULAR, exchange="NFO", tradingsymbol=ts,
            transaction_type=exit_transaction_type, quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET, product=kite.PRODUCT_NRML,
            tag=f"BREAKOUT_BOT_EXIT_{reason.replace(' ', '_').upper()}"
        )
        exit_order_id = exit_order_response.get('order_id')
        logger.info(f"Exit order placed for {ts} (ID: {exit_order_id}). Reason: {reason}")
        
        time.sleep(2)

        exit_price = entry_price # Default in case fetch fails
        try:
            exit_order_history = kite.get_order_history(order_id=exit_order_id)
            if exit_order_history and exit_order_history[0]['status'] == 'COMPLETE':
                exit_price = exit_order_history[0]['average_price']
                logger.info(f"Exit order {exit_order_id} for {ts} filled at {exit_price:.2f}.")
            else:
                logger.warning(f"Exit order {exit_order_id} for {ts} not complete. Status: {exit_order_history[0]['status'] if exit_order_history else 'N/A'}.")
                ltp_data = kite.ltp([f"NFO:{ts}"])
                if f"NFO:{ts}" in ltp_data and 'last_price' in ltp_data[f"NFO:{ts}"]:
                    exit_price = ltp_data[f"NFO:{ts}"]['last_price']
                else:
                    logger.error(f"Could not get LTP for {ts} after exit order. PnL may be inaccurate.")

        except (OrderException, NetworkException) as e:
            logger.error(f"Error fetching exit order history for {exit_order_id}: {e}. PnL may be inaccurate.")
            try:
                ltp_data = kite.ltp([f"NFO:{ts}"])
                if f"NFO:{ts}" in ltp_data and 'last_price' in ltp_data[f"NFO:{ts}"]:
                    exit_price = ltp_data[f"NFO:{ts}"]['last_price']
            except Exception:
                pass

        pnl_on_exit = (exit_price - entry_price) * qty if side == kite.TRANSACTION_TYPE_BUY else (entry_price - exit_price) * qty
        daily_pnl[symbol_name] += pnl_on_exit
        active_trades[symbol_name]["total_pnl"] += pnl_on_exit
        active_trades[symbol_name]["current_pnl"] = 0.0
        
        active_trades[symbol_name]["status"] = "CLOSED"
        active_trades[symbol_name]["tradingsymbol"] = None
        active_trades[symbol_name]["instrument_token"] = None
        active_trades[symbol_name]["instrument_type"] = None # Reset
        active_trades[symbol_name]["initial_sl_order_id"] = None
        active_trades[symbol_name]["pnl_peak"] = 0.0

        logger.info(f"{symbol_name} CLOSED ({reason}). PnL: \u20b9{pnl_on_exit:.2f}. Daily PnL: \u20b9{daily_pnl[symbol_name]:.2f}")
        tg(f"\u2705 {symbol_name} CLOSED! Reason: {reason}. PnL: \u20b9{pnl_on_exit:.2f}. Daily PnL: \u20b9{daily_pnl[symbol_name]:.2f}")
        save_state()

    except (OrderException, NetworkException, InputException) as e:
        logger.error(f"Square-off API Error for {ts} ({reason}): {e}")
        tg(f"\u274c Square-off API Error for {ts} ({reason}): {e}")
    except Exception as e:
        logger.error(f"Unexpected Error during square_off_single_position for {ts} ({reason}): {e}")
        tg(f"\u274c Unexpected Error squaring off {ts}: {e}")

def square_off_all_positions():
    """
    Squares off all currently open positions before market close.
    """
    tg("\ud83d\udd1a Market close approaching. Squaring off all open positions.")
    logger.info("Market close approaching. Squaring off all open positions.")
    for symbol_name in ["NIFTY", "BANKNIFTY"]:
        if active_trades[symbol_name]["status"] == "OPEN":
            square_off_single_position(symbol_name, "End of Day")
    tg("\u23f0 All positions squared off for the day.")
    logger.info("All positions squared off for the day.")
    save_state()

# === Main Breakout/Breakdown Strategy Logic ===
def check_breakout_breakdown():
    """
    Core strategy logic to identify breakout/breakdown signals and place trades.
    Includes PnL limits, volume confirmation, and risk-based position sizing.
    """
    # index_map is now a global variable populated at startup, no need to redefine or global it here.

    if user_equity <= 0:
        logger.warning("User equity is 0 or less. Cannot calculate position size. Skipping signal check.")
        tg("\u26a0\ufe0f Bot has no equity data. Skipping new trades.")
        return

    for symbol_name, info in index_map.items(): # Use the globally populated index_map
        if not trading_active[symbol_name]:
            logger.info(f"{symbol_name} trading disabled due to PnL limits. Skipping new entry.")
            continue
        
        if trade_cnt[symbol_name] >= MAX_TRADES:
            logger.info(f"{symbol_name} trade entry limit ({MAX_TRADES}) reached for today. Skipping new entry.")
            continue
        
        if active_trades[symbol_name]["status"] == "OPEN":
            logger.info(f"{symbol_name} already has an active trade. Skipping new entry.")
            continue

        index_token = info["token"]
        futures_token_for_volume = info["futures_token"]
        # option_df = info["df"] # Not directly used in this function, but available
        base_lot_size = info["base_lot_size"]

        df_candles = fetch_candles(index_token, futures_token_for_volume)
        if df_candles.empty:
            logger.warning(f"Insufficient historical data for {symbol_name}. Skipping signal check.")
            continue

        curr_close = df_candles['close'].iloc[-1]
        current_volume = df_candles['volume'].iloc[-1]
        
        if any(pd.isna(df_candles[col].iloc[-1]) for col in ['EMA_FAST', 'EMA_SLOW', 'ADX', 'RSI', 'ATR']):
            logger.warning(f"{symbol_name} - Latest indicator values are NaN. Skipping signal check.")
            continue

        ema_fast = df_candles['EMA_FAST'].iloc[-1]
        ema_slow = df_candles['EMA_SLOW'].iloc[-1]
        adx_val = df_candles['ADX'].iloc[-1]
        rsi_val = df_candles['RSI'].iloc[-1]
        atr_val = df_candles['ATR'].iloc[-1]

        if len(df_candles) < BREAKOUT_LOOKBACK_CANDLES + 1:
             logger.warning(f"{symbol_name} - Not enough candles for breakout lookback. Skipping.")
             continue

        lookback_candles_for_levels = df_candles.iloc[-(BREAKOUT_LOOKBACK_CANDLES + 1):-1]
        if lookback_candles_for_levels.empty:
            logger.warning(f"Not enough lookback candles for {symbol_name}. Skipping.")
            continue

        breakout_resistance = lookback_candles_for_levels['high'].max()
        breakdown_support = lookback_candles_for_levels['low'].min()
        
        historical_volumes = df_candles['volume'].iloc[:-1]
        avg_volume = historical_volumes.mean() if not historical_volumes.empty and historical_volumes.sum() > 0 else 0

        logger.info(f"\n--- {symbol_name} Signal Check ({datetime.datetime.now().time().strftime('%H:%M')}) ---")
        logger.info(f"Close: {curr_close:.2f} | R: {breakout_resistance:.2f}, S: {breakdown_support:.2f} | EMA: {ema_fast:.2f}/{ema_slow:.2f} | ADX: {adx_val:.2f}, RSI: {rsi_val:.2f}, ATR: {atr_val:.2f} | Daily PnL: \u20b9{daily_pnl[symbol_name]:.2f}")
        
        if adx_val < ADX_THRESHOLD:
            logger.info(f"{symbol_name} skipped. ADX ({adx_val:.2f}) below threshold {ADX_THRESHOLD}.")
            continue
        
        if rsi_val > RSI_OVERBOUGHT or rsi_val < RSI_OVERSOLD:
            logger.info(f"{symbol_name} skipped. RSI ({rsi_val:.2f}) extreme.")
            continue

        volume_condition_met = False
        if avg_volume > 0:
            if current_volume >= avg_volume * VOLUME_MULTIPLIER:
                volume_condition_met = True
        elif current_volume > 0:
             volume_condition_met = True
        
        if not volume_condition_met:
            logger.info(f"{symbol_name} skipped. Volume not confirming (Curr: {current_volume}, Avg: {avg_volume:.0f}).")
            continue

        selected_option_details = None
        trade_side = None
        instrument_type_selected = None

        if curr_close > breakout_resistance and ema_fast > ema_slow:
            logger.info(f"\ud83d\ude80 {symbol_name} Bullish Breakout Signal detected (Close: {curr_close:.2f} > R: {breakout_resistance:.2f}).")
            selected_option_details = get_atm_option_details(symbol_name, curr_close, 'CE')
            trade_side = kite.TRANSACTION_TYPE_BUY
            instrument_type_selected = 'CE'

        elif curr_close < breakdown_support and ema_fast < ema_slow:
            logger.info(f"\ud83d\ude80 {symbol_name} Bearish Breakdown Signal detected (Close: {curr_close:.2f} < S: {breakdown_support:.2f}).")
            selected_option_details = get_atm_option_details(symbol_name, curr_close, 'PE')
            trade_side = kite.TRANSACTION_TYPE_BUY
            instrument_type_selected = 'PE'

        if selected_option_details is not None and selected_option_details['ltp'] > 0 and trade_side:
            ts = selected_option_details['tradingsymbol']
            op_token = selected_option_details['instrument_token']
            ltp = selected_option_details['ltp']
            
            if ltp * SL_PERCENTAGE_INITIAL <= 0:
                logger.warning(f"Calculated SL for {ts} is zero or negative. Skipping trade.")
                tg(f"\u26a0\ufe0f Invalid SL for {ts}. Skipping trade.")
                continue

            risk_per_share = ltp * SL_PERCENTAGE_INITIAL
            if risk_per_share == 0:
                logger.warning(f"Calculated risk per share for {ts} is zero. Skipping trade.")
                tg(f"\u26a0\ufe0f Zero risk per share for {ts}. Skipping trade.")
                continue

            risk_amount = user_equity * RISK_PERCENTAGE_PER_TRADE
            calculated_qty = int(risk_amount / risk_per_share)
            
            qty = (calculated_qty // base_lot_size) * base_lot_size
            
            if qty == 0:
                logger.warning(f"Calculated quantity is 0 for {ts}. Skipping trade.")
                tg(f"\u26a0\ufe0f Calculated quantity 0 for {ts}. Skipping trade.")
                continue
            
            logger.info(f"Position Sizing for {ts}: Equity: \u20b9{user_equity:,.2f}, Risk: \u20b9{risk_amount:.2f}, Qty: {qty} ({ltp:.2f} @ \u20b9{risk_per_share:.2f} SL)")

            place_order(ts, op_token, instrument_type_selected, trade_side, qty, ltp, symbol_name)
        else:
            logger.info(f"{symbol_name} - No valid ATM option found or LTP is zero. Skipping trade.")

    save_state()

# === Scheduler Setup ===
# Main execution flow (moved/modified for correct initialization order)
if test_connection_and_get_equity():
    load_initial_instruments()

    # --- Populate index_map here, AFTER instruments are loaded ---
    index_map = {
        "NIFTY": {"token": 256265, "df": nifty_options, "futures_token": nifty_futures_token, "base_lot_size": 50},
        "BANKNIFTY": {"token": 260105, "df": banknifty_options, "futures_token": banknifty_futures_token, "base_lot_size": 15}
    }
    # --- END: Populate index_map ---

    load_state() # This also re-fetches equity on daily reset

    # --- Initial Caching of Option Chains (Crucial for get_atm_option_details) ---
    fetch_and_cache_option_chain_symbols("NIFTY", nifty_options)
    fetch_and_cache_option_chain_symbols("BANKNIFTY", banknifty_options)
    # --- END: Initial Caching ---

else:
    raise SystemExit("Kite connection failed or equity fetch failed. Exiting.")


# --- Diagnostic and Scheduling Calls ---
logger.info(f"Type of 'my_monitor_and_manage_positions': {type(my_monitor_and_manage_positions)}")
logger.info(f"Is 'my_monitor_and_manage_positions' callable? {callable(my_monitor_and_manage_positions)}")

try:
    logger.info("Attempting direct call to my_monitor_and_manage_positions...")
    my_monitor_and_manage_positions() # This will execute the function one time
    logger.info("Direct call to my_monitor_and_manage_positions successful.")
except Exception as e:
    logger.critical(f"Direct call to my_monitor_and_manage_positions FAILED: {e}")
    raise # Re-raise the exception to see its full traceback immediately


schedule.every(5).minutes.at(":05").do(check_breakout_breakdown)
schedule.every(1).minute.do(lambda: my_monitor_and_manage_positions())

schedule.every().day.at("15:25").do(square_off_all_positions) # Assuming market close is 15:30 IST
schedule.every().day.at("09:00").do(reset_daily_state) # Reset state before market open (assuming 9:15 open)
schedule.every(5).minutes.do(save_state)
schedule.every(60).minutes.do(lambda: tg("\u23f0 Bot heartbeat: I'm still running!"))

logger.info("Scheduler set up. Bot is now running and awaiting market events.")

while True:
    schedule.run_pending()
    time.sleep(1)
