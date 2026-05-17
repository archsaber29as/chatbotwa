import uuid
import logging
from functools import wraps
from contextvars import ContextVar

logger = logging.getLogger("tracer")

# Holds the current trace ID for this request context
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")

def new_trace():
    tid = uuid.uuid4().hex[:8]
    _trace_id.set(tid)
    return tid

def get_trace_id():
    return _trace_id.get()

def trace(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        tid = get_trace_id()
        logger.info(f"[{tid}] → {fn.__qualname__}")
        try:
            result = fn(*args, **kwargs)
            # If the return value is an HTTP response, log the status code
            if hasattr(result, "status_code"):
                logger.info(f"[{tid}] ✓ {fn.__qualname__} — HTTP {result.status_code}")
            else:
                logger.info(f"[{tid}] ✓ {fn.__qualname__}")
            return result
        except Exception as e:
            logger.error(f"[{tid}] ✗ {fn.__qualname__} — {type(e).__name__}: {e}")
            raise
    return wrapper