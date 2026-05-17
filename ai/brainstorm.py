from config import gemini_client, MODEL_BRAINSTORM
from features.memory import memory_context_block
from ai.groq_client import groq_complete
from tracer import trace

@trace
def ai_brainstorm(topic: str) -> str:
    """Use Gemini 3 Flash for brainstorming, enriched with semantic memory."""
    memory_ctx = memory_context_block(topic, min_score=0.45)
    prompt = (
        f"You are an enthusiastic brainstorming partner on WhatsApp.\n"
        f"Help the user brainstorm creative, actionable ideas for: {topic}"
        f"{memory_ctx}\n\n"
        f"Give 5-7 ideas. Use an emoji for each. Keep each idea concise but inspiring.\n"
        f"End with one short motivational line."
    )
    try:
        response = gemini_client.models.generate_content(model=MODEL_BRAINSTORM, contents=prompt)
        return f"🧠 *Brainstorm: {topic}*\n\n{response.text.strip()}"
    except Exception as e:
        err = str(e).lower()
        print(f"[Brainstorm error] {e}")
        if "not found" in err or "404" in err or "unavailable" in err:
            print("[Brainstorm] Falling back to Groq")
            try:
                result = groq_complete("", prompt, max_tokens=1024, temperature=0.8)
                return f"🧠 *Brainstorm: {topic}*\n\n{result}"
            except Exception:
                pass
        return "⚠️ Brainstorm failed. Please try again!"
