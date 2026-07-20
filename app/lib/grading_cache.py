"""Mirrors lib/grading-cache.ts. Even at temperature 0, re-grading the exact
same extracted answer against the exact same question/reference isn't
guaranteed to produce an identical mark — this memoizes the FIRST grade for a
given (question, reference, extracted answer) tuple so a re-submitted/
duplicate scan of the same paper can never silently disagree with itself."""
import re
from typing import Callable, Optional

from .server_cache import ck, with_cache

GRADE_CACHE_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


async def get_consistent_long_answer_grade(
    question_text: str,
    reference_answer: Optional[str],
    extracted_answer: str,
    max_marks: float,
    compute_live_grade: Callable[[], dict],
) -> dict:
    key = ck("answer-grade-v1", question_text, reference_answer or "", _normalize(extracted_answer), max_marks)

    async def _compute():
        return compute_live_grade()

    value, _from_cache = await with_cache(key, GRADE_CACHE_TTL_SECONDS, _compute)
    return value
