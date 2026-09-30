"""Shared prompt fragments. Mirrors frontend/lib/prompt-fragments.ts — only the
piece smart-lesson needs (engagement-level calibration by grade band).
"""
import re

# Every grade maps to an engagement "level" that calibrates HOW the lesson is
# pitched — its language, abstraction, and the style of fun. Level 1 (youngest)
# is the simplest and most playful; Level 4 (oldest) the most sophisticated.
# The constant across all four: easy to understand AND genuinely fun. Bands:
#   Grades 1-3 -> Level 1   |   4-5 -> Level 2   |   6-8 -> Level 3   |   9-10 -> Level 4
# (Grade 8 sits in Level 3; 9-10 are Level 4.)


def engagement_level(grade) -> int:
    digits = re.sub(r"[^0-9]", "", str(grade))
    g = int(digits) if digits else 0
    if g <= 0:
        return 2  # unknown grade -> sensible middle
    if g <= 3:
        return 1
    if g <= 5:
        return 2
    if g <= 8:
        return 3
    return 4


LEVEL_GUIDANCE = {
    1: 'Level 1 (Grades 1-3) — the SIMPLEST of all, highest simplicity possible. Very short sentences (about 5-8 words), only the most common everyday words, and lots of repetition. One idea at a time, ALWAYS through real objects, fingers, pictures, or the students\' own bodies — nothing abstract. Turn everything into a game, a song, a sound, or a character/story the class physically steps into; expect giggles and big reactions. Keep any numbers tiny and countable (usually under 20).',
    2: 'Level 2 (Grades 4-5). Short, clear sentences and familiar words; a new term is fine only if you explain it in the same breath. One main idea with a small twist. Concrete first, then a light bit of notation. Fun through role-play, team games, friendly competition, and real-life scenes they know (market, cricket, festival). Numbers up to a few digits, money, scores.',
    3: 'Level 3 (Grades 6-8). Full sentences and real subject vocabulary (defined quickly the first time it appears). Push some reasoning — the WHY, a rule to discover, a two-or-three-step problem. Fun through investigation, strategy, "crack the rule" challenges, light debate, and problems that feel real and a little high-stakes. Patterns and some abstraction are welcome.',
    4: 'Level 4 (Grades 9-10). Precise language and proper terminology. Expect abstraction, multi-step reasoning, and application/analysis, with connections across topics. Fun through real-world stakes, genuine problem-solving, inquiry, and argument — engaging because it is meaningful and challenging, not because it is dressed up. Never childish.',
}


def engagement_level_guidance(grade, plain: bool = False) -> str:
    """`plain` is prep_flow's --plain-language flag (see its own help text:
    "wording only — short sentences, everyday words; same numbers and
    thinking demand as the grade"): it constrains SENTENCES, never the
    cognitive level LEVEL_GUIDANCE already sets — a Level 3 class stays at
    Level 3 reasoning, just said in shorter words."""
    level = engagement_level(grade)
    guidance = f"""ENGAGEMENT LEVEL — this class is Level {level} of 4 (Level 1 = youngest & simplest, Level 4 = oldest & most advanced). Pitch EVERYTHING — the Explore scene, the Challenge, the language, the examples — to this level:
{LEVEL_GUIDANCE[level]}
Non-negotiable at every level: it must be EASY to understand AND genuinely fun to follow — never dry, never too babyish for the level, never over their heads."""
    if plain:
        guidance += """

PLAIN LANGUAGE MODE — wording only, never the thinking demand: short sentences, everyday words, as if explaining to a teacher who is still learning English. Keep the SAME numbers, the SAME reasoning steps, and the SAME grade-level thinking demand as above — simplify the sentences, never the ideas."""
    return guidance
