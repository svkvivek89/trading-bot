import json
import requests
from datetime import datetime
from pathlib import Path

# --- Twilio WhatsApp Configuration (placeholders) ---
TWILIO_ACCOUNT_SID = "YOUR_ACCOUNT_SID"
TWILIO_AUTH_TOKEN = "YOUR_AUTH_TOKEN"
WHATSAPP_FROM = "whatsapp:+14155238886"  # Twilio sandbox number
WHATSAPP_TO = "whatsapp:+911234567890"  # Replace with your WhatsApp number

# File to persist previous OI values for delta calculation
STATE_FILE = Path("oi_state.json")

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}


def fetch_oi(symbol: str) -> int:
    """Fetch total open interest for the given index symbol from NSE."""
    url = f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol.upper()}"
    r = requests.get(url, headers=NSE_HEADERS, timeout=10)
    r.raise_for_status()
    data = r.json()

    total_oi = 0
    for item in data.get("records", {}).get("data", []):
        ce = item.get("CE")
        pe = item.get("PE")
        if ce:
            total_oi += ce.get("openInterest", 0)
        if pe:
            total_oi += pe.get("openInterest", 0)
    return total_oi


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def send_whatsapp_message(message: str) -> None:
    """Send a WhatsApp message using Twilio's API."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    payload = {
        "From": WHATSAPP_FROM,
        "To": WHATSAPP_TO,
        "Body": message,
    }
    requests.post(url, data=payload, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)


def main():
    state = load_state()
    results = {}

    for symbol in ["NIFTY", "SENSEX"]:
        try:
            oi = fetch_oi(symbol)
        except Exception as exc:
            print(f"Failed to fetch OI for {symbol}: {exc}")
            continue

        prev = state.get(symbol, 0)
        delta = oi - prev
        state[symbol] = oi
        results[symbol] = {"oi": oi, "delta": delta}

    save_state(state)

    if not results:
        return

    msg_lines = [f"{symbol}: OI {info['oi']}, Δ {info['delta']}" for symbol, info in results.items()]
    # Simple decision logic: buy if delta is positive else sell
    decisions = []
    for symbol, info in results.items():
        decision = "BUY" if info["delta"] > 0 else "SELL"
        decisions.append(f"{symbol}: {decision}")
    msg_lines.append("Decisions: " + ", ".join(decisions))
    msg_lines.append("(Not financial advice)")

    message = "\n".join(msg_lines)
    send_whatsapp_message(message)


if __name__ == "__main__":
    main()
