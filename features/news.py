import datetime, requests
from bs4 import BeautifulSoup
from config import NEWS_API_KEY, now_jkt
from ai.groq_client import groq_complete
from tracer import trace

@trace
def get_news(topic: str) -> str:
    url = (
        f"https://newsapi.org/v2/everything"
        f"?q={topic}&apiKey={NEWS_API_KEY}&pageSize=5&language=en&sortBy=relevancy"
        f"&from={(now_jkt() - datetime.timedelta(days=7)).strftime('%Y-%m-%d')}"
    )
    try:
        data     = requests.get(url, timeout=10).json()
        articles = data.get("articles", [])
    except Exception:
        return f"📭 Could not fetch news for *{topic}*. Try again later."

    if not articles:
        return f"📭 No news found for *{topic}*."

    a           = articles[0]
    title       = a.get("title", "No title")
    source      = a.get("source", {}).get("name", "Unknown source")
    article_url = a.get("url", "")
    published   = a.get("publishedAt", "")[:10]
    raw_text    = a.get("content") or a.get("description") or ""

    # Try to scrape full article text
    if article_url:
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            page    = requests.get(article_url, headers=headers, timeout=10)
            soup    = BeautifulSoup(page.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            scraped = " ".join(p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 0)
            if len(scraped) > len(raw_text):
                raw_text = scraped[:5000]
        except Exception as e:
            print(f"[Scrape error] {e}")

    if not raw_text:
        raw_text = a.get("description") or "Content not available."

    prompt = f"""You are a news summarizer for WhatsApp. Summarize the article below.

🔍 *What happened:*
[2-3 sentences explaining the main event]

👥 *Who is impacted:*
[Who is affected and how]

⚠️ *Why it matters:*
[Significance or consequences]

✅ *Solution / Response:* (skip if none)
[Actions taken or official responses]

📌 *Key takeaway:*
[One concise sentence]

Article title: {title}
Article content: {raw_text}"""

    try:
        summary = groq_complete("", prompt, max_tokens=1024, temperature=0.5)
    except Exception as e:
        print(f"[News summary error] {e}")
        summary = raw_text[:500] + "..."

    return (
        f"📰 *{title}*\n"
        f"🗞 {source} · {published}\n"
        f"─────────────────\n"
        f"{summary}\n\n"
        f"🔗 {article_url}"
    )
