TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": "Save a new to-do task",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The task description"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_tasks",
            "description": "List or view pending tasks",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of tasks to show"},
                    "range_all": {"type": "boolean", "description": "Show all tasks"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": "Mark a task as done",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Keyword to find the task"}
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": "Permanently delete a task",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "index": {"type": "integer"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_task",
            "description": "Edit a task's title",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "content": {"type": "string", "description": "New task title"}
                },
                "required": ["content"]
            }
        }
    },
    # --- NOTES ---
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": "Save a new note or memo",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_notes",
            "description": "List or read saved notes",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "range_all": {"type": "boolean"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Delete a saved note",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "index": {"type": "integer"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_note",
            "description": "Edit a saved note",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "content": {"type": "string", "description": "New note content"}
                },
                "required": ["content"]
            }
        }
    },
    # --- IDEAS ---
    {
        "type": "function",
        "function": {
            "name": "add_idea",
            "description": "Save a new idea",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_ideas",
            "description": "List saved ideas",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer"},
                    "range_all": {"type": "boolean"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_idea",
            "description": "Delete a saved idea",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "index": {"type": "integer"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_idea",
            "description": "Edit a saved idea",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["content"]
            }
        }
    },
    # --- REMINDERS ---
    {
        "type": "function",
        "function": {
            "name": "add_reminder",
            "description": "Create a new reminder or alarm",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Full original message to parse"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_reminders",
            "description": "View existing reminders",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Optional date filter YYYY-MM-DD"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_reminder",
            "description": "Delete a reminder",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"}
                },
                "required": ["keyword"]
            }
        }
    },
    # --- CALENDAR ---
    {
        "type": "function",
        "function": {
            "name": "add_event",
            "description": "Create a new Google Calendar event",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Full original message to parse"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": "View or list calendar events",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date to check YYYY-MM-DD"},
                    "message": {"type": "string"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Delete a calendar event",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"}
                },
                "required": ["keyword"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_event",
            "description": "Edit an existing calendar event",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "message": {"type": "string", "description": "Full message with new details"}
                },
                "required": ["keyword", "message"]
            }
        }
    },
    # --- OTHER ---
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Fetch news headlines on a topic",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"}
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "brainstorm",
            "description": "Brainstorm ideas on a topic",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"}
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_quote",
            "description": "Get a motivational or inspirational quote",
            "parameters": {
                "type": "object",
                "properties": {
                    "context": {"type": "string", "description": "Optional theme or mood"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_budget",
            "description": "Calculate budget breakdown from a monetary amount",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Full message with amount"}
                },
                "required": ["message"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "Search through saved notes and ideas semantically",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"}
                },
                "required": ["query"]
            }
        }
    },
]