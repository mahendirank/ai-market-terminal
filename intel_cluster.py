"""
intel_cluster.py — Similarity-based headline clustering.

Groups paraphrased / multi-source variants of the same story into single
"clusters" so AI tabs see N distinct stories instead of 5N near-duplicate
headlines. The existing dedup in news.py only catches exact normalized
matches — anything reworded slips through.

Algorithm (pure Python, no external deps):

  1. Tokenize each headline → lowercase word set, drop stopwords + tickers
  2. Compute Jaccard similarity between every pair: |A ∩ B| / |A ∪ B|
  3. Single-link agglomerative clustering at threshold (default 0.45)
  4. Within each cluster: keep the highest-scoring headline as the
     representative, aggregate source list, max importance score

Why Jaccard not TF-IDF cosine: TF-IDF wants a fitted corpus; news headlines
are streamy and the marginal accuracy gain isn't worth the implementation
complexity. Jaccard at 0.45 cuts ~30-50% of near-duplicate volume in
practice.

Output schema per cluster:
  {
    "topic":      "<best representative headline>",
    "headlines":  [{"text", "source", "score", "url"} ...],
    "sources":    [<deduped source list>],
    "max_score":  <highest importance score in cluster>,
    "size":       <number of headlines in cluster>,
    "tickers":    [<deduped ticker symbols mentioned>],
  }
"""
from __future__ import annotations

import re
from typing import Iterable

# Common English stopwords + market filler words that hurt Jaccard signal.
# Keep this list tight — too many drops kills genuine similarity.
_STOP = frozenset("""
a an the and or but of for to in on at by with from as is are was were be been being
this that these those it its their there here have has had does do did will would
shall should may might must can could says said say report reports reporting
new news today latest update updates breaking just amid after before
""".split())

_TICKER_RE = re.compile(r"^[A-Z]{2,5}(?:\.NS|\.BO|=X|=F|-USD)?$")
_WORD_RE   = re.compile(r"[A-Za-z][A-Za-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Convert a headline into a comparable token set.

    - Lowercase, alphanumeric words only
    - Drop stopwords
    - Drop ALL-CAPS tickers (they're noise for similarity — same story
      can hit different tickers in different versions)
    - Keep tokens length >= 3
    """
    if not text:
        return set()
    out: set[str] = set()
    for w in _WORD_RE.findall(text):
        if _TICKER_RE.match(w):
            continue
        wl = w.lower()
        if len(wl) < 3 or wl in _STOP:
            continue
        out.add(wl)
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _extract_tickers(text: str) -> list[str]:
    """Pull obvious ticker symbols out of a headline."""
    if not text:
        return []
    return [w for w in _WORD_RE.findall(text) if _TICKER_RE.match(w)]


def cluster_headlines(
    items: Iterable[dict],
    *,
    threshold: float = 0.45,
    max_clusters: int = 40,
    text_key: str = "text",
    score_key: str = "score",
) -> list[dict]:
    """Cluster a stream of news items by headline similarity.

    Parameters
    ----------
    items
        Iterable of dicts with at least ``text_key`` present. Each dict may
        also carry ``source``, ``url``, ``score`` (importance from upstream),
        and any other fields — they're preserved on the cluster's headlines.
    threshold
        Jaccard similarity threshold to merge into the same cluster. 0.45 is
        a good default for news headlines; lower clusters more aggressively.
    max_clusters
        Cap on returned clusters. Lower-importance clusters are dropped
        once the cap is hit.

    Returns
    -------
    list[dict]
        Ordered by aggregated importance (highest first).
    """
    items_list = [it for it in items if isinstance(it, dict) and it.get(text_key)]
    if not items_list:
        return []

    # Pre-compute token sets to avoid O(n²) re-tokenization
    tokens: list[set[str]] = [_tokens(it[text_key]) for it in items_list]
    n = len(items_list)

    # Union-find for single-link clustering
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]   # path compression
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # Compare every pair — O(n²) but n is typically < 200 for news feeds
    for i in range(n):
        if not tokens[i]:
            continue
        for j in range(i + 1, n):
            if not tokens[j]:
                continue
            if _jaccard(tokens[i], tokens[j]) >= threshold:
                union(i, j)

    # Group by cluster root
    clusters: dict[int, list[int]] = {}
    for idx in range(n):
        root = find(idx)
        clusters.setdefault(root, []).append(idx)

    # Build cluster dicts
    out: list[dict] = []
    for member_indices in clusters.values():
        members = [items_list[i] for i in member_indices]
        # Pick the best representative — highest score, fallback first
        best = max(members, key=lambda m: float(m.get(score_key) or 0))
        max_score = max(float(m.get(score_key) or 0) for m in members)
        sources = sorted({m.get("source", "") for m in members if m.get("source")})
        tickers = sorted({t for m in members for t in _extract_tickers(m.get(text_key, ""))})
        out.append({
            "topic":     best.get(text_key, ""),
            "headlines": [
                {
                    "text":   m.get(text_key, ""),
                    "source": m.get("source", ""),
                    "url":    m.get("url", ""),
                    "score":  float(m.get(score_key) or 0),
                }
                for m in members
            ],
            "sources":   sources,
            "max_score": max_score,
            "size":      len(members),
            "tickers":   tickers,
        })

    out.sort(key=lambda c: (c["max_score"], c["size"]), reverse=True)
    return out[:max_clusters]


def compression_stats(raw_count: int, clusters: list[dict]) -> dict:
    """Quick stats on how much volume was compressed by clustering."""
    if raw_count == 0:
        return {"raw": 0, "clusters": 0, "compression_ratio": 0.0}
    return {
        "raw": raw_count,
        "clusters": len(clusters),
        "compression_ratio": round(1 - (len(clusters) / raw_count), 3),
    }
