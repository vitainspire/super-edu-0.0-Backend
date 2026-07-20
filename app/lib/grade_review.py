"""Mirrors lib/grade-review.ts. Flags a scanned paper for human review instead
of silently auto-accepting whatever the AI extracted. None of this blocks
saving — it's a signal shown alongside the score so a scanner/teacher knows
to double-check before trusting it."""


def assess_paper_confidence(breakdown: list[dict], extracted_answers: list[str]) -> dict:
    total_questions = len(breakdown)
    if total_questions == 0:
        return {"needsReview": True, "reviewReason": "No questions could be extracted from the image."}

    empty_count = sum(1 for a in extracted_answers if not a.strip())
    empty_ratio = empty_count / total_questions
    total_awarded = sum(b["awarded"] for b in breakdown)
    total_max = sum(b["max"] for b in breakdown)

    if empty_ratio == 1:
        return {"needsReview": True, "reviewReason": "Nothing was read off this paper — it may be blank, or the photo didn't scan well."}
    if empty_ratio >= 0.5:
        return {"needsReview": True, "reviewReason": "Most questions came back blank — the photo may be blurry, cropped, or poorly lit."}
    if total_awarded == 0 and total_max > 0:
        return {"needsReview": True, "reviewReason": "The student wrote answers but scored zero on all of them — worth a second look."}

    return {"needsReview": False, "reviewReason": None}
