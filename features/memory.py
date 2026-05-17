import sqlite3, pickle
import numpy as np
from config import gemini_client, MODEL_EMBED, now_jkt
from tracer import trace

DB_PATH = "bot.db"

# ================================================================
# EMBEDDING GENERATION
# ================================================================
def get_embedding(text: str) -> list:
    """Generate an embedding vector using Gemini Embedding 2."""
    try:
        result = gemini_client.models.embed_content(model=MODEL_EMBED, contents=text)
        return result.embeddings[0].values
    except Exception as e:
        print(f"[Embedding error] {e}")
        return []

def save_embedding(source_type: str, source_id: int, content: str):
    """Generate and store an embedding for a note or idea."""
    embedding = get_embedding(content)
    if not embedding:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO embeddings (source_type, source_id, content, embedding, timestamp) VALUES (?, ?, ?, ?, ?)",
        (source_type, source_id, content, pickle.dumps(embedding), str(now_jkt()))
    )
    conn.commit()
    conn.close()

# ================================================================
# SIMILARITY SEARCH
# ================================================================
def _cosine_similarity(a: list, b: list) -> float:
    a, b   = np.array(a), np.array(b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))

def semantic_search(query: str, top_k: int = 3, min_score: float = 0.45) -> list:
    """Return the top-k most semantically similar notes/ideas to the query."""
    query_embedding = get_embedding(query)
    if not query_embedding:
        return []

    conn  = sqlite3.connect(DB_PATH)
    rows  = conn.execute(
        "SELECT source_type, source_id, content, embedding FROM embeddings"
    ).fetchall()
    conn.close()

    results = []
    for source_type, source_id, content, embedding_blob in rows:
        try:
            embedding = pickle.loads(embedding_blob)
            score     = _cosine_similarity(query_embedding, embedding)
            if score >= min_score:
                results.append({"source_type": source_type, "content": content, "score": score})
        except Exception:
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]

def memory_context_block(query: str, min_score: float = 0.50) -> str:
    """Build a formatted context string from semantic memory for AI prompts."""
    memory = semantic_search(query, top_k=3, min_score=min_score)
    if not memory:
        return ""
    items = [f"- [{m['source_type']}] {m['content']}" for m in memory]
    return "\n\nRelevant from your notes & ideas:\n" + "\n".join(items)
