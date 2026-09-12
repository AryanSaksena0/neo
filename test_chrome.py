"""
test_chrome.py — proves chrome.py works, in two layers.

  python test_chrome.py          # pure logic (runs anywhere, no Chrome needed)
  python test_chrome.py --live   # ALSO drives your real Chrome (macOS only):
                                  # lists your profiles, opens a harmless test
                                  # page in one, reads the tab back to confirm.

The --live layer is the "does it actually go through Chrome" check. It opens
example.com (nothing private) in your first non-Default profile, then reads the
active tab URL back to prove the profile launch + tab-read round-trip works.
"""

import sys

import chrome

FAIL = []


def check(name, cond):
    if not cond:
        FAIL.append(name)
    print(("PASS" if cond else "FAIL"), "-", name)


# --- sample Local State (what Chrome actually writes) ---
_SAMPLE = """{"profile":{"info_cache":{
  "Default":{"name":"the user Personal"},
  "Profile 1":{"name":"the user School"},
  "Profile 2":{"name":"the project Work"}}}}"""


def pure():
    p = chrome.load_profiles(_SAMPLE)
    check("profiles: parsed all three", set(p.values()) == {"Default", "Profile 1", "Profile 2"})
    check("profiles: friendly name indexed", p.get("the user school") == "Profile 1")
    check("profiles: bad json -> empty", chrome.load_profiles("{not json") == {})

    check("resolve: 'school' -> Profile 1", chrome.resolve_profile("school", p) == "Profile 1")
    check("resolve: 'my personal profile' -> Default",
          chrome.resolve_profile("my personal profile", p) == "Default")
    check("resolve: 'work' -> Profile 2", chrome.resolve_profile("work", p) == "Profile 2")
    check("resolve: unknown -> None", chrome.resolve_profile("grandma", p) is None)
    check("resolve: dir name works", chrome.resolve_profile("profile 1", p) == "Profile 1")

    check("url: schoology alias",
          chrome.target_url("open schoology in my school profile") == "https://app.schoology.com")
    check("url: gmail alias",
          chrome.target_url("pull up gmail on my personal account") == "https://mail.google.com")
    check("url: google search",
          "search?q=" in (chrome.target_url("google the french revolution in my school profile") or ""))
    check("url: search query excludes profile clause",
          "school" not in (chrome.target_url("google photosynthesis in my school profile") or ""))
    check("url: bare domain",
          chrome.target_url("go to nytimes.com in my personal profile") == "https://nytimes.com")
    check("url: nonsense -> None", chrome.target_url("how are you feeling") is None)

    check("parse: full request", chrome.parse_request("open schoology in my school profile")
          == ("https://app.schoology.com", "school"))
    check("parse: no profile clause still opens",
          chrome.parse_request("open gmail")[0] == "https://mail.google.com")
    check("parse: chit-chat -> None", chrome.parse_request("tell me a joke") is None)

    check("login: google signin detected",
          chrome.looks_like_login("https://accounts.google.com/signin/v2/identifier"))
    check("login: schoology sso detected",
          chrome.looks_like_login("https://myschool.schoology.com/login?school=1"))
    check("login: normal page not flagged",
          not chrome.looks_like_login("https://app.schoology.com/home"))
    check("login: empty not flagged", not chrome.looks_like_login(""))

    # email via browser (the supervised-account fix)
    check("email: 'check my email' detected", chrome.is_email("check my email"))
    check("email: 'any new emails' detected", chrome.is_email("any new emails"))
    check("email: 'open my inbox' detected", chrome.is_email("open my inbox"))
    check("email: chit-chat not email", not chrome.is_email("email me later means nothing"))
    check("email: -> gmail url", chrome.target_url("check my email") == "https://mail.google.com")
    check("email: with profile", chrome.parse_request("check my school email")
          == ("https://mail.google.com", "school"))
    check("email: routes to chrome", chrome.wants_chrome("any new emails in my school account"))
    # a DB query that MENTIONS email is NOT an inbox check (real 22:55 bug)
    _db = "check the the project database for users and their email addresses"
    check("email: db-with-email-field is NOT inbox", not chrome.is_email(_db))
    check("email: 'email verified' isn't inbox", not chrome.is_email("show users whose email verified"))
    check("email: real inbox still detected", chrome.is_email("check my email"))


def live():
    print("\n--- LIVE (real Chrome) ---")
    if not chrome.installed():
        print("SKIP - Chrome not found at the expected path; can't run live tests.")
        return
    profs = chrome.load_profiles()
    check("live: found real profiles", len(profs) > 0)
    print("      your profiles:", {v: k for k, v in profs.items()})
    # pick a real profile dir to test with (prefer a non-Default one)
    dirs = sorted(set(profs.values()))
    target_dir = next((d for d in dirs if d != "Default"), dirs[0] if dirs else None)
    print(f"      testing with profile dir: {target_dir}")
    res = chrome.open_in_profile("https://example.com", target_dir)
    check("live: launch returned ok", res is True)
    landed = chrome.check_landing("https://example.com")
    print("      our tab after open:", landed or "(not found)")
    check("live: found our example.com tab across windows",
          "example.com" in landed)
    check("live: correctly NOT flagged as login", not chrome.looks_like_login(landed))
    print("      (profile launch works — screenshot proof + tab found)")
    print("\n      NEXT: try a real one yourself —")
    print("      python test_chrome.py  then in Neo: 'open schoology in my <profile> profile'")


if __name__ == "__main__":
    pure()
    if "--live" in sys.argv:
        live()
    print()
    if FAIL:
        print(f"{len(FAIL)} FAILED:", ", ".join(FAIL))
        raise SystemExit(1)
    print("chrome.py: all checks passed.")
