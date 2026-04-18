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
import sqlite3, requests, os, datetime, pickle, re, json, numpy as np

app = Flask(__name__)

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
# GOOGLE AUTH
# ================================================================
def get_google_services():
    creds = None
    if os.path.exists("token.pickle"):
        with open("token.pickle", "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open("token.pickle", "wb") as token:
            pickle.dump(creds, token)
    calendar = build("calendar", "v3", credentials=creds)
    sheets   = build("sheets",   "v4", credentials=creds)
    tasks    = build("tasks",    "v1", credentials=creds)
    return calendar, sheets, tasks

calendar_service, sheets_service, tasks_service = get_google_services()

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
        (source_type, source_id, content, pickle.dumps(embedding), str(datetime.datetime.now()))
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
# GEMINI 2.5 FLASH LITE — Intent Classifier
# ================================================================
_CLASSIFY_PROMPT = """You are an intent classifier for a WhatsApp personal assistant.
Classify the user's message into exactly ONE of these intents:

  reminder      — set a reminder or alarm
  add_note      — save a note or memo
  get_notes     — list or read saved notes
  add_idea      — save an idea
  get_ideas     — list or read saved ideas
  add_task      — add a to-do task
  get_tasks     — list pending tasks
  complete_task — mark a task as done
  news          — get news or headlines
  brainstorm    — brainstorm, explore ideas, get creative suggestions
  add_event     — add a calendar event
  search_memory — ask about something that might be in their notes/ideas
  chat          — general conversation or anything else

Reply ONLY with a JSON object (no markdown, no preamble):
{{"intent": "<intent>", "params": {{"content": "<extracted content if any>", "keyword": "<keyword if applicable>"}}}}

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
    now     = datetime.datetime.now()
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
        event  = {
            "summary": f"⏰ {text}",
            "start":   {"dateTime": dt.isoformat(),     "timeZone": "Asia/Jakarta"},
            "end":     {"dateTime": dt_end.isoformat(), "timeZone": "Asia/Jakarta"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "email", "minutes": 10}
                ]
            }
        }
        calendar_service.events().insert(calendarId="primary", body=event).execute()
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
        (text, str(datetime.datetime.now()))
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Store embedding for semantic memory (Gemini Embedding 2)
    save_embedding("idea", source_id, text)

    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        sheets_service.spreadsheets().values().append(
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
        result = sheets_service.spreadsheets().values().get(
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
        (text, str(datetime.datetime.now()))
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Store embedding for semantic memory (Gemini Embedding 2)
    save_embedding("note", source_id, text)

    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        sheets_service.spreadsheets().values().append(
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
        result = sheets_service.spreadsheets().values().get(
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
    conn.execute("INSERT INTO tasks (content, timestamp) VALUES (?, ?)", (text, str(datetime.datetime.now())))
    conn.commit()
    conn.close()
    try:
        tasks_service.tasks().insert(
            tasklist="@default",
            body={"title": text, "status": "needsAction"}
        ).execute()
        return f"✅ Task added!\n📋 Also added to Google Tasks."
    except Exception as e:
        return f"✅ Task saved locally.\n⚠️ Google Tasks sync failed: {str(e)}"

def get_tasks() -> str:
    try:
        result = tasks_service.tasks().list(tasklist="@default", showCompleted=False).execute()
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
        result  = tasks_service.tasks().list(tasklist="@default", showCompleted=False).execute()
        items   = result.get("items", [])
        matched = [t for t in items if keyword.lower() in t["title"].lower()]
        if not matched:
            return f"❌ No task found matching '{keyword}'."
        t = matched[0]
        tasks_service.tasks().patch(
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
        f"&from={(datetime.datetime.now() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')}"
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
    now  = datetime.datetime.now()
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
        event = {
            "summary":     title,
            "description": description,
            "start": {"dateTime": datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M").isoformat(), "timeZone": "Asia/Jakarta"},
            "end":   {"dateTime": datetime.datetime.strptime(end_dt,   "%Y-%m-%d %H:%M").isoformat(), "timeZone": "Asia/Jakarta"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "email", "minutes": 10}
                ]
            }
        }
        calendar_service.events().insert(calendarId="primary", body=event).execute()
        return f"📅 *{title}* added!\n🗓 {start_pretty} → {end_pretty}"
    except Exception as e:
        return f"⚠️ Could not add event to Calendar: {str(e)}"

# ================================================================
# REMINDER SCHEDULER
# ================================================================
def check_and_send_reminders():
    # Use a 90-second window so reminders are never missed due to scheduler timing drift
    now    = datetime.datetime.now()
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

    # Step 1: Classify intent with Gemini 2.5 Flash Lite
    classified = classify_intent(incoming)
    intent     = classified.get("intent", "chat")
    params     = classified.get("params", {})

    # Step 2: Route to the appropriate handler + model
    if intent == "reminder":
        content, remind_at = parse_reminder_with_ai(incoming)
        msg.body(save_reminder(content, remind_at))

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
