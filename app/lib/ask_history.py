"""Shared conversation-context formatter for both Ask-assistant classifiers
(teacher's /ask-intent in ai_routes2.py, admin's in admin_misc.py). Without
this, every request was classified in total isolation — a follow-up like
"what about his attendance" or "with sundays?" had nothing to resolve the
pronoun/omission against, since the previous turn's question and answer were
never shown to the model."""


def _field(h, key: str) -> str:
    """Accepts either a Pydantic-model-shaped item (admin_misc.py's ask-intent,
    .question/.answer attributes) or a plain dict (teacher_agent_routes.py's
    _recent_history) — the two existing callers use different shapes, and this
    one helper serves both rather than making every caller conform."""
    return h[key] if isinstance(h, dict) else getattr(h, key)


def history_block(history: list) -> str:
    if not history:
        return ""
    lines = "\n".join(f"Q: {_field(h, 'question')}\nA: {_field(h, 'answer')}" for h in history[-3:])
    return f"""

Recent conversation in this same chat window, most recent last (context only — if the new question is a follow-up that omits a name/class/date/subject a recent turn already established, or refers back with a pronoun like "that"/"those"/"it", fill in the missing detail from here; ignore this block entirely if the new question stands on its own):
{lines}"""
