import sqlite3, datetime
from config import (
    now_jkt, localize_jkt,
    TWILIO_SID, TWILIO_TOKEN, TWILIO_SANDBOX_NUMBER, YOUR_NUMBER,
)
from google_auth import get_google_services
from ai.groq_client import groq_complete
from tracer import trace


DB_PATH = "bot.db"

# ================================================================
# AI REMINDER PARSER
# ================================================================
@trace
def parse_reminder_with_ai(user_input: str) -> tuple:
    """Use Groq to extract reminder content and datetime from natural language."""
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
        raw   = groq_complete("", prompt, max_tokens=64, temperature=0.0)
        parts = raw.split("|")
        if len(parts) < 2:
            raise ValueError("No pipe separator in response")
        content   = parts[0].strip()
        remind_at = parts[1].strip()
        datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        return content, remind_at
    except Exception as e:
        print(f"[Reminder parse error] {e}")
        fallback_dt = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d 09:00")
        return user_input, fallback_dt

# ================================================================
# SAVE REMINDER
# ================================================================
@trace
def save_reminder(text: str, remind_at: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO reminders (content, remind_at) VALUES (?, ?)", (text, remind_at))
    conn.commit()
    conn.close()
    try:
        dt           = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        dt_end       = dt + datetime.timedelta(minutes=30)
        dt_aware     = localize_jkt(dt)
        dt_end_aware = localize_jkt(dt_end)
        event = {
            "summary": f"⏰ {text}",
            "start":   {"dateTime": dt_aware.isoformat(),     "timeZone": "Asia/Jakarta"},
            "end":     {"dateTime": dt_end_aware.isoformat(), "timeZone": "Asia/Jakarta"},
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
        dt_pretty = dt.strftime("%A, %d %B %Y at %H:%M")
        return f"⏰ Reminder set for *{dt_pretty}*!\n📅 Also added to Google Calendar."
    except Exception as e:
        return f"⏰ Reminder saved locally for *{remind_at}*.\n⚠️ Calendar sync failed: {str(e)}"

# ================================================================
# GET REMINDERS
# ================================================================
@trace
def get_reminders_list(date_hint: str = None) -> str:
    conn = sqlite3.connect(DB_PATH)
    now  = now_jkt()
    if date_hint:
        try:
            target = datetime.datetime.strptime(date_hint, "%Y-%m-%d")
            day_lo = target.strftime("%Y-%m-%d 00:00")
            day_hi = target.strftime("%Y-%m-%d 23:59")
            rows   = conn.execute(
                "SELECT content, remind_at FROM reminders WHERE remind_at BETWEEN ? AND ? AND done=0 ORDER BY remind_at",
                (day_lo, day_hi)
            ).fetchall()
            label = target.strftime("%A, %d %B %Y")
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
# DELETE REMINDER
# ================================================================
@trace
def delete_reminder(keyword: str) -> str:
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, content, remind_at FROM reminders WHERE done=0 AND content LIKE ?",
            (f"%{keyword}%",)
        ).fetchall()
        if not rows:
            conn.close()
            return f"❌ No reminder found matching '{keyword}'. Use *get reminders* to see your list."

        row = rows[0]
        conn.execute("DELETE FROM reminders WHERE id = ?", (row[0],))
        conn.commit()
        conn.close()

        # Try to delete from Google Calendar too
        try:
            calendar_svc, _, _ = get_google_services()
            now    = localize_jkt(now_jkt())
            result = calendar_svc.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                maxResults=20,
                singleEvents=True,
                orderBy="startTime",
                q=row[1],
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

# ================================================================
# SCHEDULER JOB — called every minute by APScheduler
# ================================================================
@trace
def check_and_send_reminders():
    """Fire any due reminders via Twilio and mark them done."""
    from twilio.rest import Client as TwilioClient
    now    = now_jkt()
    win_lo = (now - datetime.timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M")
    win_hi = (now + datetime.timedelta(seconds=59)).strftime("%Y-%m-%d %H:%M")
    conn   = sqlite3.connect(DB_PATH)
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
                body=f"⏰ *Reminder:* {row[1]}",
            )
            conn.execute("UPDATE reminders SET done = 1 WHERE id = ?", (row[0],))
        conn.commit()
    conn.close()
