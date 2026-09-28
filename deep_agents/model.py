"""The chat model every Deep Agent runs on.

WHY THIS MODULE EXISTS AT ALL. `prep_flow/llm.py` is the package's model door,
and it is not one this layer can use: it returns a parsed dict from a single
completion, and a Deep Agent needs a `BaseChatModel` it can hold a multi-turn
tool conversation through. So there are now two doors onto the same provider,
and this one states plainly what it gives up and what it keeps.

KEPT, because losing them would be a regression:

  * the same provider and the same models. `ai.py` reads OPENROUTER_MODEL and
    OPENROUTER_FALLBACK_MODEL; so does this, from the same environment, so a
    chapter derived through the agents runs on the model the rest of the
    pipeline was tuned against rather than on a second one nobody chose.
  * primary -> fallback. `ai.py` does this by hand inside `call_ai`: catch, then
    re-ask the cheap model. Here it is OpenRouter's own `models` array instead,
    which is the same guarantee bought more cheaply — the routing happens
    server-side, so a primary outage costs no client round trip at all.
    LangChain's `.with_fallbacks()` was the obvious alternative and is not
    usable: it returns a `RunnableWithFallbacks`, and `deepagents.resolve_model`
    accepts only a `BaseChatModel` or a `provider:model` string, so a wrapped
    model is parsed as a model NAME and fails on an attribute lookup.
  * the call label. `prep_flow/llm.py` publishes a contextvar that `ai.py` reads
    so the provenance trace can say which agent made a call. Nothing in
    LangChain's stack reads that var, so `labelled()` below sets it around an
    agent invocation and `middleware.ProvenanceMiddleware` reads it back per
    model request. See `prep_flow/trace.py`.

GIVEN UP, stated here rather than discovered later:

  * `ai.py`'s circuit breaker. It counts consecutive failures across the whole
    process and short-circuits; LangChain's fallback is per-call and has no
    memory. A provider outage therefore costs one failed primary attempt per
    agent step here, where the rest of the pipeline would have stopped asking.
    Bounded by the step budget in `registry.py`, not unbounded.
  * the JSON-repair retry. `call_json` re-asks with the parse error attached.
    Deep Agents gets structured output through `response_format` instead, which
    is a schema-validated tool call rather than free JSON — a different and
    generally stronger mechanism, but not the same one.
"""
from __future__ import annotations

import contextlib
import os
from typing import Iterator, Optional

from langchain_openai import ChatOpenAI

from prep_flow.llm import CALL_LABEL

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Read at call time, not import time: `.env` is loaded by whichever entry point
# is running (the CLI, FastAPI, experiment.py), and this module is imported by
# some of them before that happens.
def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def primary_model_name() -> str:
    return _env("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")


def fallback_model_name() -> str:
    return _env("OPENROUTER_FALLBACK_MODEL", "meta-llama/llama-3.1-8b-instruct")


def _routed(label: Optional[str]) -> Optional[str]:
    """The model `prep_flow.routing` assigns to this agent's label, if any.

    THE AGENTS WERE THE ONE PATH ROUTING COULD NOT REACH. `routing.py` has
    carried `deep:curriculum`, `deep:experience` and `deep:context` on its HARD
    tier since it was written, and every one of them was ignored: routing is
    applied inside `prep_flow.llm.call_json`, and an agent does not go through
    `call_json` — it is handed a `ChatOpenAI` at construction. So setting
    `PREP_FLOW_MODEL_HARD` moved every stage except the three the tier was
    named for.

    That matters more than a missing convenience, because the agent seam is
    exactly where the model choice is load-bearing: an agent needs multi-step
    tool use to open a skill at all, and a model that answers in one turn leaves
    the whole pedagogy library unread while producing output that looks right.

    Read at call time for the same reason `routing.resolve` is — the CLIs set
    these variables after import.
    """
    if not label:
        return None
    try:
        from prep_flow import routing
    except Exception:                   # noqa: BLE001 - routing is optional
        return None
    return routing.resolve(label)


def build_model(
    *,
    model: Optional[str] = None,
    label: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 8000,
    timeout_s: float = 120.0,
    with_fallback: bool = True,
) -> ChatOpenAI:
    """The model an agent is constructed with.

    `temperature` defaults low, and deliberately lower than generation's 0.6:
    these agents decide what must be learned, and two runs of the same chapter
    disagreeing about its prerequisites is a defect, where two phrasings of the
    same Refresher is not.

    `timeout_s` defaults high for the same reason `prep_flow/llm.py` lets a
    caller raise it — an agent turn carries a tool result and a skill body, and
    a 45-second budget sized for one completion times out on the first read.

    Returns a bare `ChatOpenAI` and not a wrapper, because that is the only
    shape `deepagents.resolve_model` will accept as a model rather than parse as
    a model name.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set — the deep agents cannot reach a model. "
            "It is the same key ai.py uses; load .env before building an agent.")

    # Precedence matches `prep_flow.llm.call_json` exactly, and deliberately:
    # an explicit argument, then the routing table, then ai.py's global default.
    # Two paths that resolved a model in different orders would make "which
    # model answered" depend on which seam the call went through.
    primary = model or _routed(label) or primary_model_name()
    extra_body: dict = {}
    if with_fallback:
        backup = fallback_model_name()
        if backup and backup != primary:
            # OpenRouter's server-side fallback: it tries these in order and
            # bills for the one that answered. `model` above stays the primary so
            # that a log line, a trace row and a dashboard entry all still name
            # the model we asked for.
            extra_body["models"] = [primary, backup]

    return ChatOpenAI(
        model=primary,
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout_s,
        max_retries=2,
        extra_body=extra_body or None,
        # OpenRouter's app-attribution headers. Purely cosmetic on the dashboard,
        # but it is how a bill gets read back to a stage six months later.
        default_headers={
            "HTTP-Referer": _env("OPENROUTER_APP_URL", "https://vitainspire.local"),
            "X-Title": _env("OPENROUTER_APP_TITLE", "VitaInspire Deep Agents"),
        },
    )


@contextlib.contextmanager
def labelled(label: str) -> Iterator[None]:
    """Attribute every model call made inside this block to `label`.

    The same contextvar `prep_flow/llm.py` sets around `call_json`, so the trace
    does not have to learn a second vocabulary for agent-made calls. Agents wrap
    a whole invocation rather than a single call, so a label here covers the
    agent's entire loop — which is the honest granularity: an agent step is not
    separately attributable to anything smaller.
    """
    token = CALL_LABEL.set(label)
    try:
        yield
    finally:
        CALL_LABEL.reset(token)
