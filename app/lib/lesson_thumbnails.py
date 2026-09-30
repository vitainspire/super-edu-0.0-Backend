"""A representative thumbnail for a generated lesson -- pulled from what's
already saved, never generated. Shared between the admin's "Browse Shared
Library" and the teacher's "Browse My Library" (admin_misc.py / teacher.py),
since both list views want the same "pick a picture to recognise this topic
by" behaviour and had started drifting into two copies of the same function.
"""
from typing import Optional


def first_lesson_image(lesson: dict) -> Optional[str]:
    """The first real textbook image found anywhere in this lesson's
    bullets, in teaching order -- a representative thumbnail for the topic
    list. A topic can have more than one image; this is just "one to
    recognise it by", not a definitive cover image."""
    def bullets_of(section):
        if isinstance(section, dict):
            return section.get("points") or section.get("recap") or []
        if isinstance(section, list):
            return section
        return []
    for key in ("previousTopicRefresher", "concept", "realLife", "challenge", "levelSet", "explore"):
        for b in bullets_of(lesson.get(key)):
            if isinstance(b, dict) and isinstance(b.get("image"), dict) and b["image"].get("url"):
                return b["image"]["url"]
    return None
