from apscheduler.schedulers.background import BackgroundScheduler
from config import TZ_JKT
from database import (
    is_pending_reset, set_pending_reset,
    touch_last_active, minutes_since_last_active,
)
from config import (
    SESSION_TIMEOUT_MINUTES,
    TWILIO_SID, TWILIO_TOKEN, TWILIO_SANDBOX_NUMBER, YOUR_NUMBER,
)

from tracer import trace
# ================================================================
# SCHEDULER INSTANCE — imported by app.py
# ================================================================
scheduler = BackgroundScheduler(timezone=TZ_JKT)

# ================================================================
# SESSION TIMEOUT — pushes idle prompt every minute
# ================================================================
@trace
def check_session_timeout():
    """Proactively prompt the user to reset if they've been idle too long."""
    if is_pending_reset():
        return
    minutes_idle = minutes_since_last_active()
    if minutes_idle is None or minutes_idle < SESSION_TIMEOUT_MINUTES:
        return

    set_pending_reset(True)
    touch_last_active()

    try:
        from twilio.rest import Client as TwilioClient
        idle_str      = f"{int(minutes_idle)} minutes"
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        twilio_client.messages.create(
            from_=TWILIO_SANDBOX_NUMBER,
            to=YOUR_NUMBER,
            body=(
                f"⏱️ It's been {idle_str} since your last message.\n\n"
                f"Start a *fresh session* or continue where you left off?\n\n"
                f"Reply *yes* to reset  |  *no* to continue"
            ),
        )
        print(f"[Session timeout] Idle prompt sent after {idle_str}.")
    except Exception as e:
        set_pending_reset(False)
        print(f"[Session timeout] Failed to send idle prompt: {e}")

# ================================================================
# REGISTER ALL JOBS
# ================================================================
@trace
def register_jobs():
    from features.reminders import check_and_send_reminders
    from features.quotes import send_scheduled_quote

    scheduler.add_job(check_and_send_reminders, "interval", minutes=1,  id="reminder_check")
    scheduler.add_job(check_session_timeout,    "interval", minutes=1,  id="session_timeout")

    # Daily quote — morning & night (Asia/Jakarta)
    scheduler.add_job(
        send_scheduled_quote, "cron",
        hour=6, minute=0,
        args=["Good Morning 🌅"],
        id="morning_quote",
    )
    scheduler.add_job(
        send_scheduled_quote, "cron",
        hour=23, minute=0,
        args=["Good Night 🌙"],
        id="night_quote",
    )
