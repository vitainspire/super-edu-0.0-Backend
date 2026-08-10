"""Seeds the Phase C Pedagogy Library pilot: two Grade 1-3 categories (Count,
Observe), Indian-contextualized, following the design doc's "Activity Category
-> Activity Template -> Context" pattern — a handful of reusable templates
each carrying many contexts, rather than one fixed activity per context (see
pedagogy_library.py's module docstring for the two-worlds rationale).

Idempotent: every table this touches is looked up by exact name before being
created, so running this twice does not duplicate anything.

    python -m scripts.seed_pedagogy_library              # seed it
    python -m scripts.seed_pedagogy_library --dry-run     # show what would happen
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.lib.supabase_clients import create_admin_client  # noqa: E402
from app.lib import pedagogy_library as pl  # noqa: E402

# name -> Everyday Indian Context Library category
CONTEXTS = {
    "Mangoes": "NATURE",
    "Tamarind Seeds": "NATURE",
    "Flowers": "NATURE",
    "Leaves": "NATURE",
    "Trees": "NATURE",
    "Pebbles": "NATURE",
    "Fruits": "NATURE",
    "Birds Around School": "NATURE",
    "Bottle Caps": "HOME",
    "School Bags": "SCHOOL",
    "Benches": "SCHOOL",
    "Windows": "SCHOOL",
    "Water Bottles": "SCHOOL",
    "Classroom Objects": "SCHOOL",
    "School Garden": "SCHOOL",
    "Market Pictures": "MARKET",
    "Vegetables": "MARKET",
    "Rangoli": "FESTIVALS",
}

# Each template: (category, name, metadata, [competency names], [context names])
TEMPLATES = [
    (
        "Count", "Count Real Objects",
        dict(
            description="Students count everyday real objects around them, out loud or by writing the number.",
            resource_level=0, duration_min=5, duration_max=10, grouping="individual",
            classroom_type="both", bloom_level="remember", fln_compatible=True,
            assessment_method="observation",
        ),
        ["Count Objects"],
        ["Tamarind Seeds", "Pebbles", "Bottle Caps", "Mangoes", "Flowers",
         "School Bags", "Benches", "Windows", "Trees", "Water Bottles"],
    ),
    (
        "Count", "Compare and Estimate Quantities",
        dict(
            description="Students compare two or more groups of real objects and estimate which "
                         "has more, or guess a total before counting.",
            resource_level=0, duration_min=5, duration_max=10, grouping="pair",
            classroom_type="both", bloom_level="understand", fln_compatible=True,
            assessment_method="oral",
        ),
        ["Compare Quantities", "Estimate Numbers"],
        ["Mangoes", "Flowers", "Pebbles"],
    ),
    (
        "Observe", "Observe Real Objects",
        dict(
            description="Students look closely at real objects or settings around them and describe what they notice.",
            resource_level=0, duration_min=5, duration_max=10, grouping="individual",
            classroom_type="both", bloom_level="remember", fln_compatible=True,
            assessment_method="oral",
        ),
        ["Observe Objects"],
        ["Classroom Objects", "School Garden", "Market Pictures", "Birds Around School"],
    ),
    (
        "Observe", "Find Similar or Different Items",
        dict(
            description="Students look at a set of real items and identify which ones are alike and which are different.",
            resource_level=0, duration_min=5, duration_max=10, grouping="individual",
            classroom_type="both", bloom_level="understand", fln_compatible=True,
            assessment_method="worksheet",
        ),
        ["Identify Similarities", "Identify Patterns"],
        ["Leaves", "Fruits"],
    ),
    (
        "Observe", "Compare Real Objects",
        dict(
            description="Students compare two or more real objects and describe how they are the same or different.",
            resource_level=0, duration_min=5, duration_max=10, grouping="pair",
            classroom_type="both", bloom_level="understand", fln_compatible=True,
            assessment_method="oral",
        ),
        ["Compare Objects"],
        ["Vegetables"],
    ),
    (
        "Observe", "Observe Visual Patterns",
        dict(
            description="Students observe a repeating visual pattern and describe or continue it.",
            resource_level=0, duration_min=5, duration_max=10, grouping="individual",
            classroom_type="indoor", bloom_level="understand", fln_compatible=True,
            assessment_method="worksheet",
        ),
        ["Identify Patterns"],
        ["Rangoli"],
    ),
]

GRADE_BAND = "1-3"


def seed(ac, dry_run: bool) -> None:
    if dry_run:
        print(f"[DRY RUN] Would seed {len(CONTEXTS)} contexts and {len(TEMPLATES)} activity templates.")
        for category, name, meta, comps, ctxs in TEMPLATES:
            print(f"  - [{category}] {name} — competencies={comps}, contexts={ctxs}")
        return

    context_ids = {}
    for name, category in CONTEXTS.items():
        context_ids[name] = pl.get_or_create_context(ac, name, category)
    print(f"Contexts ready: {len(context_ids)}")

    competency_ids = {}
    all_competency_names = {c for _, _, _, comps, _ in TEMPLATES for c in comps}
    for name in all_competency_names:
        competency_ids[name] = pl.get_or_create_competency(ac, name)
    print(f"Competencies ready: {len(competency_ids)}")

    existing = pl.list_activity_templates(ac, grade_band=GRADE_BAND)
    existing_by_key = {(t["category"], t["name"]): t for t in existing}

    created, reused = 0, 0
    for category, name, meta, comp_names, ctx_names in TEMPLATES:
        existing_template = existing_by_key.get((category, name))
        if existing_template:
            template_id = existing_template["id"]
            reused += 1
        else:
            row = pl.create_activity_template(ac, GRADE_BAND, category, name, **meta)
            template_id = row["id"]
            created += 1

        for comp_name in comp_names:
            pl.link_competency(ac, template_id, competency_ids[comp_name])
        for ctx_name in ctx_names:
            pl.link_context(ac, template_id, context_ids[ctx_name])

    print(f"Activity templates: {created} created, {reused} already existed (links re-verified either way).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    ac = create_admin_client()
    seed(ac, args.dry_run)


if __name__ == "__main__":
    main()
