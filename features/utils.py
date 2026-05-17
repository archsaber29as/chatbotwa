from config import WHATSAPP_NOTE_LIMIT

def slice_data_rows(data_rows: list, range_start=None, range_end=None, count=None, range_all=False) -> tuple:
    """Return (sliced_rows, offset) based on range params.
    offset is the 0-based index of sliced_rows[0] inside data_rows.
    Displayed number = offset + i + 1.
    """
    total = len(data_rows)
    if range_all:
        start = max(0, total - WHATSAPP_NOTE_LIMIT)
        return data_rows[start:], start
    if range_start is not None and range_end is not None:
        s = max(0, int(range_start) - 1)
        e = min(total, int(range_end))
        return data_rows[s:e], s
    if count is not None:
        n     = min(int(count), WHATSAPP_NOTE_LIMIT)
        start = max(0, total - n)
        return data_rows[start:], start
    # Default: last 10
    start = max(0, total - 10)
    return data_rows[start:], start

def has_header_row(rows: list) -> bool:
    """Return True if the first row looks like a header (timestamp/time/ts/a)."""
    return bool(rows and rows[0][0].lower() in ("timestamp", "time", "ts", "a"))
