import sqlite3
from config import now_jkt, WHATSAPP_NOTE_LIMIT
from google_auth import get_google_services
from tracer import trace

DB_PATH = "bot.db"

# ================================================================
# SAVE
# ================================================================
@trace
def save_task(text: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO tasks (content, timestamp) VALUES (?, ?)", (text, str(now_jkt())))
    conn.commit()
    conn.close()
    try:
        _, _, tasks_svc = get_google_services()
        tasks_svc.tasks().insert(
            tasklist="@default",
            body={"title": text, "status": "needsAction"},
        ).execute()
        return "✅ Task added!\n📋 Also added to Google Tasks."
    except Exception as e:
        return f"✅ Task saved locally.\n⚠️ Google Tasks sync failed: {str(e)}"

# ================================================================
# GET
# ================================================================
@trace
def get_tasks(range_start=None, range_end=None, count=None, range_all=False) -> str:
    try:
        _, _, tasks_svc = get_google_services()
        result = tasks_svc.tasks().list(tasklist="@default", showCompleted=False, maxResults=100).execute()
        items  = result.get("items", [])
        if not items:
            return "📋 No pending tasks."
        total = len(items)
        if range_all:
            start  = max(0, total - WHATSAPP_NOTE_LIMIT)
            sliced = items[start:]
            offset = start
        elif range_start is not None and range_end is not None:
            s      = max(0, int(range_start) - 1)
            e      = min(total, int(range_end))
            sliced = items[s:e]
            offset = s
        elif count is not None:
            n      = min(int(count), WHATSAPP_NOTE_LIMIT)
            start  = max(0, total - n)
            sliced = items[start:]
            offset = start
        else:
            start  = max(0, total - 10)
            sliced = items[start:]
            offset = start
        header = f"📋 *Your tasks* (showing {offset+1}–{offset+len(sliced)} of {total}):"
        lines  = [header, ""]
        for i, t in enumerate(sliced):
            lines.append(f"{offset + i + 1}. {t['title']}")
        if range_all and total > WHATSAPP_NOTE_LIMIT:
            lines.append(
                f"\n_⚠️ Showing last {WHATSAPP_NOTE_LIMIT} of {total} tasks (WhatsApp limit). "
                f"Use \"list tasks N-M\" for a specific range._"
            )
        return "\n".join(lines)
    except Exception:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT content FROM tasks WHERE done=0 ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "📋 No pending tasks."
        return "📋 *Your tasks:*\n\n" + "\n".join([f"{i+1}. {r[0]}" for i, r in enumerate(rows)])

# ================================================================
# COMPLETE
# ================================================================
@trace
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
# DELETE
# ================================================================
@trace
def delete_task(keyword: str = None, index: int = None) -> str:
    try:
        _, _, tasks_svc = get_google_services()
        result = tasks_svc.tasks().list(tasklist="@default", showCompleted=False).execute()
        items  = result.get("items", [])
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

        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM tasks WHERE content = ?", (target["title"],))
        conn.commit()
        conn.close()
        return f"🗑️ Task deleted: _{target['title']}_"
    except Exception as e:
        return f"⚠️ Could not delete task: {e}"

# ================================================================
# EDIT
# ================================================================
@trace
def edit_task(new_title: str, keyword: str = None, index: int = None) -> str:
    try:
        _, _, tasks_svc = get_google_services()
        result = tasks_svc.tasks().list(tasklist="@default", showCompleted=False).execute()
        items  = result.get("items", [])
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
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET content=? WHERE content=?", (new_title, old_title))
        conn.commit()
        conn.close()
        return f"✏️ Task updated!\n_{new_title}_"
    except Exception as e:
        return f"⚠️ Could not edit task: {e}"
