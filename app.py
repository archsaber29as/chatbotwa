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
from groq import Groq
import sqlite3, requests, os, datetime, pickle, re, json, numpy as np, pytz

app = Flask(__name__)

# ================================================================
# IN-MEMORY LOG BUFFER — captures ALL output: print, Flask, Werkzeug, APScheduler
# ================================================================
import logging, threading, sys

# ----------------------------------------------------------------
# LOGGING STRATEGY
#   bot_all.log   → everything (werkzeug, apscheduler, httpx, stdout, all HTTP)
#                   used by browser /logs endpoint
#   Google Sheet  → only httpx INFO + HTTP /webhook
#   (tab=BotLogs)   written via background queue → read by WhatsApp "logs" command
# ----------------------------------------------------------------
import time, collections as _collections

_ALL_LOG_FILE   = "bot_all.log"
_ALL_LOG_LOCK   = threading.Lock()

# Queue for pending Google Sheet rows: each item is [timestamp_str, log_str]
_SHEET_QUEUE      = _collections.deque()
_SHEET_QUEUE_LOCK = threading.Lock()

# Cache of tab names already created/verified this process run
_CREATED_LOG_TABS      = set()
_CREATED_LOG_TABS_LOCK = threading.Lock()

def _ts() -> str:
    """Current time in Asia/Jakarta as HH:MM:SS."""
    return datetime.datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%H:%M:%S")

def _ts_full() -> str:
    """Full timestamp for sheet rows: YYYY-MM-DD HH:MM:SS."""
    return datetime.datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d %H:%M:%S")

def _get_log_tab() -> str:
    """Return today's sheet tab name (Jakarta date): YYYY-MM-DD."""
    return datetime.datetime.now(pytz.timezone("Asia/Jakarta")).strftime("%Y-%m-%d")

def _ensure_log_tab(sheets_svc, tab_name: str):
    """Create the daily tab with a header row if it doesn't exist yet.
    Uses an in-process cache so the API is only called once per tab per run."""
    with _CREATED_LOG_TABS_LOCK:
        if tab_name in _CREATED_LOG_TABS:
            return
    try:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=LOG_SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()
        # Write header row into the new tab
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=LOG_SPREADSHEET_ID,
            range=f"'{tab_name}'!A1:B1",
            valueInputOption="RAW",
            body={"values": [["Timestamp", "Log"]]},
        ).execute()
    except Exception:
        pass  # Tab already exists — that's fine
    with _CREATED_LOG_TABS_LOCK:
        _CREATED_LOG_TABS.add(tab_name)

# ── bot_all.log writer ──────────────────────────────────────────
def _buf_all(line: str):
    """Append to the full log file (browser /logs). Thread-safe."""
    with _ALL_LOG_LOCK:
        try:
            with open(_ALL_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

# ── Google Sheet queue writer ────────────────────────────────────
def _buf_sheet(line: str):
    """Queue one row for the daily BotLogs Google Sheet tab."""
    with _SHEET_QUEUE_LOCK:
        _SHEET_QUEUE.append([_ts_full(), line])

# ── Background flusher ───────────────────────────────────────────
def _sheet_log_flusher():
    """Daemon thread: every 5 s flush queued rows to the correct daily tab."""
    while True:
        time.sleep(5)
        with _SHEET_QUEUE_LOCK:
            if not _SHEET_QUEUE:
                continue
            rows = list(_SHEET_QUEUE)
            _SHEET_QUEUE.clear()
        try:
            _, sheets_svc, _ = get_google_services()
            # Group rows by date so midnight crossover lands in the right tab
            by_tab = _collections.defaultdict(list)
            for row in rows:
                tab = row[0][:10]   # "YYYY-MM-DD" prefix of full timestamp
                by_tab[tab].append(row)
            for tab_name, tab_rows in by_tab.items():
                _ensure_log_tab(sheets_svc, tab_name)
                sheets_svc.spreadsheets().values().append(
                    spreadsheetId=LOG_SPREADSHEET_ID,
                    range=f"'{tab_name}'!A:B",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": tab_rows},
                ).execute()
        except Exception:
            # Put rows back — they will be retried next cycle
            with _SHEET_QUEUE_LOCK:
                _SHEET_QUEUE.extendleft(reversed(rows))

threading.Thread(target=_sheet_log_flusher, daemon=True, name="sheet-log-flusher").start()

# ── Logging handlers ─────────────────────────────────────────────

# Handler for ALL loggers → bot_all.log only
class _AllHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _buf_all(f"[{_ts()}] {record.levelname} {record.name}: {msg}")
        except Exception:
            pass

# Handler for httpx → bot_all.log + Google Sheet
class _HttpxHandler(logging.Handler):
    def emit(self, record):
        try:
            msg  = self.format(record)
            line = f"[{_ts()}] {record.levelname} {record.name}: {msg}"
            _buf_all(line)
            _buf_sheet(line)
        except Exception:
            pass

_all_handler = _AllHandler()
_all_handler.setFormatter(logging.Formatter("%(message)s"))
_all_handler.setLevel(logging.DEBUG)

_httpx_handler = _HttpxHandler()
_httpx_handler.setFormatter(logging.Formatter("%(message)s"))
_httpx_handler.setLevel(logging.INFO)

# Root logger → bot_all.log (werkzeug, apscheduler, Flask, etc.)
logging.getLogger().addHandler(_all_handler)
logging.getLogger().setLevel(logging.DEBUG)

for _lgr_name in ("werkzeug", "apscheduler", "apscheduler.executors.default"):
    _l = logging.getLogger(_lgr_name)
    _l.addHandler(_all_handler)
    _l.setLevel(logging.DEBUG)

# httpx → both sinks, INFO only
_httpx_logger = logging.getLogger("httpx")
_httpx_logger.addHandler(_httpx_handler)
_httpx_logger.setLevel(logging.INFO)
_httpx_logger.propagate = False   # prevent double-logging via root

# Intercept stdout/stderr → bot_all.log only
class _TeeStream:
    def __init__(self, original):
        self._orig = original
    def write(self, text):
        self._orig.write(text)
        stripped = text.strip()
        if stripped:
            _buf_all(f"[{_ts()}] {stripped}")
    def flush(self):
        self._orig.flush()
    def __getattr__(self, attr):
        return getattr(self._orig, attr)

sys.stdout = _TeeStream(sys.stdout)
sys.stderr = _TeeStream(sys.stderr)

# ── Log readers ──────────────────────────────────────────────────

def get_all_logs(n: int = 100) -> str:
    """Browser /logs: read last n lines from bot_all.log."""
    try:
        with _ALL_LOG_LOCK:
            with open(_ALL_LOG_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        recent = [l.rstrip("\n") for l in lines[-n:]]
        return "\n".join(recent) if recent else "No logs yet."
    except FileNotFoundError:
        return "No logs yet."
    except Exception as e:
        return f"Error reading logs: {e}"

def get_recent_logs(n: int = 20) -> str:
    """WhatsApp chatbot: read last n rows from today's daily tab in LOG_SPREADSHEET_ID."""
    try:
        _, sheets_svc, _ = get_google_services()
        tab_name = _get_log_tab()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=LOG_SPREADSHEET_ID,
            range=f"'{tab_name}'!A:B",
        ).execute()
        rows = result.get("values", [])
        # Skip header row
        if rows and rows[0][0].lower() in ("timestamp", "time", "ts"):
            rows = rows[1:]
        if not rows:
            return f"No logs yet for {tab_name}."
        recent = rows[-n:]
        lines = []
        for r in recent:
            entry = f"[{r[0]}] {r[1] if len(r) > 1 else ''}"
            # Strip everything from "| body:" onward — only keep the HTTP status line
            if "| body:" in entry:
                entry = entry[:entry.index("| body:")].rstrip()
            lines.append(entry)
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading logs from Sheet: {e}"

@app.after_request
def _log_http(response):
    """All HTTP → bot_all.log; /webhook only → also Google Sheet queue."""
    try:
        body = response.get_data(as_text=True)
        line = f"[{_ts()}] HTTP {request.method} {request.path} → {response.status_code} | body: {body[:500]}"
        _buf_all(line)
        if request.path == "/webhook":
            _buf_sheet(line)
    except Exception:
        pass
    return response

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
# MODEL_EMBED      : Gemini Embedding 2      — semantic memory (tetap Gemini)
# MODEL_BRAINSTORM : Gemini 3 Flash          — brainstorming & creative tasks (tetap Gemini)
# MODEL_GROQ       : Groq Llama 3.1 8B      — classifier, main chat, semua parser (primary)
# MODEL_FALLBACK   : Gemini 3.1 Flash Lite  — fallback jika Groq error/unavailable
# ================================================================
MODEL_EMBED      = "gemini-embedding-2-preview"      # Gemini Embedding 2    — semantic memory
MODEL_BRAINSTORM = "gemini-3-flash-preview"           # Gemini 3 Flash        — brainstorming
MODEL_GROQ       = "llama-3.1-8b-instant"             # Groq                  — primary (classifier, chat, parser)
MODEL_FALLBACK   = "gemini-3.1-flash-lite-preview"    # Gemini 3.1 Flash Lite — fallback jika Groq down/401

# ================================================================
# CLIENT & ENV CONFIG
# ================================================================
gemini_client         = genai.Client(api_key=os.environ["GEMINI_API_KEY"])  # untuk embedding & brainstorm
groq_client           = Groq(api_key=os.environ["GROQ_API_KEY"])            # untuk classifier, chat, parser
NEWS_API_KEY          = os.environ["NEWS_API_KEY"]
YOUR_NUMBER           = os.environ["YOUR_NUMBER"]
TWILIO_SID            = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN          = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_SANDBOX_NUMBER = "whatsapp:+14155238886"
SPREADSHEET_ID        = os.environ["GOOGLE_SHEET_ID"]
LOG_SPREADSHEET_ID    = os.environ["LOG_SHEET_ID"]       # separate spreadsheet for daily bot logs

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
    # Conversation history: rolling window for multi-turn chat context
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id        INTEGER PRIMARY KEY,
            role      TEXT,   -- 'user' or 'assistant'
            content   TEXT,
            timestamp TEXT
        )
    """)
    # Bot state: key-value store for last_active timestamp and pending flags
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
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
# GEMINI EMBEDDING 2 — Semantic Memory (tetap menggunakan Gemini)
# ================================================================
def get_embedding(text: str) -> list:
    """Generate an embedding vector for the given text using Gemini Embedding 2."""
    try:
        result = gemini_client.models.embed_content(
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

# ================================================================
# CONVERSATION HISTORY — rolling window for multi-turn context
# ================================================================
CONV_WINDOW = 10   # keep last N message pairs (user + assistant) in context

def _save_conv_turn(role: str, content: str):
    """Persist one conversation turn to SQLite."""
    conn = sqlite3.connect("bot.db")
    conn.execute(
        "INSERT INTO conversations (role, content, timestamp) VALUES (?, ?, ?)",
        (role, content, str(now_jkt()))
    )
    # Trim to last CONV_WINDOW * 2 messages (user + assistant pairs)
    conn.execute("""
        DELETE FROM conversations
        WHERE id NOT IN (
            SELECT id FROM conversations ORDER BY id DESC LIMIT ?
        )
    """, (CONV_WINDOW * 2,))
    conn.commit()
    conn.close()

def _load_conv_history() -> list[dict]:
    """Load recent conversation turns as a list of {role, content} dicts."""
    conn  = sqlite3.connect("bot.db")
    rows  = conn.execute(
        "SELECT role, content FROM conversations ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

def _clear_conv_history():
    """Wipe conversation history (e.g. user says 'new topic' / 'forget that')."""
    conn = sqlite3.connect("bot.db")
    conn.execute("DELETE FROM conversations")
    conn.commit()
    conn.close()

# ================================================================
# BOT STATE — key/value store for last_active & pending flags
# ================================================================
SESSION_TIMEOUT_MINUTES = 10

def _state_get(key: str) -> str | None:
    conn = sqlite3.connect("bot.db")
    row  = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None

def _state_set(key: str, value: str):
    conn = sqlite3.connect("bot.db")
    conn.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def _state_del(key: str):
    conn = sqlite3.connect("bot.db")
    conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))
    conn.commit()
    conn.close()

def _touch_last_active():
    """Record current time as last active timestamp."""
    _state_set("last_active", str(now_jkt()))

def _minutes_since_last_active() -> float | None:
    """Return minutes elapsed since last message, or None if no record."""
    raw = _state_get("last_active")
    if not raw:
        return None
    try:
        last = datetime.datetime.fromisoformat(raw)
        delta = now_jkt() - last
        return delta.total_seconds() / 60
    except Exception:
        return None

def _is_pending_reset() -> bool:
    return _state_get("pending_reset") == "1"

def _set_pending_reset(flag: bool):
    if flag:
        _state_set("pending_reset", "1")
    else:
        _state_del("pending_reset")

# ================================================================
# GROQ HELPER — wrapper dengan fallback ke Gemini 3.1 Flash Lite
# ================================================================
def _groq_complete(system_prompt: str, user_prompt: str, max_tokens: int = 1024, temperature: float = 0.7,
                   history: list[dict] | None = None) -> str:
    """Call Groq llama-3.1-8b-instant. Jika gagal (401/rate limit/error), fallback ke Gemini 3.1 Flash Lite.
    
    If `history` is provided, it is inserted between the system prompt and the
    current user message so the model has full multi-turn context.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    # --- Primary: Groq ---
    try:
        response = groq_client.chat.completions.create(
            model=MODEL_GROQ,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Groq error] {e} — falling back to Gemini 3.1 Flash Lite")

    # --- Fallback: Gemini 3.1 Flash Lite ---
    full_prompt = (f"{system_prompt} {user_prompt}" if system_prompt else user_prompt)
    response = gemini_client.models.generate_content(model=MODEL_FALLBACK, contents=full_prompt)
    return response.text.strip()

# ================================================================
# DATE PARSER — menggunakan Groq
# ================================================================
def _parse_date_from_message(text: str) -> str | None:
    """Use Groq to extract a YYYY-MM-DD date from a natural language message.
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
        result = _groq_complete("", prompt, max_tokens=20, temperature=0.0)
        result = result.strip()
        if result == "NONE" or not result:
            return None
        # Validate it looks like a date
        datetime.datetime.strptime(result, "%Y-%m-%d")
        return result
    except Exception:
        return None

# ================================================================
# GROQ — Intent Classifier (menggantikan Gemini 2.5 Flash Lite)
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
  quote         — ask for a motivational/inspirational quote (e.g. "give me a quote", "motivate me", "quote of the day", "inspire me")
  budget        — CALCULATE or COMPUTE a budget with actual numbers: user provides a specific monetary amount and wants to know how much they can spend per day / sisa uang / berapa sisa per hari / survive until payday / kalkulasi budget / hitung uang sisa. Requires a specific monetary figure or explicit calculation request.
  delete_note   — DELETE or REMOVE a saved note by number or keyword (e.g. "delete note 2", "hapus note fix the logs")
  edit_note     — EDIT or UPDATE the content of a saved note (e.g. "edit note 2 to ...", "update note fix to ...")
  delete_idea   — DELETE or REMOVE a saved idea by number or keyword
  edit_idea     — EDIT or UPDATE a saved idea
  delete_task   — DELETE or REMOVE a task (not marking as done, but fully removing it)
  edit_task     — EDIT or UPDATE a task title
  delete_event  — DELETE or REMOVE a calendar event by name or keyword (e.g. "delete event Team lunch", "hapus event meeting")
  edit_event    — EDIT or UPDATE an existing calendar event (title, time, or description)
  delete_reminder — DELETE or REMOVE a saved reminder by keyword or time
  chat          — general conversation or anything else

KEY DISAMBIGUATION RULES (apply these before classifying):
- "remind me [of/about] an event on X" → get_events (looking up an existing event, NOT setting a reminder)
- "remind me [of/about] my meeting" → get_events (retrieving existing calendar info)
- "set a reminder to X" / "remind me to X at Y" → reminder (creating a new reminder/alarm)
- "what events do I have on X" / "show my calendar for X" / "do I have anything on X" → get_events
- "add event X" / "schedule X" / "create event X" / "new event X" → add_event
- "show my reminders" / "list reminders" / "what are my reminders" → get_reminders
- The word "remind" alone does NOT mean intent=reminder. Look at the full sentence structure.
- "how to budget" / "tips for budgeting" / "how to spend daily budget wisely" / any advice or how-to question about money → chat (NOT budget). The budget intent requires actual numbers to calculate, not general advice.
- "how to spend my daily budget wisely?" → chat (advice question, no number to calculate)
- "delete/remove/hapus note/idea/task/event/reminder X" → delete_* intent (not complete_task)
- "edit/update/change/ubah note/idea/task/event/reminder X to/with Y" → edit_* intent
- "delete task X" → delete_task (permanently remove), NOT complete_task (which marks done)

Reply ONLY with a JSON object (no markdown, no preamble):
{{"intent": "<intent>", "params": {{"content": "<new content for edit intents>", "keyword": "<item to find/delete/edit>", "index": "<item number if user said e.g. note 2>", "date": "<date if mentioned, e.g. 2025-05-10>"}}}}

User message: {message}"""

def classify_intent(text: str) -> dict:
    """Use Groq Llama 3.1 8B to classify the user's intent."""
    try:
        raw = _groq_complete(
            system_prompt="You are an intent classifier. Always reply with valid JSON only. No markdown, no explanation.",
            user_prompt=_CLASSIFY_PROMPT.format(message=text),
            max_tokens=256,
            temperature=0.0,
        )
        raw    = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        print(f"[Classify] Input: '{text}' → {result}")
        return result
    except Exception as e:
        print(f"[Classify error] {e}")
        return {"intent": "chat", "params": {}}

# ================================================================
# GEMINI 3 FLASH — Brainstorming (tetap menggunakan Gemini)
# ================================================================
def ai_brainstorm(topic: str) -> str:
    """Use Gemini 3 Flash for brainstorming, enriched with semantic memory."""
    memory_ctx = _memory_context_block(topic, min_score=0.45)
    prompt = (
        f"You are an enthusiastic brainstorming partner on WhatsApp.\n"
        f"Help the user brainstorm creative, actionable ideas for: {topic}"
        f"{memory_ctx}\n\n"
        f"Give 5-7 ideas. Use an emoji for each. Keep each idea concise but inspiring.\n"
        f"End with one short motivational line."
    )
    try:
        response = gemini_client.models.generate_content(model=MODEL_BRAINSTORM, contents=prompt)
        return f"🧠 *Brainstorm: {topic}*\n\n{response.text.strip()}"
    except Exception as e:
        err = str(e).lower()
        print(f"[Brainstorm error] {e}")
        if "not found" in err or "404" in err or "unavailable" in err:
            print("[Brainstorm] Falling back to Groq")
            try:
                result = _groq_complete("", prompt, max_tokens=1024, temperature=0.8)
                return f"🧠 *Brainstorm: {topic}*\n\n{result}"
            except Exception:
                pass
        return "⚠️ Brainstorm failed. Please try again!"

# ================================================================
# GROQ — General AI + Reminder Parser + Event Parser (menggantikan Gemini 2.5 Flash)
# ================================================================
def parse_reminder_with_ai(user_input: str) -> tuple:
    """Use Groq to extract reminder content and datetime."""
    now   = now_jkt()
    today = now.strftime("%Y-%m-%d %H:%M")
    year  = now.year

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
        raw   = _groq_complete("", prompt, max_tokens=64, temperature=0.0)
        parts = raw.split("|")
        if len(parts) < 2:
            raise ValueError("No pipe separator in response")

        content   = parts[0].strip()
        remind_at = parts[1].strip()

        # Validate the parsed datetime is a real date
        datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        return content, remind_at

    except Exception as e:
        print(f"[Reminder parse error] {e}")
        fallback_dt = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d 09:00")
        return user_input, fallback_dt

def ai_chat(user_input: str) -> str:
    """General chat using Groq Llama 3.1 8B, with full conversation history + semantic memory."""
    # Detect explicit context-reset requests
    reset_triggers = {"new topic", "forget that", "start over", "reset chat",
                      "mulai baru", "hapus history", "ganti topik", "clear chat"}
    if user_input.strip().lower() in reset_triggers:
        _clear_conv_history()
        return "🔄 Got it! Fresh start — what's on your mind?"

    # Load rolling conversation history
    history    = _load_conv_history()
    memory_ctx = _memory_context_block(user_input, min_score=0.55)

    system_prompt = (
        "You are a helpful, friendly WhatsApp personal assistant. "
        "Keep replies concise and conversational — this is a chat, not an essay. "
        "Use the conversation history to maintain context across follow-up messages. "
        "If the user refers to something from earlier (e.g. 'that', 'it', 'the one you mentioned'), "
        "look it up in the history and respond accordingly."
        + (f"\n\nRelevant from user's notes & ideas:{memory_ctx}" if memory_ctx else "")
    )

    try:
        reply = _groq_complete(system_prompt, user_input, max_tokens=1024, temperature=0.7, history=history)
    except Exception as e:
        err = str(e)
        if "503" in err or "UNAVAILABLE" in err:
            return "⚠️ AI temporarily overloaded. Try again in a moment!"
        if "429" in err or "rate_limit" in err.lower():
            return "⚠️ API quota reached. Try again later."
        return "⚠️ Something went wrong. Please try again."

    # Persist both turns so next message has context
    _save_conv_turn("user",      user_input)
    _save_conv_turn("assistant", reply)

    return reply

# ================================================================
# DAILY QUOTE — API Ninjas quotes, tailored by Groq
# ================================================================
API_NINJAS_KEY = os.environ["API_NINJAS_KEY"]

# Category map: user hint → API Ninjas category
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



def _fetch_ninja_quote(category: str = "") -> dict | None:
    """Fetch one quote from API Ninjas. Returns dict with 'quote' and 'author', or None on failure."""
    try:
        url    = "https://api.api-ninjas.com/v2/randomquotes"
        params = {"category": category} if category else {}
        resp   = requests.get(url, headers={"X-Api-Key": API_NINJAS_KEY}, params=params, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return data[0]  # {"quote": "...", "author": "...", "category": "..."}
    except Exception as e:
        print(f"[API Ninjas quote error] {e}")
    return None

def _pick_category(context: str) -> str:
    """Map a user context string to an API Ninjas category."""
    lower = context.lower()
    for keyword, category in _QUOTE_CATEGORY_MAP.items():
        if keyword in lower:
            return category
    return "inspirational"  # sensible default

def generate_daily_quote(context: str = "") -> str:
    """Fetch a quote from API Ninjas and return it cleanly formatted."""
    category = _pick_category(context) if context else "inspirational"
    raw = _fetch_ninja_quote(category)

    # Fallback: try without category if first attempt failed
    if not raw:
        raw = _fetch_ninja_quote()

    if not raw:
        return "*Keep going — every step forward counts, no matter how small.*"

    quote  = raw.get("quote", "")
    author = raw.get("author", "Unknown")

    return f"_{quote}_\n{author}"

def _send_scheduled_quote(label: str):
    """Send an auto-scheduled quote to YOUR_NUMBER via Twilio."""
    try:
        body = generate_daily_quote()
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        twilio_client.messages.create(
            from_=TWILIO_SANDBOX_NUMBER,
            to=YOUR_NUMBER,
            body=body
        )
        print(f"[Quote scheduler] {label} quote sent successfully.")
    except Exception as e:
        print(f"[Quote scheduler] Failed to send {label} quote: {e}")

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
        (text, str(now_jkt()))
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Store embedding for semantic memory (Gemini Embedding 2)
    save_embedding("idea", source_id, text)

    try:
        timestamp = now_jkt().strftime("%Y-%m-%d %H:%M")
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
        (text, str(now_jkt()))
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Store embedding for semantic memory (Gemini Embedding 2)
    save_embedding("note", source_id, text)

    try:
        timestamp = now_jkt().strftime("%Y-%m-%d %H:%M")
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
    conn.execute("INSERT INTO tasks (content, timestamp) VALUES (?, ?)", (text, str(now_jkt())))
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
# NEWS → sumy + Groq summary (menggantikan Gemini 2.5 Flash)
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
        summary = _groq_complete("", prompt, max_tokens=1024, temperature=0.5)
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
# CALENDAR EVENT — menggunakan Groq
# ================================================================
def parse_event_with_ai(user_input: str) -> dict | None:
    """Use Groq to extract event title, start, end, description from natural language."""
    now  = now_jkt()
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
        raw  = _groq_complete(
            system_prompt="You are a calendar event parser. Reply with valid JSON only. No markdown, no explanation.",
            user_prompt=prompt,
            max_tokens=256,
            temperature=0.0,
        )
        raw  = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)
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

scheduler = BackgroundScheduler(timezone=TZ_JKT)
scheduler.add_job(check_and_send_reminders, "interval", minutes=1)

# ── Daily quote jobs (Jakarta time) ─────────────────────────────
scheduler.add_job(
    _send_scheduled_quote,
    "cron",
    hour=6, minute=0,
    args=["Good Morning 🌅"],
    id="morning_quote"
)
scheduler.add_job(
    _send_scheduled_quote,
    "cron",
    hour=23, minute=0,
    args=["Good Night 🌙"],
    id="night_quote"
)
scheduler.start()

# ================================================================
# BUDGET CALCULATOR — daily survival calculator until payroll (25th)
# ================================================================

FIXED_EXPENSES = [
    {"name": "House Rent",        "amount": 955_000,  "due_day": 25},
    {"name": "Internet",          "amount": 150_000,  "due_day": None},
    {"name": "Zakat",             "amount": 250_000,  "due_day": 25},
    {"name": "House Maintenance", "amount": 600_000,  "due_day": 9},
]

VARIABLE_BUDGETS = [
    {"name": "Ticket to go home", "budget": 600_000},
    {"name": "Fuel",              "budget": 70_000},
    {"name": "Laundry",          "budget": 60_000},
]

PAYROLL_DAY = 25


def _parse_budget_input(user_input: str) -> dict | None:
    now   = now_jkt()
    today = now.day
    month = now.strftime("%B")
    year  = now.year
    fixed_names    = ", ".join(e["name"] for e in FIXED_EXPENSES)
    variable_names = ", ".join(v["name"] for v in VARIABLE_BUDGETS)

    prompt = f"""You are a budget parser for a personal finance chatbot. Today is the {today}th of {month} {year}.

The user has these fixed monthly expenses: {fixed_names}
The user has these variable monthly budgets: {variable_names}

Extract:
1. "remaining_money": total money right now (integer IDR)
2. "paid_fixed": list of fixed expense names already paid this month
3. "spent_variable": dict of variable budget name → amount spent
4. "pending_conditional": list of conditional expense names still expected this month

Reply ONLY with valid JSON, no markdown:
{{"remaining_money": <int or null>, "paid_fixed": [<names>], "spent_variable": {{"<name>": <amount>}}, "pending_conditional": [<names>]}}

User message: {user_input}"""

    try:
        raw = _groq_complete(
            system_prompt="You are a budget parser. Reply with valid JSON only.",
            user_prompt=prompt,
            max_tokens=300,
            temperature=0.0,
        )
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[Budget parse error] {e}")
        return None


def _budget_interactive_prompt() -> str:
    now       = now_jkt()
    today     = now.day
    days_left = (PAYROLL_DAY - today) if today < PAYROLL_DAY else (31 - today + PAYROLL_DAY)
    lines = [
        f"💰 *Budget Calculator* — {days_left} days until payday (25th)\n",
        "Please tell me:",
        "1️⃣ How much money do you have right now?",
        "2️⃣ Which fixed expenses have you already paid?",
        f"   Options: {', '.join(e['name'] for e in FIXED_EXPENSES)}",
        "3️⃣ How much have you spent from variable budgets?",
        f"   Options: {', '.join(v['name'] for v in VARIABLE_BUDGETS)}",
        "4️⃣ Any conditional expenses still pending? (e.g. Internet)",
        "",
        "💡 *Example:*",
        "\"I have 2.500.000. Already paid: Rent, Zakat, House Maintenance.",
        "Spent: Ticket 300k, Fuel 35k, Laundry 35k. Internet still pending.\"",
    ]
    return "\n".join(lines)


def calculate_budget(user_input: str) -> str:
    now   = now_jkt()
    today = now.day

    if today <= PAYROLL_DAY:
        days_left = PAYROLL_DAY - today
    else:
        import calendar
        days_in_month = calendar.monthrange(now.year, now.month)[1]
        days_left = (days_in_month - today) + PAYROLL_DAY

    bare_triggers = {"budget", "hitung budget", "kalkulasi budget", "budget calculator",
                     "budget harian", "sisa budget", "budget check"}
    if user_input.strip().lower() in bare_triggers:
        return _budget_interactive_prompt()

    parsed = _parse_budget_input(user_input)
    if not parsed or parsed.get("remaining_money") is None:
        return _budget_interactive_prompt()

    remaining      = parsed.get("remaining_money", 0)
    paid_fixed     = [n.lower() for n in (parsed.get("paid_fixed") or [])]
    spent_variable = {k.lower(): v for k, v in (parsed.get("spent_variable") or {}).items()}
    pending_cond   = [n.lower() for n in (parsed.get("pending_conditional") or [])]

    still_owed = []
    for exp in FIXED_EXPENSES:
        name_lower  = exp["name"].lower()
        already_paid = any(name_lower in p or p in name_lower for p in paid_fixed)
        if not already_paid:
            still_owed.append(exp)

    remaining_var = []
    for var in VARIABLE_BUDGETS:
        name_lower = var["name"].lower()
        spent = 0
        for k, v in spent_variable.items():
            if name_lower in k or k in name_lower:
                spent = v
                break
        leftover = var["budget"] - spent
        if leftover > 0:
            remaining_var.append({"name": var["name"], "remaining": leftover, "spent": spent})

    pending_amounts = []
    for exp in FIXED_EXPENSES:
        name_lower = exp["name"].lower()
        if any(name_lower in p or p in name_lower for p in pending_cond):
            if not any(e["name"].lower() == name_lower for e in still_owed):
                pending_amounts.append(exp)

    total_still_owed    = sum(e["amount"] for e in still_owed) + sum(e["amount"] for e in pending_amounts)
    total_var_remaining = sum(v["remaining"] for v in remaining_var)
    total_deductions    = total_still_owed + total_var_remaining
    free_money          = remaining - total_deductions
    daily_budget        = free_money / days_left if days_left > 0 else free_money

    def fmt(n): return f"Rp {int(n):,}".replace(",", ".")

    lines = [f"💰 *Budget Breakdown* — {days_left} days to payday (25th)\n"]
    lines.append(f"💵 Current money: *{fmt(remaining)}*\n")

    if still_owed or pending_amounts:
        lines.append("📋 *Fixed expenses still to pay:*")
        for e in still_owed:
            lines.append(f"  • {e['name']}: {fmt(e['amount'])}")
        for e in pending_amounts:
            lines.append(f"  • {e['name']} (pending): {fmt(e['amount'])}")
        lines.append(f"  ➤ Total: {fmt(total_still_owed)}\n")

    if remaining_var:
        lines.append("🗂️ *Remaining variable budgets:*")
        for v in remaining_var:
            lines.append(f"  • {v['name']}: {fmt(v['remaining'])} (spent {fmt(v['spent'])})")
        lines.append(f"  ➤ Total: {fmt(total_var_remaining)}\n")

    lines.append("📊 *Summary:*")
    lines.append(f"  Money in hand:      {fmt(remaining)}")
    lines.append(f"  Total deductions:   -{fmt(total_deductions)}")
    lines.append(f"  Free money left:    {fmt(free_money)}")
    lines.append(f"  Days until payday:  {days_left} days\n")

    if daily_budget < 0:
        lines.append(f"⚠️ *You're short by {fmt(abs(free_money))}!*")
        lines.append("Consider reducing variable spending.")
    else:
        lines.append(f"✅ *Daily budget: {fmt(daily_budget)}/day*")
        if daily_budget < 50_000:
            lines.append("⚠️ Tight! Keep non-essentials minimal.")
        elif daily_budget < 100_000:
            lines.append("🟡 Manageable. Watch your spending.")
        else:
            lines.append("🟢 You're in a comfortable position!")

    return "\n".join(lines)


# ================================================================

# ================================================================
# DELETE / EDIT — Notes
# ================================================================
def delete_note(keyword: str = None, index: int = None) -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Notes!A:B"
        ).execute()
        rows = result.get("values", [])
        data_rows = [r for r in rows if len(r) >= 2]  # skip empty
        if not data_rows:
            return "📝 No notes to delete."

        # Find target row
        target_i = None
        if index is not None:
            i = int(index) - 1
            if 0 <= i < len(data_rows):
                target_i = i
        elif keyword:
            for i, r in enumerate(data_rows):
                if keyword.lower() in r[1].lower():
                    target_i = i
                    break

        if target_i is None:
            return f"❌ Note not found. Use *get notes* to see your list, then refer by number or keyword."

        deleted_text = data_rows[target_i][1]
        # Sheet row index (1-based, +1 for header if exists)
        header_offset = 1 if rows and rows[0][0].lower() in ("timestamp", "time", "ts", "a") else 0
        sheet_row = target_i + 1 + header_offset  # 1-based

        sheet_id = sheets_svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        notes_sheet_id = next((s["properties"]["sheetId"] for s in sheet_id["sheets"] if s["properties"]["title"] == "Notes"), None)
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"deleteDimension": {"range": {
                "sheetId": notes_sheet_id, "dimension": "ROWS",
                "startIndex": sheet_row - 1, "endIndex": sheet_row
            }}}]}
        ).execute()

        # Also delete from SQLite
        conn = sqlite3.connect("bot.db")
        conn.execute("DELETE FROM notes WHERE content = ?", (deleted_text,))
        conn.commit(); conn.close()
        return f"🗑️ Note deleted: _{deleted_text[:60]}_"
    except Exception as e:
        return f"⚠️ Could not delete note: {e}"


def edit_note(new_content: str, keyword: str = None, index: int = None) -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Notes!A:B"
        ).execute()
        rows = result.get("values", [])
        data_rows = [r for r in rows if len(r) >= 2]
        if not data_rows:
            return "📝 No notes to edit."

        target_i = None
        if index is not None:
            i = int(index) - 1
            if 0 <= i < len(data_rows):
                target_i = i
        elif keyword:
            for i, r in enumerate(data_rows):
                if keyword.lower() in r[1].lower():
                    target_i = i
                    break

        if target_i is None:
            return "❌ Note not found. Use *get notes* to see your list."

        header_offset = 1 if rows and rows[0][0].lower() in ("timestamp", "time", "ts", "a") else 0
        sheet_row = target_i + 1 + header_offset
        timestamp = now_jkt().strftime("%Y-%m-%d %H:%M")

        sheets_svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Notes!A{sheet_row}:B{sheet_row}",
            valueInputOption="RAW",
            body={"values": [[timestamp, new_content]]}
        ).execute()

        # Sync SQLite
        old_text = data_rows[target_i][1]
        conn = sqlite3.connect("bot.db")
        conn.execute("UPDATE notes SET content=?, timestamp=? WHERE content=?", (new_content, str(now_jkt()), old_text))
        conn.commit(); conn.close()
        return f"✏️ Note updated!\n_{new_content[:80]}_"
    except Exception as e:
        return f"⚠️ Could not edit note: {e}"


# ================================================================
# DELETE / EDIT — Ideas
# ================================================================
def delete_idea(keyword: str = None, index: int = None) -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Ideas!A:B"
        ).execute()
        rows = result.get("values", [])
        data_rows = [r for r in rows if len(r) >= 2]
        if not data_rows:
            return "💡 No ideas to delete."

        target_i = None
        if index is not None:
            i = int(index) - 1
            if 0 <= i < len(data_rows):
                target_i = i
        elif keyword:
            for i, r in enumerate(data_rows):
                if keyword.lower() in r[1].lower():
                    target_i = i
                    break

        if target_i is None:
            return "❌ Idea not found. Use *get ideas* to see your list."

        deleted_text = data_rows[target_i][1]
        header_offset = 1 if rows and rows[0][0].lower() in ("timestamp", "time", "ts", "a") else 0
        sheet_row = target_i + 1 + header_offset

        sheet_meta = sheets_svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        ideas_sheet_id = next((s["properties"]["sheetId"] for s in sheet_meta["sheets"] if s["properties"]["title"] == "Ideas"), None)
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"deleteDimension": {"range": {
                "sheetId": ideas_sheet_id, "dimension": "ROWS",
                "startIndex": sheet_row - 1, "endIndex": sheet_row
            }}}]}
        ).execute()

        conn = sqlite3.connect("bot.db")
        conn.execute("DELETE FROM ideas WHERE content = ?", (deleted_text,))
        conn.commit(); conn.close()
        return f"🗑️ Idea deleted: _{deleted_text[:60]}_"
    except Exception as e:
        return f"⚠️ Could not delete idea: {e}"


def edit_idea(new_content: str, keyword: str = None, index: int = None) -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Ideas!A:B"
        ).execute()
        rows = result.get("values", [])
        data_rows = [r for r in rows if len(r) >= 2]
        if not data_rows:
            return "💡 No ideas to edit."

        target_i = None
        if index is not None:
            i = int(index) - 1
            if 0 <= i < len(data_rows):
                target_i = i
        elif keyword:
            for i, r in enumerate(data_rows):
                if keyword.lower() in r[1].lower():
                    target_i = i
                    break

        if target_i is None:
            return "❌ Idea not found. Use *get ideas* to see your list."

        header_offset = 1 if rows and rows[0][0].lower() in ("timestamp", "time", "ts", "a") else 0
        sheet_row = target_i + 1 + header_offset
        timestamp = now_jkt().strftime("%Y-%m-%d %H:%M")

        sheets_svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Ideas!A{sheet_row}:B{sheet_row}",
            valueInputOption="RAW",
            body={"values": [[timestamp, new_content]]}
        ).execute()

        old_text = data_rows[target_i][1]
        conn = sqlite3.connect("bot.db")
        conn.execute("UPDATE ideas SET content=?, timestamp=? WHERE content=?", (new_content, str(now_jkt()), old_text))
        conn.commit(); conn.close()
        return f"✏️ Idea updated!\n_{new_content[:80]}_"
    except Exception as e:
        return f"⚠️ Could not edit idea: {e}"


# ================================================================
# DELETE / EDIT — Tasks (Google Tasks)
# ================================================================
def delete_task(keyword: str = None, index: int = None) -> str:
    try:
        _, _, tasks_svc = get_google_services()
        result = tasks_svc.tasks().list(tasklist="@default", showCompleted=False).execute()
        items = result.get("items", [])
        if not items:
            return "📋 No tasks to delete."

        target = None
        if index is not None:
            i = int(index) - 1
            if 0 <= i < len(items):
                target = items[i]
        elif keyword:
            for t in items:
                if keyword.lower() in t["title"].lower():
                    target = t
                    break

        if not target:
            return "❌ Task not found. Use *get tasks* to see your list."

        tasks_svc.tasks().delete(tasklist="@default", task=target["id"]).execute()

        conn = sqlite3.connect("bot.db")
        conn.execute("DELETE FROM tasks WHERE content = ?", (target["title"],))
        conn.commit(); conn.close()
        return f"🗑️ Task deleted: _{target['title']}_"
    except Exception as e:
        return f"⚠️ Could not delete task: {e}"


def edit_task(new_title: str, keyword: str = None, index: int = None) -> str:
    try:
        _, _, tasks_svc = get_google_services()
        result = tasks_svc.tasks().list(tasklist="@default", showCompleted=False).execute()
        items = result.get("items", [])
        if not items:
            return "📋 No tasks to edit."

        target = None
        if index is not None:
            i = int(index) - 1
            if 0 <= i < len(items):
                target = items[i]
        elif keyword:
            for t in items:
                if keyword.lower() in t["title"].lower():
                    target = t
                    break

        if not target:
            return "❌ Task not found. Use *get tasks* to see your list."

        tasks_svc.tasks().patch(
            tasklist="@default", task=target["id"], body={"title": new_title}
        ).execute()

        old_title = target["title"]
        conn = sqlite3.connect("bot.db")
        conn.execute("UPDATE tasks SET content=? WHERE content=?", (new_title, old_title))
        conn.commit(); conn.close()
        return f"✏️ Task updated!\n_{new_title}_"
    except Exception as e:
        return f"⚠️ Could not edit task: {e}"


# ================================================================
# DELETE / EDIT — Google Calendar Events
# ================================================================
def delete_event(keyword: str) -> str:
    try:
        calendar_svc, _, _ = get_google_services()
        now = now_jkt()
        result = calendar_svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
            q=keyword
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"❌ No upcoming event found matching '{keyword}'. Use *get events* to check your calendar."

        ev = events[0]
        calendar_svc.events().delete(calendarId="primary", eventId=ev["id"]).execute()
        return f"🗑️ Event deleted: _{ev.get('summary', keyword)}_"
    except Exception as e:
        return f"⚠️ Could not delete event: {e}"


def edit_event(keyword: str, new_title: str = None, new_start: str = None, new_end: str = None, new_description: str = None) -> str:
    try:
        calendar_svc, _, _ = get_google_services()
        now = now_jkt()
        result = calendar_svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
            q=keyword
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"❌ No upcoming event found matching '{keyword}'. Use *get events* to check your calendar."

        ev = events[0]
        body = {}
        if new_title:
            body["summary"] = new_title
        if new_start:
            dt_start = localize_jkt(datetime.datetime.strptime(new_start, "%Y-%m-%d %H:%M"))
            body["start"] = {"dateTime": dt_start.isoformat(), "timeZone": "Asia/Jakarta"}
        if new_end:
            dt_end = localize_jkt(datetime.datetime.strptime(new_end, "%Y-%m-%d %H:%M"))
            body["end"] = {"dateTime": dt_end.isoformat(), "timeZone": "Asia/Jakarta"}
        if new_description:
            body["description"] = new_description

        if not body:
            return "⚠️ Nothing to update. Specify a new title, time, or description."

        calendar_svc.events().patch(calendarId="primary", eventId=ev["id"], body=body).execute()
        old_title = ev.get("summary", keyword)
        return f"✏️ Event _{old_title}_ updated!"
    except Exception as e:
        return f"⚠️ Could not edit event: {e}"


# ================================================================
# DELETE — Reminders (local DB + Google Calendar)
# ================================================================
def delete_reminder(keyword: str) -> str:
    try:
        conn = sqlite3.connect("bot.db")
        rows = conn.execute(
            "SELECT id, content, remind_at FROM reminders WHERE done=0 AND content LIKE ?",
            (f"%{keyword}%",)
        ).fetchall()
        if not rows:
            conn.close()
            return f"❌ No reminder found matching '{keyword}'. Use *get reminders* to see your list."

        row = rows[0]
        conn.execute("DELETE FROM reminders WHERE id = ?", (row[0],))
        conn.commit(); conn.close()

        # Try to delete from Google Calendar too
        try:
            calendar_svc, _, _ = get_google_services()
            now = now_jkt()
            result = calendar_svc.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                maxResults=20,
                singleEvents=True,
                orderBy="startTime",
                q=row[1]
            ).execute()
            for ev in result.get("items", []):
                if keyword.lower() in ev.get("summary", "").lower():
                    calendar_svc.events().delete(calendarId="primary", eventId=ev["id"]).execute()
                    break
        except Exception:
            pass

        return f"🗑️ Reminder deleted: _{row[1]}_ (was set for {row[2]})"
    except Exception as e:
        return f"⚠️ Could not delete reminder: {e}"

# WEBHOOK — AI-powered intent routing
# ================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.form.get("Body", "").strip()
    lower    = incoming.lower()
    resp     = MessagingResponse()
    msg      = resp.message()

    # ── Step 0a: Handle pending reset confirmation ──────────────────
    if _is_pending_reset():
        _set_pending_reset(False)
        _touch_last_active()
        yes_words = {"yes", "ya", "yep", "yup", "reset", "clear", "iya", "ok", "okay", "sure"}
        no_words  = {"no", "nope", "tidak", "nggak", "ngga", "lanjut", "continue", "stay", "keep"}
        if any(w in lower for w in yes_words):
            _clear_conv_history()
            msg.body("🔄 Session reset! Fresh start — what's on your mind?")
        elif any(w in lower for w in no_words):
            msg.body("👍 Continuing your previous session. What's up?")
        else:
            # Ambiguous — treat as "no" and process normally, but re-run through intent router
            # by falling through after clearing the flag (already done above)
            msg.body("👍 Keeping your session. What's up?")
        return str(resp)

    # ── Step 0b: Session timeout check ─────────────────────────────
    minutes_idle = _minutes_since_last_active()
    if minutes_idle is not None and minutes_idle >= SESSION_TIMEOUT_MINUTES:
        _set_pending_reset(True)
        _touch_last_active()
        idle_str = f"{int(minutes_idle)} minutes"
        msg.body(
            f"⏱️ It's been {idle_str} since your last message.\n\n"
            f"Start a *fresh session* or continue where you left off?\n\n"
            f"Reply *yes* to reset  |  *no* to continue"
        )
        return str(resp)

    # Update last_active for every normal message
    _touch_last_active()

    # Step 0c: Hard-coded keyword shortcuts — never go through AI classifier
    # Only trigger on explicit /logs command to avoid false positives.
    if lower.startswith("/logs"):
        n = 20
        nums = re.findall(r"\d+", incoming)
        if nums:
            n = min(int(nums[0]), 50)
        logs = get_recent_logs(n)
        logs_truncated = logs[-1400:]
        msg.body(f"🖥️ *Last {n} log lines:*\n\n{logs_truncated}")
        return str(resp)

    # Step 1: Classify intent dengan Groq Llama 3.1 8B
    classified = classify_intent(incoming)
    intent     = classified.get("intent", "chat")
    params     = classified.get("params", {})

    # Step 2: Route to the appropriate handler
    reply_text = ""

    if intent == "reminder":
        content, remind_at = parse_reminder_with_ai(incoming)
        reply_text = save_reminder(content, remind_at)

    elif intent == "get_reminders":
        date_hint  = params.get("date") or None
        reply_text = get_reminders_list(date_hint)

    elif intent == "complete_task":
        keyword = params.get("keyword") or re.sub(
            r"complete task|finish task|done task|selesai task", "", lower
        ).strip(" :?!")
        reply_text = complete_task(keyword)

    elif intent == "get_tasks":
        reply_text = get_tasks()

    elif intent == "add_task":
        content    = params.get("content") or re.sub(
            r"add task|new task|tambah task|create task|task:", "", lower
        ).strip(" :?!") or incoming
        reply_text = save_task(content)

    elif intent == "get_notes":
        reply_text = get_notes()

    elif intent == "add_note":
        content    = params.get("content") or re.sub(
            r"note:|notes:|add note|save note|catatan:|catat", "", lower
        ).strip(" :?!") or incoming
        reply_text = save_note(content)

    elif intent == "get_ideas":
        reply_text = get_ideas()

    elif intent == "add_idea":
        content    = params.get("content") or re.sub(
            r"idea:|save idea|add idea|ide:|simpan ide", "", lower
        ).strip(" :?!") or incoming
        reply_text = save_idea(content)

    elif intent == "news":
        topic = params.get("content") or lower
        for w in ["news", "berita", "headline", "latest", "terbaru", "about", "tentang", "get", "show", "give me"]:
            topic = topic.replace(w, "").strip(" ?!.,")
        reply_text = get_news(topic or "world")

    elif intent == "brainstorm":
        topic      = params.get("content") or re.sub(
            r"brainstorm|ide|ideas?|pikir|think about|think of", "", lower
        ).strip(" :?!") or incoming
        reply_text = ai_brainstorm(topic)

    elif intent == "get_events":
        date_hint = params.get("date") or None
        if not date_hint:
            date_hint = _parse_date_from_message(incoming)
        reply_text = get_events(date_hint, incoming)

    elif intent == "add_event":
        parsed = parse_event_with_ai(incoming)
        if parsed and parsed.get("title") and parsed.get("start"):
            reply_text = save_event(
                parsed["title"].strip(),
                parsed["start"].strip(),
                parsed["end"].strip() if parsed.get("end") else None,
                parsed.get("description", "")
            )
        else:
            reply_text = (
                "⚠️ Could not understand the event.\n"
                "Try: *Add event Team lunch on April 22 at 1pm*\n"
                "Or: *New event Meeting tomorrow at 3pm for 2 hours*"
            )

    elif intent == "search_memory":
        results = semantic_search(incoming, top_k=5, min_score=0.45)
        if results:
            items = [
                f"{i+1}. {r['content']} _({r['source_type']}, {round(r['score']*100)}% match)_"
                for i, r in enumerate(results)
            ]
            reply_text = "🔍 *Found in your memory:*\n\n" + "\n".join(items)
        else:
            reply_text = "🔍 Nothing relevant found in your notes or ideas."

    elif intent == "quote":
        context    = re.sub(
            r"quote|motivate me|inspire me|motivasi|inspirasi|give me a|berikan|kasih",
            "", lower
        ).strip(" :?!")
        reply_text = generate_daily_quote(context)

    elif intent == "budget":
        reply_text = calculate_budget(incoming)

    elif intent == "delete_note":
        idx = params.get("index")
        kw  = params.get("keyword") or params.get("content")
        reply_text = delete_note(keyword=kw, index=int(idx) if idx else None)

    elif intent == "edit_note":
        idx         = params.get("index")
        kw          = params.get("keyword")
        new_content = params.get("content") or ""
        reply_text  = edit_note(new_content, keyword=kw, index=int(idx) if idx else None)

    elif intent == "delete_idea":
        idx = params.get("index")
        kw  = params.get("keyword") or params.get("content")
        reply_text = delete_idea(keyword=kw, index=int(idx) if idx else None)

    elif intent == "edit_idea":
        idx         = params.get("index")
        kw          = params.get("keyword")
        new_content = params.get("content") or ""
        reply_text  = edit_idea(new_content, keyword=kw, index=int(idx) if idx else None)

    elif intent == "delete_task":
        idx = params.get("index")
        kw  = params.get("keyword") or params.get("content")
        reply_text = delete_task(keyword=kw, index=int(idx) if idx else None)

    elif intent == "edit_task":
        idx       = params.get("index")
        kw        = params.get("keyword")
        new_title = params.get("content") or ""
        reply_text = edit_task(new_title, keyword=kw, index=int(idx) if idx else None)

    elif intent == "delete_event":
        kw = params.get("keyword") or params.get("content") or incoming
        reply_text = delete_event(kw)

    elif intent == "edit_event":
        kw    = params.get("keyword") or ""
        parsed = parse_event_with_ai(incoming)
        reply_text = edit_event(
            keyword=kw,
            new_title=parsed.get("title") if parsed else None,
            new_start=parsed.get("start") if parsed else None,
            new_end=parsed.get("end") if parsed else None,
            new_description=parsed.get("description") if parsed else None,
        )

    elif intent == "delete_reminder":
        kw = params.get("keyword") or params.get("content") or incoming
        reply_text = delete_reminder(kw)

    else:  # chat — Groq Llama 3.1 8B with full conversation history
        reply_text = ai_chat(incoming)

    # ── Save every turn (except chat, which saves itself) to conversation history ──
    # This lets follow-up messages like "can you edit that?" have full context.
    if intent != "chat" and reply_text:
        _save_conv_turn("user",      incoming)
        _save_conv_turn("assistant", reply_text)

    msg.body(reply_text)
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
    logs = get_all_logs(n).replace("<", "&lt;").replace(">", "&gt;")
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
    app.run(host="0.0.0.0", debug=True, use_reloader=False,port=int(os.environ.get("PORT", 5000)))