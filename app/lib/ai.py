"""Mirrors backend/src/lib/ai.ts — single entry point for all AI calls, with a
circuit breaker and a primary/fallback-model retry."""
import asyncio
import os
import re
import time
from typing import Optional
import httpx

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
FALLBACK_MODEL = os.environ.get("OPENROUTER_FALLBACK_MODEL", "meta-llama/llama-3.1-8b-instruct:free")
IMAGE_MODEL = os.environ.get("OPENROUTER_IMAGE_MODEL", "google/gemini-2.5-flash-image")

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


async def _call_model(model: str, messages: list[dict], options: dict, use_json_mode: bool) -> str:
    max_tokens = options.get("max_tokens")
    temperature = options.get("temperature", 0.7)
    timeout_s = options.get("timeout_s", 45)

    body: dict = {"model": model, "messages": messages, "temperature": temperature}
    if use_json_mode:
        body["response_format"] = {"type": "json_object"}
    if max_tokens:
        body["max_tokens"] = max_tokens

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

    if resp.status_code >= 400:
        raise Exception(f"AI error {resp.status_code}: {resp.text}")

    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def call_ai(messages: list[dict], options: Optional[dict] = None) -> str:
    """Circuit breaker: opens after 3 consecutive failures, auto-resets in 60s.
    Retry/backoff: primary fails -> wait 1s -> try fallback."""
    options = options or {}
    json_mode = options.get("json_mode", True)

    if _circuit_open():
        raise Exception("[ai] Circuit breaker is open — AI service temporarily unavailable")

    try:
        result = await _call_model(MODEL, messages, options, json_mode)
        _on_success()
        return result
    except Exception as primary_err:
        print(f"[ai] {MODEL} failed, trying fallback after 1s: {primary_err}")
        await asyncio.sleep(1)

        try:
            raw = await _call_model(FALLBACK_MODEL, messages, options, False)
            _on_success()
            return _strip_fences(raw)
        except Exception as fallback_err:
            _on_failure()
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
