"""Which model answers which call.

ONE GLOBAL MODEL IS ALWAYS WRONG IN ONE OF TWO DIRECTIONS. The stages in this
pipeline do not resemble each other: curriculum reasoning is a single hard
judgement per chapter that is then cached for every future class studying that
book, while move extraction is a mechanical read repeated once per window and
the learner simulation is a short roleplay repeated once per topic. A model good
enough for the first is being wasted on the third; a model cheap enough for the
third produces a chapter-defining artifact nobody should trust.

So calls are routed by TIER, and a tier is chosen by what the call actually is:

    hard      the answer becomes an artifact later stages cannot repair
    standard  real writing, judged afterwards, cheap to redo
    cheap     mechanical, high volume, self-evidently right or wrong

THE ROUTING KEY IS THE CALL LABEL, which every `call_json` already carries so
that the provenance trace can attribute a model call to the agent that made it.
Reusing it means routing needs no new argument at eleven call sites and no new
concept — the label already names the stage.

OFF BY DEFAULT. With `PREP_FLOW_ROUTING` unset every tier resolves to None,
`call_json` passes no model, and `ai.py`'s global `OPENROUTER_MODEL` answers
everything exactly as it did before this module existed. Turning routing on is
a deliberate act, and the presets below say what they cost.
"""
from __future__ import annotations

import os
from typing import Optional

# ── Tiers ────────────────────────────────────────────────────────────────────

HARD, STANDARD, CHEAP = "hard", "standard", "cheap"
TIERS = (HARD, STANDARD, CHEAP)

# Label prefix -> tier. Ordered longest-first at lookup, so `reasoning-rederive`
# cannot fall into `reasoning` by accident.
#
# The tier assignments are arguments, not preferences:
#
#   reasoning     one call per chapter, CACHED. A bad chain is served to every
#                 future run of that book and no later stage can repair it.
#   mastery       measured against the printed page; feeds the audited gap.
#   experience    the trajectory every sheet is written to deliver.
#   context-*     proposes changes to a real classroom. Cheap to be wrong in a
#                 way nobody notices until a teacher reads it.
#
#   generation    the actual prose. Judged by Node 3 and repairable, so it wants
#                 a good writer rather than the best reasoner.
#   planning      minutes and emphasis, bounded by the normaliser.
#
#   moves         reads what the page already says, in printed order.
#   knowledge-*   extraction, then matched against a library that rejects junk.
#   learner       a simulated child answering four questions.
#   consistency   a whole-chapter read for contradictions.
_LABEL_TIERS: dict[str, str] = {
    "reasoning-rederive": HARD,
    "reasoning": HARD,
    "mastery": HARD,
    "experience": HARD,
    "context-reinforcement": HARD,
    "deep:curriculum": HARD,
    "deep:experience": HARD,
    "deep:context": HARD,

    "generation": STANDARD,
    "repair": STANDARD,
    "planning": STANDARD,
    "activity-invention": STANDARD,
    "sequence-override-refresher": STANDARD,
    "feedback-optimization": STANDARD,

    "moves": CHEAP,
    "knowledge-extraction": CHEAP,
    "competency-mapping": CHEAP,
    "learner": CHEAP,
    "consistency": CHEAP,
    "judge": CHEAP,
    "teacher": CHEAP,
}


def tier_for(label: str) -> Optional[str]:
    """The tier this call belongs to, or None when nothing claims it.

    Unclaimed is a real answer: a label nobody has reasoned about should fall
    through to the global default rather than be guessed into a tier. A new
    stage therefore behaves exactly as it does today until somebody decides
    where it belongs.
    """
    label = label or ""
    for prefix in sorted(_LABEL_TIERS, key=len, reverse=True):
        if label.startswith(prefix):
            return _LABEL_TIERS[prefix]
    return None


# ── Presets ──────────────────────────────────────────────────────────────────
#
# `free` is not a guess. Every model in it was measured against this pipeline's
# real prompts, scored by this pipeline's real gates, and beat the alternatives
# on the free tier — see `modelbench/` and `eval/models/`. Re-run the sweep
# before trusting it: OpenRouter's free tier turns over, and a preset naming a
# model that has been withdrawn silently falls back to the global default.
#
# A TIER CARRIES A TOKEN MULTIPLIER AS WELL AS A MODEL, and that is not a
# refinement — it is the finding the first sweep produced. Most of the free tier
# is thinking models: they spend `max_tokens` reasoning and return EMPTY content
# when the budget runs out, which `ai.py` reports as
#
#     reasoning-only response (183 reasoning tokens) — raise max_tokens
#
# Production's budgets are sized for a model that answers directly (curriculum
# reasoning asks for `2000 + 90 * topics`). Routing such a model in without
# headroom does not degrade the answer, it produces no answer at all — three
# retries, then the fallback. The first version of this preset made exactly that
# mistake: it put a 4x model on the CHEAP tier, which serves the smallest
# budgets in the pipeline.
PRESETS: dict[str, dict[str, dict]] = {
    # Measured Sept 2026 — see eval/models/free_tier_2026-09.json.
    #
    # minimax-m3 scored 1.00 on all three tasks, was fastest on all three
    # (7.8s / 10.4s / 22.5s), and was the ONLY free candidate that answered
    # inside production's token budget. It carries the two tiers where a
    # truncated answer costs the most.
    #
    # m2.7 also scored 1.00 but needs 4x, and it is on CHEAP anyway rather than
    # collapsing everything onto one model: the free tier rate-limits per
    # provider, and a run that puts every call through one endpoint spends its
    # time backing off 429s. Two endpoints halve that exposure.
    "free": {
        HARD: {"model": "minimax/minimax-m3:free", "headroom": 1},
        STANDARD: {"model": "minimax/minimax-m3:free", "headroom": 1},
        # m2.7 also scored 1.00 and WAS on this tier, to spread rate-limit
        # exposure across two endpoints. A real trace run took it off again:
        # every cheap-tier call is a thinking call, and thinking is latency the
        # cheap tier exists to avoid. `free-spread` keeps that option.
        CHEAP: {"model": "minimax/minimax-m3:free", "headroom": 1},
    },
    # Two endpoints instead of one, for a run that keeps hitting 429s. Slower
    # per call on the cheap tier; less likely to stall on a rate limit.
    "free-spread": {
        HARD: {"model": "minimax/minimax-m3:free", "headroom": 1},
        STANDARD: {"model": "minimax/minimax-m3:free", "headroom": 1},
        CHEAP: {"model": "minimax/minimax-m2.7:free", "headroom": 4},
    },
    # The same tier boundaries on paid models, for when there is credit. Kept
    # beside the free preset so the shape is visible in one place rather than
    # rediscovered per environment. NOT measured by the sweep — the key was
    # exhausted when this was written, so treat these as a starting point.
    #
    # All Meta since the pipeline moved off Gemini. The ladder is shorter than
    # the Gemini one it replaces: OpenRouter serves no Llama above Maverick (no
    # 405B slug — it was checked, not assumed), so HARD is the top of what Meta
    # offers rather than a deliberate step up from STANDARD. Maverick does bring
    # 1M context against llama-3.3-70b's 131K, which is the one axis where HARD
    # is unambiguously roomier.
    "paid": {
        HARD: {"model": "meta-llama/llama-4-maverick", "headroom": 1},
        STANDARD: {"model": "meta-llama/llama-3.3-70b-instruct", "headroom": 1},
        CHEAP: {"model": "meta-llama/llama-3.1-8b-instruct", "headroom": 1},
    },
}


def _preset() -> dict[str, str]:
    name = (os.environ.get("PREP_FLOW_ROUTING") or "").strip().lower()
    if not name or name in ("0", "off", "false", "none"):
        return {}
    if name not in PRESETS:
        print(f"[prep_flow:routing] unknown preset {name!r}; "
              f"expected one of {', '.join(PRESETS)} — routing stays off")
        return {}
    return PRESETS[name]


# Env override per tier, always. A preset is a starting point, and pinning one
# tier while measuring another is the normal way this gets tuned.
_ENV = {HARD: "PREP_FLOW_MODEL_HARD",
        STANDARD: "PREP_FLOW_MODEL_STANDARD",
        CHEAP: "PREP_FLOW_MODEL_CHEAP"}
_ENV_HEADROOM = {HARD: "PREP_FLOW_HEADROOM_HARD",
                 STANDARD: "PREP_FLOW_HEADROOM_STANDARD",
                 CHEAP: "PREP_FLOW_HEADROOM_CHEAP"}

# A pinned model with no stated headroom gets this much room. 1 would be the
# purist choice and it is the wrong one: most of the free tier thinks before it
# answers, and the failure is silent-empty rather than degraded, so the default
# should be the one that produces an answer.
_DEFAULT_OVERRIDE_HEADROOM = 4

# THE CEILING, and the run that taught us to want one. Headroom multiplies the
# CALLER's budget, and the callers are not uniform: curriculum reasoning asks
# for ~2,400 tokens, move extraction asks for 6,000. A flat 4x turned the second
# into 24,000 — and a thinking model handed 24,000 tokens will use them. The
# first trace run sat on a single move-extraction call for over two minutes
# before it was killed.
#
# What a thinking model actually needs is room for its reasoning PLUS the answer,
# which is roughly constant per task rather than proportional to how generous the
# answer budget already was. Multiplying is still the right shape — a bigger
# answer does need a bigger think — but it needs a stop.
#
# 12,000 is measured, not chosen: the largest budget any candidate needed in the
# sweep was 9,600 (the reason task at 2,400 x 4), and every one of them answered
# inside it.
MAX_EFFECTIVE_TOKENS = 12_000


def bounded_tokens(max_tokens: int, room: int) -> int:
    """Apply headroom without letting a generous caller become a runaway.

    Never returns less than the caller asked for: routing may buy a model more
    room, never less.
    """
    if room <= 1:
        return max_tokens
    return max(max_tokens, min(max_tokens * room, MAX_EFFECTIVE_TOKENS))


def model_for_tier(tier: Optional[str]) -> Optional[str]:
    if tier not in TIERS:
        return None
    override = (os.environ.get(_ENV[tier]) or "").strip()
    if override:
        return override
    return (_preset().get(tier) or {}).get("model") or None


def headroom_for_tier(tier: Optional[str]) -> int:
    """How many times production's token budget this tier's model needs.

    1 for a model that answers directly. Higher for one that reasons first —
    those tokens come out of the same budget as the answer.
    """
    if tier not in TIERS:
        return 1
    raw = (os.environ.get(_ENV_HEADROOM[tier]) or "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            print(f"[prep_flow:routing] {_ENV_HEADROOM[tier]}={raw!r} is not a "
                  f"number; using {_DEFAULT_OVERRIDE_HEADROOM}")
            return _DEFAULT_OVERRIDE_HEADROOM
    if (os.environ.get(_ENV[tier]) or "").strip():
        # Pinned by hand and said nothing about headroom.
        return _DEFAULT_OVERRIDE_HEADROOM
    return int((_preset().get(tier) or {}).get("headroom") or 1)


def resolve(label: str) -> Optional[str]:
    """The model this call should use, or None to leave the global default alone.

    Read at CALL TIME, not import time: the CLIs and the tests set these vars
    after the module tree is already imported, and a value captured at import
    would make an env var that appears to work do nothing.
    """
    return model_for_tier(tier_for(label))


def headroom(label: str) -> int:
    """The token multiplier for this call. 1 when nothing is routed."""
    tier = tier_for(label)
    return headroom_for_tier(tier) if model_for_tier(tier) else 1


def active() -> dict:
    """What routing is currently in force. For a run banner and for the trace."""
    preset = (os.environ.get("PREP_FLOW_ROUTING") or "").strip().lower() or "off"
    return {
        "preset": preset,
        "tiers": {tier: {"model": model_for_tier(tier),
                         "headroom": headroom_for_tier(tier)} for tier in TIERS},
        "enabled": any(model_for_tier(t) for t in TIERS),
    }


def describe() -> str:
    """One line per tier, for a CLI banner."""
    state = active()
    if not state["enabled"]:
        return "[prep_flow:routing] off — every call uses ai.py's global model"
    lines = [f"[prep_flow:routing] preset={state['preset']}"]
    for tier in TIERS:
        cfg = state["tiers"][tier]
        model = cfg["model"] or "(global default)"
        room = f"x{cfg['headroom']} tokens" if cfg["headroom"] > 1 else ""
        labels = sorted(k for k, v in _LABEL_TIERS.items() if v == tier)
        lines.append(f"  {tier:9} {model:34} {room:12} {', '.join(labels[:3])}"
                     + (" …" if len(labels) > 3 else ""))
    return "\n".join(lines)
