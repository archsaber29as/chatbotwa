import requests
from config import (
    API_NINJAS_KEY,
    TWILIO_SID, TWILIO_TOKEN, TWILIO_SANDBOX_NUMBER, YOUR_NUMBER,
)
from tracer import trace

_QUOTE_CATEGORY_MAP = {
    "motivat": "inspirational",
    "inspir":  "inspirational",
    "success": "success",
    "sukses":  "success",
    "life":    "life",
    "hidup":   "life",
    "happi":   "happiness",
    "bahagia": "happiness",
    "love":    "love",
    "cinta":   "love",
    "wisdom":  "wisdom",
    "bijak":   "wisdom",
    "work":    "work",
    "kerja":   "work",
    "friend":  "friendship",
    "teman":   "friendship",
    "morning": "morning",
    "pagi":    "morning",
    "humour":  "humor",
    "humor":   "humor",
    "funny":   "humor",
    "fear":    "courage",
    "brave":   "courage",
    "berani":  "courage",
}
@trace
def _fetch_ninja_quote(category: str = "") -> dict | None:
    try:
        url    = "https://api.api-ninjas.com/v2/randomquotes"
        params = {"category": category} if category else {}
        resp   = requests.get(url, headers={"X-Api-Key": API_NINJAS_KEY}, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return data[0]
    except Exception as e:
        print(f"[API Ninjas quote error] {e}")
    return None

@trace
def _pick_category(context: str) -> str:
    lower = context.lower()
    for keyword, category in _QUOTE_CATEGORY_MAP.items():
        if keyword in lower:
            return category
    return "inspirational"

@trace
def generate_daily_quote(context: str = "") -> str:
    category = _pick_category(context) if context else "inspirational"
    raw = _fetch_ninja_quote(category) or _fetch_ninja_quote()

    if not raw:
        return "*Keep going — every step forward counts, no matter how small.*"

    quote  = raw.get("quote", "")
    author = raw.get("author", "Unknown")
    return f"_{quote}_\n{author}"

@trace
def send_scheduled_quote(label: str):
    """Send an auto-scheduled quote to YOUR_NUMBER via Twilio (called by scheduler)."""
    from twilio.rest import Client as TwilioClient
    try:
        body = generate_daily_quote()
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        twilio_client.messages.create(
            from_=TWILIO_SANDBOX_NUMBER,
            to=YOUR_NUMBER,
            body=body,
        )
        print(f"[Quote scheduler] {label} quote sent successfully.")
    except Exception as e:
        print(f"[Quote scheduler] Failed to send {label} quote: {e}")
