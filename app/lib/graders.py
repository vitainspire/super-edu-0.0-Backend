"""Mirrors lib/graders.ts. Local graders for MCQ, fill-in-blank, and
short-answer questions — no LLM call, fast and deterministic. Only
long-answer questions are sent to the LLM."""
import re


def _grade_result(marks_awarded: float, feedback: str) -> dict:
    return {"marksAwarded": marks_awarded, "feedback": feedback}


def grade_mcq(scanned: str, correct_answer: str, max_marks: float) -> dict:
    if not scanned or not scanned.strip():
        return _grade_result(0, "No answer written")
    norm = scanned.strip().upper()
    match = re.search(r"\b([A-D])\b", norm) or re.search(r"([A-D])", norm)
    letter = match.group(1) if match else ""
    if not letter:
        return _grade_result(0, "Answer not readable")
    correct = correct_answer.strip().upper()
    if letter == correct:
        return _grade_result(max_marks, "Correct")
    return _grade_result(0, f"Incorrect — answer is {correct}")


def _levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            dp[i][j] = dp[i - 1][j - 1] if a[i - 1] == b[j - 1] else 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return dp[m][n]


def grade_fib(scanned: str, correct_answer: str, max_marks: float) -> dict:
    if not scanned or not scanned.strip():
        return _grade_result(0, "No answer written")
    if not correct_answer or not correct_answer.strip():
        return _grade_result(round(max_marks * 0.5), "Could not verify")

    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s.lower().strip()))

    a, b = norm(scanned), norm(correct_answer)
    if a == b:
        return _grade_result(max_marks, "Correct")

    dist = _levenshtein(a, b)
    threshold = max(2, int(len(b) * 0.2))
    if dist <= threshold:
        return _grade_result(max_marks, "Correct (minor spelling)")

    if a in b or b in a:
        return _grade_result(round(max_marks * 0.5), "Partially correct")

    return _grade_result(0, f"Incorrect — answer: {correct_answer}")


def grade_short_answer(scanned: str, keywords: list[str], max_marks: float) -> dict:
    if not scanned or not scanned.strip():
        return _grade_result(0, "No answer written")

    if not keywords:
        return _grade_result(round(max_marks * 0.5), "Partially awarded — no key terms to verify against")

    def norm(s: str) -> str:
        return re.sub(r"[^\w\s]", "", s.lower().strip())

    text = norm(scanned)
    matched = [kw for kw in keywords if norm(kw) in text]
    ratio = len(matched) / len(keywords)

    if ratio >= 0.75:
        return _grade_result(max_marks, "Key concepts present")
    if ratio >= 0.5:
        return _grade_result(round(max_marks * 0.6), "Most key concepts present")
    if ratio >= 0.25:
        return _grade_result(round(max_marks * 0.25), "Few key concepts mentioned")
    return _grade_result(0, "Key concepts missing")
