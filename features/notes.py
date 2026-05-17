import sqlite3
from config import now_jkt, SPREADSHEET_ID, WHATSAPP_NOTE_LIMIT
from google_auth import get_google_services
from features.memory import save_embedding
from features.utils import slice_data_rows, has_header_row
from tracer import trace

DB_PATH = "bot.db"

# ================================================================
# SAVE
# ================================================================
@trace
def save_note(text: str) -> str:
    conn      = sqlite3.connect(DB_PATH)
    cursor    = conn.execute(
        "INSERT INTO notes (content, timestamp) VALUES (?, ?)",
        (text, str(now_jkt()))
    )
    source_id = cursor.lastrowid
    conn.commit()
    conn.close()

    save_embedding("note", source_id, text)

    try:
        timestamp = now_jkt().strftime("%Y-%m-%d %H:%M")
        _, sheets_svc, _ = get_google_services()
        sheets_svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Notes!A2:B",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[timestamp, text]]},
        ).execute()
        return "📝 Note saved!\n📊 Also added to Google Sheets.\n🧠 Memorized for semantic search."
    except Exception as e:
        return f"📝 Note saved locally.\n🧠 Memorized for semantic search.\n⚠️ Sheets sync failed: {str(e)}"

# ================================================================
# GET
# ================================================================
@trace
def get_notes(range_start=None, range_end=None, count=None, range_all=False) -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result    = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Notes!A:B"
        ).execute()
        rows      = result.get("values", [])
        data_rows = [r for r in rows if len(r) >= 2]
        if not data_rows:
            return "📝 No notes saved yet."
        sliced, offset = slice_data_rows(data_rows, range_start, range_end, count, range_all)
        total = len(data_rows)
        header = f"📝 *Your notes* (showing {offset+1}–{offset+len(sliced)} of {total}):"
        lines  = [header, ""]
        for i, r in enumerate(sliced):
            lines.append(f"{offset + i + 1}. {r[1]} _({r[0]})_")
        if range_all and total > WHATSAPP_NOTE_LIMIT:
            lines.append(
                f"\n_⚠️ Showing last {WHATSAPP_NOTE_LIMIT} of {total} notes (WhatsApp limit). "
                f"Use \"list notes N-M\" for a specific range._"
            )
        return "\n".join(lines)
    except Exception:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT content, timestamp FROM notes ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not rows:
            return "📝 No notes saved yet."
        return "📝 *Your notes:*\n\n" + "\n".join(
            [f"{i+1}. {r[0]} _({r[1][:10]})_" for i, r in enumerate(rows)]
        )

# ================================================================
# DELETE
# ================================================================
@trace
def delete_note(keyword: str = None, index: int = None) -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result    = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Notes!A:B"
        ).execute()
        rows      = result.get("values", [])
        data_rows = [r for r in rows if len(r) >= 2]
        if not data_rows:
            return "📝 No notes to delete."

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
            return "❌ Note not found. Use *get notes* to see your list, then refer by number or keyword."

        deleted_text  = data_rows[target_i][1]
        header_offset = 1 if has_header_row(rows) else 0
        sheet_row     = target_i + 1 + header_offset

        sheet_meta     = sheets_svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        notes_sheet_id = next(
            (s["properties"]["sheetId"] for s in sheet_meta["sheets"] if s["properties"]["title"] == "Notes"),
            None
        )
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"deleteDimension": {"range": {
                "sheetId": notes_sheet_id, "dimension": "ROWS",
                "startIndex": sheet_row - 1, "endIndex": sheet_row,
            }}}]},
        ).execute()

        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM notes WHERE content = ?", (deleted_text,))
        conn.commit()
        conn.close()
        return f"🗑️ Note deleted: _{deleted_text[:60]}_"
    except Exception as e:
        return f"⚠️ Could not delete note: {e}"

# ================================================================
# EDIT
# ================================================================
@trace
def edit_note(new_content: str, keyword: str = None, index: int = None) -> str:
    try:
        _, sheets_svc, _ = get_google_services()
        result    = sheets_svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID, range="Notes!A:B"
        ).execute()
        rows      = result.get("values", [])
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

        header_offset = 1 if has_header_row(rows) else 0
        sheet_row     = target_i + 1 + header_offset
        timestamp     = now_jkt().strftime("%Y-%m-%d %H:%M")

        sheets_svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"Notes!A{sheet_row}:B{sheet_row}",
            valueInputOption="RAW",
            body={"values": [[timestamp, new_content]]},
        ).execute()

        old_text = data_rows[target_i][1]
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE notes SET content=?, timestamp=? WHERE content=?",
            (new_content, str(now_jkt()), old_text)
        )
        conn.commit()
        conn.close()
        return f"✏️ Note updated!\n_{new_content[:80]}_"
    except Exception as e:
        return f"⚠️ Could not edit note: {e}"
