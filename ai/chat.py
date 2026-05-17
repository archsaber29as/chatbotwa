from ai.groq_client import groq_complete
from database import save_conv_turn, load_conv_history, clear_conv_history
from features.memory import memory_context_block
from tracer import trace

_RESET_TRIGGERS = {
    "new topic", "forget that", "start over", "reset chat",
    "mulai baru", "hapus history", "ganti topik", "clear chat",
}
@trace
def ai_chat(user_input: str) -> str:
    """General chat using Groq Llama 3.1 8B with conversation history + semantic memory."""
    if user_input.strip().lower() in _RESET_TRIGGERS:
        clear_conv_history()
        return "🔄 Got it! Fresh start — what's on your mind?"

    history    = load_conv_history()
    memory_ctx = memory_context_block(user_input, min_score=0.55)

    system_prompt = (
        "You are a helpful, friendly WhatsApp personal assistant. "
        "Keep replies concise and conversational — this is a chat, not an essay. "
        "Use the conversation history to maintain context across follow-up messages. "
        "If the user refers to something from earlier (e.g. 'that', 'it', 'the one you mentioned'), "
        "look it up in the history and respond accordingly."
        + (f"\n\nRelevant from user's notes & ideas:{memory_ctx}" if memory_ctx else "")
    )

    try:
        reply = groq_complete(system_prompt, user_input, max_tokens=1024, temperature=0.7, history=history)
    except Exception as e:
        err = str(e)
        if "503" in err or "UNAVAILABLE" in err:
            return "⚠️ AI temporarily overloaded. Try again in a moment!"
        if "429" in err or "rate_limit" in err.lower():
            return "⚠️ API quota reached. Try again later."
        return "⚠️ Something went wrong. Please try again."

    save_conv_turn("user",      user_input)
    save_conv_turn("assistant", reply)
    return reply
