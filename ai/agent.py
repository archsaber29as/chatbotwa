import json
from config import groq_client, MODEL_GROQ
from ai.tools import TOOLS
from ai.tool_executor import execute_tool
from database import load_conv_history, save_conv_turn


SYSTEM_PROMPT = """You are a smart WhatsApp personal assistant.
You have tools to manage tasks, notes, ideas, reminders, calendar events, news, budget, and more.
Always use a tool when the user wants to DO something. 
Only reply directly (without a tool) for general conversation.
Be concise — this is WhatsApp, keep replies short and clear.
The user may write in English or Indonesian — handle both."""

def run_agent(user_message: str) -> str:
    """Run the tool-calling agent and return the final reply."""

    # Build message history
    history = load_conv_history()  # returns list of {role, content} dicts
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += history
    messages.append({"role": "user", "content": user_message})

    # Agentic loop — LLM can call multiple tools in sequence
    for _ in range(5):  # max 5 tool calls per message
        response = groq_client.chat.completions.create(
            model=MODEL_GROQ,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=1024,
        )

        choice = response.choices[0]
        finish_reason = choice.finish_reason

        # LLM wants to call a tool
        if finish_reason == "tool_calls":
            tool_calls = choice.message.tool_calls

            # Add assistant's tool-call message to history
            messages.append(choice.message)

            # Execute each tool and feed results back
            for tc in tool_calls:
                tool_name = tc.function.name
                args = json.loads(tc.function.arguments)
                result = execute_tool(tool_name, args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

        # LLM is done — return the text reply
        elif finish_reason == "stop":
            reply = choice.message.content or ""
            save_conv_turn("user", user_message)
            save_conv_turn("assistant", reply)
            return reply

    return "⚠️ Sorry, I got stuck. Please try again."