from dotenv import load_dotenv
load_dotenv('environtment.env')

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from google import genai
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lsa import LsaSummarizer
import sqlite3, requests, os, datetime, pickle, re, json, numpy as np, pytz

app = Flask(__name__)

# ================================================================
# IN-MEMORY LOG BUFFER — captures ALL output: print, Flask, Werkzeug, APScheduler
# ================================================================
import logging, collections, threading, sys

_LOG_BUFFER      = collections.deque(maxlen=300)
_LOG_BUFFER_LOCK = threading.Lock()

def _buf(line: str):
    """Append one line to the buffer (thread-safe)."""
    with _LOG_BUFFER_LOCK:
        _LOG_BUFFER.append(line)

def _ts() -> str:
    """Current time in Asia/Jakarta as HH:MM:SS string."""
    return datetime.datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")

# 1. Custom logging handler — attaches to every logger
class _BufHandler(logging.Handler):
    def emit(self, record):
        try:
            msg  = self.format(record)
            _buf(f"[{_ts()}] {record.levelname} {record.name}: {msg}")
        except Exception:
            pass

_buf_handler = _BufHandler()
_buf_handler.setFormatter(logging.Formatter("%(message)s"))
_buf_handler.setLevel(logging.DEBUG)

# Attach to root logger — catches Flask, Werkzeug, APScheduler, etc.
logging.getLogger().addHandler(_buf_handler)
logging.getLogger().setLevel(logging.DEBUG)

# Explicitly attach to Werkzeug (HTTP request lines) and APScheduler
for _lgr in ("werkzeug", "apscheduler", "apscheduler.executors.default"):
    _l = logging.getLogger(_lgr)
    _l.addHandler(_buf_handler)
    _l.setLevel(logging.DEBUG)

# 2. Intercept stdout so print() calls are also captured
class _TeeStream:
    """Writes to both the original stream and the log buffer."""
    def __init__(self, original):
        self._orig = original
    def write(self, text):
        self._orig.write(text)
        stripped = text.strip()
        if stripped:
            _buf(f"[{_ts()}] {stripped}")
    def flush(self):
        self._orig.flush()
    def __getattr__(self, attr):
        return getattr(self._orig, attr)

sys.stdout = _TeeStream(sys.stdout)
sys.stderr = _TeeStream(sys.stderr)

def get_recent_logs(n: int = 30) -> str:
    with _LOG_BUFFER_LOCK:
        lines = list(_LOG_BUFFER)[-n:]
    return "\n".join(lines) if lines else "No logs yet."

# ================================================================
# TIMEZONE HELPER — always use Asia/Jakarta "now"
# ================================================================
TZ_JKT = pytz.timezone("Asia/Jakarta")

def now_jkt() -> datetime.datetime:
    """Return current datetime in Asia/Jakarta timezone (naive, for DB storage)."""
    return datetime.datetime.now(TZ_JKT).replace(tzinfo=None)

def localize_jkt(dt: datetime.datetime) -> datetime.datetime:
    """Attach Asia/Jakarta tzinfo to a naive datetime (for Google Calendar isoformat)."""
    return TZ_JKT.localize(dt)

# ================================================================
# MODEL CONFIG
# Each model has a distinct, specialized role.
# ================================================================
MODEL_EMBED      = "gemini-embedding-2-preview"   # Gemini Embedding 2  — semantic memory for notes & ideas
#                                                  # ⚠️ Verify the exact name at: https://ai.google.dev/gemini-api/docs/models
MODEL_CLASSIFY   = "gemini-2.5-flash-lite"         # Gemini 2.5 Flash Lite — lightweight intent classification
MODEL_BRAINSTORM = "gemini-3-flash-preview"                # Gemini 3 Flash        — brainstorming & creative tasks
#                                                  # ⚠️ Verify availability at: https://ai.google.dev/gemini-api/docs/models
MODEL_MAIN       = "gemini-2.5-flash"              # Gemini 2.5 Flash      — all other tasks (existing)

# ================================================================
# CLIENT & ENV CONFIG
# ================================================================
client             = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
NEWS_API_KEY       = os.environ["NEWS_API_KEY"]
YOUR_NUMBER        = os.environ["YOUR_NUMBER"]
TWILIO_SID         = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN       = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_SANDBOX_NUMBER = "whatsapp:+14155238886"
SPREADSHEET_ID     = os.environ["GOOGLE_SHEET_ID"]

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks"
]

# ================================================================
# GOOGLE AUTH — lazy singleton so a bad token won't crash startup
# ================================================================
_google_services_cache = None

def get_google_services():
    """Return (calendar, sheets, tasks) services. Loads once and caches.
    Reads token from GOOGLE_TOKEN_B64 env var (base64) or token.pickle file.
    Raises a clear RuntimeError if no valid credentials are available."""
    global _google_services_cache
    if _google_services_cache is not None:
        return _google_services_cache

    creds = None

    # 1. Try env var (base64-encoded pickle) — recommended for Railway
    token_b64 = os.environ.get("GOOGLE_TOKEN_B64")
    if token_b64:
        import base64, io
        try:
            creds = pickle.load(io.BytesIO(base64.b64decode(token_b64)))
            print("[Google Auth] Loaded credentials from GOOGLE_TOKEN_B64")
        except Exception as e:
            print(f"[Google Auth] Failed to decode GOOGLE_TOKEN_B64: {e}")

    # 2. Fall back to token.pickle on disk
    if creds is None and os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as f:
            creds = pickle.load(f)
        print("[Google Auth] Loaded credentials from token.pickle")

    # 3. Refresh if expired
    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            print("[Google Auth] Token refreshed successfully")
            with open("token.pickle", "wb") as f:
                pickle.dump(creds, f)
        else:
            raise RuntimeError(
                "Google credentials are invalid and cannot be refreshed. "
                "Run refresh_token.py locally and set GOOGLE_TOKEN_B64 on Railway."
            )

    if creds is None:
        raise RuntimeError(
            "No Google credentials found. "
            "Run refresh_token.py locally and set GOOGLE_TOKEN_B64 on Railway."
        )

    calendar = build("calendar", "v3", credentials=creds)
    sheets   = build("sheets",   "v4", credentials=creds)
    tasks    = build("tasks",    "v1", credentials=creds)
    _google_services_cache = (calendar, sheets, tasks)
    return _google_services_cache

# ================================================================
# DATABASE SETUP
# ================================================================
def init_db():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS ideas    (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY, content TEXT, remind_at TEXT, done INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS notes    (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS tasks    (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT, done INTEGER DEFAULT 0)")
    # Semantic memory: stores embeddings for notes & ideas
    c.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            id          INTEGER PRIMARY KEY,
            source_type TEXT,       -- 'note' or 'idea'
            source_id   INTEGER,
            content     TEXT,
            embedding   BLOB,       -- pickled list[float]
            timestamp   TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ================================================================
# GEMINI EMBEDDING 2 — Semantic Memory
# ================================================================
def get_embedding(text: str) -> list:
    """Generate an embedding vector for the given text using Gemini Embedding 2."""
    try:
        result = client.models.embed_content(
            model=MODEL_EMBED,
            contents=text
        )
        return result.embeddings[0].values
    except Exception as e:
        print(f"[Embedding error] {e}")
        return []

def save_embedding(source_type: str, source_id: int, content: str):
    """Generate and store an embedding for a note or idea."""
    embedding = get_embedding(content)
    if not embedding:
        return
    conn = sqlite3.connect("bot.db")
    conn.execute(
        "INSERT INTO embeddings (source_type, source_id, content, embedding, timestamp) VALUES (?, ?, ?, ?, ?)",
        (source_type, source_id, content, pickle.dumps(embedding), str(now_jkt()))
    )
    conn.commit()
    conn.close()

def _cosine_similarity(a: list, b: list) -> float:
    a, b   = np.array(a), np.array(b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))

def semantic_search(query: str, top_k: int = 3, min_score: float = 0.45) -> list:
    """Return the top-k most semantically similar notes/ideas to the query."""
    query_embedding = get_embedding(query)
    if not query_embedding:
        return []

    conn  = sqlite3.connect("bot.db")
    rows  = conn.execute(
        "SELECT source_type, source_id, content, embedding FROM embeddings"
    ).fetchall()
    conn.close()

    results = []
    for source_type, source_id, content, embedding_blob in rows:
        try:
            embedding = pickle.loads(embedding_blob)
            score     = _cosine_similarity(query_embedding, embedding)
            if score >= min_score:
                results.append({"source_type": source_type, "content": content, "score": score})
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]

def _memory_context_block(query: str, min_score: float = 0.50) -> str:
    """Build a formatted context string from semantic memory for AI prompts."""
    memory = semantic_search(query, top_k=3, min_score=min_score)
    if not memory:
        return ""
    items = [f"- [{m['source_type']}] {m['content']}" for m in memory]
    return "\n\nRelevant from your notes & ideas:\n" + "\n".join(items)

def _parse_date_from_message(text: str) -> str | None:
    """Use Gemini to extract a YYYY-MM-DD date from a natural language message.
    Returns None if no specific date found."""
    now = now_jkt()
    prompt = f"""Today is {now.strftime("%Y-%m-%d")} (Asia/Jakarta).
Extract the specific date being referred to in the user's message.
Reply with ONLY a date in YYYY-MM-DD format, or reply with NONE if no specific date is mentioned.

Examples:
"remind me about my event on may 10th" → {now.year}-05-10
"what do I have tomorrow" → {(now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")}
"show my calendar" → NONE
"events on 22/04" → {now.year}-04-22

User message: {text}"""
    try:
        response = client.models.generate_content(model=MODEL_CLASSIFY, contents=prompt)
        result   = response.text.strip()
        if result == "NONE" or not result:
            return None
        # Validate it looks like a date
        datetime.datetime.strptime(result, "%Y-%m-%d")
        return result
    except Exception:
        return None


# ================================================================
# GEMINI 2.5 FLASH LITE — Intent Classifier
# ================================================================
_CLASSIFY_PROMPT = """You are an intent classifier for a WhatsApp personal assistant.

IMPORTANT: Understand the full CONTEXT and MEANING of the message first. Do NOT match by keywords alone.
Ask yourself: is the user trying to CREATE something new, or RETRIEVE/LOOK UP something existing?

Classify the user's message into exactly ONE of these intents:

  reminder      — CREATE a new reminder or alarm (user wants to BE reminded later)
  get_reminders — VIEW or look up existing reminders
  add_note      — SAVE a new note or memo
  get_notes     — LIST or read saved notes
  add_idea      — SAVE a new idea
  get_ideas     — LIST or read saved ideas
  add_task      — ADD a new to-do task
  get_tasks     — LIST or view pending tasks
  complete_task — MARK a task as done
  news          — get news or headlines
  brainstorm    — brainstorm, explore ideas, get creative suggestions
  add_event     — CREATE / add a new calendar event
  get_events    — VIEW, check, look up, or list existing calendar events
  search_memory — ask about something that might be in their notes/ideas
  show_logs     — show recent bot logs or errors
  chat          — general conversation or anything else

KEY DISAMBIGUATION RULES (apply these before classifying):
- "remind me [of/about] an event on X" → get_events (looking up an existing event, NOT setting a reminder)
- "remind me [of/about] my meeting" → get_events (retrieving existing calendar info)
- "set a reminder to X" / "remind me to X at Y" → reminder (creating a new reminder/alarm)
- "what events do I have on X" / "show my calendar for X" / "do I have anything on X" → get_events
- "add event X" / "schedule X" / "create event X" / "new event X" → add_event
- "show my reminders" / "list reminders" / "what are my reminders" → get_reminders
- The word "remind" alone does NOT mean intent=reminder. Look at the full sentence structure.

Reply ONLY with a JSON object (no markdown, no preamble):
{{"intent": "<intent>", "params": {{"content": "<extracted content if any>", "keyword": "<keyword if applicable>", "date": "<date if mentioned, e.g. 2025-05-10>"}}}}

User message: {message}"""

def classify_intent(text: str) -> dict:
    """Use Gemini 2.5 Flash Lite to classify the user's intent."""
    try:
        response = client.models.generate_content(
            model=MODEL_CLASSIFY,
            contents=_CLASSIFY_PROMPT.format(message=text)
        )
        raw = re.sub(r"```json|```", "", response.text.strip()).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[Classify error] {e}")
        return {"intent": "chat", "params": {}}

# ================================================================
# GEMINI 3 FLASH — Brainstorming
# ================================================================
def ai_brainstorm(topic: str) -> str:
    """Use Gemini 3 Flash for deep brainstorming, enriched with semantic memory."""
    memory_ctx = _memory_context_block(topic, min_score=0.45)
    prompt = (
        f"You are an enthusiastic brainstorming partner on WhatsApp.\n"
        f"Help the user brainstorm creative, actionable ideas for: {topic}"
        f"{memory_ctx}\n\n"
        f"Give 5-7 ideas. Use an emoji for each. Keep each idea concise but inspiring.\n"
        f"End with one short motivational line."
    )
    try:
        response = client.models.generate_content(model=MODEL_BRAINSTORM, contents=prompt)
        return f"🧠 *Brainstorm: {topic}*\n\n{response.text.strip()}"
    except Exception as e:
        err = str(e).lower()
        print(f"[Brainstorm error] {e}")
        if "not found" in err or "404" in err or "unavailable" in err:
            # Graceful fallback to main model if Gemini 3 Flash not yet available
            print("[Brainstorm] Falling back to MODEL_MAIN")
            try:
                response = client.models.generate_content(model=MODEL_MAIN, contents=prompt)
                return f"🧠 *Brainstorm: {topic}*\n\n{response.text.strip()}"
            except Exception:
                pass
        return "⚠️ Brainstorm failed. Please try again!"

# ================================================================
# GEMINI 2.5 FLASH — General AI (existing + enhanced with memory)
# ================================================================
def parse_reminder_with_ai(user_input: str) -> tuple:
    """Use Gemini 2.5 Flash to extract reminder content and datetime."""
    now     = now_jkt()  # FIX: use Jakarta time, not server UTC
    today   = now.strftime("%Y-%m-%d %H:%M")
    year    = now.year

    prompt = f"""You are a datetime parser for a reminder bot. Current date and time: {today} (timezone: Asia/Jakarta).

Extract the reminder content and the exact target datetime from the user's message.

Rules:
- For RELATIVE times like "2 minutes from now", "in 1 hour", "30 seconds from now": calculate from the current time above.
- For PARTIAL dates like "22.04", "22/04", "april 22", "22 april": assume year {year} (or {year+1} if the date has already passed).
- For "on 22.04" or "0n 22.04" (typo): treat as April 22, {year}.
- For times like "22:04" or "22.04" that look like HH:MM: treat as a time today (or tomorrow if already past).
- If only a time is given with no date, use today if the time hasn't passed, otherwise tomorrow.
- If no time is specified for a future date, use 09:00.
- The reminder CONTENT should be just what the user wants to be reminded about (e.g. "take a break"), not the full message.

Reply ONLY in this exact format with nothing else:
CONTENT | YYYY-MM-DD HH:MM

Examples:
"set reminder in 2 minutes to drink water" → drink water | {(now + datetime.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M")}
"remind me on 22.04 to call mom" → call mom | {year}-04-22 09:00
"set reminder at 15:30 to take pills" → take pills | {now.strftime("%Y-%m-")}{now.strftime("%d")} 15:30

User message: {user_input}"""

    try:
        response  = client.models.generate_content(model=MODEL_MAIN, contents=prompt)
        raw       = response.text.strip()
        parts     = raw.split("|")
        if len(parts) < 2:
            raise ValueError("No pipe separator in response")

        content   = parts[0].strip()
        remind_at = parts[1].strip()

        # Validate the parsed datetime is a real date
        datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        return content, remind_at

    except Exception as e:
        print(f"[Reminder parse error] {e} | raw response: {getattr(response, 'text', 'N/A') if 'response' in dir() else 'no response'}")
        # Fallback: tomorrow at 09:00, but warn in content
        fallback_dt = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d 09:00")
        return user_input, fallback_dt

def ai_chat(user_input: str) -> str:
    """General chat using Gemini 2.5 Flash, enriched with semantic memory context."""
    memory_ctx = _memory_context_block(user_input, min_score=0.55)
    prompt = (
        f"You are a helpful WhatsApp personal assistant. Reply concisely and friendly."
        f"{memory_ctx}\n\nUser: {user_input}"
    )
    try:
        response = client.models.generate_content(model=MODEL_MAIN, contents=prompt)
        return response.text.strip()
    except Exception as e:
        err = str(e)
        if "503" in err or "UNAVAILABLE" in err:
            return "⚠️ AI temporarily overloaded. Try again in a moment!"
        if "429" in err or "QUOTA" in err:
            return "⚠️ API quota reached. Try again later."
        return "⚠️ Something went wrong. Please try again."

# ================================================================
# REMINDER → Google Calendar
# ================================================================
def save_reminder(text: str, remind_at: str) -> str:
    conn = sqlite3.connect("bot.db")
    conn.execute("INSERT INTO reminders (content, remind_at) VALUES (?, ?)", (text, remind_at))
    conn.commit()
    conn.close()
    try:
        dt     = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        dt_end = dt + datetime.timedelta(minutes=30)
        # FIX: localize datetimes so Google Calendar gets the correct Jakarta offset
        dt_aware     = localize_jkt(dt)
        dt_end_aware = localize_jkt(dt_end)
        event  = {
            "summary": f"⏰ {text}",
            "start":   {"dateTime": dt_aware.isoformat(),     "timeZone": "Asia/Jakarta"},
            "end":     {"dateTime": dt_end_aware.isoformat(), "timeZone": "Asia/Jakarta"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "email", "minutes": 10}
                ]
            }
        }
        calendar_svc, _, _ = get_google_services()
        calendar_svc.events().insert(calendarId="primary", body=event).execute()
        dt_pretty = dt.strftime("%A, %d %B %Y at %H:%M")
        return f"⏰ Reminder set for *{dt_pretty}*!\n📅 Also added to Google Calendar."
    except Exception as e:
        return f"⏰ Reminder saved locally for *{remind_at}*.\n⚠️ Calendar sync failed: {str(e)}"

# ================================================================
# IDEAS → Google Sheets + Embedding
# ================================================================
def save_idea(text: str) -> str:
    conn      = sqlite3.connect("bot.db")
    cursor    = conn.execute(
        "INSERT INTO ideas (content, timestamp) VALUES (?, ?)",
        (text, str(now_jkt()))  # FIX: Jakarta time
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Store embedding for semantic memory (Gemini Embedding 2)
    save_embedding("idea", source_id, text)

    try:
        timestamp = now_jkt().strftime("%Y-%m-%d %H:%M")  # FIX: Jakarta time
        _, sheets_svc, _ = get_google_services()
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Ideas!A2:B",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[timestamp, text]]}
        ).execute()
        return f"💡 Idea saved!\n📊 Also added to Google Sheets.\n🧠 Memorized for semantic search."
    except Exception as e:
        return f"💡 Idea saved locally.\n🧠 Memorized for semantic search.\n⚠️ Sheets sync failed: {str(e)}"

def get_ideas() -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Ideas!A:B"
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return "💡 No ideas saved yet."
        recent = rows[-10:]
        return "💡 *Your ideas:*\n\n" + "\n".join(
            [f"{i+1}. {r[1]} _({r[0]})_" for i, r in enumerate(recent) if len(r) >= 2]
        )
    except Exception:
        conn = sqlite3.connect("bot.db")
        rows = conn.execute("SELECT content, timestamp FROM ideas ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "💡 No ideas saved yet."
        return "💡 *Your ideas:*\n\n" + "\n".join(
            [f"{i+1}. {r[0]} _({r[1][:10]})_" for i, r in enumerate(rows)]
        )

# ================================================================
# NOTES → Google Sheets + Embedding
# ================================================================
def save_note(text: str) -> str:
    conn      = sqlite3.connect("bot.db")
    cursor    = conn.execute(
        "INSERT INTO notes (content, timestamp) VALUES (?, ?)",
        (text, str(now_jkt()))  # FIX: Jakarta time
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Store embedding for semantic memory (Gemini Embedding 2)
    save_embedding("note", source_id, text)

    try:
        timestamp = now_jkt().strftime("%Y-%m-%d %H:%M")  # FIX: Jakarta time
        _, sheets_svc, _ = get_google_services()
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Notes!A2:B",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[timestamp, text]]}
        ).execute()
        return f"📝 Note saved!\n📊 Also added to Google Sheets.\n🧠 Memorized for semantic search."
    except Exception as e:
        return f"📝 Note saved locally.\n🧠 Memorized for semantic search.\n⚠️ Sheets sync failed: {str(e)}"

def get_notes() -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Notes!A:B"
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return "📝 No notes saved yet."
        recent = rows[-10:]
        return "📝 *Your notes:*\n\n" + "\n".join(
            [f"{i+1}. {r[1]} _({r[0]})_" for i, r in enumerate(recent) if len(r) >= 2]
        )
    except Exception:
        conn = sqlite3.connect("bot.db")
        rows = conn.execute("SELECT content, timestamp FROM notes ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "📝 No notes saved yet."
        return "📝 *Your notes:*\n\n" + "\n".join(
            [f"{i+1}. {r[0]} _({r[1][:10]})_" for i, r in enumerate(rows)]
        )

# ================================================================
# TASKS → Google Tasks
# ================================================================
def save_task(text: str) -> str:
    conn = sqlite3.connect("bot.db")
    conn.execute("INSERT INTO tasks (content, timestamp) VALUES (?, ?)", (text, str(now_jkt())))  # FIX: Jakarta time
    conn.commit()
    conn.close()
    try:
        _, _, tasks_svc = get_google_services()
        tasks_svc.tasks().insert(
            tasklist="@default",
            body={"title": text, "status": "needsAction"}
        ).execute()
        return f"✅ Task added!\n📋 Also added to Google Tasks."
    except Exception as e:
        return f"✅ Task saved locally.\n⚠️ Google Tasks sync failed: {str(e)}"

def get_tasks() -> str:
    try:
        _, _, tasks_svc = get_google_services()
        result = tasks_svc.tasks().list(tasklist="@default", showCompleted=False).execute()
        items  = result.get("items", [])
        if not items:
            return "📋 No pending tasks."
        return "📋 *Your tasks:*\n\n" + "\n".join(
            [f"{i+1}. {t['title']}" for i, t in enumerate(items[:10])]
        )
    except Exception:
        conn = sqlite3.connect("bot.db")
        rows = conn.execute("SELECT content FROM tasks WHERE done=0 ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "📋 No pending tasks."
        return "📋 *Your tasks:*\n\n" + "\n".join([f"{i+1}. {r[0]}" for i, r in enumerate(rows)])

def complete_task(keyword: str) -> str:
    try:
        _, _, tasks_svc = get_google_services()
        result  = tasks_svc.tasks().list(tasklist="@default", showCompleted=False).execute()
        items   = result.get("items", [])
        matched = [t for t in items if keyword.lower() in t["title"].lower()]
        if not matched:
            return f"❌ No task found matching '{keyword}'."
        t = matched[0]
        tasks_svc.tasks().patch(
            tasklist="@default", task=t["id"], body={"status": "completed"}
        ).execute()
        return f"✅ Task *'{t['title']}'* marked as complete!"
    except Exception as e:
        return f"⚠️ Could not complete task: {str(e)}"

# ================================================================
# NEWS → sumy + Gemini 2.5 Flash summary
# ================================================================
def get_news(topic: str) -> str:
    url = (
        f"https://newsapi.org/v2/everything"
        f"?q={topic}&apiKey={NEWS_API_KEY}&pageSize=5&language=en&sortBy=relevancy"
        f"&from={(now_jkt() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')}"
    )
    try:
        data     = requests.get(url, timeout=10).json()
        articles = data.get("articles", [])
    except Exception as e:
        return f"📭 Could not fetch news for *{topic}*. Try again later."

    if not articles:
        return f"📭 No news found for *{topic}*."

    a            = articles[0]
    title        = a.get("title", "No title")
    source       = a.get("source", {}).get("name", "Unknown source")
    article_url  = a.get("url", "")
    published    = a.get("publishedAt", "")[:10]
    raw_text     = a.get("content") or a.get("description") or ""

    if article_url:
        try:
            headers     = {"User-Agent": "Mozilla/5.0"}
            page        = requests.get(article_url, headers=headers, timeout=10)
            soup        = BeautifulSoup(page.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            scraped = " ".join(p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 0)
            if len(scraped) > len(raw_text):
                raw_text = scraped[:5000]
        except Exception as e:
            print(f"[Scrape error] {e}")

    if not raw_text:
        raw_text = a.get("description") or "Content not available."

    prompt = f"""You are a news summarizer for WhatsApp. Summarize the article below.

🔍 *What happened:*
[2-3 sentences explaining the main event]

👥 *Who is impacted:*
[Who is affected and how]

⚠️ *Why it matters:*
[Significance or consequences]

✅ *Solution / Response:* (skip if none)
[Actions taken or official responses]

📌 *Key takeaway:*
[One concise sentence]

Article title: {title}
Article content: {raw_text}"""

    try:
        response = client.models.generate_content(model=MODEL_MAIN, contents=prompt)
        summary  = response.text.strip()
    except Exception as e:
        print(f"[News summary error] {e}")
        summary = raw_text[:500] + "..."

    return (
        f"📰 *{title}*\n"
        f"🗞 {source} · {published}\n"
        f"─────────────────\n"
        f"{summary}\n\n"
        f"🔗 {article_url}"
    )

# ================================================================
# CALENDAR EVENT
# ================================================================
def parse_event_with_ai(user_input: str) -> dict | None:
    """Use Gemini 2.5 Flash to extract event title, start, end, description from natural language."""
    now  = now_jkt()  # FIX: Jakarta time
    year = now.year
    prompt = f"""You are a calendar event parser. Current date and time: {now.strftime("%Y-%m-%d %H:%M")} (timezone: Asia/Jakarta).

Extract the event details from the user's message.

Rules:
- Title: the name/subject of the event
- Start: handle all natural formats: "22.04", "22/04", "april 22", "22 april", "tomorrow", "next monday", "next week", "3pm", "15:00", "15.30", "noon", "midnight", "in 2 hours"
- End: end datetime if mentioned; otherwise return an empty string (app defaults to 1 hour after start)
- Description: any extra detail; empty string if none
- Partial dates like "22.04" or "22/04" → April 22, {year} (use {year+1} if that date already passed)
- Times: "3pm"→15:00, "3.30pm"→15:30, "noon"→12:00, "midnight"→00:00; if no time given, default to 09:00
- "tomorrow" → {(now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")}
- "next Monday" → calculate from today ({now.strftime("%A, %Y-%m-%d")})

Reply ONLY as valid JSON with no markdown or preamble:
{{"title": "...", "start": "YYYY-MM-DD HH:MM", "end": "YYYY-MM-DD HH:MM or empty string", "description": "..."}}

User message: {user_input}"""
    try:
        response = client.models.generate_content(model=MODEL_MAIN, contents=prompt)
        raw      = re.sub(r"```json|```", "", response.text.strip()).strip()
        data     = json.loads(raw)
        # Validate start datetime is a real date
        datetime.datetime.strptime(data["start"], "%Y-%m-%d %H:%M")
        if data.get("end"):
            datetime.datetime.strptime(data["end"], "%Y-%m-%d %H:%M")
        return data
    except Exception as e:
        print(f"[Event parse error] {e}")
        return None

def save_event(title: str, start_dt: str, end_dt: str = None, description: str = "") -> str:
    end_dt = end_dt or (
        datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M") + datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M")

    conn = sqlite3.connect("bot.db")
    conn.execute("INSERT INTO reminders (content, remind_at) VALUES (?, ?)", (title, start_dt))
    conn.commit()
    conn.close()

    start_pretty = datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M").strftime("%A, %d %B %Y at %H:%M")
    end_pretty   = datetime.datetime.strptime(end_dt,   "%Y-%m-%d %H:%M").strftime("%H:%M")

    try:
        dt_start     = datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M")
        dt_end_obj   = datetime.datetime.strptime(end_dt,   "%Y-%m-%d %H:%M")
        # FIX: localize so Google Calendar gets the correct Jakarta offset
        dt_start_aware = localize_jkt(dt_start)
        dt_end_aware   = localize_jkt(dt_end_obj)
        event = {
            "summary":     title,
            "description": description,
            "start": {"dateTime": dt_start_aware.isoformat(), "timeZone": "Asia/Jakarta"},
            "end":   {"dateTime": dt_end_aware.isoformat(),   "timeZone": "Asia/Jakarta"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "email", "minutes": 10}
                ]
            }
        }
        calendar_svc, _, _ = get_google_services()
        calendar_svc.events().insert(calendarId="primary", body=event).execute()
        return f"📅 *{title}* added!\n🗓 {start_pretty} → {end_pretty}"
    except Exception as e:
        return f"⚠️ Could not add event to Calendar: {str(e)}"

# ================================================================
# GET EVENTS — look up Google Calendar for a given date/period
# ================================================================
def get_events(date_hint: str = None, query: str = "") -> str:
    """Fetch events from Google Calendar. If date_hint is given (YYYY-MM-DD), show that day.
    Otherwise show upcoming events for the next 7 days."""
    try:
        now = now_jkt()
        # Parse date from hint if provided
        if date_hint:
            try:
                target = datetime.datetime.strptime(date_hint, "%Y-%m-%d")
            except ValueError:
                target = now
        else:
            target = now

        day_start = localize_jkt(target.replace(hour=0, minute=0, second=0, microsecond=0))
        day_end   = localize_jkt(target.replace(hour=23, minute=59, second=59, microsecond=0))

        # If no specific date, show next 7 days
        if not date_hint:
            day_start = localize_jkt(now.replace(second=0, microsecond=0))
            day_end   = localize_jkt((now + datetime.timedelta(days=7)).replace(hour=23, minute=59, second=59))

        calendar_svc, _, _ = get_google_services()
        result = calendar_svc.events().list(
            calendarId="primary",
            timeMin=day_start.isoformat(),
            timeMax=day_end.isoformat(),
            maxResults=10,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = result.get("items", [])
        if not events:
            label = target.strftime("%A, %d %B %Y") if date_hint else "the next 7 days"
            return f"📭 No events found for *{label}*."

        label = target.strftime("%A, %d %B %Y") if date_hint else "upcoming 7 days"
        lines = [f"📅 *Your events — {label}:*\n"]
        for ev in events:
            title = ev.get("summary", "(No title)")
            start = ev.get("start", {})
            if "dateTime" in start:
                dt = datetime.datetime.fromisoformat(start["dateTime"])
                time_str = dt.strftime("%a %d %b, %H:%M")
            else:
                time_str = start.get("date", "All day")
            lines.append(f"• {time_str} — {title}")
        return "\n".join(lines)

    except Exception as e:
        print(f"[get_events error] {e}")
        return f"⚠️ Could not fetch calendar events: {str(e)}"


def get_reminders_list(date_hint: str = None) -> str:
    """List upcoming reminders from the local DB."""
    conn = sqlite3.connect("bot.db")
    now  = now_jkt()
    if date_hint:
        try:
            target   = datetime.datetime.strptime(date_hint, "%Y-%m-%d")
            day_lo   = target.strftime("%Y-%m-%d 00:00")
            day_hi   = target.strftime("%Y-%m-%d 23:59")
            rows     = conn.execute(
                "SELECT content, remind_at FROM reminders WHERE remind_at BETWEEN ? AND ? AND done=0 ORDER BY remind_at",
                (day_lo, day_hi)
            ).fetchall()
            label    = target.strftime("%A, %d %B %Y")
        except ValueError:
            rows  = []
            label = date_hint
    else:
        rows  = conn.execute(
            "SELECT content, remind_at FROM reminders WHERE remind_at >= ? AND done=0 ORDER BY remind_at LIMIT 10",
            (now.strftime("%Y-%m-%d %H:%M"),)
        ).fetchall()
        label = "upcoming"
    conn.close()

    if not rows:
        return f"⏰ No {label} reminders found."
    lines = [f"⏰ *Your {label} reminders:*\n"]
    for content, remind_at in rows:
        try:
            dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
            lines.append(f"• {dt.strftime('%a %d %b, %H:%M')} — {content}")
        except Exception:
            lines.append(f"• {remind_at} — {content}")
    return "\n".join(lines)


# ================================================================
# REMINDER SCHEDULER
# ================================================================
def check_and_send_reminders():
    # Use a 90-second window so reminders are never missed due to scheduler timing drift
    now    = now_jkt()  # FIX: Jakarta time
    win_lo = (now - datetime.timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M")
    win_hi = (now + datetime.timedelta(seconds=59)).strftime("%Y-%m-%d %H:%M")
    conn   = sqlite3.connect("bot.db")
    rows   = conn.execute(
        "SELECT id, content FROM reminders WHERE remind_at BETWEEN ? AND ? AND done = 0",
        (win_lo, win_hi)
    ).fetchall()
    if rows:
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        for row in rows:
            twilio_client.messages.create(
                from_=TWILIO_SANDBOX_NUMBER,
                to=YOUR_NUMBER,
                body=f"⏰ *Reminder:* {row[1]}"
            )
            conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (row[0],))
        conn.commit()
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send_reminders, "interval", minutes=1)
scheduler.start()

# ================================================================
# WEBHOOK — AI-powered intent routing
# ================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.form.get("Body", "").strip()
    lower    = incoming.lower()
    resp     = MessagingResponse()
    msg      = resp.message()

    # Step 0: Hard-coded keyword shortcuts — never go through AI classifier
    _log_triggers = {"show logs", "show log", "lihat log", "cek log", "log error",
                     "logs", "/logs", "show errors", "bot status", "status bot"}
    if any(t in lower for t in _log_triggers):
        n = 20
        nums = re.findall(r"\d+", incoming)
        if nums:
            n = min(int(nums[0]), 50)
        logs = get_recent_logs(n)
        msg.body(f"🖥️ *Last {n} log lines:*\n\n{logs}")
        return str(resp)

    # Step 1: Classify intent with Gemini 2.5 Flash Lite
    classified = classify_intent(incoming)
    intent     = classified.get("intent", "chat")
    params     = classified.get("params", {})

    # Step 2: Route to the appropriate handler + model
    if intent == "reminder":
        content, remind_at = parse_reminder_with_ai(incoming)
        msg.body(save_reminder(content, remind_at))

    elif intent == "get_reminders":
        date_hint = params.get("date") or None
        msg.body(get_reminders_list(date_hint))

    elif intent == "complete_task":
        keyword = params.get("keyword") or re.sub(
            r"complete task|finish task|done task|selesai task", "", lower
        ).strip(" :?!")
        msg.body(complete_task(keyword))

    elif intent == "get_tasks":
        msg.body(get_tasks())

    elif intent == "add_task":
        content = params.get("content") or re.sub(
            r"add task|new task|tambah task|create task|task:", "", lower
        ).strip(" :?!") or incoming
        msg.body(save_task(content))

    elif intent == "get_notes":
        msg.body(get_notes())

    elif intent == "add_note":
        content = params.get("content") or re.sub(
            r"note:|notes:|add note|save note|catatan:|catat", "", lower
        ).strip(" :?!") or incoming
        msg.body(save_note(content))

    elif intent == "get_ideas":
        msg.body(get_ideas())

    elif intent == "add_idea":
        content = params.get("content") or re.sub(
            r"idea:|save idea|add idea|ide:|simpan ide", "", lower
        ).strip(" :?!") or incoming
        msg.body(save_idea(content))

    elif intent == "news":
        topic = params.get("content") or lower
        for w in ["news", "berita", "headline", "latest", "terbaru", "about", "tentang", "get", "show", "give me"]:
            topic = topic.replace(w, "").strip(" ?!.,")
        msg.body(get_news(topic or "world"))

    elif intent == "brainstorm":
        # Gemini 3 Flash handles this
        topic = params.get("content") or re.sub(
            r"brainstorm|ide|ideas?|pikir|think about|think of", "", lower
        ).strip(" :?!") or incoming
        msg.body(ai_brainstorm(topic))

    elif intent == "get_events":
        date_hint = params.get("date") or None
        # If no date was extracted by classifier, try to parse it with AI
        if not date_hint:
            date_hint = _parse_date_from_message(incoming)
        msg.body(get_events(date_hint, incoming))

    elif intent == "add_event":
        # AI-powered natural language event parsing — no strict format required
        parsed = parse_event_with_ai(incoming)
        if parsed and parsed.get("title") and parsed.get("start"):
            msg.body(save_event(
                parsed["title"].strip(),
                parsed["start"].strip(),
                parsed["end"].strip() if parsed.get("end") else None,
                parsed.get("description", "")
            ))
        else:
            msg.body(
                "⚠️ Could not understand the event.\n"
                "Try: *Add event Team lunch on April 22 at 1pm*\n"
                "Or: *New event Meeting tomorrow at 3pm for 2 hours*"
            )
    elif intent == "show_logs":
        n = 20
        try:
            # allow "show last 50 logs" etc.
            nums = re.findall(r"\d+", incoming)
            if nums:
                n = min(int(nums[0]), 50)
        except Exception:
            pass
        logs = get_recent_logs(n)
        msg.body(f"🖥️ *Last {n} log lines:*\n\n```\n{logs}\n```")

    elif intent == "search_memory":
        # Gemini Embedding 2: semantic search through notes & ideas
        results = semantic_search(incoming, top_k=5, min_score=0.45)
        if results:
            items = [
                f"{i+1}. {r['content']} _({r['source_type']}, {round(r['score']*100)}% match)_"
                for i, r in enumerate(results)
            ]
            msg.body("🔍 *Found in your memory:*\n\n" + "\n".join(items))
        else:
            msg.body("🔍 Nothing relevant found in your notes or ideas.")

    else:  # chat — Gemini 2.5 Flash with memory context
        msg.body(ai_chat(incoming))

    return str(resp)

# ================================================================
# /logs — browser log viewer, auto-refreshes every 10s
# Optional: set LOG_SECRET env var to password-protect it
# If LOG_SECRET is not set, the page is open (fine for personal bots)
# ================================================================
@app.route("/logs")
def logs_endpoint():
    secret = os.environ.get("LOG_SECRET", "")
    if secret and request.args.get("secret") != secret:
        return "Unauthorized — add ?secret=YOUR_LOG_SECRET to the URL", 401
    n    = min(int(request.args.get("n", 100)), 300)
    logs = get_recent_logs(n).replace("<", "&lt;").replace(">", "&gt;")
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bot Logs</title>
  <meta http-equiv="refresh" content="10">
  <style>
    body {{ background:#0d1117; color:#c9d1d9; font-family:monospace; font-size:13px; padding:16px; margin:0 }}
    h2   {{ color:#58a6ff; margin-bottom:8px }}
    pre  {{ white-space:pre-wrap; word-break:break-all; line-height:1.6 }}
    .ts  {{ color:#8b949e }}
    .err {{ color:#ff7b72 }}
    .ok  {{ color:#56d364 }}
  </style>
</head>
<body>
  <h2>🖥️ Bot Logs <span style="font-size:11px;color:#8b949e">(auto-refresh 10s · last {n} lines)</span></h2>
  <pre>{logs}</pre>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))