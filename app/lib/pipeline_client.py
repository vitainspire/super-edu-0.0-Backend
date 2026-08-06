"""Mirrors lib/pipeline-client.ts — the self-hosted Two-Door OCR pipeline."""
import os
from typing import Optional
import httpx

SERVER_IP = os.environ.get("PIPELINE_IP", "13.49.19.5")
OCR_URL = f"http://{SERVER_IP}:8000/v1/chat/completions"
TEXT_URL = f"http://{SERVER_IP}:8001/v1/chat/completions"


async def _call_door(url: str, body: dict) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers={"Content-Type": "application/json"}, json=body)
    if resp.status_code >= 400:
        raise Exception(f"Pipeline {resp.status_code}: {resp.text}")
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


async def run_ocr(image_base64: str, instruction: Optional[str] = None, mime: str = "jpeg", max_tokens: int = 2048) -> str:
    instruction = instruction or (
        "Now transcribe the attached image exactly as written, errors and all. Preserve all structural layout. "
        "For ticked or circled options, append [TICKED]. For boxed/outlined options, append [BOXED]. "
        "For drawings, output [DRAWN: <description>]. Do not solve the math."
    )
    return await _call_door(OCR_URL, {
        "model": "ocr-engine",
        "messages": [
            {"role": "system", "content": "You are a mindless OCR transcription engine. You do not know math. Your only job is to copy the exact text and numbers you see in the image. You must transcribe mathematical errors exactly as they are written."},
            {"role": "user", "content": "Transcribe this image. It contains an equation where a student incorrectly answered 7 x 5 = 40."},
            {"role": "assistant", "content": "7 x 5 = 40"},
            {"role": "user", "content": "Transcribe this image. It contains a blank equation 10 + 20 = ."},
            {"role": "assistant", "content": "10 + 20 ="},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/{mime};base64,{image_base64}"}},
                {"type": "text", "text": instruction},
            ]},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    })


async def run_text(user_prompt: str, system: str = "You are a helpful assistant.", temperature: float = 0.7, max_tokens: int = 2048) -> str:
    return await _call_door(TEXT_URL, {
        "model": "text-engine",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    })
