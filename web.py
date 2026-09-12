"""
web.py — plain web search and page text, with no key.

Bing's HTML, then DuckDuckGo's — whichever answers. Used by search_web,
read_webpage and any skill that needs to look something up.
"""

import re
import urllib.parse

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")}


def _get(url, params=None, data=None):
    import requests
    h = dict(UA)
    h["Accept-Language"] = "en-US,en;q=0.9"
    h["Accept"] = "text/html,application/xhtml+xml"
    if data is not None:
        return requests.post(url, data=data, headers=h, timeout=12)
    return requests.get(url, params=params, headers=h, timeout=12)


def _bing(query, n):
    from bs4 import BeautifulSoup
    r = _get("https://www.bing.com/search", params={"q": query, "count": n})
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for h in soup.select("li.b_algo h2 a"):
        href = h.get("href", "")
        if href.startswith("http"):
            out.append({"title": h.get_text(strip=True), "url": href})
        if len(out) >= n:
            break
    return out


def _ddg_html(query, n):
    from bs4 import BeautifulSoup
    r = _get("https://html.duckduckgo.com/html/", data={"q": query})
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        real = qs.get("uddg", [href])[0]
        if real.startswith("//"):
            real = "https:" + real
        if real.startswith("http"):
            out.append({"title": a.get_text(strip=True), "url": real})
        if len(out) >= n:
            break
    return out


def _ddg_lite(query, n):
    from bs4 import BeautifulSoup
    r = _get("https://lite.duckduckgo.com/lite/", data={"q": query})
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("//"):
            href = "https:" + href
        if href.startswith("http") and "duckduckgo.com" not in href:
            out.append({"title": a.get_text(strip=True), "url": href})
        if len(out) >= n:
            break
    return out


def web_search(query, n=8):
    """Try multiple engines with retries so one source blocking doesn't kill it."""
    import time
    import random
    for engine in (_bing, _ddg_html, _ddg_lite):
        for _ in range(2):
            try:
                res = engine(query, n)
                if len(res) >= 3:
                    return res[:n]
            except Exception as e:
                print(f"[leads] {engine.__name__} failed: {e}")
            time.sleep(0.7 + random.random())
    print("[leads] all search engines came up empty.")
    return []


def fetch_text(url, max_chars=1500):
    """Grab readable text from a page (for grounding answers)."""
    try:
        import requests  # noqa
        from bs4 import BeautifulSoup
        r = _get(url)
        s = BeautifulSoup(r.text, "html.parser")
        for tag in s(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.extract()
        txt = " ".join(s.get_text(" ").split())
        return txt[:max_chars]
    except Exception:
        return ""


def _domain(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


