from dotenv import load_dotenv
load_dotenv('environtment.env')  # add this before anything else

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
import sqlite3, requests, os, datetime, pickle, re

app = Flask(__name__)

# --- Config ---
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
NEWS_API_KEY = os.environ["NEWS_API_KEY"]
YOUR_NUMBER = os.environ["YOUR_NUMBER"]
TWILIO_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_SANDBOX_NUMBER = "whatsapp:+14155238886"
SPREADSHEET_ID = os.environ["GOOGLE_SHEET_ID"]

# Google API scopes
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks"
]

# --- Google Auth ---
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
    sheets   = build("sheets", "v4", credentials=creds)
    tasks    = build("tasks", "v1", credentials=creds)
    return calendar, sheets, tasks

calendar_service, sheets_service, tasks_service = get_google_services()

# --- Database setup (local backup) ---
def init_db():
    conn = sqlite3.connect("bot.db")
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS ideas (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, content TEXT, remind_at TEXT, done INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT, done INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()

init_db()

# ================================================================
# REMINDER → Google Calendar
# ================================================================
def save_reminder(text, remind_at):
    # Save locally
    conn = sqlite3.connect("bot.db")
    conn.execute("INSERT INTO reminders (content, remind_at) VALUES (?, ?)", (text, remind_at))
    conn.commit()
    conn.close()

    # Sync to Google Calendar
    try:
        dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        dt_end = dt + datetime.timedelta(minutes=30)
        event = {
            "summary": f"⏰ {text}",
            "start": {"dateTime": dt.isoformat(), "timeZone": "Asia/Jakarta"},
            "end":   {"dateTime": dt_end.isoformat(), "timeZone": "Asia/Jakarta"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "email", "minutes": 10}
                ]
            }
        }
        calendar_service.events().insert(calendarId="primary", body=event).execute()
        return f"⏰ Reminder set for *{remind_at}*!\n📅 Also added to Google Calendar."
    except Exception as e:
        return f"⏰ Reminder saved locally for *{remind_at}*.\n⚠️ Calendar sync failed: {str(e)}"

# ================================================================
# IDEAS → Google Sheets (Ideas tab)
# ================================================================
def save_idea(text):
    # Save locally
    conn = sqlite3.connect("bot.db")
    conn.execute(
        "INSERT INTO ideas (content, timestamp) VALUES (?, ?)",
        (text, str(datetime.datetime.now()))
    )
    conn.commit()
    conn.close()

    # Save to Google Sheets, starting at row 2
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Ideas!A2:B",  # start at row 2
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",  # append below last row
            body={"values": [[timestamp, text]]}
        ).execute()
        return f"💡 Idea saved!\n📊 Also added to Google Sheets."
    except Exception as e:
        return f"💡 Idea saved locally.\n⚠️ Sheets sync failed: {str(e)}"

def get_ideas():
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Ideas!A:B"
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return "💡 No ideas saved yet."
        recent = rows[-10:]
        return "💡 *Your ideas:*\n\n" + "\n".join([f"{i+1}. {r[1]} _({r[0]})_" for i, r in enumerate(recent) if len(r) >= 2])
    except:
        conn = sqlite3.connect("bot.db")
        rows = conn.execute("SELECT content, timestamp FROM ideas ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "💡 No ideas saved yet."
        return "💡 *Your ideas:*\n\n" + "\n".join([f"{i+1}. {r[0]} _({r[1][:10]})_" for i, r in enumerate(rows)])

# ================================================================
# NOTES → Google Sheets (Notes tab)
# ================================================================
def save_note(text):
    conn = sqlite3.connect("bot.db")
    conn.execute(
        "INSERT INTO notes (content, timestamp) VALUES (?, ?)",
        (text, str(datetime.datetime.now()))
    )
    conn.commit()
    conn.close()

    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        sheets_service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Notes!A2:B",  # start at row 2
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[timestamp, text]]}
        ).execute()
        return f"📝 Note saved!\n📊 Also added to Google Sheets."
    except Exception as e:
        return f"📝 Note saved locally.\n⚠️ Sheets sync failed: {str(e)}"

def get_notes():
    try:
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range="Notes!A:B"
        ).execute()
        rows = result.get("values", [])
        if not rows:
            return "📝 No notes saved yet."
        recent = rows[-10:]
        return "📝 *Your notes:*\n\n" + "\n".join([f"{i+1}. {r[1]} _({r[0]})_" for i, r in enumerate(recent) if len(r) >= 2])
    except:
        conn = sqlite3.connect("bot.db")
        rows = conn.execute("SELECT content, timestamp FROM notes ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "📝 No notes saved yet."
        return "📝 *Your notes:*\n\n" + "\n".join([f"{i+1}. {r[0]} _({r[1][:10]})_" for i, r in enumerate(rows)])

# ================================================================
# TASKS → Google Tasks
# ================================================================
def save_task(text):
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

def get_tasks():
    try:
        result = tasks_service.tasks().list(tasklist="@default", showCompleted=False).execute()
        items = result.get("items", [])
        if not items:
            return "📋 No pending tasks."
        return "📋 *Your tasks:*\n\n" + "\n".join([f"{i+1}. {t['title']}" for i, t in enumerate(items[:10])])
    except:
        conn = sqlite3.connect("bot.db")
        rows = conn.execute("SELECT content FROM tasks WHERE done=0 ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "📋 No pending tasks."
        return "📋 *Your tasks:*\n\n" + "\n".join([f"{i+1}. {r[0]}" for i, r in enumerate(rows)])

def complete_task(keyword):
    try:
        result = tasks_service.tasks().list(tasklist="@default", showCompleted=False).execute()
        items = result.get("items", [])
        matched = [t for t in items if keyword.lower() in t["title"].lower()]
        if not matched:
            return f"❌ No task found matching '{keyword}'."
        t = matched[0]
        tasks_service.tasks().patch(
            tasklist="@default",
            task=t["id"],
            body={"status": "completed"}
        ).execute()
        return f"✅ Task *'{t['title']}'* marked as complete!"
    except Exception as e:
        return f"⚠️ Could not complete task: {str(e)}"

# ================================================================
# NEWS → sumy (0 Gemini calls)
# ================================================================
def get_news(topic):
    # Prepare NewsAPI query
    url = (
        f"https://newsapi.org/v2/everything"
        f"?q={topic}"
        f"&apiKey={NEWS_API_KEY}"
        f"&pageSize=5"
        f"&language=en"
        f"&sortBy=relevancy"
        f"&from={(datetime.datetime.now() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')}"
    )

    try:
        data = requests.get(url, timeout=10).json()
        articles = data.get("articles", [])
    except Exception as e:
        print(f"Error fetching NewsAPI: {e}")
        return f"📭 Could not fetch news for *{topic}*. Try again later."

    if not articles:
        return f"📭 No news found for *{topic}*."

    # Pick the first relevant article
    a = articles[0]

    title = a.get('title', 'No title')
    source = a.get('source', {}).get('name', 'Unknown source')
    article_url = a.get('url', '')
    published = a.get('publishedAt', '')[:10]

    # Try to get full article text
    raw_text = a.get('content') or a.get('description') or ''
    if article_url:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            page = requests.get(article_url, headers=headers, timeout=10)
            soup = BeautifulSoup(page.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            paragraphs = soup.find_all("p")
            scraped_text = " ".join(p.get_text().strip() for p in paragraphs if len(p.get_text().strip()) > 0)
            if len(scraped_text) > len(raw_text):
                raw_text = scraped_text[:5000]
        except Exception as e:
            print(f"Error scraping article page: {e}")

    # If still empty, fallback to short notice
    if not raw_text:
        raw_text = a.get('description') or "Content not available."

    # Prepare Gemini summarization prompt
    try:
        prompt = f"""You are a news summarizer for WhatsApp. Summarize the article below in a structured, easy-to-read format.

🔍 *What happened:*
[2-3 sentences explaining the main event clearly]

👥 *Who is impacted:*
[Who is affected and how — people, companies, countries, etc.]

⚠️ *Why it matters:*
[The significance or consequences of this event]

✅ *Solution / Response:* (skip if none)
[Any actions taken or official responses]

📌 *Key takeaway:*
[One concise sentence]

Article title: {title}
Article content: {raw_text}"""

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        summary = response.text.strip()
    except Exception as e:
        print(f"Error generating Gemini summary: {e}")
        summary = raw_text[:500] + "..."

    return (
        f"📰 *{title}*\n"
        f"🗞 {source} · {published}\n"
        f"─────────────────\n"
        f"{summary}\n\n"
        f"🔗 {article_url}"
    )
# ================================================================
# AI — only for reminder datetime parsing and general chat
# ================================================================
def parse_reminder_with_ai(user_input):
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""Extract the reminder content and datetime from this message.
Today is {today}. Reply ONLY in this format with nothing else: CONTENT | YYYY-MM-DD HH:MM
If no datetime found, use tomorrow 09:00.
Message: {user_input}"""
        )
        parts = response.text.strip().split("|")
        content   = parts[0].strip()
        remind_at = parts[1].strip() if len(parts) > 1 else (
            datetime.datetime.now() + datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d 09:00")
        return content, remind_at
    except:
        return user_input, (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d 09:00")

def ai_chat(user_input):
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"You are a helpful WhatsApp personal assistant. Reply concisely and friendly.\n\nUser: {user_input}"
        )
        return response.text.strip()
    except Exception as e:
        error_str = str(e)
        if "503" in error_str or "UNAVAILABLE" in error_str:
            return "⚠️ AI temporarily overloaded. Try again in a moment!"
        if "429" in error_str or "QUOTA" in error_str:
            return "⚠️ API quota reached. Try again later."
        return "⚠️ Something went wrong. Please try again."

# ================================================================
# Reminder scheduler — checks every minute
# ================================================================
def check_and_send_reminders():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = sqlite3.connect("bot.db")
    rows = conn.execute(
        "SELECT id, content FROM reminders WHERE remind_at = ? AND done = 0", (now,)
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

def save_event(title, start_dt, end_dt=None, description=""):
    """
    Save an event locally and to Google Calendar.
    start_dt / end_dt: string in "YYYY-MM-DD HH:MM" format
    """
    end_dt = end_dt or (datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M") + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    
    # Save locally in reminders table (optional)
    conn = sqlite3.connect("bot.db")
    conn.execute("INSERT INTO reminders (content, remind_at) VALUES (?, ?)", (title, start_dt))
    conn.commit()
    conn.close()

    try:
        dt_start = datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M")
        dt_end = datetime.datetime.strptime(end_dt, "%Y-%m-%d %H:%M")
        event = {
            "summary": title,
            "description": description,
            "start": {"dateTime": dt_start.isoformat(), "timeZone": "Asia/Jakarta"},
            "end": {"dateTime": dt_end.isoformat(), "timeZone": "Asia/Jakarta"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "email", "minutes": 10}
                ]
            }
        }
        calendar_service.events().insert(calendarId="primary", body=event).execute()
        return f"📅 Event '{title}' added from {start_dt} to {end_dt}!"
    except Exception as e:
        return f"⚠️ Could not add event to Calendar: {str(e)}"

# ================================================================
# Webhook — keyword routing
# ================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming = request.form.get("Body", "").strip()
    lower    = incoming.lower()
    resp     = MessagingResponse()
    msg      = resp.message()

    # --- REMINDER ---
    if any(w in lower for w in ["remind me", "remind", "reminder", "ingatkan", "set alarm", "alarm"]):
        content, remind_at = parse_reminder_with_ai(incoming)
        msg.body(save_reminder(content, remind_at))

    # --- TASK: complete ---
    elif any(w in lower for w in ["complete task", "finish task", "done task", "selesai task"]):
        keyword = re.sub(r"complete task|finish task|done task|selesai task", "", lower).strip(" :?!")
        msg.body(complete_task(keyword))

    # --- TASK: list ---
    elif any(w in lower for w in ["my tasks", "list task", "show task", "task saya", "lihat task", "get task"]):
        msg.body(get_tasks())

    # --- TASK: add ---
    elif any(w in lower for w in ["add task", "new task", "tambah task", "create task", "task:"]):
        content = re.sub(r"add task|new task|tambah task|create task|task:", "", lower).strip(" :?!")
        content = content or incoming
        msg.body(save_task(content))

    # --- NOTE: list ---
    elif any(w in lower for w in ["my notes", "list notes", "show notes", "catatan saya", "lihat catatan"]):
        msg.body(get_notes())

    # --- NOTE: add ---
    elif any(w in lower for w in ["note:", "notes:", "add note", "save note", "catatan:", "catat"]):
        content = re.sub(r"note:|notes:|add note|save note|catatan:|catat", "", lower).strip(" :?!")
        content = content or incoming
        msg.body(save_note(content))

    # --- IDEA: list ---
    elif any(w in lower for w in ["my ideas", "list ideas", "show ideas", "ide saya", "lihat ide"]):
        msg.body(get_ideas())

    # --- IDEA: add ---
    elif any(w in lower for w in ["idea:", "save idea", "add idea", "ide:", "simpan ide"]):
        content = re.sub(r"idea:|save idea|add idea|ide:|simpan ide", "", lower).strip(" :?!")
        content = content or incoming
        msg.body(save_idea(content))

    # --- NEWS ---
    elif any(w in lower for w in ["news", "berita", "headline"]):
        topic = lower
        for w in ["news", "berita", "headline", "latest", "terbaru", "about", "tentang", "get", "show", "give me"]:
            topic = topic.replace(w, "").strip(" ?!.,")
        topic = topic or "world"
        msg.body(get_news(topic))
    
    # --- EVENT: add ---
    elif any(w in lower for w in ["add event", "new event", "buat event"]):
        # Example format: "Add event Meeting on 2026-04-15 14:00 to 2026-04-15 15:30: Discuss project"
        m = re.match(r"(?:add event|new event|buat event)\s+(.+?)\s+on\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})(?:\s+to\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}))?(?:\s*:\s*(.*))?", incoming, re.I)
        if m:
            title, start_dt, end_dt, description = m.groups()
            end_dt = end_dt or None
            description = description or ""
            msg.body(save_event(title.strip(), start_dt.strip(), end_dt.strip() if end_dt else None, description.strip()))
        else:
            msg.body("⚠️ Could not parse event. Use format: Add event <title> on YYYY-MM-DD HH:MM to YYYY-MM-DD HH:MM : optional description")

        # --- GENERAL CHAT ---
    else:
       msg.body(ai_chat(incoming))

    return str(resp)

if __name__ == "__main__":
    app.run(debug=True, port=5000)