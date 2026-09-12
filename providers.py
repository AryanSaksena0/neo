"""
providers.py — which model Neo uses for which job, and how it survives a model
being retired underneath it.

The old code hardcoded MODEL = "gemini-2.5-flash" and kept a small candidate
list for the lite model only. Two things went wrong with that: the pinned model
aged out of date (2.5 was two generations old while the free tier had moved on),
and a retired lite id burned a 404 on every turn.

So: jobs, not models. Ask for a job ("chat", "heavy", "fast", "stt", "live"),
get back the best model that actually answers today. The first answer is cached
to models.json so later boots cost nothing, a model that starts failing is
demoted mid-session, and a provider with no key in .env is skipped entirely.

That last part is what makes second providers free to add: drop GROQ_API_KEY or
CEREBRAS_API_KEY into .env and the candidates that need them light up. Leave
them out and Neo runs on the Gemini key alone, exactly as before.

Everything above the network line is pure and tested in test_neo.py.
"""

import json
import os
import threading
import time

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models.json")
CACHE_TTL_S = 24 * 3600      # re-probe daily; models retire, new ones land


# --------------------------------------------------------------------------- #
# Providers. A provider is usable only when its key is in the environment.
# --------------------------------------------------------------------------- #
# FREE means "this key cannot generate a bill". It is not a guess and it is not
# a comment: `have()` enforces it, so a provider marked paid is invisible to the
# whole resolver unless it is named in NEO_ALLOW_PAID.
#
# This exists because Groq quietly did the opposite. The .env said the account
# "bills to a card rather than sitting on the free tier" and CLAUDE.md repeated
# it, but Groq stayed second in the `heavy` list — so every time Gemini 503'd,
# a presentation was drawn on a paid account. neo.log has 26 completed
# gpt-oss-120b calls, and the user believed the whole thing was free. Measured
# from the response headers on their key: 500,000 requests/day and 250,000 TPM,
# where Groq's free tier is about 1,000/day — a paid Developer tier.
#
# A comment cannot stop that happening again. A gate can.
PROVIDERS = {
    "gemini":     {"env": "GEMINI_API_KEY",     "free": True,
                   "base": None},
    "cerebras":   {"env": "CEREBRAS_API_KEY",   "free": True,
                   "base": "https://api.cerebras.ai/v1"},
    "openrouter": {"env": "OPENROUTER_API_KEY", "free": True,
                   "base": "https://openrouter.ai/api/v1"},
    "mistral":    {"env": "MISTRAL_API_KEY",    "free": True,
                   "base": "https://api.mistral.ai/v1"},
    # Model ids below were read from these providers' OWN /models endpoints,
    # which answer without a key — not recalled. Endpoints were probed too.
    # PAID. Marked free here on my say-so; the API disagreed on the first
    # call: HTTP 402, balance_units 0, "A payment method is required." That is
    # the Groq mistake a second time — a provider marked free because I
    # believed it had a free tier, not because anything checked.
    "sambanova":  {"env": "SAMBANOVA_API_KEY",  "free": False,
                   "base": "https://api.sambanova.ai/v1"},
    "huggingface": {"env": "HF_TOKEN",          "free": True,
                    "base": "https://router.huggingface.co/v1"},

    # ---- PAID, or free-credits-then-billing. Opt in with NEO_ALLOW_PAID. ----
    # `together` was marked FREE here an hour ago and that was the Groq mistake
    # repeating: a free credit grant is not a free tier. It bills the moment
    # the grant runs out, which is precisely when a busy day would reach it.
    # When in doubt about a provider, it goes here. Being wrong in this
    # direction costs the user a fallback; being wrong in the other costs money.
    "together":   {"env": "TOGETHER_API_KEY",   "free": False,
                   "base": "https://api.together.xyz/v1"},
    "nvidia":     {"env": "NVIDIA_API_KEY",     "free": False,
                   "base": "https://integrate.api.nvidia.com/v1"},
    "nebius":     {"env": "NEBIUS_API_KEY",     "free": False,
                   "base": "https://api.studio.nebius.ai/v1"},
    "fireworks":  {"env": "FIREWORKS_API_KEY",  "free": False,
                   "base": "https://api.fireworks.ai/inference/v1"},
    "deepinfra":  {"env": "DEEPINFRA_API_KEY",  "free": False,
                   "base": "https://api.deepinfra.com/v1/openai"},
    "groq":       {"env": "GROQ_API_KEY",       "free": False,
                   "base": "https://api.groq.com/openai/v1"},
}

# Deliberately opt-in, per provider: NEO_ALLOW_PAID=groq,together
ALLOW_PAID = {p.strip().lower()
              for p in (os.getenv("NEO_ALLOW_PAID") or "").split(",") if p.strip()}


def have(provider):
    """True when this provider has a usable key. Placeholder values from the
    .env template don't count — a key that says 'paste_your_key_here' would
    otherwise make every candidate behind it fail slowly instead of being
    skipped instantly."""
    spec = PROVIDERS.get(provider)
    if not spec:
        return False
    # The money gate, and it is here rather than in the candidate tables on
    # purpose: every path into the resolver goes through have(), so there is no
    # list anyone can edit that quietly reintroduces a billable provider.
    if not spec.get("free", False) and provider not in ALLOW_PAID:
        return False
    # Observed, not assumed: it asked for a card at runtime.
    if provider in _PAYWALLED and provider not in ALLOW_PAID:
        return False
    key = (os.getenv(spec["env"]) or "").strip()
    if not key or len(key) < 12:
        return False
    return "paste" not in key.lower() and "your_" not in key.lower()


# --------------------------------------------------------------------------- #
# Candidates, best first. Order is the whole policy — read it top to bottom as
# "try this, then this". Every list ends with something that has worked for
# months, so a bad day at the top never leaves Neo with nothing.
# --------------------------------------------------------------------------- #
# MEASURED, not guessed. Every ordering below comes from timing real calls
# against this key from the browser on 2026-08-25 (time to FIRST TOKEN, which is
# the only latency number a voice assistant cares about):
#
#   gemini-3.5-flash-lite   393-436 ms     gemini-2.5-flash        506 ms
#   gemini-3.1-flash-lite   471 ms         gemini-3.7-flash        874-5186 ms
#
# And three things that are true of THIS key specifically:
#   - gemini-2.5-pro is 404 "no longer available to new users"
#   - every Pro model returns 429: there is no free Pro quota here
#   - gemini-3.5-flash and 3.7-flash intermittently return 503 (high demand)
#   - Google Search grounding is free on 2.5-flash and 429s on the 3.x models
#
# So the newest model is NOT the right default. 2.5-flash leads the chat list
# because it is 3-10x faster to first token than 3.7, it doesn't 503, and it's
# the only one whose free search grounding works. Re-measure before reordering.
CANDIDATES = {
    # The conversation brain. Fast to first token beats benchmark scores here.
    # NEWEST FIRST. 2.5-flash sat at the top of this list for months and it
    # is the weakest model on the key AND the one with a 20-a-day quota; on
    # 11 Sept the brain was still answering on it while 3.8-flash sat unused
    # (probed: "ready" in 1.4s). Every typed/held answer, every tool result
    # polish and every vision call comes from this rung — it must be the
    # smartest thing available, with the old ones as backstops.
    "chat": [
        ("gemini", "gemini-3.8-flash"),
        ("gemini", "gemini-3.7-flash"),
        ("gemini", "gemini-3.6-flash"),
        ("gemini", "gemini-3.5-flash"),
        ("gemini", "gemini-flash-latest"),
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-3-flash-preview"),
        # Free backstops on other providers. Every Gemini entry above can be
        # exhausted on one busy day — 2.5-flash is 20 requests a day on this
        # key — and a chat model that resolves to nothing means Neo cannot
        # answer at all. A slower reply beats no reply.
        ("cerebras", "llama-3.3-70b"),
        ("sambanova", "Meta-Llama-3.3-70B-Instruct"),
        # OpenRouter is DELIBERATELY ABSENT from chat and fast. Its free tier
        # is 50 requests a day shared across every :free model and pooled per
        # ACCOUNT — not per model, the way Gemini's is. So every casual chat
        # fallback that lands here is a figure the deck cannot draw later.
        # That budget is reserved for drawing, which is the one job nothing
        # else free does well.
        ("huggingface", "Qwen/Qwen3.8-27B"),
        ("mistral", "mistral-small-latest"),
        ("groq", "openai/gpt-oss-120b"),      # paid: needs NEO_ALLOW_PAID
    ],
    # Hard thinking: multi-step plans, anything the ladder escalates.
    # No Pro model is reachable on this key, so this is a flash model told to
    # think hard, with Groq's 120b as the real step up when its key is present.
    # Groq sits SECOND on purpose, and it matters more than it used to. Gemini's
    # free tier is small, the visuals call is the largest single thing Neo asks
    # for, and 3.7-flash 503s under load — the log has both batches of one deck
    # losing to the same 503 inside a second. Groq's 120b is a separate free
    # allowance on a different provider, which is exactly what a fallback is
    # for. It is not the default because first-token latency is worse and
    # because deck.py now demotes a failing model itself, so the chain only
    # moves when something actually breaks.
    # gemini-2.5-flash IS NOT IN THIS LIST, on purpose, and it is the one
    # entry worth explaining. Measured from the 429s in neo.log, this key
    # allows 20 requests PER DAY on that model — a daily quota, not a rate:
    #   quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier, value 20.
    # A five-slide deck spends one plan call plus one call per figure, so three
    # presentations exhaust the entire day's allowance for the model that also
    # serves `chat` and every Google Search grounding call. The deck is the
    # single hungriest thing Neo does and it must not eat that pool. It also
    # timed out repeatedly on this workload (504 DEADLINE_EXCEEDED on the
    # visuals call, in the log), so it was buying nothing in exchange.
    # Verified against OpenRouter's live model list: every :free id below
    # reports a prompt AND completion price of exactly 0. Free tiers move, so
    # re-check with
    #   curl -s https://openrouter.ai/api/v1/models | ... pricing == 0
    # rather than trusting this comment.
    "heavy": [
        ("gemini", "gemini-3.8-flash"),
        ("gemini", "gemini-3.7-flash"),
        ("gemini", "gemini-3.6-flash"),
        # The visuals call is the largest single thing Neo asks for and Gemini
        # 503s under it constantly, so the free alternates matter here more
        # than anywhere else. Big context, zero cost.
        ("openrouter", "nvidia/nemotron-3-super-120b-a12b:free"),
        ("openrouter", "z-ai/glm-5.2:free"),
        ("openrouter", "minimax/minimax-m3:free"),
        ("sambanova", "gpt-oss-120b"),
        ("huggingface", "zai-org/GLM-5.3-Flash"),
        ("cerebras", "gpt-oss-120b"),
        ("gemini", "gemini-flash-latest"),
        ("groq", "openai/gpt-oss-120b"),      # paid: needs NEO_ALLOW_PAID
    ],
    # Mechanical rewrites: polishing a tool result, compressing a search hit.
    # Fastest thing that forms sentences wins; these are the 400 ms tier.
    "fast": [
        ("gemini", "gemini-3.5-flash-lite"),
        ("gemini", "gemini-3.1-flash-lite"),
        ("gemini", "gemini-flash-lite-latest"),
        ("cerebras", "llama-3.1-8b"),
        ("sambanova", "gemma-4-31B-it"),   # OpenRouter reserved for drawing
        ("gemini", "gemini-2.5-flash"),
        ("groq", "openai/gpt-oss-20b"),       # paid: needs NEO_ALLOW_PAID
    ],
    # Grounded knowledge: Google Search / URL reading / code execution. These
    # are Gemini BUILT-IN tools, and the API refuses to mix built-in tools with
    # function calling in one request — so this is a separate sub-call made from
    # agent.py rather than something the main session can just switch on.
    "grounded": [
        ("gemini", "gemini-2.5-flash"),
        ("gemini", "gemini-flash-latest"),
        ("gemini", "gemini-3.7-flash"),
    ],
    # Transcription. Gemini's own audio input leads because it is genuinely
    # free on the key Neo already has. whisper-large-v3-turbo is better ears,
    # but this Groq account has a card on file rather than free-tier limits, so
    # it is opt-in: set NEO_STT_PROVIDER=groq (about $0.04 per hour of audio,
    # which is pennies a month at voice-assistant volumes) and it moves to the
    # front. Local faster-whisper is the offline floor and lives in ears.py.
    "stt": [
        ("gemini", "gemini-3.5-flash-lite"),
        ("gemini", "gemini-3.1-flash-lite"),
        ("gemini", "gemini-2.5-flash"),
        ("groq", "whisper-large-v3-turbo"),
    ],
    # Native speech-to-speech over a websocket — the conversational mode.
    "live": [
        ("gemini", "gemini-3.1-flash-live-preview"),
        ("gemini", "gemini-live-2.5-flash-preview"),
        ("gemini", "gemini-2.0-flash-live-001"),
    ],
}


# --------------------------------------------------------------------------- #
# Pure helpers (no network, no disk) — the part worth unit-testing.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# MORE THAN ONE GEMINI KEY.
#
# Google's own 429 names the scope: GenerateRequestsPerDayPerProjectMod-FreeTier
# — read the real string in neo.log, it says PerProject. The free allowance is
# per PROJECT, not per Google account, so a second project under the SAME
# account is a second full allowance. No second identity, no terms to squint
# at, and it is the only way to keep the Gemini live VOICE alive once the first
# project is dry: nothing else free does speech-to-speech at all.
#
# Keys are read from GEMINI_API_KEY plus GEMINI_API_KEY_2, _3, ... in order.
# A key that reports a per-day 429 is stood down and the next one takes over.
# --------------------------------------------------------------------------- #
_KEY_REST_S = float(os.getenv("NEO_KEY_REST", str(3 * 3600)))
_key_down = {}          # key -> unix time it may be used again
_key_lock = threading.Lock()


def gemini_keys(env=None):
    """Every Gemini key configured, in preference order. Pure given `env`."""
    env = os.environ if env is None else env
    out, seen = [], set()
    for name in ["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(2, 9)]:
        for k in (env.get(name) or "").split(","):
            k = k.strip()
            if len(k) >= 12 and k not in seen and "paste" not in k.lower():
                seen.add(k)
                out.append(k)
    return out


def live_keys(keys=None, now=None):
    """The keys not currently stood down, best first."""
    keys = gemini_keys() if keys is None else keys
    now = time.time() if now is None else now
    with _key_lock:
        ok = [k for k in keys if _key_down.get(k, 0) <= now]
    return ok or list(keys)     # all dry: try anyway rather than go silent


def retire_key(key, now=None, rest=None):
    """This key is out of quota for the day. Stand it down."""
    if not key:
        return
    now = time.time() if now is None else now
    with _key_lock:
        _key_down[key] = now + (_KEY_REST_S if rest is None else rest)


def key_state(keys=None):
    """(live, standing down) — for the boot line and the log."""
    keys = gemini_keys() if keys is None else keys
    live = live_keys(keys)
    return len(live), len(keys) - len(live)


# --------------------------------------------------------------------------- #
# Daily quota. This key allows 20 requests PER DAY on gemini-2.5-flash and 10
# on the TTS model — measured from the 429s in neo.log, quotaId
# GenerateRequestsPerDayPerProjectPerModel-FreeTier.
#
# A per-day 429 is not a transient error and must not be treated like one. It
# means that model is finished until the quota rolls over, so every subsequent
# attempt is a guaranteed-failing round trip. Nothing tracked this, so a single
# presentation fired five figure calls at an already-exhausted model, got five
# instant 429s, and only THEN started trying something that could work — by
# which time most of the art budget was gone. That is why a deck came back with
# one figure out of five.
_EXHAUSTED = {}          # (provider, model) -> unix time it may be tried again
QUOTA_REST_S = float(os.getenv("NEO_QUOTA_REST", str(3 * 3600)))


def is_daily_quota(err):
    """True when this error means 'no more today', not 'busy right now'. Pure
    enough to test: it only reads the exception's text."""
    text = str(err or "")
    if "RESOURCE_EXHAUSTED" not in text and "429" not in text:
        return False
    return "PerDay" in text or "per day" in text.lower()


# A provider that asks for a card is not free, whatever this file claims. The
# free/paid flags are my judgement, and my judgement has now been wrong twice
# (Groq, then SambaNova). This turns the flag from a belief into an observation:
# the FIRST time a provider demands payment, it is disabled for the session and
# said out loud. Nothing bills quietly because I was confident.
_PAYWALLED = set()
_PAYMENT_SIGNS = ("payment_method_required", "payment method is required",
                  "insufficient_quota", "insufficient credit",
                  "billing", "add a payment", "402")


def is_payment_required(err):
    """True when the provider is asking for money rather than being busy."""
    t = str(err or "").lower()
    if "402" in t and "payment" not in t and "billing" not in t:
        return False            # a bare 402 in some other context
    return any(sig in t for sig in _PAYMENT_SIGNS)


def mark_paywalled(provider, log=print):
    """This provider wants a card. Stop using it, now, and say so."""
    if provider in _PAYWALLED:
        return
    _PAYWALLED.add(provider)
    try:
        log(f"[providers] {provider} asked for a payment method — disabling it. "
            f"Neo stays free; set NEO_ALLOW_PAID={provider} only if you mean "
            f"to spend money there.")
    except Exception:
        pass


def mark_exhausted(provider, model, now=None, rest=None):
    """Remember that this model is out of quota, so nothing tries it again."""
    now = time.time() if now is None else now
    _EXHAUSTED[(provider, model)] = now + (QUOTA_REST_S if rest is None else rest)


def is_exhausted(provider, model, now=None):
    until = _EXHAUSTED.get((provider, model))
    if until is None:
        return False
    if (time.time() if now is None else now) >= until:
        _EXHAUSTED.pop((provider, model), None)
        return False
    return True


def usable_candidates(job, keys_present, candidates=None, preferred=None):
    """The candidates for a job whose provider has a key, in preference order.

    keys_present is the set of provider names that are configured. `preferred`
    promotes one provider to the front — that's how NEO_STT_PROVIDER=groq moves
    paid-but-better ears ahead of the free default without editing this table.
    """
    table = candidates if candidates is not None else CANDIDATES
    order = [(p, m) for (p, m) in table.get(job, []) if p in keys_present]
    if preferred:
        order = ([c for c in order if c[0] == preferred]
                 + [c for c in order if c[0] != preferred])
    # A model that is out of quota for the day is not a candidate — trying it
    # costs a round trip and returns 429 every time. Demoted rather than
    # removed, so if EVERY candidate is exhausted the caller still gets a list
    # to fail against instead of "no model available".
    live = [c for c in order if not is_exhausted(*c)]
    return live + [c for c in order if is_exhausted(*c)] if live else order


def cache_is_fresh(entry, now, ttl=CACHE_TTL_S):
    """A cached pick is good if it names a model and hasn't aged out."""
    if not isinstance(entry, dict):
        return False
    if not entry.get("model"):
        return False
    try:
        return (now - float(entry.get("at", 0))) < ttl
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Thinking config is NOT one shape. This bit is a real bug, found by calling the
# API rather than reading about it:
#
#   gemini-2.5-flash        thinkingBudget: 0   OK      thinkingLevel  -> 400
#   gemini-3.5-flash-lite   thinkingBudget      -> 400  thinkingLevel: LOW  OK
#   gemini-3.7-flash        thinkingBudget: 0   OK      thinkingLevel: LOW  OK
#   gemini-3.6-flash        thinkingBudget      -> 400  thinkingLevel  OK
#
# Neo sent thinking_budget=0 to everything. On the flash-lite models — the ones
# that do transcription and result-polishing — that is a 400 on EVERY call, so
# cloud transcription would have failed 100% of the time and silently dropped to
# the small local model. Which is the exact bug we set out to fix.
#
# So: styles are per model, probed once, cached with the rest.
STYLE_BUDGET = "budget"    # thinking_budget=N  (2.x family)
STYLE_LEVEL = "level"      # thinking_level="LOW"/"HIGH"  (3.x family)
STYLE_NONE = "none"        # model rejects both; send neither

# Budgets are Gemini's units: 0 off, -1 dynamic, positive = ceiling.
def level_for_budget(budget):
    """Translate a budget into the nearest thinking LEVEL, for models that only
    speak levels. 0 can't be expressed as a level, so the floor is LOW."""
    if budget is None:
        return None
    if budget == 0:
        return "LOW"
    if budget < 0:
        return "HIGH"
    return "LOW" if budget <= 2048 else "HIGH"


def thinking_kwargs(style, budget):
    """The kwargs to hand types.ThinkingConfig for this model, or None when we
    should send no thinking config at all. Pure — tested.

    An UNKNOWN style returns None rather than defaulting to a budget. That
    default is exactly the bug this whole mechanism exists to fix: guessing
    'budget' at a level-only model 400s every single call. Sending nothing costs
    a little latency on one turn; sending the wrong dialect costs the turn."""
    if budget is None or style in (None, STYLE_NONE):
        return None
    if style == STYLE_LEVEL:
        lvl = level_for_budget(budget)
        return {"thinking_level": lvl} if lvl else None
    if style == STYLE_BUDGET:
        return {"thinking_budget": budget}
    return None


def style_of(model, cache=None):
    """The cached thinking style for a model, or None if we've never probed."""
    data = cache if cache is not None else _read_cache()
    return (data.get("_styles") or {}).get(model)


def remember_style(model, style):
    with _lock:
        data = _read_cache()
        data.setdefault("_styles", {})[model] = style
        _write_cache(data)


def probe_style(client, model, log=print):
    """Find out which thinking config this model accepts, cheaply, once.

    One token each way. Ordered so the common case costs a single call: try a
    budget, and only if that's rejected try a level.
    """
    known = style_of(model)
    if known:
        return known
    from google.genai import types

    def works(kwargs):
        try:
            client.models.generate_content(
                model=model, contents="hi",
                config=types.GenerateContentConfig(
                    max_output_tokens=1,
                    thinking_config=types.ThinkingConfig(**kwargs)))
            return True
        except Exception as e:
            # A 429/503 says nothing about the config's shape — don't cache a
            # style conclusion from a capacity error.
            if any(c in str(e) for c in ("429", "503", "RESOURCE_EXHAUSTED",
                                         "UNAVAILABLE")):
                raise
            return False

    try:
        if works({"thinking_budget": 0}):
            style = STYLE_BUDGET
        elif works({"thinking_level": "LOW"}):
            style = STYLE_LEVEL
        else:
            style = STYLE_NONE
    except Exception:
        return None       # busy right now; ask again later rather than guessing
    remember_style(model, style)
    if os.getenv("NEO_DEBUG") == "1":
        log(f"[models] {model} thinking style: {style}")
    return style


def demote(order, bad, keys_present=None):
    """Move a failing (provider, model) to the back of the order rather than
    dropping it. A model that 429s at noon usually works again by evening, so
    forgetting it entirely would permanently lose the best option to one bad
    minute."""
    rest = [c for c in order if c != bad]
    return rest + ([bad] if bad in order else [])


# --------------------------------------------------------------------------- #
# Cache on disk. Best-effort throughout: a corrupt or unwritable models.json
# must never stop Neo from booting, it just means we probe again.
# --------------------------------------------------------------------------- #
_lock = threading.Lock()


def _read_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cache(data):
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


def forget(job=None):
    """Drop cached picks so the next resolve re-probes. Called by the 'refresh
    your models' voice command and after a provider key is added."""
    with _lock:
        data = _read_cache()
        if job is None:
            data = {}
        else:
            data.pop(job, None)
        _write_cache(data)


def keys_present():
    return {name for name in PROVIDERS if have(name)}


# --------------------------------------------------------------------------- #
# Resolution. This is the only part that touches the network.
# --------------------------------------------------------------------------- #
def _probe_gemini(client, model):
    """One-token generate. Cheap enough to run at boot, honest about whether
    the id exists AND whether this key is allowed to call it — a model can be
    listed publicly and still be unallocated on a free key."""
    from google.genai import types
    client.models.generate_content(
        model=model, contents="hi",
        config=types.GenerateContentConfig(max_output_tokens=1))
    return True


def _probe_openai_compatible(provider, model):
    import requests
    spec = PROVIDERS[provider]
    r = requests.post(
        spec["base"] + "/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv(spec['env'])}"},
        json={"model": model, "max_tokens": 1,
              "messages": [{"role": "user", "content": "hi"}]},
        timeout=15)
    r.raise_for_status()
    return True


class _Answer:
    """What generate() hands back, whichever provider produced it.

    Shaped like the Gemini SDK's response — `.text` and a `.candidates[0]
    .finish_reason` — so a caller doesn't have to branch on the provider. That
    is the whole point: `resolve` has always been able to return a Groq model
    for a text job, but the only thing that could actually CALL Groq was
    ears.py's transcription. Everything else handed the model id straight to
    `client.models.generate_content`, i.e. asked Gemini for a model named
    `openai/gpt-oss-120b`, which is a 404. The fallback was decorative.
    """

    def __init__(self, text, finish_reason="STOP"):
        self.text = text
        self.candidates = [type("C", (), {"finish_reason": finish_reason})()]


def generate(client, provider, model, prompt, max_output_tokens=None, log=print):
    """One text completion from whichever provider owns `model`. Raises on
    failure, like the SDK does, so existing error handling still works."""
    if provider != "gemini":
        import requests
        spec = PROVIDERS.get(provider)
        if not spec or not spec.get("base"):
            raise RuntimeError(f"no endpoint for provider {provider}")
        body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if max_output_tokens:
            body["max_tokens"] = int(max_output_tokens)
        r = requests.post(
            spec["base"] + "/chat/completions",
            headers={"Authorization": f"Bearer {os.getenv(spec['env'])}"},
            json=body, timeout=90)
        # Surface the STATUS in the message. deck.py decides whether a model is
        # down by reading "503"/"429" out of the error text, and requests'
        # default HTTPError message contains the code — but only if we let it
        # raise rather than replacing it with something friendlier.
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        return _Answer((choice.get("message") or {}).get("content") or "",
                       str(choice.get("finish_reason") or "STOP").upper())

    kw = {}
    if max_output_tokens:
        try:
            from google.genai import types as _t
            kw["config"] = _t.GenerateContentConfig(
                max_output_tokens=int(max_output_tokens))
        except Exception:
            pass
    return client.models.generate_content(model=model, contents=prompt, **kw)


def _probe(client, provider, model):
    if provider == "gemini":
        return _probe_gemini(client, model)
    return _probe_openai_compatible(provider, model)


def resolve(job, client=None, log=print, probe=True):
    """Return (provider, model) for a job, or (None, None) if nothing answers.

    Uses the cached pick when it's fresh. Otherwise walks the candidate list
    and returns the first that answers a one-token probe. Speech jobs ('stt',
    'live') are never probed — a probe would cost an audio upload or a
    websocket handshake, so they resolve to the first candidate with a key and
    prove themselves on first real use instead.
    """
    order = usable_candidates(job, keys_present(),
                              preferred=os.getenv(f"NEO_{job.upper()}_PROVIDER"))
    if not order:
        return (None, None)

    with _lock:
        cached = _read_cache().get(job)
    if cache_is_fresh(cached, time.time()):
        pair = (cached.get("provider"), cached.get("model"))
        if pair in order:
            return pair

    if not probe or job in ("stt", "live") or client is None:
        provider, model = order[0]
        remember(job, provider, model)
        return (provider, model)

    for provider, model in order:
        try:
            _probe(client, provider, model)
            if (provider, model) != order[0]:
                log(f"[models] {job}: {order[0][1]} unavailable, using {model}")
            remember(job, provider, model)
            return (provider, model)
        except Exception as e:
            if os.getenv("NEO_DEBUG") == "1":
                log(f"[models] {job}: {model} failed probe ({e})")
            continue

    log(f"[models] no model answered for '{job}' — that job is disabled.")
    return (None, None)


def remember(job, provider, model):
    with _lock:
        data = _read_cache()
        data[job] = {"provider": provider, "model": model, "at": time.time()}
        _write_cache(data)


def report_failure(job, provider, model, log=print):
    """Called when a resolved model errors during real use. Clears the cache so
    the next resolve moves on instead of hammering something that's gone."""
    with _lock:
        data = _read_cache()
        cur = data.get(job) or {}
        if cur.get("model") == model:
            data.pop(job, None)
            _write_cache(data)
            log(f"[models] {job}: {model} failed in use — will re-probe.")


# Jobs that only resolve when something actually needs them. Saying
# "unresolved" for these at boot reads as broken when it just means "not asked
# for yet" — a boot line that looks like a failure is a bug in the boot line.
LAZY_JOBS = ("heavy", "fast")


def describe(jobs=("chat", "stt", "live", "fast", "heavy"), cache=None):
    """One line for the boot log and the 'what models are you running' command.
    Pure given a cache dict, so it's tested."""
    data = _read_cache() if cache is None else cache
    out = []
    for job in jobs:
        model = (data.get(job) or {}).get("model")
        if model:
            out.append(f"{job}={model}")
        else:
            out.append(f"{job}=on demand" if job in LAZY_JOBS
                       else f"{job}=UNAVAILABLE")
    return "  ".join(out)


def warm(client=None, log=print):
    """Resolve the jobs a press will need, at boot, so the log tells the truth.
    stt and live cost nothing to resolve (they're never probed — see resolve),
    so there's no reason to leave them looking broken until first use."""
    for job in ("stt", "live"):
        try:
            resolve(job, client, log)
        except Exception:
            pass


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    print("providers with keys:", ", ".join(sorted(keys_present())) or "none")
    try:
        from google import genai
        c = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    except Exception:
        c = None
    for job in ("chat", "heavy", "fast", "stt", "live"):
        print(f"{job:6} ->", resolve(job, c))


# --------------------------------------------------------------------------- #
# ONE CALL THAT SURVIVES THE QUOTA WALL.
#
# Every model on the free tier has a small DAILY allowance per key (20 a day
# on the newest flash). A tool that calls the resolved model directly and
# gives up on a 429 turns every reminder, calendar add, vision read and
# think_hard into "no model" for the rest of the day — exactly what the live
# test run showed on 11 Sept. This walks: the resolved model → (on a daily
# 429) demote it and re-resolve on the same key → the next key with quota.
# --------------------------------------------------------------------------- #
def generate_text(client, job, contents, config=None, log=print, tries=3):
    """(text, model) or ("", None). Same client type as everywhere else."""
    from google import genai as _genai
    last = None
    keys = live_keys()
    key_i = 0
    fallback = os.getenv("NEO_CHAT_MODEL") or next(
        (m for p, m in CANDIDATES.get(job, CANDIDATES["chat"]) if p == "gemini"), "gemini-3.8-flash")
    for attempt in range(tries):
        _p, model = resolve(job, client, log)
        if not model:
            # the resolver has nothing (no cache and the probe failed): call
            # the first candidate anyway rather than answer nothing
            model = fallback
        try:
            r = client.models.generate_content(model=model, contents=contents, config=config)
            text = (getattr(r, "text", "") or "").strip()
            if text:
                return text, model
            last = "empty"
        except Exception as e:
            last = e
            msg = str(e)
            if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                report_failure(job, "gemini", model, log)
                if is_daily_quota(e) and key_i + 1 < len(keys):
                    # this model is dry on this key; the same model on the
                    # next key has its own allowance. The key itself is NOT
                    # stood down — its other models are fine.
                    key_i += 1
                    client = _genai.Client(api_key=keys[key_i])
                continue
            if "503" in msg or "UNAVAILABLE" in msg or "504" in msg:
                import time as _t
                _t.sleep(1.0)
                continue
            break
    log(f"[models] {job}: no answer ({type(last).__name__ if not isinstance(last, str) else last})")
    return "", None
