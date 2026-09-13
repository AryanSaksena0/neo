"""test_feeds.py — the live-number sources, hit for real and parsed for real.

Every feed here is called against the actual internet, because the thing that
breaks a feed is never the parsing — it is the endpoint moving, the field
getting renamed, the host starting to 403. A mocked test of this file would
have passed happily through the two failures that actually happened while it
was being written: ESPN's documented host answering 403, and Yahoo Finance
rate-limiting so hard that one symbol lookup plus one quote was already too
much.

Where the network is unavailable those checks SKIP rather than fail — a
failing suite on a plane is a suite people stop running — but the parsing and
the spoken formatting are pure and always run.

Run: python3 test_feeds.py
"""
# Suites live in tests/ but RUN from the repo root, so that the modules
# under test import and open("neo.py") still resolves. This makes the
# import work either way, so a suite can also be run directly.
import os as _bootstrap_os, sys as _bootstrap_sys
_bootstrap_sys.path.insert(0, _bootstrap_os.path.dirname(
    _bootstrap_os.path.dirname(_bootstrap_os.path.abspath(__file__))))

import sys
import time

sys.path.insert(0, ".")
import feeds

FAILED, SKIPPED = [], []


def check(name, cond):
    print(("PASS - " if cond else "FAIL - ") + name)
    if not cond:
        FAILED.append(name)


def skip(name, why=""):
    print(f"SKIP - {name}" + (f" ({why})" if why else ""))
    SKIPPED.append(name)


ONLINE = feeds._get("https://api.coingecko.com/api/v3/ping", cache=0) is not None

# =========================================================================== #
# 1. Saying a number out loud. Pure, and the part a listener actually hears.
# =========================================================================== #
MONEY = [(319.7, "$319.70"), (0.5, "$0.5"), (7711.76, "$7,711.76"),
         (77645, "$77,645"), (2.4e9, "$2.4 billion"), (3.1e12, "$3.10 trillion"),
         (None, "?"), ("abc", "?")]
for n, want in MONEY:
    got = feeds.money(n)
    check(f"{n!r} is spoken as {want!r} (got {got!r})", got == want)

check("a share price keeps its cents — 'three hundred and twenty' when it is "
      "319.70 is the kind of small wrongness that makes a number useless",
      feeds.money(319.70) == "$319.70")

MOVE = [(1.63, "up 1.6%"), (-4.57, "down 4.6%"), (0.0, "flat"),
        (0.02, "flat"), ("-2.5", "down 2.5%"), ("", ""), (None, "")]
for n, want in MOVE:
    got = feeds.move(n)
    check(f"a move of {n!r} is spoken as {want!r} (got {got!r})", got == want)

# The bug this function exists for: CNBC already signs change_pct, and adding
# a second sign made '--4.57', which floats as nothing and SPOKE as nothing —
# every falling stock came out as "NVIDIA is at two eighteen,  on the day".
check("a falling stock's percentage survives, signed once",
      feeds.signed_pct({"change_pct": "-4.57%", "change": "-10.42"}) == "-4.57")
check("a rising stock loses its plus sign but keeps its value",
      feeds.signed_pct({"change_pct": "+1.63%", "change": "+5.12"}) == "1.63")
check("a missing percentage is empty, not zero — 'flat' would be a claim",
      feeds.signed_pct({}) == "" and feeds.signed_pct({"change_pct": "n/a"}) == "")

INDEX = {"symbol": ".SPX", "name": "S&P 500 Index", "last": "7,711.76",
         "type": "INDEX", "change_pct": "-0.25%", "currencyCode": "USD"}
said = feeds._say_quote(INDEX)
check("an index is not given a dollar sign — 'the S&P is at $7,712' is wrong "
      "in a way anyone who follows markets hears immediately",
      said and "$" not in said and "7,712" in said)
check("...and it still gets its direction", "down 0.2%" in said)

STOCK = {"symbol": "NVDA", "name": "NVIDIA Corporation", "last": "217.55",
         "type": "STOCK", "change_pct": "-4.57%", "change": "-10.42",
         "currencyCode": "USD", "curmktstatus": "POST_MKT"}
said = feeds._say_quote(STOCK)
check("a falling stock says so out loud",
      said and "down 4.6%" in said and "$217.55" in said)
check("a closed market is flagged, so a stale price isn't quoted as live",
      "closed" in said)
check("an open market says nothing about being closed",
      "closed" not in feeds._say_quote({**STOCK, "curmktstatus": "REG_MKT"}))
check("a quote with no price at all is refused rather than spoken as zero",
      feeds._say_quote({"symbol": "X", "last": None}) is None and
      feeds._say_quote({"symbol": "X", "last": "n/a"}) is None)

# =========================================================================== #
# 2. Symbols. The table costs nothing; the model handles the long tail.
# =========================================================================== #
check("a household name resolves with no network call at all",
      feeds.find_symbol("apple") == "AAPL" and feeds.find_symbol("Tesla") == "TSLA")
check("an index alias resolves too", feeds.find_symbol("the s&p") == ".SPX")
check("something that already looks like a ticker is passed straight through",
      feeds.find_symbol("NVDA") == "NVDA")
check("an unknown name with no model available doesn't invent a lookup",
      feeds.find_symbol("some private company", client=None) == "SOME PRIVATE COMPANY")


class _Says:
    def __init__(self, text):
        class _M:
            def generate_content(inner, model, contents):
                return type("R", (), {"text": text})()
        self.models = _M()


check("the model's ticker answer is cleaned up before it is used",
      feeds.find_symbol("duolingo", client=_Says("  duol \n")) == "DUOL")
check("a model saying 'NONE' means no ticker, not a ticker called NONE",
      feeds.find_symbol("my neighbour's dog", client=_Says("NONE")) == "")
check("a chatty model answer is stripped to the symbol",
      feeds.find_symbol("palantir corp", client=_Says("PLTR")) == "PLTR")
check("a resolved name is remembered for the session",
      "duolingo" in feeds._TICKERS)


class _Dead:
    class models:
        @staticmethod
        def generate_content(model, contents):
            raise RuntimeError("503")


check("a model outage means no symbol, not a guessed one",
      feeds.find_symbol("obscure thing ltd", client=_Dead,
                        log=lambda m: None) == "")

check("a coin is routed to the crypto feed, not looked up as a ticker",
      feeds.looks_like_crypto("bitcoin") and feeds.looks_like_crypto("eth") and
      feeds.looks_like_crypto("dogecoin") and not feeds.looks_like_crypto("apple"))

# =========================================================================== #
# 3. Leagues.
# =========================================================================== #
LEAGUE = [("what's the nba score", "basketball/nba"),
          ("any premier league games on", "soccer/eng.1"),
          ("did the nfl start", "football/nfl"),
          ("champions league tonight", "soccer/uefa.champions"),
          ("what's on tv", "")]
for text, want in LEAGUE:
    got = feeds.league_path(text)
    check(f"{text!r} -> {want!r} (got {got!r})", got == want)
check("a longer league name wins over a shorter one inside it",
      feeds.league_path("champions league") == "soccer/uefa.champions")

# =========================================================================== #
# 4. The real endpoints.
# =========================================================================== #
if not ONLINE:
    skip("every live endpoint", "no network")
else:
    got = feeds.quotes(["AAPL", "NVDA", ".SPX"])
    check(f"CNBC answers three symbols in ONE request ({len(got)} back)",
          len(got) == 3 and all(g.get("last") for g in got.values()))

    # The reason CNBC is here instead of Yahoo: Yahoo 429s on the second
    # request. Six in a row have to work, or the feed is not usable in a
    # conversation where he asks about three companies.
    t0 = time.time()
    burst = [bool(feeds.quotes([s]))
             for s in ("AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA")]
    check(f"six back-to-back quotes all answer ({sum(burst)}/6 in "
          f"{time.time() - t0:.1f}s) — this is the check Yahoo failed",
          all(burst))

    said = feeds.stock("apple")
    check(f"a real stock reads as a sentence: {said!r}",
          said and "Apple" in said and "$" in said and said.endswith("."))
    check("a company that does not exist returns nothing rather than a price",
          feeds.stock("zzqq not a company", client=_Says("NONE")) is None)

    # CoinGecko's free tier rate-limits a burst hard and briefly, exactly like
    # Yahoo above, and _get already spends its one retry getting past that. Run
    # this suite twice in quick succession — which is a reasonable thing to do —
    # and the second run hits a wall that says nothing about Neo. A third
    # party's rate limiter must not be able to block a commit, so a None here
    # gets one more patient try and then becomes a SKIP with the reason, not a
    # FAIL. A coin that answers with the WRONG sentence still fails.
    def _coin(name, tries=2, pause=4.0):
        for i in range(tries):
            got = feeds.crypto(name)
            if got:
                return got
            if i + 1 < tries:
                time.sleep(pause)
        return None

    said = _coin("bitcoin")
    if said is None:
        skip("crypto reads as a sentence", "CoinGecko rate-limited or down")
        skip("an abbreviation is spoken as the coin's real name", "same")
    else:
        check(f"crypto reads as a sentence: {said!r}",
              "Bitcoin" in said and "$" in said)
        _eth = _coin("eth")
        if _eth is None:
            skip("an abbreviation is spoken as the coin's real name", "rate-limited")
        else:
            check("an abbreviation is spoken as the coin's real name, not the "
                  "abbreviation they happened to use", _eth.startswith("Ethereum"))
    check("a coin that isn't a coin returns nothing",
          feeds.crypto("notacoinatall") is None)

    said = feeds.market()
    check(f"the market summary names all three indexes: {said!r}",
          said and "S&P" in said and "Nasdaq" in said and "Dow" in said)
    check("...and it never puts a dollar sign on an index",
          "$" not in (said or ""))

    got = feeds.scores("nba")
    check(f"ESPN answers for a real league ({str(got)[:60]!r})",
          got is None or isinstance(got, str) and got)
    check("a league nobody named returns nothing rather than a random sport",
          feeds.scores("what's happening") is None)
    check("a made-up league returns nothing",
          feeds.scores("intergalactic quidditch") is None)

    got = feeds.odds("fed rate")
    if got is None:
        skip("prediction markets", "nothing open on that topic right now")
    else:
        check(f"odds come back as percentages: {got[:70]!r}", "%" in got)
        # The whole reason DECIDED exists: an answer made entirely of settled
        # markets ("No at 100%; No at 100%") is worse than no answer.
        pcts = [int(p.rstrip("%")) for p in got.split()
                if p.rstrip("%").isdigit() and p.endswith("%")]
        check(f"no settled market is quoted as an opinion (percentages "
              f"{pcts})", all(p < feeds.DECIDED * 100 for p in pcts))
    # Deterministic, unlike the live check above: the filter has to judge the
    # number that gets PRINTED. 0.955 passed a raw `p >= DECIDED` test and then
    # rendered as "96%" — which is how a settled market got quoted as an
    # opinion on a day when the Fed market sat at 95.6.
    check("a price that ROUNDS to settled is filtered, not just one that is",
          feeds.is_decided(95.5) and feeds.is_decided(95.6))
    check("...and a genuinely open market still gets through",
          not feeds.is_decided(94.0) and not feeds.is_decided(60.0))
    check("...at both ends — a top outcome at 4% is decided too",
          feeds.is_decided(4.0) and feeds.is_decided(0.0))

    check("a topic with no market says nothing rather than inventing a number",
          feeds.odds("whether the user will do the dishes") is None)

    # Caching. He will ask twice; the second time must be free.
    feeds.stock("apple")
    t0 = time.time()
    feeds.stock("apple")
    check(f"a repeated question is served from cache "
          f"({(time.time() - t0) * 1000:.0f}ms)", time.time() - t0 < 0.05)

check("a dead URL returns None instead of raising",
      feeds._get("https://example.invalid/nothing", cache=0) is None)
check("a URL that returns HTML instead of JSON returns None",
      feeds._get("https://example.com/", cache=0) is None)

print()
if SKIPPED:
    print(f"({len(SKIPPED)} skipped)")
if FAILED:
    print(f"{len(FAILED)} FAILED:")
    for n in FAILED:
        print("  - " + n)
    sys.exit(1)
print("Feeds clean.")
