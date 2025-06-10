import pandas as pd
import numpy as np
import datetime, time, schedule, requests, os
from kiteconnect import KiteConnect

# === Credentials ===
api_key             = "xdvu36rtx33987xc"
api_secret          = "qOmM2PijRwX54a5EVyXW6g0NTz19A2Hm"
access_token        = "qOmM2PijRwX54a5EVyXW6g0NTz19A2Hm" # <<< CRITICAL: Your access token will expire daily.
                                                    # You need a mechanism to refresh this daily.
                                                    # This hardcoded token will fail tomorrow!
telegram_bot_token = "7301084716:AAGjxk1c4CLWLC0tXp_rqdvfqsdsdoxWZ0KG8" # Replace with your bot token
telegram_chat_id    = "6544776630" # Replace with your chat ID

# === Parameters ===
LOT_MULTIPLIER = 1 # Multiplier for the default lot size of the futures contract
MAX_TRADES_PER_SYMBOL = 1 # Allow only one open trade per symbol at a time

# Indicator Parameters
EMA_FAST = 14
EMA_SLOW = 200
ADX_THRESHOLD = 25
CANDLE_INTERVAL = "5minute" # KiteConnect interval format
LOOKBACK_MINUTES = 5500 # Increased further to ensure enough usable candles after dropna

# Risk Management Parameters (Dynamically adjusted using ATR)
ATR_SL_MULTIPLIER = 2.0  # Stop Loss at 2.0 * ATR from entry price
ATR_TARGET_MULTIPLIER = 4.0 # Target at 4.0 * ATR from entry price (Risk-Reward 1:2 based on ATR)

# === Kite Connect Setup ===
kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

# --- Telegram Helper ---
def tg(msg):
    try:
        # Filter out non-printable characters and surrogates
        safe_msg = ''.join(c for c in msg if c.isprintable() and not (0xD800 <= ord(c) <= 0xDFFF))
        requests.get(f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage",
                     params={"chat_id": telegram_chat_id, "text": safe_msg}, timeout=4)
    except Exception as e:
        print(f"[Telegram] Error sending message: {e}")

# --- Test Kite Connection ---
def test_connection():
    try:
        profile = kite.profile()
        name = profile.get("user_name", "Unknown User")
        tg(f"✅ Zerodha API Connected! User: {name} (Broker: {profile.get('broker')})")
        print(f"✅ Zerodha API Connected! User: {name}")
        return True
    except Exception as ex:
        tg(f"❌ Zerodha Connection Error: {ex}. Please check access token. (Current Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')})")
        print(f"❌ Zerodha Connection Error: {ex}")
        return False

if test_connection():
    # Fetch all MCX instruments once at startup
    instruments_df = pd.DataFrame(kite.instruments("MCX"))
    if instruments_df.empty:
        raise SystemExit("Failed to fetch MCX instruments. Check connection or API permissions.")
else:
    raise SystemExit("Kite connection failed. Exiting.")

# === Dynamic Token Fetch (Futures only for this strategy) ===
def get_latest_futures_contract_details():
    latest = {}
    for symbol_base in ["GOLDM", "SILVERM", "CRUDEOIL"]:
        # Filter for FUTURES, containing the base symbol, and sort by expiry
        filtered = instruments_df[
            (instruments_df['tradingsymbol'].str.contains(symbol_base)) &
            (instruments_df['instrument_type'] == 'FUT')
        ].sort_values('expiry')

        if not filtered.empty:
            row = filtered.iloc[0] # Get the nearest expiry
            latest[symbol_base] = {
                'instrument_token': row['instrument_token'],
                'tradingsymbol': row['tradingsymbol'],
                'lot_size': row['lot_size'],
                'tick_size': row['tick_size'] # Added tick_size for rounding SL/TP
            }
        else:
            print(f"Warning: No futures contract found for {symbol_base}")
            tg(f"⚠️ Warning: No futures contract found for {symbol_base}")
    return latest

commodities_data = get_latest_futures_contract_details()

# --- Print the Contracts Being Considered ---
print("\n--- Futures Contracts Being Considered ---")
tg("✨ Futures contracts in scope:")
if commodities_data:
    for base_symbol, details in commodities_data.items():
        contract_info = f"  - {base_symbol}: {details['tradingsymbol']} (Token: {details['instrument_token']}, Lot: {details['lot_size']}, Tick: {details['tick_size']})"
        print(contract_info)
        tg(f"- {details['tradingsymbol']}")
else:
    print("No futures contracts found to consider.")
    tg("No futures contracts found.")
print("------------------------------------------\n")

# === Trade State Management ===
# Using a dictionary to track open positions for each commodity
open_positions = {}
daily_trade_count = {key: 0 for key in commodities_data.keys()} # To track trades per day per symbol

def reset_daily_trade_count():
    global daily_trade_count
    for key in daily_trade_count.keys():
        daily_trade_count[key] = 0
    tg("🔄 Daily trade counters reset.")
    print("🔄 Daily trade counters reset.")

# === Session-based Liquidity Contrast ===
def session_liquidity_check(df):
    """
    Checks if the last 5 candles show significant volume contrast.
    Returns (bool, message)
    """
    try:
        if df.empty or len(df) < 5:
            return False, "Not enough data for liquidity check"

        last_5 = df.tail(5)
        up_vol = last_5[last_5['close'] > last_5['open']]['volume'].sum()
        down_vol = last_5[last_5['close'] < last_5['open']]['volume'].sum()
        total_vol = last_5['volume'].sum()

        if total_vol == 0:
            return False, "No volume in last 5 candles."

        contrast = abs(up_vol - down_vol) / total_vol
        bias = "BUYERS" if up_vol > down_vol else "SELLERS"
        is_valid = contrast > 0.25 # Tune this threshold as needed

        return is_valid, f"{bias} dominate ({contrast:.2%}). Total volume: {total_vol}"
    except Exception as e:
        return False, f"Liquidity check error: {e}"

# === Indicator Calculation ===
def fetch_candles(instrument_token):
    """
    Fetches historical data and calculates EMAs, ADX, and RSI.
    Returns a DataFrame with calculated indicators.
    """
    try:
        from_dt = datetime.datetime.now() - datetime.timedelta(minutes=LOOKBACK_MINUTES)
        to_dt = datetime.datetime.now()

        data = kite.historical_data(instrument_token, from_dt, to_dt, CANDLE_INTERVAL)
        if not data:
            print(f"No historical data found for token {instrument_token} in the last {LOOKBACK_MINUTES} minutes.")
            return pd.DataFrame()

        df = pd.DataFrame(data)
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)

        df['EMA_FAST'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
        df['EMA_SLOW'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

        high_minus_low = df['high'] - df['low']
        high_minus_prev_close = abs(df['high'] - df['close'].shift())
        low_minus_prev_close = abs(df['low'] - df['close'].shift())
        df['TR'] = pd.DataFrame({'a': high_minus_low, 'b': high_minus_prev_close, 'c': low_minus_prev_close}).max(axis=1)

        df['ATR'] = df['TR'].rolling(window=14).mean()

        plus_dm_raw = df['high'] - df['high'].shift(1)
        minus_dm_raw = df['low'].shift(1) - df['low']

        df['+DM'] = np.where(plus_dm_raw > minus_dm_raw, np.maximum(plus_dm_raw, 0), 0)
        df['-DM'] = np.where(minus_dm_raw > plus_dm_raw, np.maximum(minus_dm_raw, 0), 0)

        df['+DI'] = 100 * (df['+DM'].rolling(window=14).sum() / df['ATR'])
        df['-DI'] = 100 * (df['-DM'].rolling(window=14).sum() / df['ATR'])

        df['DX'] = (abs(df['+DI'] - df['-DI']) / (df['+DI'] + df['-DI'])).replace([np.inf, -np.inf], np.nan) * 100
        df['ADX'] = df['DX'].rolling(window=14).mean()

        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=13, adjust=False).mean()
        avg_loss = loss.ewm(com=13, adjust=False).mean()
        rs = avg_gain / avg_loss
        df['RSI'] = 100 - (100 / (1 + rs))

        df.dropna(inplace=True) # Drop any rows with NaN from indicator calculation

        required_usable_candles = EMA_SLOW + 5 # 200 for EMA_SLOW, +5 for safety/last candle checks
        if len(df) < required_usable_candles:
            print(f"Not enough *usable* data points ({len(df)}) for analysis for token {instrument_token}. Requires at least {required_usable_candles}.")
            return pd.DataFrame()

        return df
    except Exception as e:
        print(f"[Indicator Fetch/Calc Error] for token {instrument_token}: {e}")
        tg(f"⚠️ Indicator error for token {instrument_token}: {e}")
        return pd.DataFrame()


# === Order Placement (Revised for proper entry/position tracking and ATR-based SL/TP) ===
def place_order(tradingsymbol, transaction_type, quantity, entry_price, position_type, symbol_base, atr_at_entry, tick_size):
    """
    Places an entry order and updates the global open_positions.
    Calculates and stores ATR-based SL/Target prices.
    """
    try:
        ltp_data = kite.ltp(f"MCX:{tradingsymbol}")
        if f"MCX:{tradingsymbol}" not in ltp_data:
            tg(f"❌ Failed to get LTP for {tradingsymbol}. Skipping order.")
            return

        current_market_price = ltp_data[f"MCX:{tradingsymbol}"]['last_price']
        if current_market_price == 0:
            tg(f"❌ Invalid LTP for {tradingsymbol} ({current_market_price}). Skipping order.")
            return

        # Round price to nearest tick size
        def round_to_tick(price, tick):
            return round(price / tick) * tick

        # Calculate ATR-based SL and Target
        if position_type == 'long':
            stop_loss_price = round_to_tick(current_market_price - (atr_at_entry * ATR_SL_MULTIPLIER), tick_size)
            target_price = round_to_tick(current_market_price + (atr_at_entry * ATR_TARGET_MULTIPLIER), tick_size)
        else: # 'short'
            stop_loss_price = round_to_tick(current_market_price + (atr_at_entry * ATR_SL_MULTIPLIER), tick_size)
            target_price = round_to_tick(current_market_price - (atr_at_entry * ATR_TARGET_MULTIPLIER), tick_size)

        # Ensure SL/TP are not negative (especially for short positions if price goes near zero)
        stop_loss_price = max(0.01, stop_loss_price)
        target_price = max(0.01, target_price)


        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange="MCX",
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=kite.ORDER_TYPE_LIMIT, # Using LIMIT for entry at current market price for simplicity
            price=round(current_market_price, 2),
            product=kite.PRODUCT_NRML,
            tag=f"Entry_{symbol_base}"
        )

        if order_id:
            open_positions[symbol_base] = {
                'tradingsymbol': tradingsymbol,
                'position': position_type,
                'entry_price': current_market_price,
                'quantity': quantity,
                'order_id': order_id, # This is the entry order ID, not SL/TP
                'time': datetime.datetime.now(),
                'stop_loss_price': stop_loss_price,
                'target_price': target_price,
                'atr_at_entry': atr_at_entry # Store ATR for reference
            }
            tg(f"✅ Entry Order Placed! ID: {order_id}\n{tradingsymbol} {position_type.upper()} Qty: {quantity}, Entry: ₹{current_market_price:.2f}\nSL: ₹{stop_loss_price:.2f}, Target: ₹{target_price:.2f} (ATR: {atr_at_entry:.2f})")
            print(f"✅ Entry Order Placed! ID: {order_id}\n{tradingsymbol} {position_type.upper()} Qty: {quantity}, Entry: ₹{current_market_price:.2f}\nSL: ₹{stop_loss_price:.2f}, Target: ₹{target_price:.2f} (ATR: {atr_at_entry:.2f})")
        else:
            tg(f"❌ Order placement failed for {tradingsymbol}. No order ID received.")
            print(f"❌ Order placement failed for {tradingsymbol}.")

    except Exception as e:
        tg(f"❌ Order placement error for {tradingsymbol}: {e}")
        print(f"❌ Order placement error for {tradingsymbol}: {e}")

def close_position(tradingsymbol, transaction_type, quantity, position_type, symbol_base, exit_reason="signal_reversal"):
    """
    Closes an existing position and removes it from global open_positions.
    """
    try:
        ltp_data = kite.ltp(f"MCX:{tradingsymbol}")
        if f"MCX:{tradingsymbol}" not in ltp_data:
            tg(f"❌ Failed to get LTP for {tradingsymbol} to close position. Skipping.")
            return
        current_market_price = ltp_data[f"MCX:{tradingsymbol}"]['last_price']
        if current_market_price == 0:
            tg(f"❌ Invalid LTP for {tradingsymbol} to close position ({current_market_price}). Skipping.")
            return

        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange="MCX",
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=kite.ORDER_TYPE_MARKET, # Using MARKET order for quick exit
            product=kite.PRODUCT_NRML,
            tag=f"Exit_{symbol_base}_{exit_reason}"
        )

        if order_id:
            entry_data = open_positions.get(symbol_base, {})
            entry_price = entry_data.get('entry_price', 'N/A')
            profit_loss = (current_market_price - entry_price) * quantity if position_type == 'long' else (entry_price - current_market_price) * quantity

            tg(f"✅ Exit Order Placed! ID: {order_id}\n{tradingsymbol} {position_type.upper()} Closed at: ₹{current_market_price:.2f} (Reason: {exit_reason})\nP&L: ₹{profit_loss:.2f}")
            print(f"✅ Exit Order Placed! ID: {order_id}\n{tradingsymbol} {position_type.upper()} Closed at: ₹{current_market_price:.2f} (Reason: {exit_reason})\nP&L: ₹{profit_loss:.2f}")
            if symbol_base in open_positions:
                del open_positions[symbol_base]
        else:
            tg(f"❌ Exit order failed for {tradingsymbol}. No order ID received.")
            print(f"❌ Exit order failed for {tradingsymbol}.")

    except Exception as e:
        tg(f"❌ Error closing position for {tradingsymbol}: {e}")
        print(f"❌ Error closing position for {tradingsymbol}: {e}")

# === New: Monitor Open Positions for SL/Target ===
def monitor_open_positions():
    if not open_positions:
        return # No open positions to monitor

    # Get current LTP for all currently open positions
    tokens_to_fetch = [f"MCX:{pos_data['tradingsymbol']}" for pos_data in open_positions.values()]
    try:
        ltp_data_all = kite.ltp(tokens_to_fetch)
    except Exception as e:
        print(f"[LTP Fetch Error in Monitor] {e}")
        return # Skip this cycle if LTP fetch fails

    positions_to_close = []
    for symbol_base, pos_data in open_positions.items():
        tradingsymbol = pos_data['tradingsymbol']
        position_type = pos_data['position']
        stop_loss_price = pos_data['stop_loss_price']
        target_price = pos_data['target_price']
        quantity = pos_data['quantity']

        current_ltp = ltp_data_all.get(f"MCX:{tradingsymbol}", {}).get('last_price')

        if current_ltp is None or current_ltp == 0:
            print(f"Warning: Could not get valid LTP for {tradingsymbol} in monitor. Skipping.")
            continue

        # Check for Stop Loss hit
        if position_type == 'long' and current_ltp <= stop_loss_price:
            print(f"Long position SL hit for {tradingsymbol}. LTP: {current_ltp}, SL: {stop_loss_price}")
            positions_to_close.append((tradingsymbol, kite.TRANSACTION_TYPE_SELL, quantity, 'long', symbol_base, "SL_HIT"))
        elif position_type == 'short' and current_ltp >= stop_loss_price:
            print(f"Short position SL hit for {tradingsymbol}. LTP: {current_ltp}, SL: {stop_loss_price}")
            positions_to_close.append((tradingsymbol, kite.TRANSACTION_TYPE_BUY, quantity, 'short', symbol_base, "SL_HIT"))

        # Check for Target hit (only if SL not hit in the same check cycle)
        if (position_type == 'long' and current_ltp >= target_price) and not (position_type == 'long' and current_ltp <= stop_loss_price):
             print(f"Long position Target hit for {tradingsymbol}. LTP: {current_ltp}, Target: {target_price}")
             positions_to_close.append((tradingsymbol, kite.TRANSACTION_TYPE_SELL, quantity, 'long', symbol_base, "TARGET_HIT"))
        elif (position_type == 'short' and current_ltp <= target_price) and not (position_type == 'short' and current_ltp >= stop_loss_price):
             print(f"Short position Target hit for {tradingsymbol}. LTP: {current_ltp}, Target: {target_price}")
             positions_to_close.append((tradingsymbol, kite.TRANSACTION_TYPE_BUY, quantity, 'short', symbol_base, "TARGET_HIT"))

    for args in positions_to_close:
        close_position(*args)


# === Trade Entry/Exit Logic ===
def check_entry_and_manage_positions():
    now = datetime.datetime.now().time()
    # Define your trading hours for MCX (Adjust these timings precisely as per MCX market hours for your commodities)
    if not ((datetime.time(9, 0) <= now <= datetime.time(23, 30)) or
            (datetime.time(0, 0) <= now <= datetime.time(0, 30))):
        return

    for symbol_base, details in commodities_data.items():
        instrument_token = details['instrument_token']
        tradingsymbol = details['tradingsymbol']
        lot_size = details['lot_size']
        tick_size = details['tick_size']

        try:
            df = fetch_candles(instrument_token)
            if df.empty:
                print(f"Skipping {symbol_base}: No valid data received for analysis.")
                continue

            if len(df) < 2:
                print(f"Skipping {symbol_base}: Not enough processed data for current analysis.")
                continue

            curr_close = df['close'].iloc[-1]
            prev_close = df['close'].iloc[-2]
            ema_fast = df['EMA_FAST'].iloc[-1]
            ema_slow = df['EMA_SLOW'].iloc[-1]
            prev_ema_fast = df['EMA_FAST'].iloc[-2]
            prev_ema_slow = df['EMA_SLOW'].iloc[-2]
            adx_val = df['ADX'].iloc[-1]
            rsi_val = df['RSI'].iloc[-1]
            atr_val = df['ATR'].iloc[-1] # Get current ATR for dynamic SL/Target
            recent_high = df['high'].iloc[-5:].max()
            recent_low = df['low'].iloc[-5:].min()


            liquidity_ok, liq_msg = session_liquidity_check(df)
            if not liquidity_ok:
                tg(f"⚠️ {symbol_base} skipped: {liq_msg}. (Low liquidity/bias).")
                print(f"⚠️ {symbol_base} skipped: {liq_msg}.")
                continue

            if adx_val < ADX_THRESHOLD:
                tg(f"🔍 {symbol_base} skipped. ADX ({adx_val:.2f}) below threshold {ADX_THRESHOLD}.")
                print(f"🔍 {symbol_base} skipped. ADX={adx_val:.2f}, RSI={rsi_val:.2f}. (No strong trend).")
                continue

            if 45 < rsi_val < 55:
                tg(f"🔍 {symbol_base} skipped. RSI ({rsi_val:.2f}) in neutral zone.")
                print(f"🔍 {symbol_base} skipped. RSI={rsi_val:.2f}. (Neutral momentum).")
                continue

            buy_signal_triggered = (prev_ema_fast <= prev_ema_slow) and \
                                   (ema_fast > ema_slow) and \
                                   (curr_close > recent_high) and \
                                   (rsi_val > 55)

            sell_signal_triggered = (prev_ema_fast >= prev_ema_slow) and \
                                    (ema_fast < ema_slow) and \
                                    (curr_close < recent_low) and \
                                    (rsi_val < 45)

            current_position_status = open_positions.get(symbol_base, {'position': 'none'})['position']

            # Exit based on signal reversal (independent of ATR-based SL/Target)
            if current_position_status == 'long' and sell_signal_triggered:
                tg(f"✖️ Closing LONG position for {tradingsymbol} due to Death Cross!")
                print(f"✖️ Closing LONG position for {tradingsymbol} due to Death Cross!")
                close_position(tradingsymbol, kite.TRANSACTION_TYPE_SELL, lot_size * LOT_MULTIPLIER, 'long', symbol_base, "SIGNAL_REVERSAL")
                return # Exit this position and move to next symbol, don't check for entry immediately

            elif current_position_status == 'short' and buy_signal_triggered:
                tg(f"✖️ Closing SHORT position for {tradingsymbol} due to Golden Cross!")
                print(f"✖️ Closing SHORT position for {tradingsymbol} due to Golden Cross!")
                close_position(tradingsymbol, kite.TRANSACTION_TYPE_BUY, lot_size * LOT_MULTIPLIER, 'short', symbol_base, "SIGNAL_REVERSAL")
                return # Exit this position and move to next symbol, don't check for entry immediately


            # Entry Logic
            if current_position_status == 'none':
                if daily_trade_count[symbol_base] >= MAX_TRADES_PER_SYMBOL:
                    print(f"Max trades ({MAX_TRADES_PER_SYMBOL}) reached for {symbol_base} today.")
                    continue

                if buy_signal_triggered:
                    tg(f"📈 BUY Signal for {tradingsymbol}!")
                    print(f"📈 BUY Signal for {tradingsymbol}!")
                    place_order(tradingsymbol, kite.TRANSACTION_TYPE_BUY, lot_size * LOT_MULTIPLIER, curr_close, 'long', symbol_base, atr_val, tick_size)
                    daily_trade_count[symbol_base] += 1
                elif sell_signal_triggered:
                    tg(f"📉 SELL (Short) Signal for {tradingsymbol}!")
                    print(f"📉 SELL (Short) Signal for {tradingsymbol}!")
                    place_order(tradingsymbol, kite.TRANSACTION_TYPE_SELL, lot_size * LOT_MULTIPLIER, curr_close, 'short', symbol_base, atr_val, tick_size)
                    daily_trade_count[symbol_base] += 1

        except Exception as e:
            tg(f"⚠️ {symbol_base} main logic error: {e}")
            print(f"⚠️ {symbol_base} main logic error: {e}")


# === Scheduler ===
tg("🚀 Gold/SilverM/Crude Oil Directional Trading Bot Started")
reset_daily_trade_count() # Reset counts at startup

schedule.every(1).minutes.do(check_entry_and_manage_positions) # Check for entry/exit signals every minute
schedule.every(1).minutes.do(monitor_open_positions) # Check SL/Target for open positions every minute
schedule.every().day.at("09:00").do(reset_daily_trade_count)

# --- Main Loop ---
print("Bot running... monitoring market.")
while True:
    schedule.run_pending()
    time.sleep(1)
