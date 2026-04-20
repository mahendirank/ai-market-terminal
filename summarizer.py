import requests
import os

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL      = "qwen2.5:7b"
MIN_LENGTH = 150   # only summarize if text is longer than this


def summarize(text, max_words=30):
    if len(text) <= MIN_LENGTH:
        return text  # short headline — return as-is

    prompt = (
        f"Summarize the following news in 1-2 sentences, max {max_words} words. "
        f"Focus on market impact (gold, Fed, oil, geopolitics). Be direct.\n\n"
        f"NEWS: {text[:1000]}\n\nSUMMARY:"
    )

    try:
        res = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
            timeout=20,
        )
        summary = res.json()["response"].strip()
        # Clean up — take first sentence only if multiple
        summary = summary.split("\n")[0].strip()
        return summary if summary else text[:200]
    except:
        return text[:200]


def summarize_news(news_list):
    summarized = []
    for item in news_list:
        if isinstance(item, dict):
            text    = item["text"]
            summary = summarize(text)
            summarized.append({**item, "text": summary, "summarized": len(text) > MIN_LENGTH})
        else:
            summarized.append(summarize(item))
    return summarized


if __name__ == "__main__":
    test = (
        "JP Morgan has released its quarterly outlook suggesting that the Federal Reserve "
        "will cut interest rates twice in 2025, citing cooling inflation and weakening labor "
        "market data. The bank believes gold could benefit significantly as real yields fall "
        "and the dollar weakens. Analysts recommend overweight positioning in commodities."
    )
    print("Original:", test)
    print("\nSummary:", summarize(test))
