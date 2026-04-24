"""
Full article fetcher — extracts readable text from any news URL.
Uses newspaper3k (best), then BeautifulSoup fallback, then meta description.
No API key needed.
"""
import os, re, requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Paywall domains — skip full fetch, just open in new tab
PAYWALL_DOMAINS = {
    "bloomberg.com", "ft.com", "wsj.com", "economist.com",
    "barrons.com", "seekingalpha.com", "theatlantic.com",
}


def _is_paywall(url):
    for domain in PAYWALL_DOMAINS:
        if domain in url:
            return True
    return False


def _fetch_newspaper(url):
    try:
        from newspaper import Article
        art = Article(url)
        art.download()
        art.parse()
        text = art.text.strip()
        if len(text) > 200:
            return {
                "title":   art.title or "",
                "text":    text[:3000],
                "authors": art.authors,
                "image":   art.top_image or "",
                "source":  "newspaper",
            }
    except Exception:
        pass
    return None


def _fetch_bs4(url):
    try:
        resp = requests.get(url, timeout=10, headers=HEADERS)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "header", "footer",
                         "aside", "form", "iframe", "noscript"]):
            tag.decompose()

        # Try article tag first, then main, then body
        container = (
            soup.find("article") or
            soup.find("main") or
            soup.find(class_=re.compile(r"article|content|story|post", re.I)) or
            soup.body
        )
        if not container:
            return None

        paras = [p.get_text(" ", strip=True) for p in container.find_all("p")]
        text  = "\n\n".join(p for p in paras if len(p) > 60)

        title = ""
        t = soup.find("h1")
        if t:
            title = t.get_text(strip=True)

        image = ""
        og_img = soup.find("meta", property="og:image")
        if og_img:
            image = og_img.get("content", "")

        if len(text) > 200:
            return {
                "title":   title,
                "text":    text[:3000],
                "authors": [],
                "image":   image,
                "source":  "bs4",
            }
    except Exception:
        pass
    return None


def _fetch_meta(url):
    """Last resort — grab og:description as preview."""
    try:
        resp = requests.get(url, timeout=8, headers=HEADERS)
        soup = BeautifulSoup(resp.text, "html.parser")
        desc = (
            (soup.find("meta", property="og:description") or {}).get("content", "") or
            (soup.find("meta", attrs={"name": "description"}) or {}).get("content", "")
        )
        title = (soup.find("meta", property="og:title") or {}).get("content", "")
        image = (soup.find("meta", property="og:image") or {}).get("content", "")
        if desc:
            return {
                "title":   title,
                "text":    desc[:500] + "\n\n[Full article at source — click Open button]",
                "authors": [],
                "image":   image,
                "source":  "meta",
            }
    except Exception:
        pass
    return None


def fetch_article(url):
    """
    Fetch full article text from URL.
    Returns dict: title, text, authors, image, source, paywall
    """
    if not url or not url.startswith("http"):
        return {"error": "No URL", "paywall": False}

    if _is_paywall(url):
        return {
            "title":   "",
            "text":    "This article is behind a paywall. Click 'Open Article' to read on the original site.",
            "authors": [],
            "image":   "",
            "source":  "paywall",
            "paywall": True,
        }

    result = _fetch_newspaper(url) or _fetch_bs4(url) or _fetch_meta(url)

    if result:
        result["paywall"] = False
        return result

    return {
        "title":   "",
        "text":    "Could not extract article content. Click 'Open Article' to read on the original site.",
        "authors": [],
        "image":   "",
        "source":  "failed",
        "paywall": False,
    }
