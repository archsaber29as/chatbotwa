from dotenv import load_dotenv
load_dotenv("environtment.env")

import os, datetime, pytz
from google import genai
from groq import Groq

# ================================================================
# TIMEZONE HELPERS
# ================================================================
TZ_JKT = pytz.timezone("Asia/Jakarta")

def now_jkt() -> datetime.datetime:
    """Current datetime in Asia/Jakarta (naive, for DB storage)."""
    return datetime.datetime.now(TZ_JKT).replace(tzinfo=None)

def localize_jkt(dt: datetime.datetime) -> datetime.datetime:
    """Attach Asia/Jakarta tzinfo to a naive datetime (for Google Calendar)."""
    return TZ_JKT.localize(dt)

# ================================================================
# AI MODEL NAMES
# ================================================================
MODEL_EMBED      = "gemini-embedding-2-preview"    # Gemini Embedding 2  — semantic memory
MODEL_BRAINSTORM = "gemini-3-flash-preview"         # Gemini 3 Flash      — brainstorming
MODEL_GROQ       = "llama-3.1-8b-instant"           # Groq                — primary (classifier, chat, parsers)
MODEL_FALLBACK   = "gemini-3.1-flash-lite-preview"  # Gemini Flash Lite   — fallback if Groq errors

# ================================================================
# AI CLIENTS
# ================================================================
gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
groq_client   = Groq(api_key=os.environ["GROQ_API_KEY"])

# ================================================================
# TWILIO
# ================================================================
TWILIO_SID            = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN          = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_SANDBOX_NUMBER = "whatsapp:+14155238886"
YOUR_NUMBER           = os.environ["YOUR_NUMBER"]

# ================================================================
# EXTERNAL APIS
# ================================================================
NEWS_API_KEY   = os.environ["NEWS_API_KEY"]
API_NINJAS_KEY = os.environ["API_NINJAS_KEY"]

# ================================================================
# GOOGLE
# ================================================================
SPREADSHEET_ID     = os.environ["GOOGLE_SHEET_ID"]
LOG_SPREADSHEET_ID = os.environ["LOG_SHEET_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
]

# ================================================================
# WHATSAPP LIMITS
# ================================================================
WHATSAPP_CHAR_LIMIT = 4000
WHATSAPP_NOTE_LIMIT = 60   # ~60 items fit safely in one WhatsApp message

# ================================================================
# SESSION / CONVERSATION
# ================================================================
SESSION_TIMEOUT_MINUTES = 10
CONV_WINDOW             = 10   # keep last N user+assistant pairs in context

# ================================================================
# BUDGET (personal — edit to match your own expenses)
# ================================================================
PAYROLL_DAY = 25

FIXED_EXPENSES = [
    {"name": "House Rent",        "amount": 955_000,  "due_day": 25},
    {"name": "Internet",          "amount": 150_000,  "due_day": None},
    {"name": "Zakat",             "amount": 250_000,  "due_day": 25},
    {"name": "House Maintenance", "amount": 600_000,  "due_day": 9},
]

VARIABLE_BUDGETS = [
    {"name": "Ticket to go home", "budget": 600_000},
    {"name": "Fuel",              "budget": 70_000},
    {"name": "Laundry",           "budget": 60_000},
]
