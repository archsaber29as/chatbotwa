import logging, threading, sys, time, datetime, collections
from config import LOG_SPREADSHEET_ID, TZ_JKT

_ALL_LOG_FILE   = "bot_all.log"
_ALL_LOG_LOCK   = threading.Lock()

_SHEET_QUEUE      = collections.deque()
_SHEET_QUEUE_LOCK = threading.Lock()

_CREATED_LOG_TABS      = set()
_CREATED_LOG_TABS_LOCK = threading.Lock()

# ================================================================
# TIMESTAMP HELPERS
# ================================================================
def _ts() -> str:
    return datetime.datetime.now(TZ_JKT).strftime("%H:%M:%S")

def _ts_full() -> str:
    return datetime.datetime.now(TZ_JKT).strftime("%Y-%m-%d %H:%M:%S")

def _get_log_tab() -> str:
    return datetime.datetime.now(TZ_JKT).strftime("%Y-%m-%d")

# ================================================================
# SHEET TAB MANAGEMENT
# ================================================================
def _ensure_log_tab(sheets_svc, tab_name: str):
    with _CREATED_LOG_TABS_LOCK:
        if tab_name in _CREATED_LOG_TABS:
            return
    try:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=LOG_SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
        ).execute()
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=LOG_SPREADSHEET_ID,
            range=f"'{tab_name}'!A1:B1",
            valueInputOption="RAW",
            body={"values": [["Timestamp", "Log"]]},
        ).execute()
    except Exception:
        pass  # Tab already exists — fine
    with _CREATED_LOG_TABS_LOCK:
        _CREATED_LOG_TABS.add(tab_name)

# ================================================================
# WRITE HELPERS
# ================================================================
def _buf_all(line: str):
    with _ALL_LOG_LOCK:
        try:
            with open(_ALL_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

def _buf_sheet(line: str):
    with _SHEET_QUEUE_LOCK:
        _SHEET_QUEUE.append([_ts_full(), line])

# ================================================================
# BACKGROUND SHEET FLUSHER
# ================================================================
def _sheet_log_flusher():
    while True:
        time.sleep(5)
        with _SHEET_QUEUE_LOCK:
            if not _SHEET_QUEUE:
                continue
            rows = list(_SHEET_QUEUE)
            _SHEET_QUEUE.clear()
        try:
            from google_auth import get_google_services
            _, sheets_svc, _ = get_google_services()
            by_tab = collections.defaultdict(list)
            for row in rows:
                tab = row[0][:10]
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
            with _SHEET_QUEUE_LOCK:
                _SHEET_QUEUE.extendleft(reversed(rows))

# ================================================================
# LOGGING HANDLERS
# ================================================================
class _AllHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _buf_all(f"[{_ts()}] {record.levelname} {record.name}: {msg}")
        except Exception:
            pass

class _HttpxHandler(logging.Handler):
    def emit(self, record):
        try:
            msg  = self.format(record)
            line = f"[{_ts()}] {record.levelname} {record.name}: {msg}"
            _buf_all(line)
            _buf_sheet(line)
        except Exception:
            pass

# ================================================================
# STDOUT/STDERR TEE
# ================================================================
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

# ================================================================
# LOG READERS
# ================================================================
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
    """WhatsApp /logs command: read last n rows from today's daily sheet tab."""
    try:
        from google_auth import get_google_services
        _, sheets_svc, _ = get_google_services()
        tab_name = _get_log_tab()
        result = sheets_svc.spreadsheets().values().get(
            spreadsheetId=LOG_SPREADSHEET_ID,
            range=f"'{tab_name}'!A:B",
        ).execute()
        rows = result.get("values", [])
        if rows and rows[0][0].lower() in ("timestamp", "time", "ts"):
            rows = rows[1:]
        if not rows:
            return f"No logs yet for {tab_name}."
        recent = rows[-n:]
        lines = []
        for r in recent:
            entry = f"[{r[0]}] {r[1] if len(r) > 1 else ''}"
            if "| body:" in entry:
                entry = entry[:entry.index("| body:")].rstrip()
            lines.append(entry)
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading logs from Sheet: {e}"

# ================================================================
# SETUP — call this once at startup
# ================================================================
def setup_logging():
    """Wire up all handlers and redirect stdout/stderr."""
    _all_handler = _AllHandler()
    _all_handler.setFormatter(logging.Formatter("%(message)s"))
    _all_handler.setLevel(logging.DEBUG)

    _httpx_handler = _HttpxHandler()
    _httpx_handler.setFormatter(logging.Formatter("%(message)s"))
    _httpx_handler.setLevel(logging.INFO)

    tracer_logger = logging.getLogger("tracer")
    tracer_logger.addHandler(_httpx_handler)
    tracer_logger.setLevel(logging.DEBUG)
    tracer_logger.propagate = True
    
    logging.getLogger().addHandler(_all_handler)
    logging.getLogger().setLevel(logging.DEBUG)

    for name in ("werkzeug", "apscheduler", "apscheduler.executors.default"):
        lgr = logging.getLogger(name)
        lgr.addHandler(_all_handler)
        lgr.setLevel(logging.DEBUG)

    httpx_logger = logging.getLogger("httpx")
    httpx_logger.addHandler(_httpx_handler)
    httpx_logger.setLevel(logging.INFO)
    httpx_logger.propagate = False

    sys.stdout = _TeeStream(sys.stdout)
    sys.stderr = _TeeStream(sys.stderr)

    threading.Thread(target=_sheet_log_flusher, daemon=True, name="sheet-log-flusher").start()
