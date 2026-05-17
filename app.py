# At the top with your other imports
import logging
from tracer import new_trace, get_trace_id

import os, re
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

# ── Startup sequence (order matters) ────────────────────────────
from logging_setup import setup_logging
setup_logging()

from database import init_db, clear_conv_history, save_conv_turn
from database import is_pending_reset, set_pending_reset, touch_last_active

from scheduler import scheduler, register_jobs
from logging_setup import get_all_logs, get_recent_logs

# ── AI ───────────────────────────────────────────────────────────
from ai.classifier import classify_intent
from ai.chat       import ai_chat
from ai.brainstorm import ai_brainstorm

# ── Features ────────────────────────────────────────────────────
from features.reminders import parse_reminder_with_ai, save_reminder, get_reminders_list, delete_reminder
from features.notes     import save_note,  get_notes,  delete_note,  edit_note
from features.ideas     import save_idea,  get_ideas,  delete_idea,  edit_idea
from features.tasks     import save_task,  get_tasks,  complete_task, delete_task, edit_task
from features.calendar  import (
    parse_event_with_ai, parse_date_from_message,
    save_event, get_events, delete_event, edit_event,
)
from features.news    import get_news
from features.quotes  import generate_daily_quote
from features.budget  import calculate_budget
from features.memory  import semantic_search

# ── Flask app ────────────────────────────────────────────────────
app = Flask(__name__)
@app.after_request
def log_response(response):
    logger.info(f"[{get_trace_id()}] ◀ RESPONSE: HTTP {response.status_code}")
    return response
# ── DB + Scheduler ───────────────────────────────────────────────
init_db()
register_jobs()
scheduler.start()

# ── tracer log ───────────────────────────────────────────────
from tracer import new_trace, trace, logger, get_trace_id


# ================================================================
# WEBHOOK
# ================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    tid = new_trace()
    logger.info(f"[{tid}] ▶ INCOMING: {request.form.get('Body', '').strip()[:80]}")
    incoming = request.form.get("Body", "").strip()
    lower    = incoming.lower()
    resp     = MessagingResponse()
    msg      = resp.message()

    # ── Step 0a: Pending session-reset confirmation ──────────────
    if is_pending_reset():
        set_pending_reset(False)
        touch_last_active()
        yes_words = {"yes", "ya", "yep", "yup", "reset", "clear", "iya", "ok", "okay", "sure"}
        no_words  = {"no", "nope", "tidak", "nggak", "ngga", "lanjut", "continue", "stay", "keep"}
        if any(w in lower for w in yes_words):
            clear_conv_history()
            msg.body("🔄 Session reset! Fresh start — what's on your mind?")
        else:
            msg.body("👍 Continuing your previous session. What's up?")
        return str(resp)

    # ── Step 0b: Update last_active ──────────────────────────────
    touch_last_active()

    # ── Step 0c: /logs shortcut ──────────────────────────────────
    if lower.startswith("/logs"):
        n    = 20
        nums = re.findall(r"\d+", incoming)
        if nums:
            n = min(int(nums[0]), 50)
        msg.body(f"🖥️ *Last {n} log lines:*\n\n{get_recent_logs(n)[-1400:]}")
        return str(resp)

    # ── Step 1: Classify intent ──────────────────────────────────
    classified = classify_intent(incoming)
    intent     = classified.get("intent", "chat")
    params     = classified.get("params", {})
    reply_text = ""

    # ── Step 2: Route ────────────────────────────────────────────
    if intent == "reminder":
        content, remind_at = parse_reminder_with_ai(incoming)
        reply_text = save_reminder(content, remind_at)

    elif intent == "get_reminders":
        reply_text = get_reminders_list(params.get("date"))

    elif intent == "complete_task":
        keyword    = params.get("keyword") or re.sub(
            r"complete task|finish task|done task|selesai task", "", lower
        ).strip(" :?!")
        reply_text = complete_task(keyword)

    elif intent == "get_tasks":
        reply_text = get_tasks(
            range_start=params.get("range_start"),
            range_end=params.get("range_end"),
            count=params.get("count"),
            range_all=bool(params.get("range_all")),
        )

    elif intent == "add_task":
        content    = params.get("content") or re.sub(
            r"add task|new task|tambah task|create task|task:", "", lower
        ).strip(" :?!") or incoming
        reply_text = save_task(content)

    elif intent == "get_notes":
        reply_text = get_notes(
            range_start=params.get("range_start"),
            range_end=params.get("range_end"),
            count=params.get("count"),
            range_all=bool(params.get("range_all")),
        )

    elif intent == "add_note":
        content    = params.get("content") or re.sub(
            r"note:|notes:|add note|save note|catatan:|catat", "", lower
        ).strip(" :?!") or incoming
        reply_text = save_note(content)

    elif intent == "get_ideas":
        reply_text = get_ideas(
            range_start=params.get("range_start"),
            range_end=params.get("range_end"),
            count=params.get("count"),
            range_all=bool(params.get("range_all")),
        )

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
        date_hint  = params.get("date") or parse_date_from_message(incoming)
        reply_text = get_events(date_hint, incoming)

    elif intent == "add_event":
        parsed = parse_event_with_ai(incoming)
        if parsed and parsed.get("title") and parsed.get("start"):
            reply_text = save_event(
                parsed["title"].strip(),
                parsed["start"].strip(),
                parsed["end"].strip() if parsed.get("end") else None,
                parsed.get("description", ""),
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
            items      = [
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
        idx        = params.get("index")
        kw         = params.get("keyword") or params.get("content")
        reply_text = delete_note(keyword=kw, index=int(idx) if idx else None)

    elif intent == "edit_note":
        idx        = params.get("index")
        kw         = params.get("keyword")
        new_content = params.get("content") or ""
        reply_text  = edit_note(new_content, keyword=kw, index=int(idx) if idx else None)

    elif intent == "delete_idea":
        idx        = params.get("index")
        kw         = params.get("keyword") or params.get("content")
        reply_text = delete_idea(keyword=kw, index=int(idx) if idx else None)

    elif intent == "edit_idea":
        idx         = params.get("index")
        kw          = params.get("keyword")
        new_content = params.get("content") or ""
        reply_text  = edit_idea(new_content, keyword=kw, index=int(idx) if idx else None)

    elif intent == "delete_task":
        idx        = params.get("index")
        kw         = params.get("keyword") or params.get("content")
        reply_text = delete_task(keyword=kw, index=int(idx) if idx else None)

    elif intent == "edit_task":
        idx        = params.get("index")
        kw         = params.get("keyword")
        new_title  = params.get("content") or ""
        reply_text = edit_task(new_title, keyword=kw, index=int(idx) if idx else None)

    elif intent == "delete_event":
        kw         = params.get("keyword") or params.get("content") or incoming
        reply_text = delete_event(kw)

    elif intent == "edit_event":
        kw     = params.get("keyword") or ""
        parsed = parse_event_with_ai(incoming)
        reply_text = edit_event(
            keyword=kw,
            new_title=parsed.get("title")       if parsed else None,
            new_start=parsed.get("start")       if parsed else None,
            new_end=parsed.get("end")           if parsed else None,
            new_description=parsed.get("description") if parsed else None,
        )

    elif intent == "delete_reminder":
        kw         = params.get("keyword") or params.get("content") or incoming
        reply_text = delete_reminder(kw)

    else:
        reply_text = ai_chat(incoming)

    # ── Save non-chat turns to conversation history ──────────────
    if intent != "chat" and reply_text:
        save_conv_turn("user",      incoming)
        save_conv_turn("assistant", reply_text)

    msg.body(reply_text)
    return str(resp)

# ================================================================
# /logs — browser log viewer (auto-refreshes every 10s)
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
  </style>
</head>
<body>
  <h2>🖥️ Bot Logs <span style="font-size:11px;color:#8b949e">(auto-refresh 10s · last {n} lines)</span></h2>
  <pre>{logs}</pre>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}

# ================================================================
# ENTRY POINT
# ================================================================
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        debug=False,
        use_reloader=False,
        port=int(os.environ.get("PORT", 5000)),
    )
