import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..lib.ai import call_ai

router = APIRouter()

SYSTEM_PROMPT = """You are a school schedule assistant. Parse a description of school hours and return a JSON object with this exact shape:
{
  "startTime": "HH:MM",
  "endTime": "HH:MM",
  "periodMins": number,
  "breaks": [
    { "label": string, "startTime": "HH:MM", "endTime": "HH:MM" }
  ]
}
Rules:
- All times in 24-hour HH:MM format (e.g. 09:00, 13:30)
- periodMins is the duration of each academic period in minutes (default 45 if not mentioned)
- Extract all breaks — short breaks, lunch break, prayer time, recess etc.
- Sort breaks by startTime ascending
- Return ONLY the JSON object, no extra text"""


class ScheduleAIBody(BaseModel):
    prompt: str


@router.post("/schedule-ai")
async def schedule_ai(body: ScheduleAIBody):
    if not body.prompt:
        raise HTTPException(status_code=400, detail="prompt required")
    try:
        raw = await call_ai(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": body.prompt}],
            {"json_mode": True, "max_tokens": 400, "temperature": 0.2},
        )
        form = json.loads(raw)
        return {"form": form}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
