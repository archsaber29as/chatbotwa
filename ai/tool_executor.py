import json
from features.tasks     import save_task, get_tasks, complete_task, delete_task, edit_task
from features.notes     import save_note, get_notes, delete_note, edit_note
from features.ideas     import save_idea, get_ideas, delete_idea, edit_idea
from features.reminders import parse_reminder_with_ai, save_reminder, get_reminders_list, delete_reminder
from features.calendar  import parse_event_with_ai, parse_date_from_message, save_event, get_events, delete_event, edit_event
from features.news      import get_news
from features.quotes    import generate_daily_quote
from features.budget    import calculate_budget as _calculate_budget
from features.memory    import semantic_search
from ai.brainstorm      import ai_brainstorm

def execute_tool(tool_name: str, args: dict) -> str:
    """Execute the tool the LLM chose and return a string reply."""

    # TASKS
    if tool_name == "add_task":
        return save_task(args["content"])
    elif tool_name == "get_tasks":
        return get_tasks(count=args.get("count"), range_all=args.get("range_all", False))
    elif tool_name == "complete_task":
        return complete_task(args["keyword"])
    elif tool_name == "delete_task":
        return delete_task(keyword=args.get("keyword"), index=args.get("index"))
    elif tool_name == "edit_task":
        return edit_task(args["content"], keyword=args.get("keyword"), index=args.get("index"))

    # NOTES
    elif tool_name == "add_note":
        return save_note(args["content"])
    elif tool_name == "get_notes":
        return get_notes(count=args.get("count"), range_all=args.get("range_all", False))
    elif tool_name == "delete_note":
        return delete_note(keyword=args.get("keyword"), index=args.get("index"))
    elif tool_name == "edit_note":
        return edit_note(args["content"], keyword=args.get("keyword"), index=args.get("index"))

    # IDEAS
    elif tool_name == "add_idea":
        return save_idea(args["content"])
    elif tool_name == "get_ideas":
        return get_ideas(count=args.get("count"), range_all=args.get("range_all", False))
    elif tool_name == "delete_idea":
        return delete_idea(keyword=args.get("keyword"), index=args.get("index"))
    elif tool_name == "edit_idea":
        return edit_idea(args["content"], keyword=args.get("keyword"), index=args.get("index"))

    # REMINDERS
    elif tool_name == "add_reminder":
        content, remind_at = parse_reminder_with_ai(args["message"])
        return save_reminder(content, remind_at)
    elif tool_name == "get_reminders":
        return get_reminders_list(args.get("date"))
    elif tool_name == "delete_reminder":
        return delete_reminder(args["keyword"])

    # CALENDAR
    elif tool_name == "add_event":
        parsed = parse_event_with_ai(args["message"])
        if parsed and parsed.get("title") and parsed.get("start"):
            return save_event(
                parsed["title"].strip(),
                parsed["start"].strip(),
                parsed.get("end"),
                parsed.get("description", ""),
            )
        return "⚠️ Could not understand the event. Try: *Add event Team lunch on April 22 at 1pm*"
    elif tool_name == "get_events":
        date_hint = args.get("date") or parse_date_from_message(args.get("message", ""))
        return get_events(date_hint, args.get("message", ""))
    elif tool_name == "delete_event":
        return delete_event(args["keyword"])
    elif tool_name == "edit_event":
        parsed = parse_event_with_ai(args["message"])
        return edit_event(
            keyword=args["keyword"],
            new_title=parsed.get("title") if parsed else None,
            new_start=parsed.get("start") if parsed else None,
            new_end=parsed.get("end") if parsed else None,
            new_description=parsed.get("description") if parsed else None,
        )

    # OTHER
    elif tool_name == "get_news":
        return get_news(args.get("topic", "world"))
    elif tool_name == "brainstorm":
        return ai_brainstorm(args["topic"])
    elif tool_name == "get_quote":
        return generate_daily_quote(args.get("context", ""))
    elif tool_name == "calculate_budget":
        return _calculate_budget(args["message"])
    elif tool_name == "search_memory":
        results = semantic_search(args["query"], top_k=5, min_score=0.45)
        if results:
            items = [
                f"{i+1}. {r['content']} _({r['source_type']}, {round(r['score']*100)}% match)_"
                for i, r in enumerate(results)
            ]
            return "🔍 *Found in your memory:*\n\n" + "\n".join(items)
        return "🔍 Nothing relevant found in your notes or ideas."

    return f"⚠️ Unknown tool: {tool_name}"