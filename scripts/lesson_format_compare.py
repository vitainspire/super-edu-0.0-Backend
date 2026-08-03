"""
Generate the SAME lesson plan as JSON and as Markdown, then compare.

This is a genuinely different question from the textbook-extraction comparison,
and the answer may well go the other way. Extraction produces a flat ontology
whose hierarchy maps cleanly onto heading depth. A prep-material lesson does not:
its schema is nested with strict cardinality that the UI depends on —
`explore.points` is 1-3 objects, `explore.scenario` is a required sibling of it,
`challenge.activity` must differ from Explore's, `levelSet.extendPrompt` is a
single line next to a bullet list. Markdown has to encode all of that in
convention rather than syntax.

What it holds constant
----------------------
Both arms use the identical system prompt, teaching profile, grade, subject,
topic, previous topic, avoid-list and resource list, built by
test_prep_material.build_v2_prompts. Only the "return this shape" section
differs. Both results are then run through the SAME validator
(test_prep_material.validate_v2_lesson), so a format cannot look better by
being held to a looser standard.

What it measures
----------------
  parse            Did the response become a lesson at all, first try?
  repair calls     Extra LLM calls needed to get there. The production route
                   carries two of these (_repairJson and repairLesson) and a
                   provider switch to gpt-4o-mini specifically because Gemini
                   "even in JSON mode, frequently drops the comma between two"
                   fields — that tax is real and it is what this measures.
  validation       Issues from the shared validator, before and after repair.
  fidelity         Field-by-field: which required fields survived each format.
  cost / latency   Real usage off the API response.

What it cannot measure
----------------------
Whether the lesson is any *good*. Both are printed in full so you can read them.
That judgement is yours — the harness only tells you what each format costs to
obtain and how reliably it arrives intact.

Usage
-----
    python lesson_format_compare.py --grade 5 --subject Mathematics \
        --topic "Fractions" --profile storyteller

    python lesson_format_compare.py --topic "Photosynthesis" --grade 6 \
        --subject Science --profile activity_led --repeats 2

    python lesson_format_compare.py --list-profiles
    python lesson_format_compare.py --dry-run        # no API calls
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import test_prep_material as tpm  # noqa: E402  (same directory)

FORMATS = ("json", "markdown")

# Fields the UI actually renders. Fidelity is scored against this list rather
# than "did it parse", because a lesson missing explore.scenario renders a blank
# card even though the parse succeeded.
REQUIRED_FIELDS = [
    "planningNote", "concept", "explore.scenario", "explore.points",
    "challenge.activity", "challenge.points", "materialsUsed",
    "levelSet.points", "levelSet.extendPrompt",
]
OPTIONAL_FIELDS = ["previousTopicRefresher", "explore.imageFocus"]


# ── Markdown dialect ──────────────────────────────────────────────────────────
# `::` separates a bullet's text from its optional detail. Chosen because it does
# not occur in ordinary prose, unlike "-" or ":" which appear constantly in
# teacher scripts ("Ask: what changed?").

# Every list states its cardinality explicitly and shows more than one example
# line. The first version of this shape showed a single "- <...>" per section and
# the model produced exactly one bullet per section — a 5-word Explore scenario
# against JSON's three sentences. That was this prompt under-specifying volume,
# not Markdown being less expressive: the JSON template anchors quantity by
# repeating entries and inlining "... up to 3 total" at every list. Any format
# comparison has to match that anchoring or it just measures prompt quality.
MARKDOWN_SHAPE = """Return ONLY Markdown in exactly this shape. Use `::` to separate a
bullet's main text from its optional detail, and omit the `::` entirely when a
bullet needs no detail. Fill EVERY section below — a missing section is a broken sheet.

planning-note: <1-2 sentences of YOUR OWN reasoning, written first: given this profile and the previous topic, what should this lesson's Explore scenario be, and which Challenge activity fits (and isn't in the avoid-list)?>

## Refresher: <the exact previous topic name given above>
- <short warm reminder of that topic> :: <optional detail>
- <a second reminder bullet> :: <optional detail>
- <a third, ending with a one-line bridge into today's topic>
(2 or 3 bullets. Omit this whole `## Refresher` section ONLY if there is no previous topic.)

## Concept
- <one syllabus-aligned idea, one short sentence> :: <optional detail>
- <the second idea, one short sentence> :: <optional detail>
- <the third idea, one short sentence>
(EXACTLY 2 or 3 bullets — never just one.)

## Explore
scenario: <2-3 FULL SENTENCES: a vivid, specific real-life scene the class recognizes, that this creative activity happens inside of — not an abstract 'today we will learn X', and not a bare phrase like 'sharing a pizza'. Name the place, the people, the objects.>
sketch: <one short phrase describing the single most useful thing to sketch on the board>
- <the creative activity itself — what the teacher does and says first> :: <optional detail>
- <what students do next> :: <optional detail>
- <how the concept emerges from what they tried>
(EXACTLY 2 or 3 bullets — never just one.)

## Challenge
activity: <the exact name of ONE activity from the bank above, not Explore's activity, not in the avoid-list>
- <how it plays out for this topic, step one> :: <optional detail>
- <step two> :: <optional detail>
- <step three>
(EXACTLY 2 or 3 bullets — never just one.)

## Materials
- <an item actually referenced in Explore or Challenge>
- <another such item>
(List every item actually used — nothing invented, nothing unused. Never leave this empty.)

## Level set
- <recap tied back to the SAME Explore scenario> :: <optional detail>
- <a second recap bullet> :: <optional detail>
extend: <one open-ended line inviting another real-life connection — this line is required>
(2 or 3 bullets, then the required `extend:` line.)"""


def to_markdown_prompt(json_user_prompt: str) -> str:
    """Swap only the output-shape section of the real prompt.

    Everything above 'Return ONLY valid JSON' — profile, grade, topic, activity
    bank, weak topics, avoid-list — is reused verbatim, so the two arms differ in
    serialisation and nothing else.
    """
    marker = "Return ONLY valid JSON"
    idx = json_user_prompt.find(marker)
    if idx == -1:
        raise RuntimeError(
            "Could not find the shape section in build_v2_prompts' output — "
            "the prompt changed and this script needs updating rather than guessing."
        )
    head = json_user_prompt[:idx]

    # Keep the trailing Rules block: it is content policy (bullet caps, banned
    # words, scannability), not serialisation, and both arms must obey it.
    rules_idx = json_user_prompt.find("\nRules:")
    rules = json_user_prompt[rules_idx:] if rules_idx > idx else ""
    rules = rules.replace(
        '- "detail" is only included on a bullet when it\'s genuinely useful',
        '- a `::` detail is only included on a bullet when it\'s genuinely useful',
    )
    return head + MARKDOWN_SHAPE + "\n" + rules


# ── Markdown → lesson dict ────────────────────────────────────────────────────

def _bullet(line: str) -> Optional[dict]:
    m = re.match(r"^\s*[-*+]\s+(.*)$", line)
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None
    if "::" in body:
        text, _, detail = body.partition("::")
        out = {"text": text.strip()}
        if detail.strip():
            out["detail"] = detail.strip()
        return out
    return {"text": body}


def parse_markdown_lesson(markdown: str) -> tuple:
    """Parse the dialect above into the v2 lesson dict. Never raises.

    Returns (lesson, warnings). Mirrors markdown_ontology's contract: unparseable
    input yields a partial-but-valid dict rather than an exception, so a truncated
    response still produces whatever arrived.
    """
    warnings: list = []
    lesson: dict = {}
    if not markdown:
        return {}, ["empty response"]

    lines = markdown.replace("\r\n", "\n").split("\n")
    if lines and re.match(r"^\s*```", lines[0]):
        lines = lines[1:]
        while lines and not re.match(r"^\s*```", lines[-1]):
            lines.pop() if not lines[-1].strip() else lines.append("") or None
            break
        lines = [l for l in lines if not re.match(r"^\s*```", l)]

    section = None
    buckets: dict = {}
    scalars: dict = {}

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        head = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if head:
            title = head.group(1).strip()
            low = title.lower()
            if low.startswith("refresher"):
                section = "refresher"
                _, _, prev = title.partition(":")
                if prev.strip():
                    scalars["previousTopic"] = prev.strip()
            elif low.startswith("concept"):
                section = "concept"
            elif low.startswith("explore"):
                section = "explore"
            elif low.startswith("challenge"):
                section = "challenge"
            elif low.startswith("material"):
                section = "materials"
            elif low.startswith("level"):
                section = "levelSet"
            else:
                section = None
                warnings.append(f"unrecognised section heading: {title!r}")
            buckets.setdefault(section, [])
            continue

        kv = re.match(r"^([A-Za-z][A-Za-z -]{0,24}):\s*(.+)$", stripped)
        if kv and not stripped.startswith(("-", "*", "+")):
            key = kv.group(1).strip().lower().replace(" ", "-")
            value = kv.group(2).strip()
            if key in ("planning-note", "planningnote"):
                scalars["planningNote"] = value
            elif key == "scenario":
                scalars["scenario"] = value
            elif key in ("sketch", "image-focus", "imagefocus"):
                scalars["imageFocus"] = value
            elif key == "activity":
                scalars["activity"] = value
            elif key in ("extend", "extend-prompt", "extendprompt"):
                scalars["extendPrompt"] = value
            else:
                warnings.append(f"unrecognised key: {key!r}")
            continue

        b = _bullet(line)
        if b is not None and section:
            buckets.setdefault(section, []).append(b)
        elif b is not None:
            warnings.append("bullet outside any section — dropped")

    def bullets(name: str) -> list:
        return buckets.get(name, []) or []

    if scalars.get("planningNote"):
        lesson["planningNote"] = scalars["planningNote"]

    if bullets("refresher"):
        lesson["previousTopicRefresher"] = {
            "previousTopic": scalars.get("previousTopic") or "",
            "recap": bullets("refresher"),
        }
    else:
        lesson["previousTopicRefresher"] = None

    lesson["concept"] = bullets("concept")
    lesson["explore"] = {"points": bullets("explore")}
    if scalars.get("scenario"):
        lesson["explore"]["scenario"] = scalars["scenario"]
    if scalars.get("imageFocus"):
        lesson["explore"]["imageFocus"] = scalars["imageFocus"]

    lesson["challenge"] = {"points": bullets("challenge")}
    if scalars.get("activity"):
        lesson["challenge"]["activity"] = scalars["activity"]

    lesson["materialsUsed"] = [b["text"] for b in bullets("materials")]

    lesson["levelSet"] = {"points": bullets("levelSet")}
    if scalars.get("extendPrompt"):
        lesson["levelSet"]["extendPrompt"] = scalars["extendPrompt"]

    return lesson, warnings


# ── Fidelity ──────────────────────────────────────────────────────────────────

def _dig(obj: dict, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def field_report(lesson: dict) -> dict:
    present = {}
    for f in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        v = _dig(lesson or {}, f)
        present[f] = bool(v) if not isinstance(v, (list, str)) else len(v) > 0
    return present


def missing_required(lesson: dict) -> list:
    rep = field_report(lesson)
    return [f for f in REQUIRED_FIELDS if not rep.get(f)]


def prose_chars(lesson: dict) -> int:
    """Total length of every teacher-facing string, ignoring keys and structure.

    This is the metric that separates "the cheap format under-generated" from
    "the expensive format spent tokens on punctuation". Output tokens alone
    conflate the two: a format can look 3x cheaper either because it said less
    or because it wrapped the same words in less syntax, and those have opposite
    implications.
    """
    total = 0

    def walk(v):
        nonlocal total
        if isinstance(v, str):
            total += len(v)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    walk(lesson or {})
    return total


def bullet_count(lesson: dict) -> int:
    """Structural coverage: did both formats fill the same number of slots?"""
    n = 0
    for path in (("concept",), ("explore", "points"), ("challenge", "points"),
                 ("levelSet", "points")):
        cur = lesson or {}
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        n += len(cur or [])
    ref = (lesson or {}).get("previousTopicRefresher") or {}
    n += len(ref.get("recap") or [])
    return n


# ── One run ───────────────────────────────────────────────────────────────────

@dataclass
class Run:
    fmt: str
    repeat: int
    parsed_first_try: bool = False
    repair_calls: int = 0
    initial_issues: list = field(default_factory=list)
    remaining_issues: list = field(default_factory=list)
    parser_warnings: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    raw: str = ""
    lesson: dict = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.lesson) and not self.missing

    def summary(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("raw", "lesson")}
        d["ok"] = self.ok
        d["raw_chars"] = len(self.raw)
        d["fields"] = field_report(self.lesson)
        return d


def _usage_wrapped_post(captured: dict):
    import requests
    real = requests.post

    def spy(url, **kwargs):
        body = kwargs.get("json") or {}
        if isinstance(body, dict) and "messages" in body:
            body.setdefault("usage", {})["include"] = True
            captured["calls"] += 1
        resp = real(url, **kwargs)
        try:
            u = (resp.json() or {}).get("usage") or {}
            captured["in"] += int(u.get("prompt_tokens") or 0)
            captured["out"] += int(u.get("completion_tokens") or 0)
            if u.get("cost") is not None:
                captured["cost"] += float(u["cost"])
        except Exception:
            pass
        return resp

    return real, spy


def run_once(fmt: str, repeat: int, args, profile) -> Run:
    import requests

    run = Run(fmt=fmt, repeat=repeat)
    system_prompt, json_user = tpm.build_v2_prompts(
        args.topic, args.subject, args.grade, args.subtopic, args.class_size, profile,
        weak_topics=tpm._split(args.weak_topics),
        context_note=args.context_note,
        previous_topic=args.previous_topic,
        avoid_activities=tpm._split(args.avoid),
    )
    user_prompt = json_user if fmt == "json" else to_markdown_prompt(json_user)

    captured = {"in": 0, "out": 0, "cost": 0.0, "calls": 0}
    real_post, spy = _usage_wrapped_post(captured)
    requests.post = spy
    started = time.time()
    try:
        raw = tpm.call_openrouter_text(system_prompt, user_prompt)
        run.raw = raw

        if fmt == "json":
            try:
                lesson = tpm._extract_json(raw)
                run.parsed_first_try = bool(lesson)
            except Exception as exc:
                run.parser_warnings.append(f"JSON parse failed: {type(exc).__name__}: {exc}")
                lesson = {}
            if not lesson:
                # Exactly what the production route does next: a second LLM call
                # whose only job is to repair syntax.
                run.repair_calls += 1
                try:
                    lesson = tpm._extract_json(tpm.call_openrouter_text(
                        "You are given text that is meant to be a single JSON object but has syntax "
                        "errors. Return the SAME content as valid, parseable JSON — fix only the "
                        "syntax. Do not change, add, or remove any actual content. Return ONLY JSON.",
                        raw,
                    ))
                except Exception as exc:
                    run.parser_warnings.append(f"JSON repair failed: {exc}")
                    lesson = {}
        else:
            lesson, warnings = parse_markdown_lesson(raw)
            run.parser_warnings = warnings
            run.parsed_first_try = bool(lesson.get("concept") or lesson.get("explore", {}).get("points"))

        resources = profile.resources if profile else []
        avoid = tpm._split(args.avoid)
        lesson = tpm.sanitize_lesson(lesson, resources)
        run.initial_issues = tpm.validate_v2_lesson(lesson, resources, avoid)
        run.remaining_issues = run.initial_issues

        if run.initial_issues:
            run.repair_calls += 1
            lesson = tpm.sanitize_lesson(tpm.repair_lesson(lesson, run.initial_issues), resources)
            run.remaining_issues = tpm.validate_v2_lesson(lesson, resources, avoid)

        run.lesson = lesson
        run.missing = missing_required(lesson)
    except Exception as exc:
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        requests.post = real_post

    run.elapsed_s = round(time.time() - started, 1)
    run.tokens_in = captured["in"]
    run.tokens_out = captured["out"]
    run.cost_usd = captured["cost"]
    return run


# ── Dry run ───────────────────────────────────────────────────────────────────

DRY_JSON = json.dumps({
    "planningNote": "This teacher leans on stories, so Explore is a market scene.",
    "previousTopicRefresher": {"previousTopic": "Whole numbers",
                               "recap": [{"text": "We counted whole baskets last time."}]},
    "concept": [{"text": "A fraction names equal parts of one whole."},
                {"text": "The bottom number says how many equal parts."}],
    "explore": {"scenario": "Amma buys one big watermelon at the Sunday market and four children want a fair share.",
                "points": [{"text": "Fold a paper watermelon into four equal parts.",
                            "detail": "Ask: is every part the same size? How do you know?"}],
                "imageFocus": "a circle folded into four equal parts"},
    "challenge": {"activity": "Market stall role-play",
                  "points": [{"text": "Two children sell half-slices, two buy them."}]},
    "materialsUsed": ["chalk", "paper"],
    "levelSet": {"points": [{"text": "Back to the watermelon — show one half with your hands."}],
                 "extendPrompt": "Where else at home do you cut something into equal parts?"},
}, ensure_ascii=False)

DRY_MARKDOWN = """planning-note: This teacher leans on stories, so Explore is a market scene.

## Refresher: Whole numbers
- We counted whole baskets last time.

## Concept
- A fraction names equal parts of one whole.
- The bottom number says how many equal parts.

## Explore
scenario: Amma buys one big watermelon at the Sunday market and four children want a fair share.
sketch: a circle folded into four equal parts
- Fold a paper watermelon into four equal parts. :: Ask: is every part the same size? How do you know?

## Challenge
activity: Market stall role-play
- Two children sell half-slices, two buy them.

## Materials
- chalk
- paper

## Level set
- Back to the watermelon — show one half with your hands.
extend: Where else at home do you cut something into equal parts?
"""


def dry_runs(args, profile) -> list:
    out = []
    for fmt in FORMATS:
        for repeat in range(1, args.repeats + 1):
            run = Run(fmt=fmt, repeat=repeat)
            if fmt == "json":
                run.raw = DRY_JSON
                run.lesson = json.loads(DRY_JSON)
                run.parsed_first_try = True
                run.tokens_out = 520
                run.cost_usd = 0.0014
                run.elapsed_s = 7.0
            else:
                run.raw = DRY_MARKDOWN
                run.lesson, run.parser_warnings = parse_markdown_lesson(DRY_MARKDOWN)
                run.parsed_first_try = True
                run.tokens_out = 330
                run.cost_usd = 0.0009
                run.elapsed_s = 5.0
            resources = profile.resources if profile else []
            run.initial_issues = tpm.validate_v2_lesson(run.lesson, resources, None)
            run.remaining_issues = run.initial_issues
            run.missing = missing_required(run.lesson)
            out.append(run)
    return out


# ── Report ────────────────────────────────────────────────────────────────────

def render_lesson(lesson: dict) -> str:
    def bl(bs, indent="    "):
        out = []
        for b in bs or []:
            out.append(f"{indent}- {b.get('text','')}")
            if b.get("detail"):
                out.append(f"{indent}    ↳ {b['detail']}")
        return out

    L = []
    if lesson.get("planningNote"):
        L.append(f"  [planning] {lesson['planningNote']}")
    ref = lesson.get("previousTopicRefresher")
    if ref:
        L.append(f"  REFRESHER — {ref.get('previousTopic','')}")
        L += bl(ref.get("recap"))
    L.append("  CONCEPT")
    L += bl(lesson.get("concept"))
    ex = lesson.get("explore") or {}
    L.append("  EXPLORE")
    if ex.get("scenario"):
        L.append(f"    scenario: {ex['scenario']}")
    if ex.get("imageFocus"):
        L.append(f"    sketch:   {ex['imageFocus']}")
    L += bl(ex.get("points"))
    ch = lesson.get("challenge") or {}
    L.append(f"  CHALLENGE — {ch.get('activity','(none)')}")
    L += bl(ch.get("points"))
    L.append(f"  MATERIALS: {', '.join(lesson.get('materialsUsed') or []) or '(none)'}")
    ls = lesson.get("levelSet") or {}
    L.append("  LEVEL SET")
    L += bl(ls.get("points"))
    if ls.get("extendPrompt"):
        L.append(f"    extend: {ls['extendPrompt']}")
    return "\n".join(L)


def build_report(runs: list, args, profile) -> tuple:
    by = {f: [r for r in runs if r.fmt == f] for f in FORMATS}
    lines, w = [], None
    out: list = []
    w = out.append

    w("# Lesson plan: JSON vs Markdown")
    w("")
    w(f"- Topic: **{args.topic}** · Grade {args.grade} {args.subject}"
      + (f" · subtopic: {args.subtopic}" if args.subtopic else ""))
    w(f"- Profile: `{args.profile}`")
    if args.previous_topic:
        w(f"- Previous topic: {args.previous_topic}")
    if args.avoid:
        w(f"- Avoid activities: {args.avoid}")
    w(f"- Repeats per format: {args.repeats}")
    w(f"- Model: `{os.environ.get('OPENROUTER_MODEL', 'google/gemini-2.5-flash')}`")
    w("")
    w("Both arms share the same system prompt, profile, activity bank and rules — only the "
      "output-shape section differs — and both are scored by the same "
      "`validate_v2_lesson`.")
    w("")

    def agg(fmt, fn):
        vals = [fn(r) for r in by[fmt]]
        vals = [v for v in vals if v is not None]
        if not vals:
            return "—"
        if all(isinstance(v, bool) for v in vals):
            return f"{sum(vals)}/{len(vals)}"
        if len(set(vals)) == 1:
            return f"{vals[0]:g}" if isinstance(vals[0], (int, float)) else str(vals[0])
        # Precision has to follow magnitude: a fixed .1f turned per-lesson costs
        # of ~$0.005 into "0.0", which made the cost row read as free.
        mean = statistics.mean(vals)
        digits = 5 if max(abs(v) for v in vals) < 1 else 1
        return f"{mean:.{digits}f} (min {min(vals):g}, max {max(vals):g})"

    w("## Reliability and cost")
    w("")
    w("| Metric | JSON | Markdown |")
    w("|---|---|---|")
    rows = [
        ("usable lesson", lambda r: r.ok),
        ("parsed first try", lambda r: r.parsed_first_try),
        ("repair LLM calls", lambda r: r.repair_calls),
        ("validator issues (initial)", lambda r: len(r.initial_issues)),
        ("validator issues (after repair)", lambda r: len(r.remaining_issues)),
        ("missing required fields", lambda r: len(r.missing)),
        ("parser warnings", lambda r: len(r.parser_warnings)),
        ("bullets filled", lambda r: bullet_count(r.lesson) or None),
        ("prose chars (content)", lambda r: prose_chars(r.lesson) or None),
        ("output tokens", lambda r: r.tokens_out or None),
        ("prose chars per token", lambda r: round(prose_chars(r.lesson) / r.tokens_out, 2)
            if r.tokens_out else None),
        ("latency s", lambda r: r.elapsed_s or None),
        ("cost USD", lambda r: round(r.cost_usd, 5) or None),
    ]
    for label, fn in rows:
        w(f"| {label} | {agg('json', fn)} | {agg('markdown', fn)} |")
    w("")

    # ── Content vs syntax ─────────────────────────────────────────────────────
    def mean_of(fmt, fn):
        vals = [fn(r) for r in by[fmt] if fn(r)]
        return statistics.mean(vals) if vals else 0

    j_prose, m_prose = mean_of("json", lambda r: prose_chars(r.lesson)), mean_of("markdown", lambda r: prose_chars(r.lesson))
    j_tok, m_tok = mean_of("json", lambda r: r.tokens_out), mean_of("markdown", lambda r: r.tokens_out)
    if j_prose and j_tok and m_tok:
        w("### Is the cheaper format saying less, or just spending fewer tokens on syntax?")
        w("")
        w(f"- Content delivered: Markdown carries **{m_prose/j_prose*100:.0f}%** of JSON's prose "
          f"({m_prose:.0f} vs {j_prose:.0f} chars).")
        w(f"- Tokens spent: Markdown uses **{m_tok/j_tok*100:.0f}%** of JSON's output tokens "
          f"({m_tok:.0f} vs {j_tok:.0f}).")
        gap = (m_prose / j_prose) / (m_tok / j_tok) if m_tok else 0
        if gap > 1.5:
            w(f"- The gap between those two is the answer: JSON spends roughly **{gap:.1f}x more "
              "tokens per character of actual content**. Quoted keys, braces and escaped strings "
              "tokenize far worse than prose does.")
        elif gap < 0.9:
            w("- Markdown is delivering proportionally LESS content than it saves in tokens — it is "
              "under-generating, not compressing. Treat the cost saving as illusory and fix the "
              "prompt's cardinality hints before comparing again.")
        w("")

    jc = sum(r.cost_usd for r in by["json"]) / max(1, len(by["json"]))
    mc = sum(r.cost_usd for r in by["markdown"]) / max(1, len(by["markdown"]))
    if jc:
        w(f"Markdown is **{(1 - mc/jc)*100:.0f}% cheaper** per lesson "
          f"(${mc:.5f} vs ${jc:.5f}).")
        w("")

    # ── Field fidelity ────────────────────────────────────────────────────────
    w("## Field fidelity")
    w("")
    w("Whether each field the UI renders actually arrived. A format that parses but "
      "drops `explore.scenario` renders a blank card.")
    w("")
    w("| Field | JSON | Markdown |")
    w("|---|---|---|")
    for f in REQUIRED_FIELDS + OPTIONAL_FIELDS:
        def hit(fmt):
            got = sum(1 for r in by[fmt] if field_report(r.lesson).get(f))
            return f"{got}/{len(by[fmt])}"
        tag = "" if f in REQUIRED_FIELDS else " *(optional)*"
        w(f"| `{f}`{tag} | {hit('json')} | {hit('markdown')} |")
    w("")

    # ── Issues seen ───────────────────────────────────────────────────────────
    for fmt in FORMATS:
        issues = sorted({i for r in by[fmt] for i in r.remaining_issues})
        warns = sorted({x for r in by[fmt] for x in r.parser_warnings})
        errs = [r.error for r in by[fmt] if r.error]
        if issues or warns or errs:
            w(f"### {fmt} — problems")
            w("")
            for i in issues:
                w(f"- validator: {i}")
            for x in warns:
                w(f"- parser: {x}")
            for e in errs:
                w(f"- error: {e}")
            w("")

    # ── The actual lessons ────────────────────────────────────────────────────
    w("## The lessons — read these, the harness can't judge them")
    w("")
    for fmt in FORMATS:
        first = next((r for r in by[fmt] if r.lesson), None)
        if not first:
            w(f"### {fmt}: nothing usable produced")
            w("")
            continue
        w(f"### From {fmt}")
        w("")
        w("```")
        w(render_lesson(first.lesson))
        w("```")
        w("")
    w("### Raw responses")
    w("")
    for fmt in FORMATS:
        first = next((r for r in by[fmt] if r.raw), None)
        if not first:
            continue
        w(f"<details><summary>{fmt} — raw ({len(first.raw)} chars)</summary>")
        w("")
        w("```")
        w(first.raw[:3000])
        w("```")
        w("")
        w("</details>")
        w("")

    payload = {
        "params": {k: v for k, v in vars(args).items()},
        "profile": args.profile,
        "cost_per_lesson": {"json": jc, "markdown": mc},
        "runs": [r.summary() for r in runs],
        "lessons": {f"{r.fmt}_{r.repeat}": r.lesson for r in runs},
        "raw": {f"{r.fmt}_{r.repeat}": r.raw for r in runs},
    }
    return "\n".join(out), payload


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Compare JSON vs Markdown lesson-plan generation.")
    p.add_argument("--topic", default="Fractions")
    p.add_argument("--subject", default="Mathematics")
    p.add_argument("--grade", default="5")
    p.add_argument("--subtopic")
    p.add_argument("--class-size", type=int)
    p.add_argument("--profile", default="storyteller")
    p.add_argument("--previous-topic")
    p.add_argument("--weak-topics")
    p.add_argument("--avoid")
    p.add_argument("--context-note")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--list-profiles", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out-dir", default=str(_HERE / "lesson_format_output"))
    args = p.parse_args()

    if args.list_profiles:
        tpm.list_profiles()
        return 0

    if args.profile not in tpm.PRESET_PROFILES:
        raise SystemExit(
            f"Unknown profile {args.profile!r}. Available: "
            f"{', '.join(sorted(tpm.PRESET_PROFILES))}"
        )
    profile = tpm.PRESET_PROFILES[args.profile]

    if args.dry_run:
        print("Dry run — canned responses, no API calls.\n")
        runs = dry_runs(args, profile)
    else:
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise SystemExit("OPENROUTER_API_KEY not set")
        runs = []
        total = len(FORMATS) * args.repeats
        n = 0
        for fmt in FORMATS:
            for repeat in range(1, args.repeats + 1):
                n += 1
                print(f"[{n}/{total}] {fmt} run {repeat} …", flush=True)
                run = run_once(fmt, repeat, args, profile)
                status = "ok" if run.ok else f"PROBLEM ({run.error or run.missing})"
                print(f"    {status} · {run.elapsed_s}s · out {run.tokens_out:,} tok "
                      f"· ${run.cost_usd:.5f} · {run.repair_calls} repair call(s) "
                      f"· {len(run.remaining_issues)} issue(s)")
                runs.append(run)

    report, payload = build_report(runs, args, profile)
    print()
    print(report)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", f"{args.grade}_{args.subject}_{args.topic}".lower()).strip("_")
    (out_dir / f"{slug}.md").write_text(report, encoding="utf-8")
    (out_dir / f"{slug}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWritten: {out_dir / (slug + '.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
