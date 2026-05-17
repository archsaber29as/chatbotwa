from config import groq_client, gemini_client, MODEL_GROQ, MODEL_FALLBACK
from tracer import trace

@trace
def groq_complete(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 1024,
    temperature: float = 0.7,
    history: list[dict] | None = None,
) -> str:
    """Call Groq llama-3.1-8b-instant.
    Falls back to Gemini 3.1 Flash Lite on any error (401, rate limit, etc.).

    If `history` is provided it is inserted between the system prompt and the
    current user message so the model has full multi-turn context.
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    # --- Primary: Groq ---
    try:
        response = groq_client.chat.completions.create(
            model=MODEL_GROQ,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Groq error] {e} — falling back to Gemini 3.1 Flash Lite")

    # --- Fallback: Gemini 3.1 Flash Lite ---
    full_prompt = (f"{system_prompt} {user_prompt}" if system_prompt else user_prompt)
    response = gemini_client.models.generate_content(model=MODEL_FALLBACK, contents=full_prompt)
    return response.text.strip()
