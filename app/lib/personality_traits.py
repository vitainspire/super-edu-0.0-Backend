"""Mirrors lib/personality-traits.ts. Rotates automatically by calendar date —
no teacher or student ever picks a trait. Same trait for every student on a
given date; only the story's setting/characters vary, via that student's
interests."""
from datetime import date, timezone, datetime

PERSONALITY_TRAITS = [
    "Patience",
    "Empathy",
    "Kindness",
    "Perseverance",
    "Honesty",
    "Teamwork",
    "Gratitude",
    "Courage",
    "Responsibility",
    "Respect",
    "Self-Control",
    "Confidence",
]


def today_date_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _day_of_year(date_str: str) -> int:
    d = date.fromisoformat(date_str)
    start = date(d.year, 1, 1)
    return (d - start).days + 1


def trait_for_date(date_str: str) -> str:
    return PERSONALITY_TRAITS[_day_of_year(date_str) % len(PERSONALITY_TRAITS)]
