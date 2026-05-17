import sqlite3, datetime, re, json
from config import now_jkt, localize_jkt
from google_auth import get_google_services
from ai.groq_client import groq_complete
from tracer import trace

DB_PATH = "bot.db"

# ================================================================
# AI EVENT PARSER
# ================================================================
@trace
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
        raw  = groq_complete(
            system_prompt="You are a calendar event parser. Reply with valid JSON only. No markdown, no explanation.",
            user_prompt=prompt,
            max_tokens=256,
            temperature=0.0,
        )
        raw  = re.sub(r"```json|```", "", raw).strip()
        data = json.loads(raw)
        datetime.datetime.strptime(data["start"], "%Y-%m-%d %H:%M")
        if data.get("end"):
            datetime.datetime.strptime(data["end"], "%Y-%m-%d %H:%M")
        return data
    except Exception as e:
        print(f"[Event parse error] {e}")
        return None

# ================================================================
# AI DATE PARSER (for get_events fallback)
# ================================================================
@trace
def parse_date_from_message(text: str) -> str | None:
    """Use Groq to extract a YYYY-MM-DD date from a natural language message."""
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
        result = groq_complete("", prompt, max_tokens=20, temperature=0.0).strip()
        if result == "NONE" or not result:
            return None
        datetime.datetime.strptime(result, "%Y-%m-%d")
        return result
    except Exception:
        return None

# ================================================================
# SAVE EVENT
# ================================================================
@trace
def save_event(title: str, start_dt: str, end_dt: str = None, description: str = "") -> str:
    end_dt = end_dt or (
        datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M") + datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO reminders (content, remind_at) VALUES (?, ?)", (title, start_dt))
    conn.commit()
    conn.close()

    start_pretty = datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M").strftime("%A, %d %B %Y at %H:%M")
    end_pretty   = datetime.datetime.strptime(end_dt,   "%Y-%m-%d %H:%M").strftime("%H:%M")

    try:
        dt_start_aware = localize_jkt(datetime.datetime.strptime(start_dt, "%Y-%m-%d %H:%M"))
        dt_end_aware   = localize_jkt(datetime.datetime.strptime(end_dt,   "%Y-%m-%d %H:%M"))
        event = {
            "summary":     title,
            "description": description,
            "start": {"dateTime": dt_start_aware.isoformat(), "timeZone": "Asia/Jakarta"},
            "end":   {"dateTime": dt_end_aware.isoformat(),   "timeZone": "Asia/Jakarta"},
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "popup", "minutes": 10},
                    {"method": "email", "minutes": 10},
                ],
            },
        }
        calendar_svc, _, _ = get_google_services()
        calendar_svc.events().insert(calendarId="primary", body=event).execute()
        return f"📅 *{title}* added!\n🗓 {start_pretty} → {end_pretty}"
    except Exception as e:
        return f"⚠️ Could not add event to Calendar: {str(e)}"

# ================================================================
# GET EVENTS
# ================================================================
@trace
def get_events(date_hint: str = None, query: str = "") -> str:
    """Fetch events from Google Calendar for a given date or the next 7 days."""
    try:
        now = now_jkt()
        if date_hint:
            try:
                target = datetime.datetime.strptime(date_hint, "%Y-%m-%d")
            except ValueError:
                target = now
        else:
            target = now

        day_start = localize_jkt(target.replace(hour=0,  minute=0,  second=0,  microsecond=0))
        day_end   = localize_jkt(target.replace(hour=23, minute=59, second=59, microsecond=0))

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
            orderBy="startTime",
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
                dt       = datetime.datetime.fromisoformat(start["dateTime"])
                time_str = dt.strftime("%a %d %b, %H:%M")
            else:
                time_str = start.get("date", "All day")
            lines.append(f"• {time_str} — {title}")
        return "\n".join(lines)

    except Exception as e:
        print(f"[get_events error] {e}")
        return f"⚠️ Could not fetch calendar events: {str(e)}"

# ================================================================
# DELETE EVENT
# ================================================================
@trace
def delete_event(keyword: str) -> str:
    try:
        calendar_svc, _, _ = get_google_services()
        now    = localize_jkt(now_jkt())
        result = calendar_svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
            q=keyword,
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"❌ No upcoming event found matching '{keyword}'. Use *get events* to check your calendar."
        ev = events[0]
        calendar_svc.events().delete(calendarId="primary", eventId=ev["id"]).execute()
        return f"🗑️ Event deleted: _{ev.get('summary', keyword)}_"
    except Exception as e:
        return f"⚠️ Could not delete event: {e}"

# ================================================================
# EDIT EVENT
# ================================================================
@trace
def edit_event(
    keyword: str,
    new_title: str = None,
    new_start: str = None,
    new_end: str = None,
    new_description: str = None,
) -> str:
    try:
        calendar_svc, _, _ = get_google_services()
        now    = localize_jkt(now_jkt())
        result = calendar_svc.events().list(
            calendarId="primary",
            timeMin=now.isoformat(),
            maxResults=20,
            singleEvents=True,
            orderBy="startTime",
            q=keyword,
        ).execute()
        events = result.get("items", [])
        if not events:
            return f"❌ No upcoming event found matching '{keyword}'. Use *get events* to check your calendar."

        ev   = events[0]
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
