import sqlite3
from config import now_jkt, CONV_WINDOW

DB_PATH = "bot.db"

# ================================================================
# SCHEMA SETUP
# ================================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS ideas     (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, content TEXT, remind_at TEXT, done INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS notes     (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS tasks     (id INTEGER PRIMARY KEY, content TEXT, timestamp TEXT, done INTEGER DEFAULT 0)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id        INTEGER PRIMARY KEY,
            role      TEXT,
            content   TEXT,
            timestamp TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            id          INTEGER PRIMARY KEY,
            source_type TEXT,
            source_id   INTEGER,
            content     TEXT,
            embedding   BLOB,
            timestamp   TEXT
        )
    """)
    conn.commit()
    conn.close()

# ================================================================
# BOT STATE — key/value store for last_active & pending flags
# ================================================================
def state_get(key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row  = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None

def state_set(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def state_del(key: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM bot_state WHERE key = ?", (key,))
    conn.commit()
    conn.close()

def touch_last_active():
    state_set("last_active", str(now_jkt()))

def minutes_since_last_active() -> float | None:
    import datetime
    raw = state_get("last_active")
    if not raw:
        return None
    try:
        last  = datetime.datetime.fromisoformat(raw)
        delta = now_jkt() - last
        return delta.total_seconds() / 60
    except Exception:
        return None

def is_pending_reset() -> bool:
    return state_get("pending_reset") == "1"

def set_pending_reset(flag: bool):
    if flag:
        state_set("pending_reset", "1")
    else:
        state_del("pending_reset")

# ================================================================
# CONVERSATION HISTORY — rolling window for multi-turn context
# ================================================================
def save_conv_turn(role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO conversations (role, content, timestamp) VALUES (?, ?, ?)",
        (role, content, str(now_jkt()))
    )
    conn.execute("""
        DELETE FROM conversations
        WHERE id NOT IN (
            SELECT id FROM conversations ORDER BY id DESC LIMIT ?
        )
    """, (CONV_WINDOW * 2,))
    conn.commit()
    conn.close()

def load_conv_history() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM conversations ORDER BY id ASC"
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]

def clear_conv_history():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM conversations")
    conn.commit()
    conn.close()
