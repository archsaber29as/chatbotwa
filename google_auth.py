import os, pickle
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from config import SCOPES
from tracer import trace

_google_services_cache = None

def get_google_services():
    """Return (calendar, sheets, tasks) services. Loads once and caches.
    Reads token from GOOGLE_TOKEN_B64 env var (base64) or token.pickle file.
    Raises RuntimeError if no valid credentials are available."""
    global _google_services_cache
    if _google_services_cache is not None:
        return _google_services_cache

    creds = None

    # 1. Try env var (base64-encoded pickle) — recommended for Railway/cloud
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
