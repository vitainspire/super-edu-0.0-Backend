"""Mirrors backend/src/lib/indian-festivals.ts — a curated fallback for
lunar/Hindu festivals the computed public-holiday source has no data for.
Best-effort from general knowledge, not a verified/live source — never
auto-inserted, only ever returned as suggestions. Extend this table annually."""

FESTIVALS_BY_YEAR: dict[int, list[dict]] = {
    2025: [
        {"title": "Makar Sankranti", "date": "2025-01-14"},
        {"title": "Maha Shivratri", "date": "2025-02-26"},
        {"title": "Holi", "date": "2025-03-14"},
        {"title": "Ram Navami", "date": "2025-04-06"},
        {"title": "Buddha Purnima", "date": "2025-05-12"},
        {"title": "Raksha Bandhan", "date": "2025-08-09"},
        {"title": "Janmashtami", "date": "2025-08-16"},
        {"title": "Ganesh Chaturthi", "date": "2025-08-27"},
        {"title": "Dussehra", "date": "2025-10-02"},
        {"title": "Diwali", "date": "2025-10-20"},
        {"title": "Guru Nanak Jayanti", "date": "2025-11-05"},
    ],
    2026: [
        {"title": "Makar Sankranti", "date": "2026-01-14"},
        {"title": "Maha Shivratri", "date": "2026-02-15"},
        {"title": "Holi", "date": "2026-03-04"},
        {"title": "Ram Navami", "date": "2026-03-26"},
        {"title": "Buddha Purnima", "date": "2026-05-01"},
        {"title": "Raksha Bandhan", "date": "2026-08-28"},
        {"title": "Janmashtami", "date": "2026-09-04"},
        {"title": "Ganesh Chaturthi", "date": "2026-09-14"},
        {"title": "Dussehra", "date": "2026-10-21"},
        {"title": "Diwali", "date": "2026-11-08"},
        {"title": "Guru Nanak Jayanti", "date": "2026-11-24"},
    ],
}


def get_major_festival_suggestions(year: int) -> list[dict]:
    """Returns [] for any year outside the curated table — never guesses beyond it."""
    return FESTIVALS_BY_YEAR.get(year, [])
