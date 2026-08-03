"""
A/B extraction quality between model tiers, so EXTRACTION_MODEL_TIER=simple can be
adopted on evidence instead of hope.

Routing the cheap tier at the four trivial calls saves ~4.7% per book. Routing it
at per-chapter content extraction saves ~70% — but that is the call that reads
Telugu and Hindi textbook pages, and a cheap model that transliterates instead of
transcribing produces an extraction that looks complete and is quietly wrong.
This harness runs the SAME pages through both tiers and reports what differs.

What it decides automatically
-----------------------------
  script preservation   Does the output keep the book's script, or did the model
                        transliterate to Latin? An automatic fail — no human needed.
  coverage              topics / subtopics / exercises / sidebars found per run.
  page agreement        Do the tiers agree which pages a topic spans?
  format compliance     Parser warning count, invalid skill_type / exercise_type.
                        A proxy for "followed the output contract".
  stability             Run-to-run variance within one tier, so a difference
                        between tiers isn't confused with the model's own noise.
  cost / latency        Real token usage from the API response.

What it cannot decide
---------------------
Whether "సంఖ్యలు" is what is actually printed on page 24. The harness aligns topics
across tiers and emits a REVIEW SHEET of just the disagreements, so a reader of the
script checks a dozen specific pairs instead of proof-reading two whole extractions.

Usage
-----
Compare one chapter, two runs per tier (4 calls, roughly $0.05):
    python extraction_quality_compare.py --pdf book.pdf --pages 12-18 --chapter 2

More confidence, more spend:
    python extraction_quality_compare.py --pdf book.pdf --pages 12-18 --repeats 3

Several chapters:
    python extraction_quality_compare.py --pdf book.pdf --chapters "2:12-18,3:19-26"

Exercise the harness without spending anything:
    python extraction_quality_compare.py --dry-run

Writes report.md + report.json under --out-dir.
"""

import argparse
import difflib
import json
import os
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
sys.path.insert(0, str(_BACKEND))

TIERS = ("standard", "simple")

# Unicode blocks for the scripts this app's textbooks actually use.
SCRIPT_BLOCKS = {
    "Devanagari": (0x0900, 0x097F),
    "Bengali":    (0x0980, 0x09FF),
    "Gurmukhi":   (0x0A00, 0x0A7F),
    "Gujarati":   (0x0A80, 0x0AFF),
    "Odia":       (0x0B00, 0x0B7F),
    "Tamil":      (0x0B80, 0x0BFF),
    "Telugu":     (0x0C00, 0x0C7F),
    "Kannada":    (0x0C80, 0x0CFF),
    "Malayalam":  (0x0D00, 0x0D7F),
}


# ── Metrics (pure — unit-tested in tests/test_extraction_quality_metrics.py) ───

def script_histogram(text: str) -> dict:
    """Count characters per named script block, plus Latin. Ignores digits,
    punctuation and whitespace, which are shared across scripts and would
    otherwise swamp the signal."""
    counts: dict = {}
    for ch in text or "":
        if ch.isspace() or ch.isdigit():
            continue
        if unicodedata.category(ch).startswith("P"):
            continue
        code = ord(ch)
        if 0x41 <= code <= 0x7A and ch.isalpha():
            counts["Latin"] = counts.get("Latin", 0) + 1
            continue
        for name, (lo, hi) in SCRIPT_BLOCKS.items():
            if lo <= code <= hi:
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


def dominant_script(text: str) -> Optional[str]:
    hist = script_histogram(text)
    return max(hist, key=hist.get) if hist else None


def names_text(ontology: dict) -> str:
    """Just the transcribed-verbatim fields. Summaries are written in English by
    instruction, so including them would mask a transliterated topic name."""
    e = (ontology or {}).get("entities", {}) or {}
    parts = [t.get("name", "") for t in e.get("topics", [])]
    parts += [s.get("name", "") for s in e.get("subtopics", [])]
    parts += [c.get("title", "") for c in e.get("chapters", [])]
    return " ".join(p for p in parts if p)


def coverage(ontology: dict) -> dict:
    e = (ontology or {}).get("entities", {}) or {}
    return {k: len(e.get(k, []) or []) for k in
            ("chapters", "topics", "subtopics", "exercises", "sidebars")}


def type_validity(ontology: dict) -> dict:
    """How often the model produced a type token from the allowed vocabulary.

    The parser reclassifies invalid ones, so this measures contract-following
    rather than data loss — and that is exactly the signal that predicts whether
    a cheaper model can be trusted with the format.
    """
    from app.lib.markdown_ontology import VALID_SKILL_TYPES, VALID_EXERCISE_TYPES

    e = (ontology or {}).get("entities", {}) or {}
    skills = [s.get("skill_type") for s in e.get("subtopics", []) or []]
    exs = [x.get("exercise_type") for x in e.get("exercises", []) or []]
    generic_skill = sum(1 for s in skills if s == "general_skill")
    generic_ex = sum(1 for x in exs if x == "general_activity")
    return {
        "subtopics": len(skills),
        "valid_skill_types": sum(1 for s in skills if s in VALID_SKILL_TYPES),
        # A pile of generic types usually means the parser fell back to keyword
        # inference because the model omitted them, not that everything is generic.
        "generic_skill_types": generic_skill,
        "exercises": len(exs),
        "valid_exercise_types": sum(1 for x in exs if x in VALID_EXERCISE_TYPES),
        "generic_exercise_types": generic_ex,
    }


def page_spans(ontology: dict) -> dict:
    e = (ontology or {}).get("entities", {}) or {}
    return {
        (t.get("name") or "").strip().lower(): (t.get("page_start"), t.get("page_end"))
        for t in e.get("topics", []) or []
    }


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def align_topics(a: dict, b: dict, threshold: float = 0.72) -> dict:
    """Greedy best-match align topic names between two runs.

    Fuzzy rather than exact because the interesting failure is a *near* miss — a
    dropped word, a transliteration, a mis-read character. Exact matching would
    file those as "only in A" and hide that they are the same topic read badly.
    """
    a_names = [(t.get("name") or "") for t in (a.get("entities", {}).get("topics") or [])]
    b_names = [(t.get("name") or "") for t in (b.get("entities", {}).get("topics") or [])]

    remaining = list(enumerate(b_names))
    matched: list = []
    only_a: list = []

    for an in a_names:
        best_idx, best_ratio = None, 0.0
        for pos, (bi, bn) in enumerate(remaining):
            ratio = difflib.SequenceMatcher(None, _norm(an), _norm(bn)).ratio()
            if ratio > best_ratio:
                best_idx, best_ratio = pos, ratio
        if best_idx is not None and best_ratio >= threshold:
            _, bn = remaining.pop(best_idx)
            matched.append({"standard": an, "simple": bn,
                            "similarity": round(best_ratio, 3),
                            "identical": _norm(an) == _norm(bn)})
        else:
            only_a.append(an)

    return {
        "matched": matched,
        "only_in_standard": only_a,
        "only_in_simple": [bn for _, bn in remaining],
        "identical_count": sum(1 for m in matched if m["identical"]),
    }


# ── One measured run ──────────────────────────────────────────────────────────

@dataclass
class Run:
    tier: str
    repeat: int
    chapter: int
    ok: bool = True
    error: str = ""
    elapsed_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    models_used: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    markdown: str = ""
    ontology: dict = field(default_factory=dict)

    def summary(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("ontology", "markdown")}
        d["coverage"] = coverage(self.ontology)
        d["scripts"] = script_histogram(names_text(self.ontology))
        d["dominant_script"] = dominant_script(names_text(self.ontology))
        d["types"] = type_validity(self.ontology)
        d["markdown_chars"] = len(self.markdown)
        d["warning_count"] = len(self.warnings)
        return d


def measure_run(ve, doc_path: str, chapter: int, pages: list, language: str,
                tier: str, repeat: int) -> Run:
    """Run one real extraction at one tier, capturing usage off the HTTP response.

    call_gemini discards the usage block, so the post is wrapped here rather than
    changing production code to serve a measurement script.
    """
    import fitz
    import requests

    run = Run(tier=tier, repeat=repeat, chapter=chapter)
    captured = {"in": 0, "out": 0, "cost": 0.0, "models": []}
    real_post = requests.post

    def spy_post(url, **kwargs):
        body = kwargs.get("json") or {}
        if isinstance(body, dict) and "messages" in body:
            body.setdefault("usage", {})["include"] = True
            captured["models"].append(body.get("model"))
        resp = real_post(url, **kwargs)
        try:
            usage = (resp.json() or {}).get("usage") or {}
            captured["in"] += int(usage.get("prompt_tokens") or 0)
            captured["out"] += int(usage.get("completion_tokens") or 0)
            if usage.get("cost") is not None:
                captured["cost"] += float(usage["cost"])
        except Exception:
            pass
        return resp

    doc = fitz.open(doc_path)
    started = time.time()
    try:
        ve.requests.post = spy_post
        ve.EXTRACTION_MODEL_TIER = tier

        warnings_sink: list = []
        markdown_sink: list = []
        ontology = ve.extract_chapter_batched(
            doc,
            pages=pages,
            chap_num=chapter,
            chap_title=f"Chapter {chapter}",
            language=language,
            global_chapter_list=f"  {chapter}. Chapter {chapter}",
            warnings_sink=warnings_sink,
            markdown_sink=markdown_sink,
        )
        run.ontology = ontology
        run.warnings = warnings_sink
        run.markdown = "\n\n".join(m for m in markdown_sink if m)
        run.ok = bool((ontology.get("entities", {}).get("topics")))
        if not run.ok:
            run.error = "no topics extracted"
    except Exception as exc:
        run.ok = False
        run.error = f"{type(exc).__name__}: {exc}"
    finally:
        ve.requests.post = real_post
        doc.close()

    run.elapsed_s = round(time.time() - started, 1)
    run.tokens_in = captured["in"]
    run.tokens_out = captured["out"]
    run.cost_usd = captured["cost"]
    run.models_used = sorted(set(m for m in captured["models"] if m))
    return run


# ── Dry run ───────────────────────────────────────────────────────────────────

DRY_STANDARD = """---
chapter: 2
title: సంఖ్యలు మన చుట్టూ
pages: 12-18
---

## సంఖ్యలు లెక్కించడం
---
pages: 12-14
---

Students count objects up to twenty and match each count to its numeral.

### ఒకటి రాయడం `writing_skill` p12
Trace the numeral while saying its name aloud.

#### Exercises
- `counting_activity` p13 — Count the mangoes in the basket and write the numeral.
- `writing_practice` p14 — Trace each numeral three times.

## ఆకారాలు
---
pages: 15-18
prerequisites: [సంఖ్యలు లెక్కించడం]
---

Students identify circles and squares in everyday objects.

### వృత్తం `recognition_skill` p15
Find round objects in the classroom.

#### Exercises
- `matching_exercise` p16 — Match each object to its shape.
"""

# Deliberately degraded the way a weaker vision model actually fails: the script
# is transliterated to Latin, a topic is missed, types are dropped, and the page
# numbers drift.
DRY_SIMPLE = """---
chapter: 2
title: Sankhyalu Mana Chuttu
pages: 12-18
---

## Sankhyalu Lekkinchadam
---
pages: 12-15
---

Students count objects and write numbers.

### Okati Rayadam
Trace the numeral.

#### Exercises
- p13 — Count the mangoes and write the number.
"""


def dry_runs(ve) -> list:
    from app.lib.markdown_ontology import parse_markdown_ontology

    out: list = []
    scripted = {"standard": DRY_STANDARD, "simple": DRY_SIMPLE}
    for tier in TIERS:
        for repeat in range(1, 3):
            parsed = parse_markdown_ontology(
                scripted[tier], chapter_number=2, chapter_title="Chapter 2",
            )
            run = Run(tier=tier, repeat=repeat, chapter=2, ok=parsed.ok)
            run.ontology = parsed.ontology
            run.warnings = parsed.warnings
            run.markdown = scripted[tier]
            run.elapsed_s = 12.0 if tier == "standard" else 5.0
            run.tokens_in = 18576
            run.tokens_out = 1577 if tier == "standard" else 780
            run.cost_usd = 0.0095 if tier == "standard" else 0.0022
            run.models_used = [ve.model_for_tier(tier)]
            out.append(run)
    return out


# ── Report ────────────────────────────────────────────────────────────────────

def spread(values: list) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "—"
    if len(vals) == 1:
        return f"{vals[0]:g}"
    return f"{statistics.mean(vals):.1f} (min {min(vals):g}, max {max(vals):g})"


def build_report(runs: list, args) -> tuple:
    by_tier = {t: [r for r in runs if r.tier == t] for t in TIERS}
    lines: list = []
    w = lines.append

    w("# Extraction quality: standard vs simple tier")
    w("")
    w(f"- PDF: `{Path(args.pdf).name if args.pdf else '(dry run)'}`")
    w(f"- Repeats per tier: {args.repeats}")
    for tier in TIERS:
        models = sorted({m for r in by_tier[tier] for m in r.models_used})
        w(f"- `{tier}` → {', '.join(models) or 'n/a'}")
    w("")

    # ── Verdict first ─────────────────────────────────────────────────────────
    blockers: list = []
    std_ok = [r for r in by_tier["standard"] if r.ok]
    sim_ok = [r for r in by_tier["simple"] if r.ok]

    std_script = dominant_script(" ".join(names_text(r.ontology) for r in std_ok))
    sim_script = dominant_script(" ".join(names_text(r.ontology) for r in sim_ok))
    if std_script and sim_script and std_script != sim_script:
        blockers.append(
            f"SCRIPT MISMATCH — standard transcribes in {std_script}, simple in {sim_script}. "
            "The cheap model is transliterating, not reading. Automatic disqualification."
        )
    if not sim_ok:
        blockers.append("The simple tier produced no usable extraction in any run.")

    std_topics = [coverage(r.ontology)["topics"] for r in std_ok]
    sim_topics = [coverage(r.ontology)["topics"] for r in sim_ok]
    if std_topics and sim_topics and statistics.mean(sim_topics) < 0.8 * statistics.mean(std_topics):
        blockers.append(
            f"COVERAGE LOSS — simple found {statistics.mean(sim_topics):.1f} topics on average "
            f"vs {statistics.mean(std_topics):.1f}. It is missing content, not just describing it differently."
        )

    std_cost = sum(r.cost_usd for r in by_tier["standard"]) / max(1, len(by_tier["standard"]))
    sim_cost = sum(r.cost_usd for r in by_tier["simple"]) / max(1, len(by_tier["simple"]))
    saving = (1 - sim_cost / std_cost) * 100 if std_cost else 0

    w("## Verdict")
    w("")
    if blockers:
        w(f"**Do not switch.** The simple tier is {saving:.0f}% cheaper per chapter and:")
        w("")
        for b in blockers:
            w(f"- {b}")
    else:
        w(f"**No automatic disqualifier.** The simple tier is {saving:.0f}% cheaper per chapter "
          "and matched on script, coverage and format compliance.")
        w("")
        w("This is necessary but not sufficient: nothing here verifies that a transcribed "
          "name matches what is printed on the page. Work the review sheet below before "
          "switching.")
    w("")

    # ── Per-tier metrics ──────────────────────────────────────────────────────
    w("## Measurements")
    w("")
    w("| Metric | standard | simple |")
    w("|---|---|---|")

    def col(fn):
        return [f"{fn(by_tier[t])}" for t in TIERS]

    rows = [
        ("runs succeeded", lambda rs: f"{sum(1 for r in rs if r.ok)}/{len(rs)}"),
        ("topics", lambda rs: spread([coverage(r.ontology)["topics"] for r in rs if r.ok])),
        ("subtopics", lambda rs: spread([coverage(r.ontology)["subtopics"] for r in rs if r.ok])),
        ("exercises", lambda rs: spread([coverage(r.ontology)["exercises"] for r in rs if r.ok])),
        ("sidebars", lambda rs: spread([coverage(r.ontology)["sidebars"] for r in rs if r.ok])),
        ("dominant script", lambda rs: dominant_script(
            " ".join(names_text(r.ontology) for r in rs if r.ok)) or "—"),
        ("parser warnings", lambda rs: spread([len(r.warnings) for r in rs])),
        ("generic skill_types", lambda rs: spread(
            [type_validity(r.ontology)["generic_skill_types"] for r in rs if r.ok])),
        ("generic exercise_types", lambda rs: spread(
            [type_validity(r.ontology)["generic_exercise_types"] for r in rs if r.ok])),
        ("tokens out", lambda rs: spread([r.tokens_out for r in rs if r.ok])),
        ("latency s", lambda rs: spread([r.elapsed_s for r in rs if r.ok])),
        ("cost USD / chapter", lambda rs: f"${(sum(r.cost_usd for r in rs)/max(1,len(rs))):.4f}"),
    ]
    for label, fn in rows:
        a, b = col(fn)
        w(f"| {label} | {a} | {b} |")
    w("")

    # ── Stability within a tier ───────────────────────────────────────────────
    if args.repeats > 1:
        w("### Run-to-run stability")
        w("")
        w("A difference between tiers only means something if it exceeds each tier's own noise.")
        w("")
        for tier in TIERS:
            counts = [coverage(r.ontology)["topics"] for r in by_tier[tier] if r.ok]
            if len(counts) > 1:
                w(f"- `{tier}`: topic counts across runs = {counts} "
                  f"(spread {max(counts) - min(counts)})")
        w("")

    # ── Review sheet ──────────────────────────────────────────────────────────
    w("## Review sheet — needs a reader of the script")
    w("")
    if not (std_ok and sim_ok):
        w("Not enough successful runs on both tiers to align topics.")
    else:
        alignment = align_topics(std_ok[0].ontology, sim_ok[0].ontology)
        if not alignment["matched"]:
            # Happens when the cheap model transliterates: the two names share no
            # characters, so nothing aligns at all. Say that, rather than reporting
            # "0 of 0 identical", which reads like agreement.
            w("**No topic names could be aligned between the two tiers at all.** They share "
              "too few characters to be treated as the same topic — which is itself the "
              "finding, usually transliteration or wholesale different reading.")
            w("")
        else:
            w(f"{alignment['identical_count']} of {len(alignment['matched'])} matched topic names "
              "are character-identical between tiers.")
            w("")
            differing = [m for m in alignment["matched"] if not m["identical"]]
            if differing:
                w("Check these — same topic, different transcription:")
                w("")
                w("| standard | simple | similarity |")
                w("|---|---|---:|")
                for m in differing:
                    w(f"| {m['standard']} | {m['simple']} | {m['similarity']} |")
                w("")
            else:
                w("Every matched name is identical.")
                w("")

        if alignment["only_in_standard"]:
            w("**Only the standard tier found these — check whether they are really on the page:**")
            for n in alignment["only_in_standard"]:
                w(f"- {n}")
            w("")
        if alignment["only_in_simple"]:
            w("**Only the simple tier found these — likely hallucinated or mis-split:**")
            for n in alignment["only_in_simple"]:
                w(f"- {n}")
            w("")

        std_pages = page_spans(std_ok[0].ontology)
        sim_pages = page_spans(sim_ok[0].ontology)
        disputed = [
            (m["standard"], std_pages.get(_norm(m["standard"])), sim_pages.get(_norm(m["simple"])))
            for m in alignment["matched"]
            if std_pages.get(_norm(m["standard"])) != sim_pages.get(_norm(m["simple"]))
        ]
        if disputed:
            w("**Page ranges the tiers disagree on — check the printed page numbers:**")
            w("")
            w("| topic | standard | simple |")
            w("|---|---|---|")
            for name, a, b in disputed:
                w(f"| {name} | {a} | {b} |")
            w("")

    w("## Raw output")
    w("")
    for tier in TIERS:
        first = next((r for r in by_tier[tier] if r.ok), None)
        if not first:
            continue
        w(f"### {tier} — first 1500 chars of Markdown")
        w("")
        w("```markdown")
        w(first.markdown[:1500])
        w("```")
        w("")

    payload = {
        "params": {k: v for k, v in vars(args).items()},
        "blockers": blockers,
        "cost_per_chapter": {"standard": std_cost, "simple": sim_cost},
        "saving_pct": saving,
        "runs": [r.summary() for r in runs],
        "alignment": (align_topics(std_ok[0].ontology, sim_ok[0].ontology)
                      if std_ok and sim_ok else None),
        "markdown": {f"{r.tier}_{r.repeat}": r.markdown for r in runs},
    }
    return "\n".join(lines), payload, blockers


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_chapter_specs(args) -> list:
    if args.chapters:
        out = []
        for part in args.chapters.split(","):
            chap, _, rng = part.strip().partition(":")
            first, _, last = rng.partition("-")
            out.append((int(chap), int(first), int(last or first)))
        return out
    first, _, last = args.pages.partition("-")
    return [(args.chapter, int(first), int(last or first))]


def load_env():
    for candidate in (_BACKEND.parent / "frontend" / ".env.local", _BACKEND / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    p = argparse.ArgumentParser(
        description="A/B extraction quality between model tiers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--pdf", help="textbook PDF to extract from")
    p.add_argument("--pages", default="1-7", help="1-based page range, e.g. 12-18")
    p.add_argument("--chapter", type=int, default=1, help="chapter number for ids")
    p.add_argument("--chapters", help='multiple, as "2:12-18,3:19-26"')
    p.add_argument("--language", default="auto", help='"Telugu", "Hindi", or "auto"')
    p.add_argument("--repeats", type=int, default=2,
                   help="runs per tier — >1 separates model difference from run noise")
    p.add_argument("--dry-run", action="store_true",
                   help="exercise the harness on canned output, no API calls")
    p.add_argument("--out-dir", default=str(_HERE / "extraction_quality_output"))
    args = p.parse_args()

    if not args.dry_run and not args.pdf:
        raise SystemExit("--pdf is required (or use --dry-run)")

    load_env()
    from app.lib import vision_extraction as ve

    original_tier = ve.EXTRACTION_MODEL_TIER
    if ve.EXTRACTION_FORMAT != "markdown":
        print(f"! EXTRACTION_FORMAT is '{ve.EXTRACTION_FORMAT}'. This harness reads the "
              "Markdown path's parser warnings; run with EXTRACTION_FORMAT=markdown.",
              file=sys.stderr)

    try:
        if args.dry_run:
            print("Dry run — canned output, no API calls.\n")
            runs = dry_runs(ve)
        else:
            if not os.environ.get("OPENROUTER_API_KEY"):
                raise SystemExit("OPENROUTER_API_KEY not set")
            language = args.language
            if language == "auto":
                print("Detecting language…")
                language = ve.detect_language_vision(args.pdf)
                print(f"  {language}\n")

            runs = []
            specs = parse_chapter_specs(args)
            total = len(specs) * len(TIERS) * args.repeats
            n = 0
            for chapter, first, last in specs:
                pages = list(range(first - 1, last))
                for tier in TIERS:
                    for repeat in range(1, args.repeats + 1):
                        n += 1
                        print(f"[{n}/{total}] chapter {chapter} pages {first}-{last} "
                              f"· {tier} · run {repeat} …", flush=True)
                        run = measure_run(ve, args.pdf, chapter, pages, language, tier, repeat)
                        status = "ok" if run.ok else f"FAILED ({run.error})"
                        print(f"    {status} · {run.elapsed_s}s · out {run.tokens_out:,} tok "
                              f"· ${run.cost_usd:.4f} · {len(run.warnings)} warning(s)")
                        runs.append(run)
                        if n < total:
                            time.sleep(ve.INTER_CALL_DELAY)
    finally:
        ve.EXTRACTION_MODEL_TIER = original_tier

    report, payload, blockers = build_report(runs, args)
    print()
    print(report)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWritten: {out_dir / 'report.md'}")
    print(f"         {out_dir / 'report.json'}")

    # Non-zero exit on a blocker so this can gate a config change in CI.
    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
