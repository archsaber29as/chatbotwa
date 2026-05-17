import re, json
from ai.groq_client import groq_complete
from tracer import trace
_CLASSIFY_PROMPT = """You are an intent classifier for a WhatsApp personal assistant.

IMPORTANT: Understand the full CONTEXT and MEANING of the message first. Do NOT match by keywords alone.
Ask yourself: is the user trying to CREATE something new, or RETRIEVE/LOOK UP something existing?

Classify the user's message into exactly ONE of these intents:

  reminder      — CREATE a new reminder or alarm (user wants to BE reminded later)
  get_reminders — VIEW or look up existing reminders
  add_note      — SAVE a new note or memo
  get_notes     — LIST or read saved notes
  add_idea      — SAVE a new idea
  get_ideas     — LIST or read saved ideas
  add_task      — ADD a new to-do task
  get_tasks     — LIST or view pending tasks
  complete_task — MARK a task as done
  news          — get news or headlines
  brainstorm    — brainstorm, explore ideas, get creative suggestions
  add_event     — CREATE / add a new calendar event
  get_events    — VIEW, check, look up, or list existing calendar events
  search_memory — ask about something that might be in their notes/ideas
  quote         — ask for a motivational/inspirational quote (e.g. "give me a quote", "motivate me", "quote of the day", "inspire me")
  budget        — CALCULATE or COMPUTE a budget with actual numbers: user provides a specific monetary amount and wants to know how much they can spend per day / sisa uang / berapa sisa per hari / survive until payday / kalkulasi budget / hitung uang sisa. Requires a specific monetary figure or explicit calculation request.
  delete_note   — DELETE or REMOVE a saved note by number or keyword (e.g. "delete note 2", "hapus note fix the logs")
  edit_note     — EDIT or UPDATE the content of a saved note (e.g. "edit note 2 to ...", "update note fix to ...")
  delete_idea   — DELETE or REMOVE a saved idea by number or keyword
  edit_idea     — EDIT or UPDATE a saved idea
  delete_task   — DELETE or REMOVE a task (not marking as done, but fully removing it)
  edit_task     — EDIT or UPDATE a task title
  delete_event  — DELETE or REMOVE a calendar event by name or keyword (e.g. "delete event Team lunch", "hapus event meeting")
  edit_event    — EDIT or UPDATE an existing calendar event (title, time, or description)
  delete_reminder — DELETE or REMOVE a saved reminder by keyword or time
  chat          — general conversation or anything else

KEY DISAMBIGUATION RULES (apply these before classifying):
- "remind me [of/about] an event on X" → get_events (looking up an existing event, NOT setting a reminder)
- "remind me [of/about] my meeting" → get_events (retrieving existing calendar info)
- "set a reminder to X" / "remind me to X at Y" → reminder (creating a new reminder/alarm)
- "what events do I have on X" / "show my calendar for X" / "do I have anything on X" → get_events
- "add event X" / "schedule X" / "create event X" / "new event X" → add_event
- "show my reminders" / "list reminders" / "what are my reminders" → get_reminders
- The word "remind" alone does NOT mean intent=reminder. Look at the full sentence structure.
- "how to budget" / "tips for budgeting" / "how to spend daily budget wisely" / any advice or how-to question about money → chat (NOT budget). The budget intent requires actual numbers to calculate, not general advice.
- "how to spend my daily budget wisely?" → chat (advice question, no number to calculate)
- "delete/remove/hapus note/idea/task/event/reminder X" → delete_* intent (not complete_task)
- "edit/update/change/ubah note/idea/task/event/reminder X to/with Y" → edit_* intent
- "delete task X" → delete_task (permanently remove), NOT complete_task (which marks done)

For get_notes, get_ideas, get_tasks intents, also extract optional display range params:
- "list notes 9-13" or "notes 9 to 13" → range_start: 9, range_end: 13
- "list notes 5" or "show 5 notes" → count: 5  (show last N)
- "list notes all" or "show all notes" → range_all: true
- "list notes" (no range) → leave range fields absent

Reply ONLY with a JSON object (no markdown, no preamble):
{{"intent": "<intent>", "params": {{"content": "<new content for edit intents>", "keyword": "<item to find/delete/edit>", "index": "<item number if user said e.g. note 2>", "date": "<date if mentioned, e.g. 2025-05-10>", "range_start": "<int or null>", "range_end": "<int or null>", "count": "<int or null>", "range_all": "<true or null>"}}}}

User message: {message}"""

@trace
def classify_intent(text: str) -> dict:
    """Use Groq to classify the user's intent. Returns {intent, params}."""
    try:
        raw = groq_complete(
            system_prompt="You are an intent classifier. Always reply with valid JSON only. No markdown, no explanation.",
            user_prompt=_CLASSIFY_PROMPT.format(message=text),
            max_tokens=256,
            temperature=0.0,
        )
        raw    = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        print(f"[Classify] Input: '{text}' → {result}")
        return result
    except Exception as e:
        print(f"[Classify error] {e}")
        return {"intent": "chat", "params": {}}
