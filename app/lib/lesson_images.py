"""A lesson bullet's images -- shared by both generation pipelines
(smart_lesson_routes.py's live per-class route and prep_pipeline_bridge.py's
shared/batch route) so the two never drift into two different shapes for the
same thing the frontend renders identically either way.

A bullet carries `images: [{"url": ..., "caption"?: ..., "aiGenerated"?: true}, ...]`
-- zero, one, or several, by relevance, never capped at one. Real textbook
illustrations and AI-generated board sketches live in the same list; an
AI sketch is only ever added when the list is still empty (see each
pipeline's own fallback logic), never to top up a section that already has a
real picture.
"""
from typing import Optional


def bullet_images(bullet: dict) -> list:
    """Every image already on this bullet. Reads the `images` list, and
    falls back to the older singular `image` dict so a lesson persisted
    before this schema existed (shared_prep_materials, months old; a
    teacher's saved prep_materials row) still renders instead of silently
    losing its picture."""
    images = bullet.get("images")
    if isinstance(images, list):
        return [im for im in images if isinstance(im, dict) and im.get("url")]
    legacy = bullet.get("image")
    if isinstance(legacy, dict) and legacy.get("url"):
        return [legacy]
    return []


def add_image(bullet: dict, image: dict) -> bool:
    """Append `image` to this bullet's image list, unless that exact url is
    already there. Returns whether it actually added something, so a caller
    counting "images attached" doesn't double-count a no-op."""
    existing = bullet_images(bullet)
    if any(im.get("url") == image.get("url") for im in existing):
        return False
    bullet.pop("image", None)
    bullet["images"] = existing + [image]
    return True


def has_image(bullet: dict) -> bool:
    return bool(bullet_images(bullet))
