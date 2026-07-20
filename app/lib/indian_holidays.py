"""Mirrors backend/src/lib/indian-holidays.ts, using the Python `holidays`
package instead of the TS original's `date-holidays` npm package."""
import holidays as holidays_lib

# The TS original's `date-holidays` had no data for Islamic holidays at all
# (moon-sighting-dependent, can legitimately vary by a day between regions —
# a hardcoded guess would be actively misleading, not just imprecise) and
# deliberately never included them. Python's `holidays` package DOES include
# them, so they're filtered out here by name to keep the same behavior.
_EXCLUDED_NAME_FRAGMENTS = ("id-ul-fitr", "id-ul-zuha", "bakrid", "muharram", "milad-un-nabi")


def get_indian_holidays_for_year(year: int) -> list[dict]:
    """India's public holidays for a given year, computed (not guessed).
    Deduped by date+name since the library can report the same observance
    under multiple entries."""
    h = holidays_lib.India(years=year, categories=("public",))

    seen: set[str] = set()
    result: list[dict] = []
    for date, name in sorted(h.items()):
        if any(frag in name.lower() for frag in _EXCLUDED_NAME_FRAGMENTS):
            continue
        date_str = date.isoformat()
        key = f"{date_str}|{name}"
        if key in seen:
            continue
        seen.add(key)
        result.append({"title": name, "startDate": date_str, "endDate": date_str})

    result.sort(key=lambda r: r["startDate"])
    return result
