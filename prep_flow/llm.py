"""Every LLM call this package makes goes through here.

`ai.call_ai` already gives us a circuit breaker and a primary/fallback retry,
so this adds only what a 40-topic batch needs on top of it:

  * a bounded concurrency gate, so context assembly fanning out over forty
    topics doesn't open forty sockets and trip the breaker on rate limits
    rather than on real failures;
  * a JSON retry that shows the model its own broken output. The fallback model
    runs with json_mode OFF (see ai.py), so a parse failure is a normal event
    on a long batch, not an exception — and re-asking with the error attached
    fixes it far more often than re-rolling the same prompt does;
  * an optional required-keys check, because "valid JSON that is missing
    `materials`" fails in a much more confusing place if it is allowed through.
"""
import asyncio
import json
from typing import Optional, Sequence

from . import routing
from .deps import ai_module, call_ai, env_int, strip_fences

# ai.py's own contextvar, under a private name here: this module is the one
# place that knows a call's `label`, ai.py is the one place that makes the HTTP
# request, so the label has to travel between them out of band.
#
# Taken off `deps.ai_module` rather than a plain `import ai` — deliberately.
# deps.py resolves ai.py as `app.lib.ai` under the flat-checkout shim, so
# `import ai` here would hand back a SECOND, independent copy of the module: the
# label would be set on one copy's contextvar and read from the other's, and
# every call would be reported as unlabelled. Best-effort either way — a
# checkout whose ai.py predates the trace hooks still runs, it just cannot name
# the agent behind each call.
import contextvars                                  # noqa: E402
_CALL_LABEL = getattr(ai_module, "CALL_LABEL", None) or contextvars.ContextVar(
    "ai_call_label", default="")

# The same var, public, for the one case call_json cannot cover: an agent that
# reaches the model through a BORROWED function rather than through this module
# (context assembly calling the pilot's extract_knowledge_from_text, which owns
# its own prompt and calls call_ai itself). Such a call is still that agent's
# call and still costs its budget, so it sets this around the await and the
# trace names it like any other instead of reporting "(unlabelled)".
CALL_LABEL = _CALL_LABEL

_MAX_CONCURRENCY = env_int("PREP_FLOW_LLM_CONCURRENCY", 4)
_gate: Optional[asyncio.Semaphore] = None


def _semaphore() -> asyncio.Semaphore:
    # Created lazily and cached: a module-level Semaphore binds to whichever
    # event loop imported it, which is the wrong one as soon as FastAPI and the
    # CLI both use this module in the same process.
    global _gate
    loop = asyncio.get_running_loop()
    if _gate is None or getattr(_gate, "_prep_flow_loop", None) is not loop:
        _gate = asyncio.Semaphore(_MAX_CONCURRENCY)
        _gate._prep_flow_loop = loop  # type: ignore[attr-defined]
    return _gate


class LLMShapeError(RuntimeError):
    """Valid JSON, wrong shape — kept distinct from a parse failure because the
    two need different retries (re-ask with the keys named, vs. re-ask with the
    JSON error)."""


def error_window(text: str, error: Exception, before: int = 400, after: int = 200) -> str:
    """The text around a JSON parse failure, for a human and for the model.

    A bare "Expecting ',' delimiter: line 37 column 7" is not a diagnosis — it
    says where parsing stopped, not what is wrong there. The pilot's
    `_parse_json_response` learned this and prints a window; this is the same
    idea, reused for both the retry message and the final exception.
    """
    position = getattr(error, "pos", None)
    if position is None or not text:
        return ""
    start, end = max(0, position - before), min(len(text), position + after)
    return (f"{'…' if start else ''}{text[start:end]}{'…' if end < len(text) else ''}")


def _decode(raw: str, required: Sequence[str]) -> dict:
    cleaned = strip_fences(raw or "")
    data = json.loads(cleaned)           # JSONDecodeError handled by the caller

    # A bare array where an object was asked for is a real and frequent
    # deviation: asked for {"materials": [...]}, the model answers [...] — the
    # content is entirely correct and only the envelope is missing. When exactly
    # one key was required there is no ambiguity about which key it belongs
    # under, so adopt it rather than spending a retry on punctuation.
    if isinstance(data, list) and len(required) == 1:
        data = {required[0]: data}

    if not isinstance(data, dict):
        raise LLMShapeError(
            f"expected a JSON object with key(s) {', '.join(required) or '(any)'}, "
            f"got {type(data).__name__}")

    missing = [k for k in required if k not in data]
    if missing:
        # Same idea one level in: the right list under the wrong name is worth
        # rescuing when there is only one plausible candidate.
        if len(missing) == 1 and len(data) == 1:
            only_value = next(iter(data.values()))
            if isinstance(only_value, list):
                return {missing[0]: only_value}
        raise LLMShapeError(f"missing required key(s): {', '.join(missing)}")
    return data


async def call_json(
    prompt: str,
    *,
    label: str,
    required: Sequence[str] = (),
    temperature: float = 0.5,
    max_tokens: int = 6000,
    attempts: int = 3,
    system: Optional[str] = None,
    model: Optional[str] = None,
    timeout_s: Optional[float] = None,
    reasoning: Optional[dict] = None,
) -> dict:
    """One JSON-returning LLM call, retried with its own error fed back.

    `label` names the call in log lines and in the exception — on a 40-topic run
    with six agent types, "model response wasn't valid JSON" without a label is
    not a diagnosis.

    `model`, if given, overrides BOTH the routing table and ai.py's global
    default for THIS call only —
    the fallback model is unaffected, so a slower/heavier primary still has
    the same fast safety net underneath it. Lets a caller route only the
    stage that benefits from it (writing the actual prose, say) to a
    different model than the "pre-generation" stages (sequencing, context
    assembly, reasoning, ...), without a global env var forcing every call in
    the pipeline onto the same one.

    `timeout_s`, if given, overrides ai.py's global request timeout for this
    call — worth raising alongside `model` when that model is genuinely
    slower per request (a bigger prompt, a heavier model), rather than
    letting it hit the default timeout and quietly fall back every time.

    `reasoning`, if given, is passed straight through as OpenRouter's
    `reasoning` map (e.g. `{"enabled": True}`) — only meaningful for models
    that support thinking tokens, and ignored by ai.py otherwise. Reasoning
    tokens draw from the same `max_tokens` budget as the JSON answer itself,
    so a caller enabling this should raise `max_tokens` too, not just flip
    this on.
    """
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    options: dict = {"json_mode": True, "temperature": temperature, "max_tokens": max_tokens}

    # ROUTING, resolved from the label this call already carries. Precedence is
    # explicit argument, then the tier table, then ai.py's global default —
    # so a caller that named a model still gets it, and a pipeline with routing
    # off behaves exactly as it did before `routing.py` existed.
    #
    # Resolved here rather than at each call site because the label is the only
    # thing routing needs and every call already has one. See prep_flow/routing.py.
    routed = model or routing.resolve(label)
    if routed:
        options["model"] = routed
        # A thinking model spends this budget reasoning before it writes the
        # answer, and when it runs out it returns EMPTY content rather than a
        # short answer — ai.py reports it as "reasoning-only response". The
        # multiplier is a property of the tier's model, measured by
        # `modelbench`, so the caller's max_tokens stays sized for the task and
        # routing supplies the room its own choice of model needs.
        room = routing.headroom(label) if not model else 1
        if room > 1:
            options["max_tokens"] = routing.bounded_tokens(max_tokens, room)
    if timeout_s is not None:
        options["timeout_s"] = timeout_s
    if reasoning:
        options["reasoning"] = reasoning

    last_error: Optional[Exception] = None
    last_raw = ""
    # `label` already names this call in the log lines and the exception below;
    # publishing it here is what lets ai.py — which is shared by every agent and
    # cannot tell them apart — attribute the model call itself to the agent that
    # asked for it. Set per attempt, so a retry is labelled too. See
    # prep_flow/trace.py.
    _label_token = _CALL_LABEL.set(label)
    try:
        async with _semaphore():
            for attempt in range(1, attempts + 1):
                try:
                    raw = await call_ai(messages, options)
                except Exception as exc:            # transport / breaker / both-models-failed
                    last_error = exc
                    if attempt == attempts:
                        break
                    await asyncio.sleep(1.5 * attempt)
                    continue

                try:
                    return _decode(raw, required)
                except (json.JSONDecodeError, LLMShapeError) as exc:
                    last_error = exc
                    last_raw = raw or ""
                    if attempt == attempts:
                        break

                    # A parse failure sitting right at the end of the response is
                    # truncation, not a formatting slip -- ai.py already prints a
                    # "hit the token ceiling" WARNING for this case, but that
                    # signal never reached this retry loop, so a truncated call
                    # was re-asked at the SAME max_tokens and truncated again at
                    # the same place, three times, for nothing (seen for real:
                    # repair[T3#1] in the v12 run, 3/3 attempts cut off within a
                    # few chars of each other). Re-asking can only fix a genuine
                    # formatting mistake; it cannot fix "the budget was too
                    # small", so that case gets a bigger budget instead of a
                    # bigger nudge.
                    # NOT exc.pos-based: json's "Unterminated string starting
                    # at" reports where the offending token BEGAN, which can
                    # sit well before the true cutoff for a long "detail"
                    # sentence — verified this gives false negatives on a
                    # realistic truncated payload. Whether the response ends
                    # closed is reliable regardless of token length: a
                    # truncated response stops wherever the budget ran out
                    # (never on a clean '}'/']'), while a complete-but-malformed
                    # one (e.g. a missing comma) still ends closed because the
                    # model believed it had finished.
                    truncated = (
                        isinstance(exc, json.JSONDecodeError)
                        and not strip_fences(last_raw).rstrip().endswith(("}", "]"))
                    )
                    if truncated:
                        grown = min(int(options["max_tokens"] * 1.6) + 200, options["max_tokens"] + 4000)
                        print(f"[prep_flow:{label}] attempt {attempt} truncated at the token "
                              f"ceiling ({options['max_tokens']} tokens); retrying with "
                              f"max_tokens={grown} instead of re-asking at the same budget")
                        options["max_tokens"] = grown
                        messages = ([{"role": "system", "content": system}] if system else []) + [
                            {"role": "user", "content": prompt},
                        ]
                        continue

                    print(f"[prep_flow:{label}] attempt {attempt} unusable ({exc}); re-asking")

                    # Point at the break rather than replaying the response.
                    #
                    # This previously echoed `raw[:4000]` back as an assistant turn,
                    # which was actively harmful: a six-section sheet runs to ~8.6k
                    # characters, so the model was shown a copy of its own output cut
                    # off mid-JSON and asked to "fix only what is broken". The
                    # truncation looked like the defect, and three attempts in a row
                    # failed at the same place. A window around the actual error, with
                    # the original prompt intact, is both smaller and correct.
                    window = error_window(strip_fences(last_raw), exc)
                    detail = (f"\n\nThe text around the failure was:\n{window}"
                              if window else "")
                    messages = ([{"role": "system", "content": system}] if system else []) + [
                        {"role": "user", "content": prompt},
                        {"role": "user", "content": (
                            f"Your previous answer could not be parsed: {exc}."
                            f"{detail}\n\n"
                            "Answer the original request again, in full, as ONE valid JSON "
                            "object — no prose, no markdown fences, no commentary. Pay "
                            "particular attention to closing every array with ] and every "
                            "object with }, and to commas between items. If length is the "
                            "problem, shorten the prose in 'detail' rather than dropping "
                            "any required field."
                        )},
                    ]

    finally:
        _CALL_LABEL.reset(_label_token)
    window = error_window(strip_fences(last_raw), last_error) if last_error else ""
    raise RuntimeError(
        f"[prep_flow:{label}] failed after {attempts} attempt(s): {last_error}"
        + (f"\nlast response was {len(last_raw)} chars; text around the failure:\n{window}"
           if window else ""))


async def gather_bounded(coros: Sequence, limit: int = 4) -> list:
    """asyncio.gather with a ceiling, returning exceptions rather than raising.

    A batch node must not lose thirty-nine successful topics because the
    fortieth timed out, so failures come back as exception objects for the
    caller to record per topic.
    """
    gate = asyncio.Semaphore(max(1, limit))

    async def run(coro):
        async with gate:
            return await coro

    return await asyncio.gather(*(run(c) for c in coros), return_exceptions=True)
