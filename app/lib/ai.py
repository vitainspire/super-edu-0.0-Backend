"""Mirrors backend/src/lib/ai.ts — single entry point for all AI calls, with a
circuit breaker and a primary/fallback-model retry."""
import asyncio
import contextvars
import os
import re
import time
from typing import Optional
import httpx

# ─── Per-call tracing ──────────────────────────────────────────────────────────
# Every model call in this repo goes through call_ai(), and call_ai() has no idea
# which agent it is standing inside. These two hooks are what let a terminal trace
# say "this 18-second call belonged to generation, and cost 12k characters of
# prompt" instead of "something called the model":
#
#   * CALL_LABEL — a contextvar the caller sets around its await (prep_flow/llm.py
#     does this for every call_json), carrying the same label that already names
#     the call in that module's log lines and exceptions.
#   * prep_flow.trace.note_llm_call — the ledger the active run's trace collects
#     into. Imported lazily and best-effort: ai.py is imported BY prep_flow (via
#     prep_flow/deps.py), so a module-level import here would be circular, and a
#     caller with no prep_flow at all must still work.
#
# TRACE_CALLS prints one line per call as it goes out and one as it comes back.
# On by default because a run this expensive with no per-call visibility is the
# thing that made a stalled stage indistinguishable from a slow one; set
# AI_TRACE_CALLS=0 to silence it without losing the ledger.
CALL_LABEL: contextvars.ContextVar = contextvars.ContextVar("ai_call_label", default="")
TRACE_CALLS = os.environ.get("AI_TRACE_CALLS", "1") not in ("0", "false", "no")

_trace_module = "unresolved"


def _tracer():
    """prep_flow.trace, if this process has it. Resolved once, lazily."""
    global _trace_module
    if _trace_module == "unresolved":
        try:
            from prep_flow import trace as module
            _trace_module = module
        except Exception:
            _trace_module = None
    return _trace_module


def _note(**call) -> None:
    tracer = _tracer()
    if tracer is not None:
        try:
            tracer.note_llm_call(**call)
        except Exception:
            pass        # a broken trace must never fail a real model call


def _agent_for(label: str) -> str:
    """Which pipeline agent this call belongs to — from its label, or from the
    stack when it has none."""
    tracer = _tracer()
    if tracer is None:
        return "?"
    try:
        agent = tracer.owner_of_label(label) if label and label != "(unlabelled)" else "?"
        return tracer.owner_from_stack() if agent == "?" else agent
    except Exception:
        return "?"

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
# Was "meta-llama/llama-3.1-8b-instruct:free" -- that ":free" slug is retired
# (verified live against OpenRouter's own /api/v1/models listing, which no
# longer carries it), so every fallback attempt was silently 404ing and a
# primary-model failure never actually had a working fallback to land on.
# Moved to the cheaper Gemini tier rather than a Llama slug: still a distinct
# model/quota from MODEL above, so a fallback still means something when the
# primary call itself errors, not just when it's rate-limited.
FALLBACK_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "google/gemini-2.5-flash-lite")
# Deliberately still Gemini even where the text model has moved on: there is
# no image-generation model on every text vendor, so this stays pinned
# independently of MODEL/FALLBACK_MODEL above.
IMAGE_MODEL = os.environ.get("OPENROUTER_IMAGE_MODEL", "google/gemini-2.5-flash-image")

# Printed once, at import time (so once per process start, not per call) —
# this is what actually answers "did my .env change take effect", instead of
# guessing. python-dotenv's load_dotenv() does NOT override a variable already
# set in the shell session by default, so a stale OPENROUTER_MODEL exported
# earlier in the same terminal would silently win over any number of .env
# edits — this line is what makes that visible instead of invisible.
print(f"[ai] primary model: {MODEL} | fallback: {FALLBACK_MODEL} "
      f"(from OPENROUTER_MODEL={os.environ.get('OPENROUTER_MODEL') or '(unset — using default)'})")

# Applied when a caller does not set its own — see _call_model for why leaving
# it unset is expensive rather than neutral. Sits clear of the largest explicit
# cap anywhere here (8192), so nothing an uncapped caller asks for truncates.
#
# Beware when the balance runs low: OpenRouter reserves credit for the whole
# max_tokens up front and refuses the request if the balance cannot cover it — a
# 402 before a single token is generated, not a short reply. That ceiling moves
# with the balance, so on a nearly empty account this needs lowering (via
# OPENROUTER_MAX_TOKENS) rather than debugging; it measured ~15999 at zero.
DEFAULT_MAX_TOKENS = int(os.environ.get("OPENROUTER_MAX_TOKENS", "16000"))

# Tuned against the models actually configured here, which answer well inside it.
# Env-overridable because it is the first thing to raise when routing to slower
# models — free tiers in particular queue behind paid traffic, where 45s is
# marginal and a timeout costs the primary call plus the fallback attempt.
DEFAULT_TIMEOUT_S = int(os.environ.get("OPENROUTER_TIMEOUT_S", "45"))

# ─── Circuit Breaker ───────────────────────────────────────────────────────────
FAILURE_THRESHOLD = 3
RESET_SECONDS = 60

_failures = 0
_open_since: float = 0  # epoch seconds when circuit opened (0 = closed)


def _circuit_open() -> bool:
    global _failures, _open_since
    if _open_since == 0:
        return False
    if time.time() - _open_since > RESET_SECONDS:
        _failures = 0
        _open_since = 0
        return False
    return True


def _on_success():
    global _failures, _open_since
    _failures = 0
    _open_since = 0


def _on_failure():
    global _failures, _open_since
    _failures += 1
    if _failures >= FAILURE_THRESHOLD:
        _open_since = time.time()


def _strip_fences(s: str) -> str:
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", s)
    return m.group(1) if m else s


async def _call_model(model: str, messages: list[dict], options: dict, use_json_mode: bool,
                      meta: Optional[dict] = None) -> str:
    """`meta`, if given, is filled with what the trace wants to report about
    this one HTTP call — token usage, finish reason, status — none of which
    survives in the returned string."""
    # Omitting max_tokens does not mean "no limit" — it means the model's own
    # ceiling, and OpenRouter authorises a request against the maximum it could
    # return, not what it does return. An uncapped call can reserve several
    # times the credit any call in this codebase actually needs, and gets
    # refused at a balance that would comfortably have paid for the real
    # completion. DEFAULT_MAX_TOKENS is what keeps a low balance from 402-ing
    # before the first token; callers that pass their own value keep it.
    max_tokens = options.get("max_tokens") or DEFAULT_MAX_TOKENS
    temperature = options.get("temperature", 0.7)
    timeout_s = options.get("timeout_s", DEFAULT_TIMEOUT_S)

    body: dict = {"model": model, "messages": messages, "temperature": temperature}
    if use_json_mode:
        body["response_format"] = {"type": "json_object"}
    body["max_tokens"] = max_tokens
    # OpenRouter's reasoning map — {"enabled": True} at minimum, optionally
    # "effort"/"max_tokens"/"exclude" too. Passed through as-is rather than
    # validated here: only the caller that set it knows what that model
    # actually accepts.
    if options.get("reasoning"):
        body["reasoning"] = options["reasoning"]

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://eduteach.app",
                    "X-Title": "EduTeach",
                },
                json=body,
            )
        except httpx.TimeoutException as e:
            raise Exception(f"AI request timed out after {timeout_s}s") from e

    if meta is not None:
        meta["status"] = resp.status_code

    if resp.status_code >= 400:
        raise Exception(f"AI error {resp.status_code}: {resp.text}")

    data = resp.json()
    if meta is not None:
        meta["usage"] = data.get("usage") or {}
        meta["finish_reason"] = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
    choices = data.get("choices") or []
    if not choices:
        # Some free models answer 200 with no choices at all rather than an error.
        raise Exception(f"AI returned no choices: {str(data)[:200]}")

    choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content")

    # A reasoning model can spend its whole completion budget thinking and return
    # content: null with the prose in "reasoning" instead. Returned unchecked this
    # became a None handed back to callers that require a string — and because it
    # was not an exception, the fallback model was never tried. Raising makes it a
    # failure the retry path can actually handle.
    if not isinstance(content, str) or not content.strip():
        reasoning = message.get("reasoning")
        detail = f"finish_reason={choice.get('finish_reason')!r}"
        if reasoning:
            usage = (data.get("usage") or {}).get("completion_tokens_details") or {}
            detail += (f", reasoning-only response "
                       f"({usage.get('reasoning_tokens')} reasoning tokens) — "
                       f"raise max_tokens for this model")
        raise Exception(f"AI returned empty content from {model} ({detail})")

    # Non-empty but TRUNCATED is a different failure from non-empty but badly
    # written, and they need opposite responses: truncation needs a bigger
    # max_tokens (or a smaller window), while malformed JSON needs a re-ask.
    # Both surface downstream as the same "unterminated string" parse error,
    # so without this line the caller's retry looks equally reasonable in a
    # case where it cannot possibly help — re-asking a prompt that overflowed
    # just overflows again, three times, then fails the window.
    if choice.get("finish_reason") == "length":
        usage = data.get("usage") or {}
        print(f"[ai] WARNING: {model} hit the token ceiling mid-response "
              f"(max_tokens={max_tokens}, completion_tokens={usage.get('completion_tokens')}, "
              f"{len(content)} chars returned). The answer is CUT OFF, so any JSON in it is "
              f"incomplete — raise max_tokens for this call, or reduce how much it is "
              f"asked to write at once.")

    return content


def _report(label: str, agent: str, model: str, prompt_chars: int, content: str,
            started: float, meta: dict, *, fallback: bool) -> None:
    """One returning call: printed for whoever is watching, and recorded in the
    active run's ledger. Token counts come from OpenRouter's own `usage` block
    rather than being estimated from the characters."""
    seconds = time.time() - started
    usage = meta.get("usage") or {}
    if TRACE_CALLS:
        tokens = (f"tokens {usage.get('prompt_tokens', '?')} in / "
                  f"{usage.get('completion_tokens', '?')} out")
        print(f"[llm <<] {agent:<19} {label:<24} {model:<32} {len(content or ''):>7,}ch out "
              f"in {seconds:5.1f}s | {tokens} | finish={meta.get('finish_reason')}"
              + ("  <-- FALLBACK MODEL" if fallback else ""))
    _note(label=label, model=model, prompt_chars=prompt_chars,
          response_chars=len(content or ""), seconds=seconds, fallback=fallback, usage=usage)


async def call_ai(messages: list[dict], options: Optional[dict] = None) -> str:
    """Circuit breaker: opens after 3 consecutive failures, auto-resets in 60s.
    Retry/backoff: primary fails -> wait 1s -> try fallback.

    `options["model"]`, if set, replaces the global MODEL default for this
    call only — the circuit breaker and the fallback model are shared across
    every caller regardless (a primary that a caller deliberately routed
    elsewhere should still fail safely onto the same small, fast fallback
    everything else uses).
    """
    options = options or {}
    json_mode = options.get("json_mode", True)
    model = options.get("model") or MODEL

    # Named by whichever caller set it (prep_flow/llm.py::call_json does, for
    # every call it makes) — this is the only thing that tells the trace which
    # agent a given call belongs to, since call_ai is shared by all of them.
    label = CALL_LABEL.get() or "(unlabelled)"
    prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
    agent = _agent_for(label)
    if TRACE_CALLS:
        print(f"[llm >>] {agent:<19} {label:<24} {model:<32} {prompt_chars:>7,}ch in "
              f"({len(messages)} msg) max_tokens={options.get('max_tokens') or DEFAULT_MAX_TOKENS} "
              f"temp={options.get('temperature', 0.7)} json={'on' if json_mode else 'off'}")

    if _circuit_open():
        _note(label=label, model=model, prompt_chars=prompt_chars, response_chars=0,
              seconds=0.0, error="circuit breaker open")
        raise Exception("[ai] Circuit breaker is open — AI service temporarily unavailable")

    started = time.time()
    meta: dict = {}
    try:
        result = await _call_model(model, messages, options, json_mode, meta)
        _on_success()
        _report(label, agent, model, prompt_chars, result, started, meta, fallback=False)
        return result
    except Exception as primary_err:
        print(f"[ai] {model} failed after {time.time() - started:.1f}s, trying fallback "
              f"after 1s: {primary_err}")
        _note(label=label, model=model, prompt_chars=prompt_chars, response_chars=0,
              seconds=time.time() - started, error=str(primary_err)[:120])
        await asyncio.sleep(1)

        fallback_started = time.time()
        fallback_meta: dict = {}
        try:
            raw = await _call_model(FALLBACK_MODEL, messages, options, False, fallback_meta)
            _on_success()
            _report(label, agent, FALLBACK_MODEL, prompt_chars, raw, fallback_started,
                    fallback_meta, fallback=True)
            return _strip_fences(raw)
        except Exception as fallback_err:
            _on_failure()
            _note(label=label, model=FALLBACK_MODEL, prompt_chars=prompt_chars,
                  response_chars=0, seconds=time.time() - fallback_started,
                  fallback=True, error=str(fallback_err)[:120])
            await asyncio.sleep(2)
            raise Exception(f"[ai] Both models failed. Primary: {primary_err}. Fallback: {fallback_err}") from fallback_err


async def generate_illustration(prompt: str, timeout_s: float = 30) -> Optional[dict]:
    """Mirrors ai.ts's generateIllustration — a single best-effort image-gen
    call, independent of the text circuit breaker. Never raises; a failed or
    timed-out image just resolves to None so one bad image can't fail a caller."""
    if not API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://eduteach.app",
                    "X-Title": "EduTeach",
                },
                json={
                    "model": IMAGE_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "modalities": ["image", "text"],
                },
            )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        url = data.get("choices", [{}])[0].get("message", {}).get("images", [{}])[0].get("image_url", {}).get("url")
        return {"url": url} if isinstance(url, str) and url else None
    except Exception as e:
        print(f"[ai] illustration generation failed: {e}")
        return None


LESSON_IMAGE_BUCKET = "lesson-images"


def illustration_key(parts: list) -> str:
    """Mirrors ai.ts's illustrationKey — a stable storage path slug from
    whatever identifying parts a caller has (class/topic/subtopic/section)."""
    import re as _re
    cleaned = []
    for p in parts:
        if not p:
            continue
        slug = _re.sub(r"^-|-$", "", _re.sub(r"[^a-z0-9]+", "-", str(p).lower()))[:48]
        cleaned.append(slug)
    return "/".join(cleaned)


def _ensure_lesson_image_bucket(admin) -> None:
    try:
        admin.storage.create_bucket(LESSON_IMAGE_BUCKET, options={"public": True})
    except Exception:
        pass  # already exists


async def store_illustration(admin, data_url: str, key: str) -> Optional[str]:
    """Mirrors ai.ts's storeIllustration — moves a generated image out of the
    inline data: URL response into Storage, returning a public URL. Never
    raises; a failed upload just resolves to None so a missing diagram can't
    fail the whole caller."""
    import re as _re
    try:
        match = _re.match(r"^data:(image/[a-z+]+);base64,(.+)$", data_url, _re.I)
        if not match:
            return data_url if data_url.startswith("http") else None
        content_type, b64 = match.group(1), match.group(2)
        import base64 as _base64
        image_bytes = _base64.b64decode(b64)
        ext = "png" if content_type == "image/png" else "webp" if content_type == "image/webp" else "jpg"
        path = f"{key}.{ext}"

        _ensure_lesson_image_bucket(admin)
        admin.storage.from_(LESSON_IMAGE_BUCKET).upload(
            path, image_bytes, {"content-type": content_type, "upsert": "true"},
        )
        return admin.storage.from_(LESSON_IMAGE_BUCKET).get_public_url(path)
    except Exception as e:
        print(f"[ai] illustration upload failed: {e}")
        return None
