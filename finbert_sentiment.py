"""
finbert_sentiment.py — Local financial-tone scoring with FinBERT (Phase 3b).

Runs ProsusAI/finbert (BERT fine-tuned on financial text; labels
positive / negative / neutral) fully locally on CPU — no API keys, no cost,
unlike the Groq-backed sentiment panel. Complements it: FinBERT scores every
headline deterministically, Groq narrates.

Model weights (~440MB) download once on first use into /app/db/hf-cache,
which lives on the persistent volume — surviving container recreation.
torch + transformers are installed in the container but intentionally NOT in
requirements.txt (they would triple the image build); see the comment there.

The model loads lazily in a background thread on first request; until it is
ready the API reports status so the panel can show progress instead of
blocking a worker for minutes.
"""
import os, threading

os.environ.setdefault("HF_HOME", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "db", "hf-cache"))

# ProsusAI/finbert (not yiyanghkust/finbert-tone: its config predates the
# `model_type` key and current transformers refuses to load it)
MODEL = "ProsusAI/finbert"
_lock = threading.Lock()
_pipeline = None
_state = "cold"          # cold → loading → ready | failed
_error = ""


def _load():
    global _pipeline, _state, _error
    try:
        from transformers import pipeline
        _pipeline = pipeline("text-classification", model=MODEL, top_k=None, device=-1)
        _state = "ready"
    except Exception as e:
        _state, _error = "failed", f"{type(e).__name__}: {e}"
        print(f"[finbert] load failed: {_error}", flush=True)


def _ensure_loading():
    global _state
    with _lock:
        if _state == "cold":
            _state = "loading"
            threading.Thread(target=_load, daemon=True).start()


def analyze(texts: list[str]) -> dict:
    """Score headlines → aggregate tone. Non-blocking: returns a status dict
    until the model is ready."""
    _ensure_loading()
    if _state in ("cold", "loading"):
        return {"status": "loading", "note": "FinBERT downloading/loading (first run ~440MB)"}
    if _state == "failed":
        return {"status": "failed", "error": _error,
                "note": "pip install torch transformers (see finbert_sentiment.py)"}
    texts = [t[:300] for t in texts if t and len(t) > 15][:120]
    if not texts:
        return {"status": "ok", "scored": 0}
    results = _pipeline(texts, batch_size=16, truncation=True, max_length=64)
    pos = neg = neu = 0
    extremes = {"bullish": (0.0, ""), "bearish": (0.0, "")}
    for text, scores in zip(texts, results):
        by = {s["label"].lower(): s["score"] for s in scores}
        p, n = by.get("positive", 0), by.get("negative", 0)
        top = max(by, key=by.get)
        if top == "positive":
            pos += 1
            if p > extremes["bullish"][0]:
                extremes["bullish"] = (p, text)
        elif top == "negative":
            neg += 1
            if n > extremes["bearish"][0]:
                extremes["bearish"] = (n, text)
        else:
            neu += 1
    total = pos + neg + neu
    net = round((pos - neg) / total * 100)
    label = ("BULLISH" if net > 15 else "BEARISH" if net < -15 else "NEUTRAL")
    return {
        "status": "ok", "scored": total,
        "bullish_pct": round(pos / total * 100), "bearish_pct": round(neg / total * 100),
        "neutral_pct": round(neu / total * 100), "net": net, "label": label,
        "top_bullish": extremes["bullish"][1], "top_bearish": extremes["bearish"][1],
    }


if __name__ == "__main__":
    import json, time
    _ensure_loading()
    while _state == "loading":
        time.sleep(2)
    print(json.dumps(analyze([
        "Gold surges to record high as central banks accelerate buying",
        "TSMC warns of severe order cuts amid chip demand collapse",
        "Fed holds rates steady as expected",
    ]), indent=2))
