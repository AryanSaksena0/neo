"""
feeds.py — live numbers Neo can quote: markets, crypto, scores, odds.

Every source here is free and KEYLESS. Nothing in this file needs an account,
a card, or a line in .env, which is the point: Neo's free-forever constraint
means a knowledge source that needs a paid key is not a knowledge source, it is
a liability waiting for a bill.

    stocks       quote.cnbc.com                    quotes, many at once
    crypto       api.coingecko.com                 free tier, no key
    scores       site.web.api.espn.com             live and scheduled games
    odds         gamma-api.polymarket.com          prediction markets
                 api.elections.kalshi.com          the regulated one

All four were called from this machine before this file was written. Two notes
from that, because the obvious choice is wrong in both cases:
`site.api.espn.com` — the host every example uses — answers 403 here, while
`site.web.api.espn.com` answers 200 with the same payload. And Yahoo Finance,
the obvious place for a stock price, rate-limits so tightly that ONE symbol
lookup followed by ONE quote is already too many requests; after a few minutes
of testing it stopped answering this address at two-second gaps at all. CNBC
took six back-to-back requests without complaint and returns a whole list of
symbols in a single call, so that is what this uses.

Everything returns a SPOKEN sentence or two, already rounded and already in
words. A number read out to four decimal places is a number nobody heard.
"""

import json
import re
import subprocess
import time
import urllib.parse

TIMEOUT = 12
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
_CACHE = {}
CACHE_S = 90          # a price does not change between two sentences
RETRY_S = 1.5         # one pause, then one retry; see _get


def _get(url, cache=CACHE_S):
    """JSON from a URL. None on any failure — a feed that is down is not an
    exception, it is one sentence about not being able to check.

    curl rather than urllib, because urllib is not built to look like a
    browser and several of these hosts care.

    Yahoo also rate-limits hard and briefly: a short burst of requests earns
    429 for about twenty seconds and then it is fine again. (This was first
    diagnosed as TLS fingerprinting, because curl was answering while urllib
    429'd — but that was just the burst limit arriving between the two tests.
    Measured properly: 429, then 200 at twenty seconds, and 200 ever after.)
    So a 429 gets ONE retry after a short pause and otherwise turns into "I
    couldn't get that just now" — waiting twenty seconds in a spoken
    conversation is not an option, and the cache means the next question
    usually costs nothing at all.
    """
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < cache:
        return hit[1]
    for attempt in (0, 1):
        try:
            r = subprocess.run(
                ["curl", "-s", "--compressed", "-m", str(TIMEOUT),
                 "-A", UA, "-H", "Accept: application/json", url],
                capture_output=True, timeout=TIMEOUT + 4)
            if r.returncode != 0 or not r.stdout:
                return None
            data = json.loads(r.stdout.decode("utf-8", "replace"))
        except Exception:
            if attempt == 0:
                time.sleep(RETRY_S)
                continue
            return None
        _CACHE[url] = (time.time(), data)
        return data
    return None


def money(n, currency="USD"):
    """A price, the way it is said out loud."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    sign = "$" if currency == "USD" else ""
    if abs(n) >= 1_000_000_000_000:
        return f"{sign}{n / 1e12:.2f} trillion"
    if abs(n) >= 1_000_000_000:
        return f"{sign}{n / 1e9:.1f} billion"
    if abs(n) >= 10000:
        return f"{sign}{n:,.0f}"
    if abs(n) >= 1:
        # Cents stay. "Apple is at three hundred and twenty" when it is at
        # 319.70 is the kind of small wrongness that makes a number useless.
        return f"{sign}{n:,.2f}"
    return f"{sign}{n:.4f}".rstrip("0")


def move(pct):
    """'up two and a half percent' — direction first, because that is the
    part they actually asked about."""
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return ""
    if abs(pct) < 0.05:
        return "flat"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.1f}%"


# --------------------------------------------------------------------------- #
# Stocks.
# --------------------------------------------------------------------------- #
_TICKERS = {}          # name -> symbol, for this session

# The handful anybody actually asks about out loud, so the common case costs
# nothing at all. Everything else goes to the model, which knows tickers cold.
_KNOWN = {
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA", "microsoft": "MSFT",
    "google": "GOOGL", "alphabet": "GOOGL", "amazon": "AMZN", "meta": "META",
    "facebook": "META", "netflix": "NFLX", "openai": "", "palantir": "PLTR",
    "s&p": ".SPX", "s&p 500": ".SPX", "sp500": ".SPX", "the s&p": ".SPX",
    "nasdaq": ".IXIC", "dow": ".DJI", "dow jones": ".DJI",
    "vix": ".VIX", "russell": ".RUT",
}


def find_symbol(name, client=None, model=None, log=print):
    """'apple' -> 'AAPL'. '' when there is no such listed company.

    There is no free keyless ticker search that works — CNBC's is gone, Yahoo's
    is behind the same rate limit as its quotes. But a ticker is exactly the
    kind of thing a language model knows without looking anything up, so the
    model is asked. It is one short call, cached for the session, and only for
    names not in the table above.
    """
    key = (name or "").strip().lower()
    if not key:
        return ""
    if key in _TICKERS:
        return _TICKERS[key]
    if key in _KNOWN:
        return _KNOWN[key]
    raw = (name or "").strip()
    if raw.isupper() and 1 <= len(raw) <= 5 and raw.isalpha():
        return raw                      # already a ticker
    if client is None:
        return raw.upper()
    try:
        r = client.models.generate_content(
            model=model or "gemini-2.5-flash",
            contents=(f"What is the stock ticker symbol for {raw!r}? Reply "
                      "with ONLY the symbol, uppercase, nothing else. If it is "
                      "not a publicly traded company, reply NONE."))
        got = (getattr(r, "text", "") or "").strip().upper()
    except Exception as e:
        log(f"[feeds] couldn't look up a ticker: {type(e).__name__}")
        return ""
    got = re.sub(r"[^A-Z.\-]", "", got)[:8]
    if not got or got == "NONE":
        _TICKERS[key] = ""
        return ""
    _TICKERS[key] = got
    return got


def quotes(symbols):
    """Live quotes for one or more symbols, in ONE request.

    CNBC rather than Yahoo, and this was measured rather than assumed. Yahoo's
    chart endpoint rate-limits so tightly that two requests in a row is already
    too many — the symbol lookup and the quote together were enough to earn a
    429, and after a few minutes of testing it stopped answering this address
    at two-second gaps as well. CNBC answered six back-to-back requests without
    complaint and takes a whole list in one call.
    """
    if isinstance(symbols, str):
        symbols = [symbols]
    symbols = [s for s in symbols if s]
    if not symbols:
        return {}
    data = _get("https://quote.cnbc.com/quote-html-webservice/quote.htm"
                "?symbols=" + urllib.parse.quote("|".join(symbols))
                + "&requestMethod=itv&noform=1&partnerId=2&fund=1"
                "&exthrs=1&output=json")
    rows = ((data or {}).get("ITVQuoteResult") or {}).get("ITVQuote") or []
    if isinstance(rows, dict):
        rows = [rows]
    out = {}
    for q in rows:
        sym = q.get("symbol")
        if sym and q.get("last"):
            out[sym.upper()] = q
    return out


def signed_pct(q):
    """The day's move as a signed number. '' if the feed didn't give one.

    CNBC reports change_pct as '+1.63%' on the way up and '-4.57%' on the way
    down, so the sign is already there. Adding one from the `change` field
    produced '--4.57', which floats as nothing, which spoke as nothing: every
    falling stock came out as "NVIDIA is at two eighteen,  on the day".
    """
    raw = str(q.get("change_pct") or "").replace("%", "").replace("+", "").strip()
    if not raw:
        return ""
    try:
        float(raw)
    except ValueError:
        return ""
    return raw


def _say_quote(q):
    """One quote, as a spoken sentence."""
    name = q.get("name") or q.get("shortName") or q.get("symbol")
    try:
        last = float(str(q.get("last")).replace(",", ""))
    except (TypeError, ValueError):
        return None
    # An index has no price and no currency — "the S&P is at $7,712" is wrong
    # in a way anyone who follows markets will hear immediately.
    is_index = (q.get("type") or "").upper() == "INDEX" or \
        str(q.get("symbol", "")).startswith(".")
    if is_index:
        out = f"{name} is at {last:,.0f}"
    else:
        out = f"{name} is at {money(last, q.get('currencyCode', 'USD'))}"
    pct = signed_pct(q)
    if pct:
        out += f", {move(pct)} on the day"
    state = (q.get("curmktstatus") or "").upper()
    if state and "REG" not in state:
        # Wording matters here. This used to read "the market's closed, so
        # that's the last price", and the model took that as a reason not to
        # give the price at all: asked what Apple was trading at, with $319.70
        # in front of it, Neo said "markets are closed, so I don't have a live
        # quote". The number is the answer; the closure is a footnote.
        out += " (that's the last trade — the market is closed now)"
    return out + "."


def stock(name, client=None, model=None, log=print):
    """What a stock is doing right now, as a spoken sentence. None if unknown."""
    symbol = find_symbol(name, client=client, model=model, log=log)
    if not symbol:
        return None
    got = quotes([symbol])
    q = got.get(symbol.upper())
    if not q:
        return None
    return _say_quote(q)


def market(_=""):
    """The whole market in one line: S&P, Nasdaq, Dow. One request."""
    got = quotes([".SPX", ".IXIC", ".DJI"])
    order = [(".SPX", "The S&P"), (".IXIC", "the Nasdaq"), (".DJI", "the Dow")]
    bits = []
    for sym, label in order:
        q = got.get(sym)
        if not q:
            continue
        pct = signed_pct(q)
        if pct:
            bits.append(f"{label} is {move(pct)}")
    return ", ".join(bits) + "." if bits else None


_COINS = {"bitcoin", "btc", "ethereum", "eth", "solana", "sol", "dogecoin",
          "doge", "ripple", "xrp", "cardano", "ada", "litecoin", "polkadot",
          "chainlink", "avalanche", "monero", "tether", "usdc", "binance coin",
          "bnb", "shiba inu", "polygon", "toncoin", "sui", "aptos"}


def looks_like_crypto(text):
    """Is they asking about a coin? Cheap, and only used to pick which feed."""
    low = (text or "").strip().lower()
    return low in _COINS or low.endswith(("coin", "crypto"))


def crypto(name):
    """Same, for coins. CoinGecko's free tier, no key."""
    slug = (name or "").strip().lower().replace(" ", "-")
    alias = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana",
             "doge": "dogecoin", "xrp": "ripple", "ada": "cardano"}
    slug = alias.get(slug, slug)
    data = _get("https://api.coingecko.com/api/v3/simple/price?ids="
                + urllib.parse.quote(slug)
                + "&vs_currencies=usd&include_24hr_change=true")
    row = (data or {}).get(slug)
    if not row:
        return None
    out = f"{slug.replace('-', ' ').title()} is at {money(row.get('usd'))}"
    ch = row.get("usd_24h_change")
    if ch is not None:
        out += f", {move(ch)} in the last day"
    return out + "."


# --------------------------------------------------------------------------- #
# Scores.
# --------------------------------------------------------------------------- #
LEAGUES = {
    "nba": "basketball/nba", "wnba": "basketball/wnba",
    "ncaab": "basketball/mens-college-basketball",
    "nfl": "football/nfl", "ncaaf": "football/college-football",
    "mlb": "baseball/mlb", "nhl": "hockey/nhl",
    "premier league": "soccer/eng.1", "epl": "soccer/eng.1",
    "la liga": "soccer/esp.1", "champions league": "soccer/uefa.champions",
    "mls": "soccer/usa.1", "f1": "racing/f1",
}


def league_path(text):
    """Which league they meant. '' if they didn't name one."""
    low = (text or "").lower()
    for name in sorted(LEAGUES, key=len, reverse=True):
        if name in low:
            return LEAGUES[name]
    return ""


def scores(text, limit=6):
    """Today's games in a league. A spoken list, or None."""
    path = league_path(text)
    if not path:
        return None
    data = _get("https://site.web.api.espn.com/apis/site/v2/sports/"
                + path + "/scoreboard", cache=30)
    events = (data or {}).get("events") or []
    if not events:
        return None
    lines = []
    for ev in events[:limit]:
        try:
            comp = ev["competitions"][0]
            status = comp["status"]["type"]
            teams = comp["competitors"]
            named = {t.get("homeAway"): t for t in teams}
            home, away = named.get("home", teams[0]), named.get("away", teams[-1])
            hn = home["team"].get("shortDisplayName") or home["team"]["abbreviation"]
            an = away["team"].get("shortDisplayName") or away["team"]["abbreviation"]
            if status.get("state") == "pre":
                lines.append(f"{an} at {hn}, {status.get('shortDetail', 'later')}")
            else:
                hs, as_ = home.get("score", "0"), away.get("score", "0")
                tail = ("final" if status.get("completed")
                        else status.get("shortDetail", "in progress"))
                lines.append(f"{an} {as_}, {hn} {hs} — {tail}")
        except (KeyError, IndexError, TypeError):
            continue
    return "; ".join(lines) if lines else None


# --------------------------------------------------------------------------- #
# Prediction markets.
# --------------------------------------------------------------------------- #
# A market trading at 99% is not an opinion, it is a settled question waiting
# for its paperwork — and a whole answer made of them ("No at 100%; No at 100%")
# is worse than saying nothing. Odds this lopsided are dropped.
DECIDED = 0.96


def is_decided(pct):
    """True when a market is settled enough that quoting it as an opinion is
    noise rather than information. Takes the percentage AS DISPLAYED.

    That last part is the bug this exists to close. The polymarket path filtered
    the raw price (0..1) and then printed it rounded, so 0.955 passed a
    `p >= DECIDED` test and came out of the renderer as "96%" — the settled
    market DECIDED is here to keep out, quoted as though it were an opinion.
    Judge the number that will actually be shown, and the filter and the
    rendering can't disagree.

    Both ends count: a top outcome at 4% is just as decided as one at 96%.
    """
    pct = round(pct)
    return pct >= DECIDED * 100 or pct <= (1 - DECIDED) * 100


def polymarket(query, limit=3):
    """What the crowd thinks something costs. A spoken list, or None."""
    data = _get("https://gamma-api.polymarket.com/public-search?q="
                + urllib.parse.quote(query or "") + "&limit_per_type=8",
                cache=120)
    events = []
    if isinstance(data, dict):
        events = data.get("events") or []
    elif isinstance(data, list):
        events = data
    picks = []
    for ev in events:
        if ev.get("closed") or not ev.get("active", True):
            continue
        title = (ev.get("title") or "").strip()
        for m in (ev.get("markets") or []):
            if m.get("closed") or m.get("archived") or not m.get("active", True):
                continue
            try:
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                outcomes = m.get("outcomes")
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if not prices or not outcomes:
                    continue
                top = max(range(len(prices)), key=lambda i: float(prices[i]))
                p = float(prices[top])
            except (ValueError, TypeError, KeyError, IndexError):
                continue
            if is_decided(p * 100):
                continue
            picks.append((float(m.get("volume24hr") or 0),
                          (m.get("question") or title)[:90],
                          str(outcomes[top]), p * 100))
    if not picks:
        return None
    # Busiest first: a market nobody is trading is not a price, it is a guess
    # with a number on it.
    picks.sort(key=lambda p: -p[0])
    return "; ".join(f"{q} — {o} at {pct:.0f}%" for _, q, o, pct in picks[:limit])


def kalshi(query, limit=3):
    """Kalshi, the regulated one. Public read, no key."""
    data = _get("https://api.elections.kalshi.com/trade-api/v2/markets"
                "?limit=60&status=open", cache=120)
    markets = (data or {}).get("markets") or []
    want = [w for w in (query or "").lower().split() if len(w) > 3]
    hits = []
    for m in markets:
        title = (m.get("title") or "") + " " + (m.get("subtitle") or "")
        if want and not any(w in title.lower() for w in want):
            continue
        price = m.get("last_price") or m.get("yes_bid")
        if price is None:
            continue
        try:
            price = int(price)
        except (TypeError, ValueError):
            continue
        if is_decided(price):
            continue                 # already decided; see is_decided
        hits.append(f"{title.strip()[:90]} — yes at {price}%")
        if len(hits) >= limit:
            break
    return "; ".join(hits) if hits else None


def odds(query):
    """Both prediction markets, whichever has it."""
    parts = [p for p in (polymarket(query), kalshi(query)) if p]
    return " | ".join(parts) if parts else None
